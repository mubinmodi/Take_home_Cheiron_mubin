"""The Visualization Specification: the service's own, renderer-independent chart contract."""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class VisualizationType(StrEnum):
    BAR_CHART = "bar_chart"
    GROUPED_BAR_CHART = "grouped_bar_chart"
    TIME_SERIES = "time_series"
    SINGLE_VALUE = "single_value"
    TABLE = "table"


class FieldType(StrEnum):
    QUANTITATIVE = "quantitative"
    TEMPORAL = "temporal"
    ORDINAL = "ordinal"
    NOMINAL = "nominal"


class Channel(BaseModel):
    """Binds one field of each datum to a visual channel."""

    field: str
    type: FieldType
    title: str


class Encoding(BaseModel):
    """Which datum fields drive which channels. Unused channels are omitted."""

    x: Channel | None = None
    y: Channel | None = None
    color: Channel | None = Field(default=None, description="Series / comparison group.")
    value: Channel | None = Field(default=None, description="single_value: the number shown.")
    columns: list[Channel] | None = Field(default=None, description="table: columns in display order.")
    tooltip: list[Channel] = Field(default_factory=list)


class RenderMetadata(BaseModel):
    """Everything a renderer needs that is not in the data itself."""

    units: str = "trials"
    sort: Literal["data_order"] = Field(
        default="data_order", description="Data is already in display order; renderers must keep it."
    )
    time_granularity: Literal["year"] | None = None
    category_order: list[str] | None = Field(default=None, description="Display order of x categories.")
    series_order: list[str] | None = Field(default=None, description="Display order of color groups.")
    top_n: int | None = Field(default=None, description="Categories kept before folding the rest into 'Other'.")
    other_bucket: bool = False
    multi_valued: bool = Field(
        default=False,
        description="One trial can appear in several categories, so categories may sum to more than the total.",
    )
    cohort_size: int = Field(description="Distinct trials in the answer's cohort.")
    total_rows: int | None = Field(default=None, description="table: rows available; data may show fewer.")


class VisualizationSpec(BaseModel):
    """Type, title, encoding, data and metadata: renderable without any other context.

    Every datum carries `trial_ids` (its Citation); details are in the response `evidence`.
    """

    type: VisualizationType
    title: str
    subtitle: str | None = None
    encoding: Encoding
    data: list[dict[str, Any]]
    metadata: RenderMetadata
