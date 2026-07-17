# Testing runbook — Point-in-Polygon Service

How this project is tested: the maintainer review gate, how to run the suite,
what it guarantees, and the confusing-but-likely edge cases that trip people up.

Standalone doc (like `docs/deployment.md`), not under `specs/` or `plans/`.

---

## Stage 0 — Maintainer PR-review gate (the first test is a human)

No feature reaches `main` without the maintainer's line-by-line review (the 5%
review, `CLAUDE.md`). This is the first testing stage, and every feature branch
stops here before merge.

> **Current state:** there are **no open PRs**. F1–F6 and the v1.0.0 release are
> all merged; the tree is clean. This checklist applies to the *next* feature PR
> (F5b or a retrofit).

Before you rebase-and-merge a feature PR, confirm:

1. **CI is green** — the `CI` check (tests + secret scan, see §7) passes on the PR.
2. **Adversarial-review record** — the PR was run through an adversarial pass and
   its findings are either fixed (with a commit) or consciously waived. Every v1
   feature has this; the record lives in the PR conversation / commit trail.
3. **Verification evidence in the PR body** — the actual command run and its
   output (full suite green, plus the feature's specific verify step — e.g.
   `pytest tests/test_offline.py` for the offline criterion). No feature is
   "done" on assertion alone.
4. **The diff, read** — line by line. Pay special attention to the non-negotiables
   (§9): no `arcpy`/proprietary dep, no address/token in logs or exceptions, the
   OpenAPI contract still equals §4.
5. **The frozen SPEC is untouched** — `docs/specs/SPEC.md` is immutable; a needed
   change is a proposed delta the maintainer ratifies, never an edit inside the PR.
6. **Merge strategy** — rebase-and-merge (no squash, no merge commit), so each
   commit stays in the audit trail.

Then merge. Locally: `gh pr merge <n> --rebase`.

---

## 1. Set up

The suite imports the `app` package, so it needs the editable install. **Always
run inside the project venv.**

```bash
python -m venv .venv          # first time only
source .venv/bin/activate     # every session — see edge case H if you skip this
pip install -e ".[dev]"       # runtime deps + pytest + respx
```

## 2. Run the suite

```bash
pytest                    # everything (config: [tool.pytest.ini_options] testpaths=["tests"])
pytest -q                 # quiet (what CI runs)
pytest -rs                # show SKIP reasons — do this at least once (edge case A)
pytest -x                 # stop at first failure
pytest tests/test_chain.py            # one file
pytest tests/test_api.py -k locate    # one pattern
pytest tests/test_offline.py -q       # the §7 offline acceptance criterion alone
```

The suite is **~123 tests** and runs in a couple of seconds. It needs **no
network** and **no running server** (see §4).

## 3. What's covered

| Test file | Feature | Focus |
|---|---|---|
| `test_data.py` | F1 | pipeline output schema (layers, columns, CRS, non-empty) |
| `test_lookup.py` | F2 | `PolygonLookup` engine: reprojection, `covers`, boundary, unknown layer |
| `test_geocoding_arcgis.py` | F3 | ArcGIS adapter (respx-mocked): match/no-match/timeout/500/PII |
| `test_api.py` | F4 | the §4 endpoints, error model, no-PII logging, OpenAPI contract |
| `test_census.py` / `test_nominatim.py` | F5 | the fallback adapters (respx-mocked), D6 scoring |
| `test_normalize.py` | F5 | USPS Pub. 28 normalization (pure, D8) |
| `test_local_points.py` | F5 | offline geocoder + address parsing (synthetic gpkg) |
| `test_chain.py` | F5 | `GeocoderChain` D7 fall-through semantics |
| `test_offline.py` | F5 | §7 air-gapped: geocode→locate with the network severed |

## 4. What a green run guarantees (the invariants)

- **Offline** — every upstream HTTP call is `respx`-mocked; the suite passes with
  no internet. `test_offline.py` goes further and severs sockets to prove the
  air-gapped path.
- **No PII** — a test captures all log output during a request and asserts the
  queried address appears nowhere; adapter tests assert the address/token never
  reach an exception message.
- **Contract ≡ spec** — `test_api.py` asserts `/openapi.json`'s path+method set
  equals SPEC §4 exactly (see edge case D).

## 5. Before you trust a green run

- **Build the real data.** The shipped `data/layers.gpkg` is committed, so
  real-data tests run out of the box. If you work from a data-stripped checkout,
  build it first — otherwise those tests **skip silently** (edge case A):
  ```bash
  python scripts/build_data.py          # writes data/layers.gpkg
  ```
