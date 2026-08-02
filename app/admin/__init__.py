"""F8 — the local layer installer: install a new polygon layer without editing code.

Today, adding a layer means editing `scripts/build_data.py` and hand-writing a
`[[layers]]` block in config.toml. F8 replaces that with a local tool where an
operator drops a file in, looks at the shapes drawn on screen, and confirms.

F8-T1 (this much) is the part that decides whether a candidate layer is fit to
install, and says so in words a campaign volunteer can act on:

    codes     — one registry of every rejection and every flag, with a stable
                code an operator can quote and the docs can index.
    validate  — the checks themselves, over an already-loaded frame of shapes.
                Pure: no files, no network, no web framework.

Later tasks add the file reader, the preview, the page, and the commit step on
top of these two.

ArcGIS / ArcPy equivalent
    The role ArcGIS Pro's "Add Data" plus the Define Projection / Repair
    Geometry prompts play for an analyst — but aimed at someone who has never
    opened a GIS program, and with the wording to match.
"""
from __future__ import annotations

from app.admin.codes import (
    LAYER_CODES,
    SEVERITIES,
    SEVERITY_BLOCKING,
    SEVERITY_WARNING,
    Finding,
    LayerCode,
    UnknownLayerCodeError,
    build_finding,
    get_code,
    has_blocking,
    sort_findings,
)
from app.admin.validate import (
    SOURCE_ARCGIS_REST,
    SOURCE_GEOJSON,
    SOURCE_GEOPACKAGE,
    SOURCE_SHAPEFILE,
    SOURCE_UNKNOWN,
    CandidateContext,
    InstalledLayer,
    validate_candidate,
)

__all__ = [
    "LAYER_CODES",
    "SEVERITIES",
    "SEVERITY_BLOCKING",
    "SEVERITY_WARNING",
    "Finding",
    "LayerCode",
    "UnknownLayerCodeError",
    "build_finding",
    "get_code",
    "has_blocking",
    "sort_findings",
    "CandidateContext",
    "InstalledLayer",
    "validate_candidate",
    "SOURCE_SHAPEFILE",
    "SOURCE_GEOPACKAGE",
    "SOURCE_GEOJSON",
    "SOURCE_ARCGIS_REST",
    "SOURCE_UNKNOWN",
]
