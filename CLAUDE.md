# CLAUDE.md — Point-in-Polygon Service

@docs/conventions.md

## What this is

A FOSS point-in-polygon web service (FastAPI + GeoPandas). Generic engine; first
dataset = Chicago / Cook County police districts. The runtime must stay
open-source and key-free.

- **Intention + API contract:** `docs/specs/SPEC.md` — the contract. Do **not**
  edit it to match the code; propose deltas, the maintainer ratifies.
- **Build detail:** `docs/plans/PLAN.md` — mutable.

## Golden rules

1. **Keep it simple.** No dependency or abstraction that isn't earned by a
   current feature.
2. **No proprietary runtime deps** — no `arcpy`, no ArcGIS license, no mandatory
   paid API keys. Consuming a *public* ArcGIS REST endpoint over HTTP is fine.
3. **Document every open-source step with its ArcPy/ArcGIS equivalent** in a
   docstring. The next maintainer may know one toolset and not the other.
4. **No persistence of queried addresses or PII.**
5. **Clear, nominative names** (`PolygonLookup`, `ArcGISRestGeocoder` — not `pl`,
   `g`).
6. **Root cause, not symptom.** Fix the thing, not the surface.
7. **Spec is immutable once ratified; plans change.** Never edit the spec you are
   held to.

## Learning dial: 5% (floor)

Agent authors; the maintainer reviews **every** PR and logs it. No 🎓 features
unless the maintainer tags one.

## Workflow

- **Spec review + planning — Ultrathink.** Batch clarifying questions, paraphrase
  the brief back, then draft the plan.
- **Implementation — ultracode**, unless a planning prompt says otherwise.
- **Adversarial review — mandatory** (`.claude/agents/adversarial-reviewer.md`):
  at the end of every feature, and at the start of every session (re-read the
  SPEC feature list, then an adversarial pass on what shipped). Never resume
  blind.
- **Verification before "done":** run a real check (pytest / build / lint) and
  show the command and its output. No feature is done on assertion.

## Gates

- **Gate 0 — SPEC ratified. [CLEARED 2026-07-06]** `docs/specs/SPEC.md` is
  frozen; planning (ultrathink) and build (ultracode) may proceed from it.
- Every feature is separated by a gate; crossing one needs the maintainer's
  verbal yes.

## Sourcing & license

- Standalone repo: `docs/conventions.md` is vendored here and `@import`ed
  locally — never reaching into a parent that won't exist in a clone.
- **AGPLv3.** Keep the license notice intact; it is the reason the tool stays
  open even when someone runs it as a hosted service.

-----
July 6, 2026

#AI/Claude
