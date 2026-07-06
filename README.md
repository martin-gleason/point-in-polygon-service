# Point-in-Polygon Service

A fully open-source web service that answers one question: **given an address
(or a raw point), which polygon of a given layer contains it?**

The engine is layer-agnostic. The first configured layer is Chicago / Cook
County police districts — *"what police district does this address fall in?"* —
but the same service can point at voting precincts, wards, or any polygon set
with zero code changes. Built to run on open-source tools and shoestring
infrastructure (FastAPI + GeoPandas, AGPLv3, no Esri software, no mandatory API
keys), so nonprofits, mutual-aid groups, and small government offices can
self-host it for near-$0.

**Status:** pre-build. The spec is ratified (Gate 0 cleared 2026-07-06); the
implementation plan is drafted and awaiting ratification. No feature code yet.

## Documents

| Document | What it is |
|---|---|
| `docs/specs/SPEC.md` | The ratified contract — purpose, API, non-negotiables. Frozen. |
| `docs/plans/PLAN.md` | The implementation plan — features, tasks, gates, hooks. Mutable. |
| `docs/conventions.md` | ID grammar, branch/commit/PR conventions. |
| `CLAUDE.md` | Standing rules the coding agent works under. |

## Project layout

```
app/                # FastAPI service + PolygonLookup engine (F2, F4)
app/geocoding/      # Pluggable Geocoder providers (F3, F5)
data/               # Shipped GeoPackages + layer config (F1)
static/             # Minimal decoupled frontend (F6)
scripts/            # Data pipeline, one-off tools
tests/              # pytest suite
docs/               # specs/ (frozen), plans/ (working), provenance
```

## Scaffolding (for maintainers coming from ArcGIS or QGIS)

You do not need any GIS software to stand this project up — only Python 3.11+
(on Windows, macOS, or Linux). From the directory where you keep your projects:

```
python point-in-polygon-service/bootstrap.py
```

`bootstrap.py` is idempotent and non-clobbering: re-running it only fills in
whatever is missing and never overwrites existing work. It uses only the Python
standard library — no `pip install`, no shell, no internet required.

Once feature work ships, the runtime install will be:

```
pip install -e .
uvicorn app.main:app
```

…on a machine with no Esri software and no API keys (spec §7).

## License

[AGPLv3](LICENSE). The copyleft holds even when the service is run over a
network — that is the point: the tool stays open even when someone runs it as a
hosted service.

-----
July 6, 2026

#AI/Claude
