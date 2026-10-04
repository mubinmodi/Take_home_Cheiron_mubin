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
    NETWORK_GRAPH = "network_graph"
    HISTOGRAM = "histogram"
    TIMELINE = "timeline"


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
    x2: Channel | None = Field(default=None, description="timeline: bar end (x is the start).")
    y: Channel | None = None
    color: Channel | None = Field(default=None, description="Series / comparison group.")
    value: Channel | None = Field(default=None, description="single_value: the number shown.")
    columns: list[Channel] | None = Field(default=None, description="table: columns in display order.")
    label: Channel | None = Field(default=None, description="network_graph: node label.")
    size: Channel | None = Field(default=None, description="network_graph: node size.")
    source: Channel | None = Field(default=None, description="network_graph: edge start node ID.")
    target: Channel | None = Field(default=None, description="network_graph: edge end node ID.")
    weight: Channel | None = Field(default=None, description="network_graph: edge thickness.")
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
    node_kinds: list[str] | None = Field(
        default=None, description="network_graph: node kinds in display order (e.g. sponsor, drug)."
    )
    bipartite: bool = Field(default=False, description="network_graph: edges only join different node kinds.")
    min_edge_trials: int | None = Field(default=None, description="network_graph: edges need this many trials.")
    bins: list[dict[str, Any]] | None = Field(
        default=None, description="histogram: bins in order, each {label, min, max}; max null = no upper limit."
    )


class NetworkData(BaseModel):
    """`network_graph` data. Nodes and edges are both Datums: each carries `trial_count` and `trial_ids`."""

    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]


class VisualizationSpec(BaseModel):
    """Type, title, encoding, data and metadata: renderable without any other context.

    Every Datum carries `trial_ids` (its Citation) and, when it shows a count, `trial_count`;
    details are in the response `evidence`.
    """

    type: VisualizationType
    title: str
    subtitle: str | None = None
    encoding: Encoding
    data: list[dict[str, Any]] | NetworkData = Field(description="Rows, or nodes and edges for a network.")
    metadata: RenderMetadata

    def datums(self) -> list[dict[str, Any]]:
        """Every Datum, whatever the data's shape: the rows, or the nodes followed by the edges."""
        if isinstance(self.data, NetworkData):
            return [*self.data.nodes, *self.data.edges]
        return self.data

    def rows(self) -> list[dict[str, Any]]:
        """Row-shaped data (every type except networks)."""
        if isinstance(self.data, NetworkData):
            raise TypeError(f"{self.type} data is nodes and edges, not rows")
        return self.data
