"""Renderer: translate our Visualization Specification into Vega-Lite and draw it.

Reads only the specification. Vega-Lite receives finished values: no aggregate, bin or
timeUnit transforms, because counting (and therefore citation) belongs to our code.
"""

from typing import Any, Literal

import vl_convert

from clinical_trials_viz.catalog import NOT_REPORTED
from clinical_trials_viz.models.spec import Channel, NetworkData, VisualizationSpec, VisualizationType

ImageFormat = Literal["png", "svg"]

_VL_TYPE = {"quantitative": "quantitative", "temporal": "temporal", "ordinal": "ordinal", "nominal": "nominal"}
_WIDTH = 640


class NotRenderable(Exception):
    """The visualization type has no image form (e.g. a table)."""


def _values(spec: VisualizationSpec) -> list[dict[str, Any]]:
    data = spec.rows()
    if spec.type is VisualizationType.TIME_SERIES and spec.encoding.x:
        # Undated trials stay in the data (and citations) but have no place on a time axis.
        data = [d for d in data if d[spec.encoding.x.field] != NOT_REPORTED]
    return [{k: v for k, v in d.items() if k != "trial_ids"} for d in data]


def _channel(c: Channel, **extra: Any) -> dict[str, Any]:
    return {"field": c.field, "type": _VL_TYPE[c.type], "title": c.title, **extra}


def _tooltip(spec: VisualizationSpec) -> list[dict[str, Any]]:
    return [_channel(c) for c in spec.encoding.tooltip]


def to_vega_lite(spec: VisualizationSpec) -> dict[str, Any]:
    if spec.type is VisualizationType.NETWORK_GRAPH:
        return _network(spec)
    enc, meta = spec.encoding, spec.metadata
    title: dict[str, Any] = {"text": spec.title, "anchor": "start"}
    if spec.subtitle:
        title["subtitle"] = spec.subtitle
    base: dict[str, Any] = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "title": title,
        "data": {"values": _values(spec)},
        "config": {"view": {"stroke": None}, "axis": {"labelLimit": 220}},
    }

    match spec.type:
        case VisualizationType.SINGLE_VALUE:
            assert enc.value
            return {
                **base,
                "width": 300,
                "height": 120,
                "mark": {"type": "text", "fontSize": 64, "fontWeight": "bold"},
                "encoding": {"text": _channel(enc.value)},
            }

        case VisualizationType.TIME_SERIES:
            assert enc.x and enc.y
            return {
                **base,
                "width": _WIDTH,
                "height": 320,
                "mark": {"type": "line", "point": True},
                "encoding": {
                    "x": _channel(enc.x, sort=meta.category_order),
                    "y": _channel(enc.y),
                    "tooltip": _tooltip(spec),
                },
            }

        case VisualizationType.BAR_CHART:
            assert enc.x and enc.y
            # Horizontal bars keep long category names readable.
            height = max(120, 26 * len(spec.rows()))
            return {
                **base,
                "width": _WIDTH - 200,
                "height": height,
                "mark": "bar",
                "encoding": {
                    "y": _channel(enc.x, sort=meta.category_order),
                    "x": _channel(enc.y),
                    "tooltip": _tooltip(spec),
                },
            }

        case VisualizationType.GROUPED_BAR_CHART:
            assert enc.x and enc.y and enc.color
            categories = meta.category_order or []
            height = max(160, 18 * len(spec.rows()) + 10 * len(categories))
            return {
                **base,
                "width": _WIDTH - 200,
                "height": height,
                "mark": "bar",
                "encoding": {
                    "y": _channel(enc.x, sort=categories),
                    "yOffset": {"field": enc.color.field, "sort": meta.series_order},
                    "x": _channel(enc.y),
                    "color": _channel(enc.color, sort=meta.series_order),
                    "tooltip": _tooltip(spec),
                },
            }

    raise NotRenderable(f"{spec.type} has no image form")


def _short(label: str, limit: int = 40) -> str:
    return label if len(label) <= limit else label[: limit - 1] + "…"


