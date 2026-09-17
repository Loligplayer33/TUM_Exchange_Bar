# AGENTS.md

Instructions and technical guidelines for AI agents working in `tumexchange-bars`.

## About the Project & Motivation

### Background & Context
TUMexchange is the central university-wide exchange program of the Technical University of Munich (TUM), facilitating study-abroad placements at partner universities outside the European Erasmus program (spanning North America, Latin America, Asia, Oceania, Africa, and the UK).

### The Problem
* **Visual-Only Statistics:** TUM publishes historical placement success rates exclusively as graphical vector bar charts embedded in annual PDF documents ("TUMexchange Bewerbungschancen"). No machine-readable numerical tables, percentage values, or searchable databases are provided to students.
* **Fragmented 3-Year Windows:** Each release document only reports a rolling 3-year observation window (e.g. Release 2026 covers 2023–2025; Release 2027 covers 2024–2026). Tracking multi-year trends or comparing historical shifts requires tedious manual PDF inspection.
* **Disconnected Academic Requirements:** Academic eligibility requirements (eligible degree levels like Bachelor vs. Master, departmental subject restrictions, language prerequisites, semester dates, and application deadlines) live completely separately in TUM's MoveON publisher portal. Students are forced to cross-reference two disjointed platforms across hundreds of partner universities.

### Project Goals & Motivation
1. **Mathematical Extraction & Calibration:** Measure visual vector bar geometry directly from source PDFs (`pdfplumber`), calibrate them against known anchor widths, and convert them into exact, auditable placement rates (0–100%).
2. **Unified Historical Timeline:** Consolidate multiple overlapping release cycles into a coherent, queryable database (SQLite, CSV, JSON), resolving university name variations and tracking exchange availability across years.
3. **MoveON Enrichment:** Scrape and link partner profiles, faculty/department requirements, language thresholds, and links to official student experience reports.
4. **Student-Centered Discovery:** Present all data in an accessible, lightning-fast, portable HTML application that lets students filter by country, degree level, and subject area to evaluate realistic admission chances and make informed study-abroad choices.

## Core Commands

* **Environment setup:**
  ```bash
  uv sync
  ```
* **Run test suite:**
  ```bash
  uv run python -m unittest
  ```
  *(Note: uses Python's standard `unittest`, not `pytest`.)*
* **Regenerate combined report:**
  ```bash
  uv run tumexchange report data/output/combined/bars.sqlite \
    --output data/output/combined \
    --profiles data/source/moveon/catalog.json \
    --profile-matches data/source/moveon/matches.json
  ```
* **Extract a new release PDF:**
  ```bash
  uv run tumexchange extract <pdf-path> --output <output-dir>
  ```
* **Local preview server (multi-threaded):**
  ```bash
  python3 -m http.server 8080 -d data/output/combined
  ```
  Accessible at `http://localhost:8080/`.

## Architecture & Codebase Layout

* `tumexchange/cli.py`: CLI dispatcher (`extract`, `report`, `combine`, `query`, `fetch-profiles`, `match-profiles`).
* `tumexchange/extract.py`: Vector chart extraction from PDFs via `pdfplumber`, geometry calculation, calibration anchors.
* `tumexchange/report.py`: HTML report generator, SVG bar visualizations, grouping by university, MoveON profile integration, filter/sort client scripts.
* `tumexchange/moveon.py` & `tumexchange/profiles.py`: Scraper and matcher for TUM MoveON publisher portal relations and degree requirements.
* `data/source/`: Source PDFs (`TUMexchange_Chances_2026.pdf`, `TUMexchange_Chances_2027.pdf`) and MoveON catalog/matches JSON.
* `data/output/`: Extracted SQLite databases (`bars.sqlite`), CSV exports (`rows.csv`), and generated HTML reports.

## Domain Invariants & Rules

1. **Release Years vs Observation Years:**
   Filenames indicate document release years, not observation years. For example, release 2027 contains observation years 2024–2026; release 2026 contains 2023–2025.
2. **Bar Meaning & Acceptance Rates:**
   Vector bars indicate the placement rate of applicants.
   * Full bar (100%): All applicants were placed, or no students applied that year.
   * Partial bar: Percent of applicants accepted (e.g. 50% = half placed).
   * `no_applications` status is displayed as `100% (No applicants)` because places exceeded demand.
   * `no_cooperation` / `no_places` are non-scoring years (`No active exchange`) and excluded from university averages.
3. **UI & Student-Facing Contracts:**
   The generated HTML report is student-facing. Keep internal pipeline developer metadata (PDF point widths, SHA-256 hashes, bounding box coordinates, calibration IDs, raw JSON dump toggles) out of the visual UI, while maintaining complete provenance in the underlying SQLite database, CSV exports, and embedded `<script id="report-data">` JSON.
4. **Standalone Portability:**
   The generated report HTML (`combined-report.html`) must remain self-contained (inline CSS and vanilla JavaScript without external runtime CDNs or remote dependencies).
