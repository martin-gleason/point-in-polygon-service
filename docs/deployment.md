# Deployment — Point-in-Polygon Service

How to stand up the service on shoestring infrastructure, and how to install it
on a machine that has no internet at all. This is a small FOSS tool: the runtime
is `fastapi` + `uvicorn[standard]` + `geopandas` + `httpx` (all pip wheels),
Python **3.11+**, and the whole app is `pip install` + one `uvicorn` command.

The repo ships everything the service needs at its root:

- `data/layers.gpkg` — the polygon layers (police districts + municipalities).
- `config.toml` — layer and geocoder configuration. Override its location with
  the `PIP_CONFIG` environment variable (see `app/config.py`).
- `static/` — the zero-dependency test UI, mounted by `app/main.py`.

The canonical run command is always:

```
uvicorn app.main:app --no-access-log
```

`--no-access-log` is **not optional** — it keeps queried addresses (PII) out of
the service's own logs (SPEC §9). Every command below carries it.

> **⚠️ Reverse-proxy access logs (read this before you put anything in front of
> the service).** `--no-access-log` only silences *uvicorn's* log. If you place
> an **nginx / Apache / cloud load-balancer (ALB)** in front of the service —
> for TLS, a hostname, or rate-limiting — that proxy keeps **its own** access
> log, and the `/geocode` and `/locate` endpoints take the address as a **GET
> query-string parameter**. Unless you disable or scrub that proxy log, it
> **will** record every queried address, re-introducing exactly the PII leak the
> service was built to avoid. You must configure the proxy yourself:
> - **nginx:** `access_log off;` in the `location` / `server` block, or a
>   `log_format` that omits `$request`/`$query_string`.
> - **Apache:** remove `%r`/`%q` from `LogFormat`, or `CustomLog /dev/null`.
> - **Cloud ALB:** leave access logging disabled, or strip the query string.
> This is the operator's responsibility; the app cannot reach into a proxy it
> does not control.

---

## 1. Shoestring hosting options

### (a) Free-tier PaaS

Any PaaS that runs a Python web process works (Render, Railway, Fly.io, and the
like — pick whatever free tier is current). Point the platform's start command
at uvicorn and bind the port the platform hands you via `$PORT`, on `0.0.0.0`:

```
uvicorn app.main:app --no-access-log --host 0.0.0.0 --port $PORT
```

Install step: `pip install .` (the platform runs this from `pyproject.toml`).
Note that a PaaS almost always terminates TLS at its own edge proxy — see the
reverse-proxy warning above; check whether that platform logs request URLs.

**Render, specifically (the v1.0.0 deploy target).** The repo ships a
`render.yaml` blueprint — connect the repo once in the Render dashboard
(New → Blueprint) and it builds straight from the `Dockerfile` (this is the
Docker path, not the `$PORT`/bare-uvicorn path above; the Dockerfile's `CMD`
hardcodes `--port 8000` and `EXPOSE 8000` matches it, so **do not** add a
`PORT` env var — Render auto-detects the bound port, and setting `PORT`
explicitly breaks that detection). `healthCheckPath: /health` matches the
Dockerfile's own `HEALTHCHECK`.

Verified (2026-07-23) against the reverse-proxy access-log warning above:
Render only generates HTTP request logs — which include the requested URL,
i.e. the `?address=` query string — **on a Pro workspace or higher**. The
`free` plan this blueprint uses has no HTTP request/access logging at all, so
there is no edge-log PII exposure today. If this service is ever upgraded to
a Pro workspace, that changes — treat "upgrade to Pro" as a decision that
needs its own PII review (log retention settings, log-stream scrubbing, or a
proxy in front that strips the query string) before flipping the plan.

### (b) ~$5/month VPS

A single small VPS (any provider) comfortably runs this. Two ways to run it:

**Option 1 — Docker (using the F6-T3 `Dockerfile`).** The shipped image already
bakes in the `--no-access-log` run command as its `CMD`:

```
docker build -t pip-service .
docker run -d --restart unless-stopped -p 8000:8000 pip-service
```