- **Run the secret scanner** — it's part of "passing" in CI (§7):
  ```bash
  python scripts/check_no_secrets.py
  ```

---

## 6. Confusing-but-likely edge cases

These are the real footguns — every one has bitten during development.

**A. "123 passed" but the real geometry was never exercised.**
If `data/layers.gpkg` is absent, the real-data tests in `test_api.py` and
`test_lookup.py` are `skipif`-guarded and **skip** — you see PASSED with a lower
count, not FAILED. A clean run that "passes" can still be hollow. Diagnose with
`pytest -rs` and look for `data/layers.gpkg not built`. Fix: run
`scripts/build_data.py`.

**B. The chain-default 502 trap.**
The default provider is the `cook_county_arcgis → census` chain (D7). A test that
mocks *only* ArcGIS failing makes the chain fall through to Census — and if you
didn't also mock Census, respx (default `assert_all_mocked`) raises an
`AllMockedAssertionError`, which is **not** an `httpx.HTTPError`, so the adapter
never wraps it in `GeocoderUnavailable`. You get a bewildering **500 instead of
502**. Rule: any "upstream failure → 502" test must mock **every** provider the
chain can reach as failing. (This is why `test_api.py`'s 502 tests mock both.)

**C. Never blanket-patch `socket.socket` in an offline test.**
`test_offline.py` cuts the network, but patching `socket.socket` wholesale breaks
asyncio's internal self-pipe and you get the cryptic
`'_UnixSelectorEventLoop' object has no attribute '_ssock'`. Patch only the
*outbound* paths — `socket.getaddrinfo`, `socket.create_connection`,
`socket.socket.connect`. Air-gapping means no traffic leaves the box, not that
sockets can't be constructed.

**D. The OpenAPI contract test fails *on purpose* when you add an endpoint.**
`test_openapi_matches_section_4_contract_exactly` asserts the route set equals
SPEC §4 exactly. Add or rename an endpoint and it goes red — that is the guardrail
catching drift from the frozen spec, **not** a broken test. The fix is a ratified
SPEC delta, then update the assertion — never quietly widen the test to hide a new
route.

**E. `/geocode` and `/locate` return bare dicts (no `response_model`).**
FastAPI won't validate or enforce their shape, so a dropped field won't surface as
a 500 — the **tests are the only guard**. That's why explicit `matched_address`
presence/absence assertions exist. If you change the geocode object, a green run
without updated assertions proves nothing.

**F. Nominatim `from_config` raising `ValueError` is correct behavior.**
No `user_agent` → `ValueError`, by design (the OSM policy blocks anonymous
traffic). A test asserting that is testing fail-fast config, not a bug.

**G. Boundary points are deliberately fuzzy in the fixtures.**
`test_lookup.py` builds two squares with a 200-ft *overlap strip* rather than a
shared edge, because the WGS84↔EPSG:3435 round-trip perturbs an exact-edge point
by ~1e-4 ft — you cannot reliably hit a shared boundary. Do **not** "fix" a flaky
boundary test by tightening the tolerance; the overlap strip is the intended
technique.

**H. `python: command not found` / `ModuleNotFoundError: app`.**
Both mean you're outside the venv or didn't `pip install -e`. The suite imports
`app`, which only resolves from the editable install. `source .venv/bin/activate`
first. (On this project `python` may not be on the bare PATH at all — use the
venv's interpreter.)

**I. The offline geocoder never raises — a miss is data.**
`local_points` matches `number + street` after normalization. A full
`"121 N La Salle St, Chicago, 60602"` now works (city/ZIP are stripped and used as
an optional filter), but a query with **no leading number**, a unit number, or an
intersection is a `no_match` — never a `GeocoderUnavailable`. Don't write a test
expecting it to raise; there is no transport to fail.

**J. Green pytest is not a green build.**
CI runs the secret scanner (`scripts/check_no_secrets.py`) **before** pytest. A
committed token fails the build even with pytest fully green. Run the scanner
locally before you push.

---

## 7. CI

`.github/workflows/ci.yml` runs on every PR to `main` and every push to `main`:

1. Python 3.12, `pip install -e ".[dev]"`.
2. Secret-scanner self-test, then the secret scan (`scripts/check_no_secrets.py`) —
   SPEC §9, no committed credentials.
3. `pytest -q`.

Because the suite is fully mocked and the data is committed, CI needs no secrets,
no services, and no network — it runs in seconds. Branch protection requiring this
check is a one-time maintainer chore (C4) once enabled on the remote.