def _network(spec: VisualizationSpec) -> dict[str, Any]:
    """Two-column layout for bipartite networks: first node kind on the left, second on the right,
    each sorted by trial count (the data order). Positions come from the spec alone."""
    data = spec.data
    if not isinstance(data, NetworkData) or not spec.metadata.bipartite:
        raise NotRenderable("only two-sided networks have an image form yet")
    kinds = spec.metadata.node_kinds or []
    columns: dict[str, list[dict[str, Any]]] = {k: [] for k in kinds}
    for node in data.nodes:
        columns.setdefault(node["kind"], []).append(node)
    rows = max((len(c) for c in columns.values()), default=1)
    position: dict[str, tuple[float, float]] = {}
    nodes = []
    for x, (kind, members) in enumerate(columns.items()):
        offset = (rows - len(members)) / 2  # centre the shorter column
        for i, node in enumerate(members):
            position[node["id"]] = (x, i + offset)
            nodes.append({"x": x, "y": i + offset, "label": _short(node["label"]), "kind": kind,
                          "trial_count": node["trial_count"], "align": "right" if x == 0 else "left"})  # fmt: skip
    edges = [
        {"x": position[e["source"]][0], "y": position[e["source"]][1],
         "x2": position[e["target"]][0], "y2": position[e["target"]][1], "trial_count": e["trial_count"]}
        for e in data.edges
    ]  # fmt: skip
    x_scale = {"domain": [-0.9, 1.9], "nice": False}
    y_scale = {"domain": [-0.5, rows - 0.5], "reverse": True, "nice": False}
    axis_off = {"axis": None}
    title: dict[str, Any] = {"text": spec.title, "anchor": "start"}
    if spec.subtitle:
        title["subtitle"] = spec.subtitle
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "title": title,
        "width": 760,
        "height": max(200, 24 * rows),
        "config": {"view": {"stroke": None}},
        "layer": [
            {
                "data": {"values": edges},
                "mark": {"type": "rule", "opacity": 0.35, "color": "#7a7a7a"},
                "encoding": {
                    "x": {"field": "x", "type": "quantitative", "scale": x_scale, **axis_off},
                    "y": {"field": "y", "type": "quantitative", "scale": y_scale, **axis_off},
                    "x2": {"field": "x2"},
                    "y2": {"field": "y2"},
                    "strokeWidth": {"field": "trial_count", "type": "quantitative", "title": "Shared trials",
                                    "scale": {"range": [0.5, 6]}},
                },
            },
            {
                "data": {"values": nodes},
                "mark": {"type": "circle", "opacity": 1},
                "encoding": {
                    "x": {"field": "x", "type": "quantitative", "scale": x_scale, **axis_off},
                    "y": {"field": "y", "type": "quantitative", "scale": y_scale, **axis_off},
                    "size": {"field": "trial_count", "type": "quantitative", "title": "Trials",
                             "scale": {"range": [40, 600]}},
                    "color": {"field": "kind", "type": "nominal", "title": "Entity", "sort": kinds},
                },
            },
            {
                "data": {"values": nodes},
                "transform": [{"filter": "datum.align == 'right'"}],
                "mark": {"type": "text", "align": "right", "dx": -12, "fontSize": 11},
                "encoding": {
                    "x": {"field": "x", "type": "quantitative", "scale": x_scale, **axis_off},
                    "y": {"field": "y", "type": "quantitative", "scale": y_scale, **axis_off},
                    "text": {"field": "label"},
                },
            },
            {
                "data": {"values": nodes},
                "transform": [{"filter": "datum.align == 'left'"}],
                "mark": {"type": "text", "align": "left", "dx": 12, "fontSize": 11},
                "encoding": {
                    "x": {"field": "x", "type": "quantitative", "scale": x_scale, **axis_off},
                    "y": {"field": "y", "type": "quantitative", "scale": y_scale, **axis_off},
                    "text": {"field": "label"},
                },
            },
        ],
    }  # fmt: skip


def render(spec: VisualizationSpec, fmt: ImageFormat) -> bytes:
    vl = to_vega_lite(spec)
    if fmt == "svg":
        return vl_convert.vegalite_to_svg(vl, allowed_base_urls=[]).encode()
    return vl_convert.vegalite_to_png(vl, scale=2, allowed_base_urls=[])