The container listens on `0.0.0.0:8000` inside the image; publish it with `-p`.

**Option 2 — bare systemd + uvicorn.** No Docker needed. Install into a venv and
let systemd keep it alive:

```
python -m venv /opt/pip-service/venv
/opt/pip-service/venv/bin/pip install .
```

`/etc/systemd/system/pip-service.service`:

```ini
[Unit]
Description=Point-in-Polygon Service
After=network.target

[Service]
WorkingDirectory=/opt/pip-service
ExecStart=/opt/pip-service/venv/bin/uvicorn app.main:app --no-access-log --host 127.0.0.1 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Then `systemctl enable --now pip-service`. Binding to `127.0.0.1` keeps the
service private and lets a local nginx/Apache add TLS in front (mind the
reverse-proxy access-log warning above).

### (c) On-prem Windows box

geopandas ships Windows wheels, so no build tools are required. From a
`cmd`/PowerShell prompt with Python 3.11+ installed:

```
py -m venv venv
venv\Scripts\pip install .
venv\Scripts\uvicorn app.main:app --no-access-log --host 127.0.0.1 --port 8000
```

For an always-on service on Windows, wrap that command with a process manager
(NSSM as a Windows service, or Task Scheduler "run at startup"). This is also
the typical host for the air-gapped install below.

---

## 2. Fully-offline / air-gapped install (SPEC §7)

The locked-workstation acceptance story: a target machine with **no internet and
no Esri software**. You build a pip **wheelhouse** on a connected machine, carry
it to the target on removable media, and install from it with no network.

**On a connected machine** (same OS/Python version as the target — build the
wheelhouse *on Windows* for a Windows target, since that pulls the Windows
wheels; geopandas publishes them):

```
pip download . -d wheelhouse
```

`pip download .` resolves the project's dependencies from `pyproject.toml` and
downloads every wheel (fastapi, uvicorn[standard], geopandas, httpx, and their
transitive deps) into `wheelhouse/`. Copy that directory plus the repo to the
target.

**On the air-gapped target** (no index, install only from the local dir):

```
pip install --no-index --find-links wheelhouse .
uvicorn app.main:app --no-access-log
```

`--no-index` forbids any network fetch; `--find-links wheelhouse` satisfies every
dependency from the carried wheels.

**Pair it with the offline geocoder (SPEC §5 mode 3).** With no internet, the
default Cook County / Census geocoders can't be reached, so switch to the
`local_points` provider, which matches addresses against a local address-point
GeoPackage. That file is ~2M points and is **not** committed; build it once on
your own hardware:

```
python scripts/build_data.py --address-points
```

Then enable the provider in `config.toml` (the block is present, commented) **and
make it the default** — this second step is essential. Requests that don't name a
`?provider=` (the test UI sends none) go to whichever provider carries
`default = true`, and out of the box that is the online `cook_county_arcgis →
census` chain, which is unreachable on an air-gapped box. Move the default onto
the offline provider:

```toml
[[geocoders]]
id = "offline"
type = "local_points"
path = "data/address_points.gpkg"
layer = "address_points"
number_field = "number"
street_field = "street"
city_field = "city"
zip_field = "zip"
default = true          # ← the no-provider default; without this, requests
                        #   still hit the online chain and fail with no internet
```

and remove `default = true` from the `[[geocoders]] id = "default"` chain block
(exactly one provider may be marked default — the service refuses to start with
two). The online `cook_county_arcgis` / `census` entries can be left in place
(they simply go unused offline) or commented out.

See `docs/data-provenance.md` for the address-point source and build details.
With the offline provider **as the default**, the service geocodes and locates a
district with no server, no internet, and no ArcGIS license.

---

## 3. AGPL reminder

The service is **AGPL-3.0**. Running it as a network service triggers the AGPL's
source-availability obligation: **users interacting with it over the network are
entitled to the complete corresponding source**, including any modifications you
made. Keep the `LICENSE` file intact and make your source available to your
users. That copyleft is the point — the tool stays open even when run as a
hosted service.

-----
2026-07-17

#AI/Claude
</content>
</invoke>
