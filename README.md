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

**Status:** v1.0.0 shipped (2026-07-17) and deployed. Live instance:
**https://point-in-polygon-service.onrender.com** — try the test UI at `/`, or
`/docs` for the interactive API.

## Documents

| Document | What it is |
|---|---|
| [`README.md`](README.md) | This file — what the service is, how to stand it up. |
| [`CHANGELOG.md`](CHANGELOG.md) | Release history (starts at v1.0.0). |
| [`render.yaml`](render.yaml) | The Render blueprint for the live instance above (Docker build, free plan). |
| [`LICENSE`](LICENSE) | AGPLv3. |

The project's spec, plans, runbooks (testing, deployment, adding a polygon
layer), and data provenance are maintained **outside this repository** and are
not published here. If you are running this service and need them, ask the
maintainer.

The API documents itself: `/docs` on a running instance is the interactive
Swagger UI, and `/openapi.json` is the generated contract — neither is
hand-maintained, so neither can drift from the code.

## Project layout

```
app/                # FastAPI service + PolygonLookup engine (F2, F4)
app/geocoding/      # Pluggable Geocoder providers (F3, F5)
data/               # Shipped GeoPackages + layer config (F1)
static/             # Minimal decoupled frontend (F6)
scripts/            # Data pipeline, one-off tools
tests/              # pytest suite
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
uvicorn app.main:app --no-access-log
```

…on a machine with no Esri software and no API keys (spec §7). The
`--no-access-log` flag keeps request parameters out of any log (no-PII, spec §9);
the service also serves a static test page at `/` and the generated contract at
`/openapi.json`.

## Deployment

The live instance above runs on Render's free plan, built from `render.yaml` /
the repo's `Dockerfile`. Other hosting options (a ~$5/mo VPS via Docker or
systemd, an on-prem Windows box) and the fully-offline / air-gapped install
path (build a pip wheelhouse, carry it over, `pip install --no-index`) are
covered in the maintainer's deployment runbook.

**Reverse-proxy access logs — read this before putting anything in front of the
service.** `--no-access-log` silences the service's *own* log, but an
nginx/Apache/cloud-ALB you put in front keeps its own, and `/geocode` and
`/locate` take the address as a **GET query-string parameter**. Unless you
disable or scrub that proxy log, it will record every queried address,
re-introducing exactly the PII leak this service is built to avoid. That is the
operator's responsibility — the app cannot reach into a proxy it does not
control.

## License

[AGPLv3](LICENSE). The copyleft holds even when the service is run over a
network — that is the point: the tool stays open even when someone runs it as a
hosted service.

-----
July 6, 2026

#AI/Claude
