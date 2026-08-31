# Batch lookups — running a whole spreadsheet through the service

How to take a file of addresses (or coordinates) and get the containing polygon
for every row: a CSV, an Excel `.xlsx`, or a link-shared Google Sheet in, and a
`.csv` back out with the answers appended to your original columns.

**Input may be any of those three; output is always a `.csv`.** Excel opens
`.csv` files normally. The reason for the restriction is in
[Gotchas](#5-gotchas).

This is **F7**, added by spec delta Δ3 (ratified 2026-08-01). It runs as a local
CLI — your file never leaves your machine, and the service stores nothing.

> **ArcGIS / ArcPy equivalent.** This replaces `arcpy.geocoding.geocodeAddresses`
> (a table of addresses in, a point feature class out) followed by
> `arcpy.analysis.SpatialJoin` against the boundary layer, then exporting the
> joined table back to a spreadsheet. Here it is one pass with no intermediate
> feature classes, no geodatabase, and no Esri license.

---

## 1. The short version

```bash
source .venv/bin/activate

# Rows that already have coordinates — no network, no geocoder, seconds.
python scripts/batch_locate.py caseload.csv \
    --out located.csv --layer police_districts \
    --lat-column lat --lon-column lon

# Rows that have addresses — geocodes each one, so it is rate-limited.
python scripts/batch_locate.py caseload.csv \
    --out located.csv --layer police_districts \
    --address-column "Home Address"
```

Your original columns come back untouched and in their original order, with the
results appended as `pip_*` columns: `pip_status`, `pip_reason`,
`pip_matched_address`, `pip_score`, `pip_provider`, `pip_lon`, `pip_lat`, and
one column per layer attribute (`pip_dist_num`, `pip_dist_name`).

`pip_status` is one of `matched`, `outside_all_polygons`, `no_geocode`, or
`error` — and `pip_reason` says why for anything that isn't `matched`.

## 2. Which input do you have?

| Source | How to pass it | Needs |
|---|---|---|
| CSV | the file path | nothing extra |
| XLSX | the file path | `pip install -e ".[batch]"` |
| Google Sheet | paste the browser URL in quotes | nothing extra |

**Legacy `.xls` is not supported.** Open it in Excel and save as `.csv` or
`.xlsx`. Supporting the old BIFF format would mean adding a dependency for a
dying format, which the project's simplicity rule doesn't earn.

### Google Sheets: the sharing step

This is the step that generates every support question. The sheet must be
readable without signing in:

**Share → General access → Anyone with the link → Viewer.**

Then copy the URL from your browser's address bar and pass it in quotes:

```bash
python scripts/batch_locate.py \
    "https://docs.google.com/spreadsheets/d/1AbC.../edit#gid=0" \
    --out located.csv --layer police_districts --address-column Address
```

If the sheet isn't shared, you get a message saying exactly that — the tool can
tell "not shared" apart from a genuine network error. `--sheet-gid` picks a
specific tab if the URL doesn't carry one. Both the private
(`/d/<id>/edit`) and the "Publish to web" (`/d/e/<id>/pub`) URL forms work.

**No API key and no Google credentials are needed**, ever. The tool reads the
sheet's public CSV export, the same way it consumes a public ArcGIS endpoint.
Private sheets requiring OAuth are deliberately out of scope.

## 3. How long will it take?

**This is the thing to plan for.** Every address needs one geocoder call, and
the default is a polite one call per second:

| Rows | Address rows (1/s) | lat/lon rows |
|---|---|---|
| 100 | under 2 minutes | instant |
| 1,000 | about 17 minutes | a second or two |
| 5,000 | about 83 minutes | a few seconds |

The CLI prints the estimate before it starts. Ctrl-C is safe — a CSV run keeps
the rows already written.

**Three ways to make a big run fast:**

1. **Geocode once, locate many.** If you'll run the same caseload against
   several layers, geocode it once, then use `--lat-column pip_lat
   --lon-column pip_lon` on the output for every subsequent layer. Those runs
   need no network at all.
2. **Use the offline geocoder** for anything over a few hundred addresses. It
   matches against a local address-point GeoPackage with no rate limit and no
   network. See `docs/runbooks/deployment.md` §2 to configure `local_points`.
3. **Trial-run with `--max-rows 20`** before committing to the whole file. Check
   the output looks right, *then* run the rest.

**Do not point a batch run at the shared public Nominatim instance.** The tool
refuses it, because the OSM usage policy forbids service-rate traffic against
it. `--allow-nominatim` exists only for an instance you host yourself.

Be considerate of Cook County's public locator too — it's a service run for
residents. The default rate limit is there for a reason; don't lower it.

## 4. Checking the results before you trust them

A batch job that returns 2,000 confidently wrong rows is far more dangerous
than one that crashes, because nobody re-checks a spreadsheet that looks fine.

- **Spot-check a handful.** Take three rows whose answer you already know and
  confirm them against the single-address endpoint (`/locate`, see
  `docs/runbooks/testing.md` §3).
- **Compare the row counts.** The CLI prints `read N data rows` and
  `wrote M rows`. If either differs from what your source actually contains,
  something was dropped — investigate before using the output.
- **Read any WARNING lines.** A warning about a cell spanning multiple lines
  usually means an unclosed `"` quote merged rows together, and rows are
  missing. It is only harmless if your file genuinely uses multi-line cells.
- **Sort by `pip_status`** and look at everything that isn't `matched`.

Exit codes: `0` every row matched, `1` the run finished but some rows did not
match, `2` the run could not finish (the message says why).

## 5. Gotchas

**Column names are never guessed.** If you mistype `--address-column`, the tool
fails and lists the columns your file actually has. It will not silently pick a
column that looks close — guessing wrong on a few thousand real addresses
produces plausible, wrong, unnoticed output.

**Don't write output over your input.** The tool refuses when `--out` resolves
to the source path (it would truncate your file mid-read).

**Don't re-run over a previous output file.** It already has `pip_*` columns, and
a second run would produce duplicates that neither this tool nor pandas can read
unambiguously. The tool refuses, before creating any file. Write to a new name.

**Excel mangles data before we ever see it.** Leading-zero ZIP codes become
`60622` → `60622` but `07001` → `7001`, long house numbers go scientific, and
dates get reformatted. The reader keeps every cell as text, but a ZIP already
destroyed in the source file is unrecoverable. Format address columns as **Text**
in Excel before saving.

**Output is `.csv` only — `--out something.xlsx` is refused.** Writing an Excel
file means copying every row, addresses and all, through a temporary working file
before the workbook is assembled. That copy is cleaned up when a run ends
normally, but a power cut or a killed process leaves it sitting on disk, and
SPEC §9 does not allow a queried address to be left anywhere. So the Excel
*writer* is gone; the Excel *reader* is untouched. Excel opens `.csv` files
normally. The refusal happens in the first second, before your file is read and
before any geocoding starts, so a long run can never be wasted on it.

**A killed run still leaves a valid partial file.** CSV rows are flushed as they
are written, so a run interrupted at row 1,400 gives you 1,400 usable rows.

**The output file is created readable only by your user account** (mode `0600`
where that means anything). It contains the addresses you looked up — keep it
somewhere private and delete it when you're done. The tool says so at the end of
every run. If you are overwriting a file that already exists, its existing
permissions are left alone.

**Output cells are made safe for Excel.** A cell beginning `=`, `+`, `-`, or `@`
is a formula-injection vector when the file is opened in a spreadsheet, and
geocoders return text we don't control. Every written cell, including the header
row and your own passthrough columns, is neutralized with a leading apostrophe.
Coordinates are exempted so negative longitudes stay numeric.

**A hostile spreadsheet can't exhaust your memory.** `.xlsx` inputs are checked
for decompression-bomb ratios before being opened.

## 6. What the service never does

Per SPEC §9, and worth being precise about:

- The **service** persists nothing. There is no job queue, no upload store, no
  cache, no scratch file.
- Your **output file** obviously contains your addresses — that's your own data,
  on your own machine, written where you asked. That is not a violation; it is
  the point.
- **No address is ever written to a log, an error message, or a warning.** Row
  numbers and column names appear in diagnostics; cell contents never do.
- A **lat/lon run touches no network whatsoever** — no geocoder is even
  constructed. That is the air-gapped path, and it is tested with sockets
  severed.

---

August 2026

#AI/Claude
