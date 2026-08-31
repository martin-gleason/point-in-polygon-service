# Register — point-in-polygon-service

**Reconstructed from this project's own documents, not authored.** Every row
names the file and line it came from. A row nobody can trace back is a claim,
and a claim written down reads exactly like a recorded fact.

A `Status` of `unknown` means the source said nothing about status. It is a
question for the owner, not a guess.

---

## Decisions (D)

ADR-style: numbered, dated, **immutable once ratified**. A decision is never edited —
it is superseded by a later entry that links back. A rejected decision stays, struck
through, so it is not re-proposed.

**One intake path.** Anything that changes scope enters as a `D<n>`. An item with no
number has not been decided, however clearly it was said aloud.

| ID | Status | Milestone | Decision | Source |
|---|---|---|---|---|
| D10 | unknown | — | In-process plugin, or out-of-process shim? | `docs/plans/PLAN.md:243` |
| D11 | unknown | — | Entry-point group name | `docs/plans/PLAN.md:244` |
| D12 | unknown | — | Where the plugin lives | `docs/plans/PLAN.md:245` |
| D13 | unknown | — | Concurrency | `docs/plans/PLAN.md:246` |
| D14 | unknown | — | Scoring | `docs/plans/PLAN.md:247` |
| D15 | unknown | — | Synchronous, streaming, in-memory. No job queue, no server-side state. | `docs/plans/PLAN.md:188` |
| D16 | unknown | — | Google Sheets = published-CSV export only. No API key, no OAuth. | `docs/plans/PLAN.md:189` |
| D17 | unknown | — | XLSX via openpyxl, declared as an optional extra [batch]. | `docs/plans/PLAN.md:190` |
| D18 | unknown | — | Column mapping is explicit. Never guessed. | `docs/plans/PLAN.md:191` |
| D19 | unknown | — | Per-row isolation. | `docs/plans/PLAN.md:192` |
| D20 | unknown | — | Rate limiting on by default, and Nominatim refused outright. | `docs/plans/PLAN.md:193` |
| D21 | unknown | — | Escape formula-injection on output. | `docs/plans/PLAN.md:194` |
| D22 | unknown | — | Separate ASGI app, never a router on app.main. | `docs/plans/PLAN.md:207` |
| D23 | unknown | — | Loopback bind + one-time token + Host header check. | `docs/plans/PLAN.md:208` |
| D24 | unknown | — | Nothing is written until the operator confirms. | `docs/plans/PLAN.md:209` |
| D25 | unknown | — | Commit = normalize to GeoPackage + append TOML + timestamped backup. | `docs/plans/PLAN.md:210` |
| D26 | unknown | — | Validate by actually loading it the way the service does. | `docs/plans/PLAN.md:211` |
| D27 | unknown | — | Two severities: blocking errors and acknowledgeable warnings. | `docs/plans/PLAN.md:212` |
| D28 | unknown | — | Simplify geometry for display only. | `docs/plans/PLAN.md:213` |
| D29 | unknown | — | Harvest every vintage signal the format offers, and name the ones it doesn't. | `docs/plans/PLAN.md:214` |

## Risks (RR)

| RR1 | [docs-ignored] A documentation tree that can never be committed, reviewed, or seen by anyone cloning. The compliance checker cannot tell: it reads the filesystem, not the index. | proposed | — | — | Evidence: `git check-ignore -v docs CLAUDE.md` → .gitignore:27:CLAUDE.md	CLAUDE.md | agent |

| ID | Risk | Status | Likelihood | Impact | Mitigation | Owner | Source |
|---|---|---|---|---|---|---|---|

*(none found in this project's documents)*

## Owner items (O)

Outstanding items only the owner can close.

| ID | Item | P | Status | Source |
|---|---|---|---|---|

*(none found in this project's documents)*

## Chores (C)

> **Two rows were removed by hand on 2026-08-31.** `docs/plans/PLAN.md` uses `C1`/`C2` as
> *category* labels in a findings table — the cells read "PII" and "contract" — not as chore
> ids. The reconstructor cannot tell those apart from a real chore and re-adds them on every
> run; that is `O11` in the baseline register, not a fixed thing.

`conventions.md`: *a chore gets a file only when it has tasks and a verification
step; a one-line chore lives in the register.* This is that register. A chore with
its own plan file is listed here too, with a link, so one read gives all of them —
the absence of that read is how a chore killed by a ratified delta stayed open for
days on the owner's track.

| ID | Chore | P | Status | Owner | Plan | Source |
|---|---|---|---|---|---|---|

## Gates (G)

A gate is an **event**, not a place — but the event has to be recorded somewhere or it
exists only in the conversation where it happened. It is not inferable from the
filesystem: the first attempt flagged six features as awaiting a gate and all six were
already built.

| ID | Gate | Status | Plan written | Crossed | What the owner said | Source |
|---|---|---|---|---|---|---|

*(none found in this project's documents)*

## Hooks (H)

Deterministic enforcement. Prose is advisory; hooks are not. An `H<n>` is a plan-local
label and **never appears in a commit, branch, or PR title**.

| ID | Hook | Surface | Protects | Status | Source |
|---|---|---|---|---|---|
| H1 | Spec immutability (§ preamble, rule 7) | — | — | unknown | `docs/plans/v1.0-archive/PLAN.md:282` |
| H2 | Tests pass before merge | — | — | unknown | `docs/plans/v1.0-archive/PLAN.md:283` |
| H3 | No committed credentials (§9) | — | — | unknown | `docs/plans/v1.0-archive/PLAN.md:284` |
| H4 | No PII in logs (§9) | — | — | unknown | `docs/plans/v1.0-archive/PLAN.md:285` |
| H5 | OpenAPI ≡ implementation ≡ §4 (§9) | — | — | unknown | `docs/plans/v1.0-archive/PLAN.md:286` |
| H6 | No proprietary dep in the core or default install (§9) | — | — | unknown | `docs/plans/PLAN.md:743` |
| H7 | Plugin laziness | — | — | unknown | `docs/plans/PLAN.md:744` |
| H8 | Batch never persists (§9, D15) | — | — | unknown | `docs/plans/PLAN.md:745` |
| H9 | Output is injection-safe (D21) | — | — | unknown | `docs/plans/PLAN.md:746` |
| H10 | The admin tool is never publicly served (D22) | — | — | unknown | `docs/plans/PLAN.md:747` |

## Mutations (M)

Named ways to break the code, each paired with the test that must catch it.
A test is not evidence until a mutation proves it can fail.

| ID | File | Mutation | Caught by | Status | Source |
|---|---|---|---|---|---|

*(none found in this project's documents)*

-----

#AI/Claude
