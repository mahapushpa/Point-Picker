# Land Parcel Measurement & Revenue Records Tool

A fully-offline desktop tool for tracing land-parcel boundaries from scanned or
digital survey documents (PDF / DXF / image) and producing accurate area,
perimeter, and segment-length figures alongside the parcel's revenue-record
identification details.

See [PROJECT_BRIEF.md](PROJECT_BRIEF.md) for the full specification. The prior
browser prototypes whose *logic* seeds this tool are kept under
[reference/](reference/) for reference only.

## Status — Milestone 1 complete

Milestone 1 delivers the **portable project store**: create/open a project
folder (`project.db` + `sources/` + `exports/`) and prove its SQLite schema
works identically from a local disk or a copied/mounted pen drive. No GUI yet.

Later milestones (file rendering, scale calibration, polygon tracing, unit
profiles, identification templates, summary export) are scaffolded as
docstring-only stubs under `src/` and will be filled in one milestone at a time.

## Architecture rule (non-negotiable)

`src/core/` and `src/io/` contain **zero** PySide6 / UI-framework imports — pure
Python, independently testable. `src/ui/` is a thin layer that only calls into
`core`/`io` and never contains logic. This is what keeps a future mobile/tablet
UI a UI-only rewrite. The rule is enforced by a test
([tests/test_architecture.py](tests/test_architecture.py)), not just convention.

## Layout

```
src/
  main.py            Milestone 1 CLI (create / info / selfcheck); GUI entry later
  core/              pure Python domain logic
    project_db.py    SQLite schema + read/write for one project folder  (DONE)
    scale.py units.py geometry.py polygon.py templates.py               (stubs)
  io/                pure-Python file loaders (pdf/dxf/image)            (stubs)
  ui/                PySide6 layer, no logic                             (stubs)
  export/            PDF/CSV/JSON summary generation                    (stub)
config/
  templates/         land-type identification field sets (JSON, editable)
  unit_profiles/     built-in area units (sq m / sq ft / acre / hectare)
tests/               stdlib unittest suite (no third-party install needed)
reference/           prior prototypes, kept for logic reference only
```

## Requirements

- Python 3.11+ (uses only the standard library for Milestone 1)
- Milestone 1 needs no third-party packages. Packages in
  [requirements.txt](requirements.txt) arrive from Milestone 2 onward.

## Using it (Milestone 1)

```bash
# Create a new portable project folder
python src/main.py create ./my-parcel-project --name "Village X survey"

# Inspect its schema and contents
python src/main.py info ./my-parcel-project

# Prove portability: build a project, copy the whole folder to a second
# location, reopen it from there, and confirm nothing absolute was stored
python src/main.py selfcheck
```

A project folder is self-contained: moving, backing up, or copying it to a pen
drive is just copying the folder. Source documents stay as files under
`sources/` and are referenced by relative path from `project.db` — never
embedded as database blobs.

## Tests

```bash
python -m unittest discover -s tests -v
# or, if you have pytest installed:
pytest tests
```
