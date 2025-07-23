"""Allocate an experiment to a workflow run."""

import tempfile
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import boto3
import pandas as pd
import yaml
from hatchet_sdk import TaskRunRef, TriggerWorkflowOptions
from pydantic import BaseModel, Field, FilePath
from tqdm import tqdm

from scythe.base import S3Url
from scythe.registry import Standalone, TInput, TOutput
from scythe.scatter_gather import (
    RecursionMap,
    ScatterGatherInput,
    ScatterGatherResult,
    scatter_gather,
)
from scythe.storage import ScytheStorageSettings
from scythe.utils.results import save_and_upload_parquets

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client
else:
    S3Client = object

s3 = boto3.client("s3")


class ExperimentSpecsMismatchError(Exception):
    """An error raised when the specs for an experiment do not match the expected type."""

    def __init__(self, expected_type: type, actual_type: type):
        """Initialize the error."""
        self.expected_type = expected_type
        self.actual_type = actual_type
        super().__init__(
            f"Expected type {expected_type.__name__}, got {actual_type.__name__}."
        )


class DuplicateSourceFilesError(Exception):
    """An error raised when a file is duplicated in the source files."""

    def __init__(self, file_name: str, field_name: str):
        """Initialize the error."""
        self.file_name = file_name
        self.field_name = field_name
        super().__init__(
            f"File with name {file_name} for field {field_name} is duplicated in the "
            f"source files (i.e. multiple files with the same name but in different "
            "directories.) "
        )


class SourceFileLocations(BaseModel):
    """The locations of the source files for an experiment."""

    files: dict[str, set[S3Url]]


class ExperimentRunManifest(BaseModel):
    """The manifest for an experiment run."""

    workflow_run_id: str
    experiment_id: str
    experiment_name: str
    io_spec: S3Url
    source_files: S3Url | None = None


