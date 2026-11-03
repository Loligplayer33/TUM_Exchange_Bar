# TUMexchange bar extraction

Find the Website under: https://tum-exchange-bar.netlify.app/

This project turns the historical bars in TUMexchange selection charts into
queryable observations. It keeps the original PDF geometry, each document's
own calibration, and the resulting normalized bar score so every conversion
can be audited or recalculated.

The source files are annual releases whose observation years overlap. The
2026 release contains 2023–2025; the 2027 release contains 2024–2026. The year
in a filename is therefore the document release year, not an observation year.

## Setup

The project uses a local `uv` environment and a lockfile:

```sh
uv sync
```

The source PDF for the first extraction is kept at
`data/source/TUMexchange_Chances_2027.pdf`. Do not replace it silently: its
SHA-256 hash is recorded with the generated observations and calibration.

## Extract the supplied PDF

Run the installed CLI through `uv`:

```sh
uv run tumexchange extract \
  data/source/TUMexchange_Chances_2027.pdf \
  --output data/output/TUMexchange_Chances_2027
```

The extraction writes the queryable SQLite database, CSV and JSON exports, an
offline HTML report, and the calibration/provenance record into the selected
output directory. The HTML file can be opened directly; it does not need a
web server or network connection.

To see the exact options available in the installed version:

```sh
uv run tumexchange --help
uv run tumexchange extract --help
```

The read-only CLI query is useful when a shell table or JSON result is more
convenient than opening SQLite directly:

```sh
uv run tumexchange query \
  data/output/TUMexchange_Chances_2027/bars.sqlite \
  --country "Brazil" \
  --format table
```

## Browse university averages

Open `data/output/TUMexchange_Chances_2027/report.html` in a browser. The main
list shows one average per university, calculated from all available annual
scores. Assigned 100% entries are included; N/A entries are excluded. The
scored-year count shows how many years contribute to the average.

Click a university to expand its annual entries, newest year first. Each
annual entry has a separate control for the original measurement details.
Country, search, and status filters select universities; they do not change
their averages. A status match means at least one annual entry has that status.
Hover over or focus the status help control for the meaning of Assigned 100%.
The CSV, JSON, and database retain the individual annual entries.

## University profiles from MoveON

The combined report can be enriched with the public university profiles from
TUM's MoveON directory. Fetch the saved catalog when the source needs a
refresh:

```sh
uv run tumexchange fetch-profiles \
  --output data/source/moveon/catalog.json
```

The importer checks the complete catalog against the source's current relation
count, so additions and removals can be handled on later refreshes. The saved
catalog keeps all public fields, source identifiers, schema version and retrieval
timestamp, exact degree-level and subject-area values, and empty values where the
source is silent (shown as `Unknown`), plus links for further information. Profile data is kept
separate from historical score records: refreshing it does not recalculate
bars or averages, and the published report does not need to fetch MoveON at
runtime.

Generate the report with a reviewed matching file:

```sh
uv run tumexchange report \
  data/output/combined/bars.sqlite \
  --output data/output/combined \
  --profiles data/source/moveon/catalog.json \
  --profile-matches data/source/moveon/matches.json
```

The matching file records reviewed aliases. Ambiguous names are left
unmatched rather than being silently fuzzy-matched. A profile can therefore
appear without historical score rows; historical rows remain available even
when a university is absent from the latest profile catalog. Refreshing the
catalog and rebuilding the report are separate operations, so reviewed
matches persist across source updates. A refresh reports added, removed,
changed and unchanged relation IDs. A validated import replaces the catalog
atomically. Check changes without replacing the saved file with:

```sh
uv run tumexchange fetch-profiles --output data/source/moveon/catalog.json --check
```

Fact sheets that TUM serves through JavaScript link back to its directory and
retain their document IDs in the catalog. Ordinary external links are preserved.

For a local preview of the already-built combined report:

```sh
npx --yes live-server data/output/combined \
  --host=127.0.0.1 --port=8080 --no-browser \
  --entry-file=combined-report.html
```

## What the score means

The numeric value is a normalized bar score under the working assumption that
bar length changes linearly. It is not a measured admission probability and
the source does not establish that it is one.

For a measured rectangle, the calculation is:

```text
score_percent = 100 * raw_width_pt / full_width_pt
```

Values within the configured measurement tolerance of the full width are
snapped to exactly 100%; a bar that exceeds the calibrated reference beyond
that tolerance is treated as a calibration error rather than silently clipped.
The raw width and full width remain stored at PDF precision; the HTML report
may show a rounded percentage for readability.

Measurements use PDF points (1/72 inch), taken from the vector rectangle in
the PDF. Screenshot pixels are display-dependent and must not be mixed with
PDF points. If a future source is raster-only, its pixel coordinate system
must be recorded and calibrated separately.

UFRJ's 2025 observation is the explicit known-full reference in both supplied
documents. It measures `60.11997975` PDF points in the 2026 release and
`48.1200255` points in the differently scaled 2027 release. The darker
`no applications` boxes are about `51.12` points wide, so they are excluded
from geometric calibration.

Calibration is also per annual column. In the 2026 release, the 2023 track is
`59.2799835` points while the 2024–2025 tracks are about `60.12` points.
Each annual width is accepted only when at least three numeric bars repeat at
that track maximum; a unique longest bar is not treated as proof of 100%.

