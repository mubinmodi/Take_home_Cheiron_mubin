"""Renderer: translate our Visualization Specification into Vega-Lite and draw it.

Reads only the specification. Vega-Lite receives finished values: no aggregate, bin or
timeUnit transforms, because counting (and therefore citation) belongs to our code.
"""

import logging
import math
from typing import Any, Literal

import vl_convert

from clinical_trials_viz.catalog import NOT_REPORTED
from clinical_trials_viz.models.spec import Channel, NetworkData, VisualizationSpec, VisualizationType

ImageFormat = Literal["png", "svg"]
log = logging.getLogger(__name__)

_VL_TYPE = {"quantitative": "quantitative", "temporal": "temporal", "ordinal": "ordinal", "nominal": "nominal"}
_WIDTH = 640


class NotRenderable(Exception):
    """The visualization type has no image form (e.g. a table)."""


DATUM_INDEX = "_datum"  # position in spec.datums(), so an interactive page can map a click to its Citation
# The image renderer and the web page (web/index.html) use the same Vega-Lite release.
VEGA_LITE_VERSION = "6.4"
_SCHEMA = "https://vega.github.io/schema/vega-lite/v6.json"
LEGEND_GREY = "#8a8a8a"  # size and width legend symbols: visible on light and dark backgrounds