# TODO: consider factoring this into a an ExperimentRun Class
# which ought to include things like artifact location,
# automatically uploading referenced artifacts, etc.
def allocate_experiment(  # noqa: C901
    experiment_id: str,
    experiment: Standalone[TInput, TOutput],
    specs: Sequence[TInput],
    recursion_map: RecursionMap | None = None,
    construct_filekey: Callable[[str], str] | None = None,
    storage_settings: ScytheStorageSettings | None = None,
    s3_client: S3Client | None = None,
) -> TaskRunRef[ScatterGatherInput, ScatterGatherResult]:
    """Allocate an experiment to a workflow run."""
    s3_client = s3_client or s3
    storage_settings = storage_settings or ScytheStorageSettings()

    mismatching_types = [
        type(spec)
        for spec in specs
        if not isinstance(spec, experiment.config.input_validator)
    ]
    if mismatching_types:
        raise ExperimentSpecsMismatchError(
            expected_type=experiment.config.input_validator,
            actual_type=mismatching_types[0],
        )
    for spec in specs:
        spec.experiment_id = experiment_id

    # handle local file transfer to s3
    # TODO: this will cause a race condition if multile files have the same name but
    # live in different directories.
    # hence the check above
    def construct_source_filekey(pth: FilePath, field_name: str):
        return f"{storage_settings.BUCKET_PREFIX}/{experiment_id}/artifacts/{field_name}/{pth.name}"

    local_source_paths = [spec._source_file_paths for spec in specs]
    source_files: dict[str, set[FilePath]] = {}
    at_least_one_source_file = False
    for spec_paths in local_source_paths:
        for field_name, fpath in spec_paths.items():
            source_files.setdefault(field_name, set()).add(fpath)
            at_least_one_source_file = True

    all_source_file_names: dict[str, set[str]] = {}
    for field_name, fpaths in source_files.items():
        for fpath in fpaths:
            file_key = construct_source_filekey(fpath, field_name)
            if file_key in all_source_file_names.get(field_name, set()):
                raise DuplicateSourceFilesError(file_key, field_name)
            all_source_file_names.setdefault(field_name, set()).add(file_key)

    source_files_s3_urls = None
    if at_least_one_source_file:
        source_files_s3_urls, source_files_s3_url_maps = upload_source_files(
            source_files, s3_client, storage_settings.BUCKET, construct_source_filekey
        )
        for spec in specs:
            for field_name, fpath in spec._source_file_paths.items():
                uri_map = source_files_s3_url_maps[field_name]
                uri = uri_map[fpath]
                setattr(spec, field_name, uri)

    # TODO: upload manifest of source files to s3

    df = pd.DataFrame([s.model_dump(mode="json") for s in specs])
    df_name = "specs"

    def construct_filekey_(filename: str):
        return f"{storage_settings.BUCKET_PREFIX}/{experiment_id}/artifacts/specs/{filename}.pq"

    construct_filekey = construct_filekey or construct_filekey_

    uris = save_and_upload_parquets(
        collected_dfs={df_name: df},
        s3=s3_client,
        bucket=storage_settings.BUCKET,
        output_key_constructor=construct_filekey,
    )
    uri = uris[df_name]
    scatter_gather_input = ScatterGatherInput(
        experiment_id=experiment_id,
        task_name=experiment.name,
        specs_path=uri,
        recursion_map=recursion_map or RecursionMap(path=None, factor=10, max_depth=0),
        storage_settings=storage_settings,
    )

    run_ref = scatter_gather.run_no_wait(
        scatter_gather_input,
        options=TriggerWorkflowOptions(
            additional_metadata={
                "experiment_id": experiment_id,
                "experiment_name": experiment.name,
                "level": 0,
            }
        ),
    )
    workflow_run_id = run_ref.workflow_run_id

    input_validator = experiment.input_validator
    output_validator = experiment._output_validator
    if output_validator is None:
        msg = "Output validator is not set for experiment"
        raise ValueError(msg)

    class ExperimentIO(BaseModel):
        """The input and output schema for the experiment."""

        input: input_validator = Field(
            default=..., description="The input for the experiment."
        )
        output: output_validator = Field(  # pyright: ignore [reportInvalidTypeForm]
            default=..., description="The output for the experiment."
        )

    schema = ExperimentIO.model_json_schema()

    with tempfile.TemporaryDirectory() as temp_dir:
        tdir = Path(temp_dir)
        manifest_path = tdir / "manifest.yml"
        io_path = tdir / "experiment_io_spec.yml"
        source_files_path = tdir / "source_files.yml"

        # dump and upload the schema
        with open(io_path, "w") as f:
            yaml.dump(schema, f, indent=2)
        io_file_key = f"{storage_settings.BUCKET_PREFIX}/{experiment_id}/artifacts/experiment_io_spec.yml"
        s3_client.upload_file(
            Bucket=storage_settings.BUCKET,
            Key=io_file_key,
            Filename=io_path.as_posix(),
        )

        # dump and upload the source files
        s3_source_files_key = f"{storage_settings.BUCKET_PREFIX}/{experiment_id}/artifacts/source_files.yml"
        if source_files_s3_urls:
            with open(source_files_path, "w") as f:
                yaml.dump(
                    source_files_s3_urls.model_dump(mode="json"),
                    f,
                    indent=2,
                )
            s3_client.upload_file(
                Bucket=storage_settings.BUCKET,
                Key=s3_source_files_key,
                Filename=source_files_path.as_posix(),
            )

        manifest_file_key = (
            f"{storage_settings.BUCKET_PREFIX}/{experiment_id}/manifest.yml"
        )
        io_spec_uri = S3Url(f"s3://{storage_settings.BUCKET}/{io_file_key}")
        source_files_uri = S3Url(
            f"s3://{storage_settings.BUCKET}/{s3_source_files_key}"
        )
        manifest = ExperimentRunManifest(
            workflow_run_id=workflow_run_id,
            experiment_id=experiment_id,
            experiment_name=experiment.name,
            io_spec=io_spec_uri,
            source_files=source_files_uri,
        )
        with open(manifest_path, "w") as f:
            yaml.dump(manifest.model_dump(mode="json"), f, indent=2)
        s3_client.upload_file(
            Bucket=storage_settings.BUCKET,
            Key=manifest_file_key,
            Filename=manifest_path.as_posix(),
        )

    return run_ref


def upload_source_files(
    source_files: dict[str, set[FilePath]],
    s3_client: S3Client,
    bucket: str,
    construct_filekey: Callable[[FilePath, str], str],
) -> tuple[SourceFileLocations, dict[str, dict[FilePath, S3Url]]]:
    """Upload source files to S3."""

    def handle_path(pth: FilePath, field_name: str):
        filekey = construct_filekey(pth, field_name)
        uri = S3Url(f"s3://{bucket}/{filekey}")
        s3_client.upload_file(
            Bucket=bucket,
            Key=filekey,
            Filename=pth.as_posix(),
        )
        return field_name, uri

    args: list[tuple[FilePath, str]] = []
    for field_name, paths in source_files.items():
        for pth in paths:
            args.append((pth, field_name))
    with ThreadPoolExecutor(max_workers=10) as executor:
        first_args = [a[0] for a in args]
        second_args = [a[1] for a in args]
        results = list(
            tqdm(
                executor.map(handle_path, first_args, second_args),
                total=len(args),
                desc="Uploading source files",
            )
        )
    uris: dict[str, set[S3Url]] = {}
    for field_name, uri in results:
        uris.setdefault(field_name, set()).add(uri)
    uri_maps: dict[str, dict[FilePath, S3Url]] = {}
    for (field_name, uri), (pth, _) in zip(results, args, strict=True):
        uri_maps.setdefault(field_name, {})[pth] = uri
    return SourceFileLocations(files=uris), uri_maps
