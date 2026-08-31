# Documentation index — Point-in-Polygon Service

> **These docs are intentionally not published in the git repo.** `docs/` and
> `CLAUDE.md` are gitignored; they live on the maintainer's machine. Nothing
> here is reachable from a clone, so the root `README.md` deliberately does not
> link to it.

## Runbooks — how to *do* things (`runbooks/`)

Operational procedure. Start here for any hands-on task.

| Runbook | Use it when |
|---|---|
| [`runbooks/adding-a-layer.md`](runbooks/adding-a-layer.md) | **Adding new shapes / polygons** — precincts, wards, service areas. Get the shapes into a GeoPackage, register them in `config.toml`, record provenance, verify. |
| [`runbooks/batch-lookups.md`](runbooks/batch-lookups.md) | **Running a whole spreadsheet** — CSV, XLSX, or a link-shared Google Sheet through `scripts/batch_locate.py`. Runtimes, the Sheets sharing step, and how to check results before trusting them. |
| [`runbooks/testing.md`](runbooks/testing.md) | Running the suite, the maintainer PR-review gate, manual API testing, and the edge cases that trip people up. |
| [`runbooks/deployment.md`](runbooks/deployment.md) | Standing the service up: shoestring hosts, the air-gapped install, the reverse-proxy access-log warning. |

## Contract and plans

| Document | What it is |
|---|---|
| [`specs/SPEC.md`](specs/SPEC.md) | The ratified contract — purpose, API, geocoding modes, non-negotiables. **Frozen**; propose deltas, never edit in place. |
| [`plans/PLAN.md`](plans/PLAN.md) | **Active plan** — F7 (batch lookups from CSV/XLSX/Google Sheets), F5b (the optional `arcpy` plugin), and the repo-hygiene chores C5–C7. Draft; F7 is blocked on spec delta Δ3. |
| [`plans/v1.0-archive/PLAN.md`](plans/v1.0-archive/PLAN.md) | The v1 implementation plan, archived at v1.0.0. |

## Reference

| Document | What it is |
|---|---|
| [`data-provenance.md`](data-provenance.md) | Where every shipped layer came from: source, retrieval date, license, feature counts, field mapping. Update this whenever you add or rebuild a layer. |
| [`conventions.md`](conventions.md) | ID grammar (Feature / Task / Chore / Retrofit), branch, commit, and PR conventions. `@import`ed by `CLAUDE.md`. |
| [`pr-review.md`](pr-review.md) | The maintainer's review log. |

## The three-way split

- **`specs/`** — *what* we are building. Long-lived, rarely changes, frozen once
  ratified.
- **`plans/`** — *how and when*. Short-lived working docs; archived per version
  rather than deleted.
- **`runbooks/`** — *how to operate it*. Living procedure, updated whenever the
  procedure changes.

---

August 2026

#AI/Claude
