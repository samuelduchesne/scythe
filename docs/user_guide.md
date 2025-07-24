# User Guide

This guide explains how to use **Scythe** to manage embarrassingly parallel experiments with [Hatchet](https://hatchet.run).

## Installation

Install the package from PyPI:

```bash
pip install scythe-engine
```

## Creating Experiments

1. Define input and output specifications by subclassing `ExperimentInputSpec` and `ExperimentOutputSpec` from `scythe.base`.
2. Decorate an experiment function with `@ExperimentRegistry.Register()` to expose it as a Hatchet workflow.
3. Optionally start a `ScytheWorker` to run the workflows.

## Allocating Experiments

Use `allocate_experiment` from `scythe.allocate` to upload specs and trigger the scatter/gather workflow. The function handles uploading local artifacts and creates a manifest describing the experiment run.

## Results

Outputs are stored as Parquet files in your configured S3 bucket. Scalar results are combined in `scalars.pq`. Additional data frames can be added via the `dataframes` field of `ExperimentOutputSpec`.

## Example

A minimal example creating an experiment is shown below:

```python
from pydantic import Field
from scythe.base import ExperimentInputSpec, ExperimentOutputSpec
from scythe.registry import ExperimentRegistry

class MyInput(ExperimentInputSpec):
    value: int = Field(...)

class MyOutput(ExperimentOutputSpec):
    doubled: int = Field(...)

@ExperimentRegistry.Register()
def double(input_spec: MyInput) -> MyOutput:
    return MyOutput(doubled=input_spec.value * 2)
```

The registry exposes the experiment as a Hatchet workflow which can be executed via scatter/gather.
