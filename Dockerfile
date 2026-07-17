# Point-in-Polygon Service — container image.
#
# FOSS, key-free, offline-capable runtime (SPEC §7). geopandas/pyogrio ship
# manylinux wheels, so no apt/GDAL system packages are needed — pip alone
# builds the whole dependency set. If a future dependency needs a system
# library, add it here with a comment; prefer none.
FROM python:3.12-slim

# App source, config.toml, and data all resolve relative to this directory
# (app/config.py derives PROJECT_ROOT from the package's parent), so the whole
# service lives under one WORKDIR that also becomes CWD for uvicorn.
WORKDIR /srv/app

# Copy only what the running service needs. Tests, docs, scripts, the build
# venv, and .git are excluded here and reinforced by .dockerignore.
COPY pyproject.toml README.md LICENSE ./
COPY app/ ./app/
COPY static/ ./static/
COPY config.toml ./config.toml
COPY data/layers.gpkg ./data/layers.gpkg

# Install the package (and its pinned runtime deps) from pyproject. --no-cache-dir
# keeps the image small; the installed copy pulls in fastapi/uvicorn/geopandas/httpx.
RUN pip install --no-cache-dir .

# Run as a non-root user. The service reads only bundled, read-only data and
# persists nothing (D4/D5 no-PII), so the default ownership is sufficient.
RUN useradd --create-home --system appuser
USER appuser

EXPOSE 8000

# stdlib-only healthcheck — no curl in the slim image, and no new dependency.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"]

# Single uvicorn process. --no-access-log is REQUIRED (D5 no-PII): the access
# log is where an address passed in a GET query string would otherwise be
# persisted. A reverse proxy an agency adds has its own log to configure.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