def _values(spec: VisualizationSpec) -> list[dict[str, Any]]:
    rows = [{**{k: v for k, v in d.items() if k != "trial_ids"}, DATUM_INDEX: i} for i, d in enumerate(spec.rows())]
    if spec.type is VisualizationType.TIME_SERIES and spec.encoding.x:
        # Undated trials stay in the data (and citations) but have no place on a time axis.
        rows = [d for d in rows if d[spec.encoding.x.field] != NOT_REPORTED]
    return rows


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
        "$schema": _SCHEMA,
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
                    # One line per series when the chart crosses two dimensions ("phases per year").
                    **({"color": _channel(enc.color, sort=meta.series_order)} if enc.color else {}),
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

        case VisualizationType.SCATTER_PLOT:
            assert enc.x and enc.y and enc.color
            return {
                **base,
                "width": _WIDTH,
                "height": 380,
                "mark": {"type": "point", "filled": True, "opacity": 0.6, "size": 30},
                "encoding": {
                    "x": _channel(enc.x),
                    "y": _channel(enc.y, scale={"type": meta.y_scale}, axis=_y_axis(spec, enc.y.field)),
                    "color": _channel(enc.color, sort=meta.series_order),
                    "tooltip": _tooltip(spec),
                },
            }

        case VisualizationType.TIMELINE:
            assert enc.x and enc.x2 and enc.y and enc.color
            return {
                **base,
                "width": _WIDTH,
                "height": max(120, 18 * len(spec.rows())),
                "mark": {"type": "bar", "cornerRadius": 2},
                "encoding": {
                    "x": _channel(enc.x, title="Start → primary completion"),
                    "x2": {"field": enc.x2.field},
                    "y": _channel(enc.y, sort=meta.category_order, axis={"labelLimit": 320, "title": None}),
                    "color": _channel(enc.color, sort=meta.series_order),
                    "tooltip": _tooltip(spec),
                },
            }

        case VisualizationType.HISTOGRAM:
            assert enc.x and enc.y and enc.color
            return {
                **base,
                "width": _WIDTH,
                "height": 320,
                "mark": "bar",
                "encoding": {
                    # Tilted: bucket names ("101–200", "Not reported") overlap when level at page widths.
                    "x": _channel(enc.x, sort=meta.category_order, axis={"labelAngle": -30}),
                    "y": _channel(enc.y, stack="zero"),
                    "color": _channel(enc.color, sort=meta.series_order),
                    "order": {"field": enc.color.field, "sort": "ascending"},
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


def _y_axis(spec: VisualizationSpec, field: str) -> dict[str, Any]:
    """Symmetric-log axes need explicit ticks: 0 and powers of ten up to the largest value."""
    if spec.metadata.y_scale != "symlog":
        return {}
    top = max((d[field] for d in spec.rows()), default=1) or 1
    ticks, value = [0], 1
    while value <= top * 10:
        ticks.append(value)
        value *= 10
    return {"values": ticks, "format": ",.0f"}


def _short(label: str, limit: int = 40) -> str:
    return label if len(label) <= limit else label[: limit - 1] + "…"


def _network(spec: VisualizationSpec) -> dict[str, Any]:
    """Positions come from the spec alone. Two node kinds: two columns (first kind left), each sorted
    by trial count. One node kind: a circle, heaviest first."""
    data = spec.data
    if not isinstance(data, NetworkData):
        raise NotRenderable("network_graph needs nodes and edges")
    kinds = spec.metadata.node_kinds or []
    position: dict[str, tuple[float, float]] = {}
    nodes = []
    if spec.metadata.bipartite:
        columns: dict[str, list[dict[str, Any]]] = {k: [] for k in kinds}
        for node in data.nodes:
            columns.setdefault(node["kind"], []).append(node)
        rows = max((len(c) for c in columns.values()), default=1)
        for x, members in enumerate(columns.values()):
            offset = (rows - len(members)) / 2  # centre the shorter column
            for i, node in enumerate(members):
                position[node["id"]] = (x, i + offset)
        x_scale = {"domain": [-1.5, 2.0], "nice": False}  # room for long sponsor names on the left
        y_scale = {"domain": [-0.5, rows - 0.5], "reverse": True, "nice": False}
        width, height = 760, max(200, 24 * rows)
    else:
        # One node kind: a circle in data order (heaviest first, clockwise from the top).
        count = max(len(data.nodes), 1)
        for i, node in enumerate(data.nodes):
            angle = math.pi / 2 - 2 * math.pi * i / count
            position[node["id"]] = (math.cos(angle), math.sin(angle))
        x_scale = {"domain": [-1.9, 1.9], "nice": False}
        y_scale = {"domain": [-1.25, 1.25], "nice": False}
        width, height = 760, 520
    for i, node in enumerate(data.nodes):
        x, y = position[node["id"]]
        left = x < 0 or (spec.metadata.bipartite and x == 0)
        nodes.append({"x": x, "y": y, "label": _short(node["label"]), "kind": node["kind"],
                      "trial_count": node["trial_count"], "align": "right" if left else "left",
                      DATUM_INDEX: i})  # fmt: skip
    edges = [
        {"x": position[e["source"]][0], "y": position[e["source"]][1],
         "x2": position[e["target"]][0], "y2": position[e["target"]][1], "shared_trials": e["trial_count"],
         DATUM_INDEX: len(data.nodes) + j}
        for j, e in enumerate(data.edges)
    ]  # fmt: skip
    axis_off = {"axis": None}
    title: dict[str, Any] = {"text": spec.title, "anchor": "start"}
    if spec.subtitle:
        title["subtitle"] = spec.subtitle
    return {
        "$schema": _SCHEMA,
        "title": title,
        "width": width,
        "height": height,
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
                    # Its own field: legends on the same field get merged (link widths drawn as huge circles).
                    "strokeWidth": {"field": "shared_trials", "type": "quantitative", "title": "Shared trials",
                                    "scale": {"range": [0.5, 6]}, "legend": {"symbolStrokeColor": LEGEND_GREY}},
                },
            },
            {
                "data": {"values": nodes},
                "mark": {"type": "circle", "opacity": 1},
                "encoding": {
                    "x": {"field": "x", "type": "quantitative", "scale": x_scale, **axis_off},
                    "y": {"field": "y", "type": "quantitative", "scale": y_scale, **axis_off},
                    "size": {"field": "trial_count", "type": "quantitative", "title": "Trials",
                             "scale": {"range": [40, 600]}, "legend": {"symbolFillColor": LEGEND_GREY}},
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


def chart_problem(spec: VisualizationSpec) -> str | None:
    """Why this specification cannot become an image, or None. It compiles the chart to Vega without
    drawing it (milliseconds), so a broken chart is caught before its `chart_url` is handed out."""
    try:
        vl_convert.vegalite_to_vega(to_vega_lite(spec), vl_version=VEGA_LITE_VERSION)
    except NotRenderable as exc:
        return str(exc)
    except Exception as exc:  # vl-convert raises ValueError for an invalid spec; anything else is a bug
        log.warning("chart for %s failed to compile: %s", spec.type, exc)
        return f"{type(exc).__name__}: {str(exc)[:160]}"
    return None


def render(spec: VisualizationSpec, fmt: ImageFormat) -> bytes:
    vl = to_vega_lite(spec)
    if fmt == "svg":
        return vl_convert.vegalite_to_svg(vl, vl_version=VEGA_LITE_VERSION, allowed_base_urls=[]).encode()
    return vl_convert.vegalite_to_png(vl, vl_version=VEGA_LITE_VERSION, scale=2, allowed_base_urls=[])