The UFRJ 2024 partial bar validates the per-document conversion: it normalizes
to about 66.67% in the 2026 release and 66.58% in the 2027 release despite the
different absolute widths. Across more than 150 common scored observations,
the two independently calibrated releases agree within 0.2 percentage points.

The source text states `no applications`, `no places`, and `no cooperation`
instead of drawing an ordinary numeric bar. Under the current working model,
`no applications` receives an assigned score of 100, with no raw numeric
width; its provenance records that the value was assigned rather than
measured. `no places` and `no cooperation` remain N/A and have no numeric
score. In the supplied document this produces 344 scored entries (327
measured and 17 assigned) and 31 N/A entries out of 375 university-year
cells.

## Calibration and reuse

Every observation records its source document hash, page, annual column, row
location, raw rectangle, full reference width, calibration identifier, and
extraction status. The calibration record also stores the reference rows and
the method used to select them, so a changed calibration can be reviewed
without losing the original measurement.

The extractor derives year columns, numeric-bar height, bar-track origins, and
country-label typography per document. It supports both supplied vector
templates and their different bar scales. It does not claim to support
arbitrary or raster-only PDFs. Every new template still fails closed until its
vector geometry and known-full reference can be validated; the largest
observed bar is never assumed to be 100% automatically.

## Query the SQLite output

The SQLite export is intended for direct inspection as well as use by the
HTML report. For example, after the extraction above:

```sh
sqlite3 data/output/TUMexchange_Chances_2027/bars.sqlite
```

```sql
SELECT country,
       COUNT(score_percent) AS scored_cells,
       AVG(score_percent) AS mean_score_percent
FROM observations
GROUP BY country
ORDER BY mean_score_percent DESC;
```

`COUNT(score_percent)` excludes N/A cells. Later summaries can calculate the
median or chosen quantiles over the same measured or assigned-score policy;
those summaries should state that policy explicitly. Grade and real admission
outcome data are a future, separate input and are not inferred by this
extractor.

## Another PDF and calibration

Use a separate output directory for each source PDF. UFRJ 2025 is selected as
the known-full reference by default:

```sh
uv run tumexchange extract /path/to/another.pdf --output data/output/another --reference-country Brazil --reference-year 2025
```

If a filename has no release year, pass `--source-release-year`. This value
controls which source wins when annual observations overlap:

```sh
uv run tumexchange extract /path/to/chart.pdf \
  --output data/output/chart \
  --source-release-year 2028
```

Alternatively, supply a JSON file through `--calibration` with explicit
`full_width_pt_by_year` values measured in that document. For example, the
following illustrates a hypothetical chart whose full bars are 60 points:

```json
{"full_width_pt_by_year": {"2023": 60, "2024": 60, "2025": 60}}
```

Replace both the years and widths with verified values. An exported calibration
is bound to its source hash. Cross-source reuse requires the explicit
`--allow-cross-source-calibration` flag after checking the geometry. Scanned PDFs
and other layouts need an additional extraction adapter. Unresolved cells fail
by default; `--allow-incomplete` retains them for inspection.

## Repeated runs and combined queries

An identical rerun is allowed. A changed extraction of the same source is
rejected by an existing database; use a new output directory for recalibration.
Exports are staged and checked before existing files are replaced. Each file
replacement is atomic, but the collection is not a single filesystem transaction.

Combine distinct source documents in a separate database:

```sh
uv run tumexchange import data/output/TUMexchange_Chances_2027/records.json --database data/combined.sqlite
```

Keep `calibration.json` beside each imported `records.json`. Import other
sources with the same command. `source_observations` preserves every raw
document row. The queryable `observations` table contains exactly one current
row per normalized university name and observation year: the highest
`source_release_year` wins regardless of import order, while an older
non-overlapping year remains current. Different documents claiming the same
university/year in the same release are rejected as ambiguous.

Build the combined offline UI and CSV from that current snapshot:

```sh
uv run tumexchange report data/combined.sqlite --output data/output/combined
```

University names are matched after Unicode, case, and whitespace
normalization. Release-only trailing acronyms such as `(NTU)` and the
`planned transfer …` annotation are ignored for identity, while distinct
school/program qualifiers remain significant. Universities absent from the
latest release keep their historical observations and receive a “Not listed
in release …” note in the combined top-level UI.

## Interpretation and assumptions

The source describes each bar from the number of places and applications in a
past selection round. This tool applies the project owner's working model:

- a known full numeric bar is represented as 100;
- bar width maps linearly to the normalized score, so half width maps to 50;
- `no applications` is explicitly assigned 100, without pretending its text
  background is a measured numeric bar;
- `no places` and `no cooperation` are N/A and excluded from means;
- university means include measured and assigned scores and exclude N/A years.

These are comparison assumptions, not claims established by the PDF. The
score is not an individual probability of admission, does not currently use a
student's grade, and does not establish that grades alone caused historical
selection outcomes. Grade examples, predictions, medians, and quantiles belong
to the planned later modelling step and are intentionally not inferred here.

## Tests

```sh
uv run python -m unittest discover -s tests -v
```

Tests cover source counts and year mapping, calibration, assigned scores,
changed numeric bar widths, incomplete extraction, and consistent reruns.
