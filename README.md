# Land Parcel Measurement & Revenue Records Tool

A fully-offline desktop tool for tracing land-parcel boundaries from scanned or
digital survey documents (PDF / DXF / image) and producing accurate area,
perimeter, and segment-length figures alongside the parcel's revenue-record
identification details.

See [PROJECT_BRIEF.md](PROJECT_BRIEF.md) for the full specification. The prior
browser prototypes whose *logic* seeds this tool are kept under
[reference/](reference/) for reference only.

## Status — Milestones 1–2 complete

- **Milestone 1 — portable project store**: create/open a project folder
  (`project.db` + `sources/` + `exports/`); its SQLite schema works identically
  from a local disk or a copied/mounted pen drive (confirmed on real hardware).
- **Milestone 2 — open + display + pan/zoom**: open a PDF or image, render it to
  a native `QGraphicsView` canvas, pan by dragging and zoom around the cursor
  with the scroll wheel. Loaders live in `src/io/` (pure Python, no Qt); the
  window and canvas are the only Qt code.

Later milestones (scale calibration, polygon tracing, unit profiles,
identification templates, summary export, DXF) are scaffolded as docstring-only
stubs under `src/` and will be filled in one milestone at a time.

### Run commands

Invoke as a module from the repository root (`python src/main.py …` also works):

```bash
python -m src.main gui [optional-file.pdf|png|jpg]   # Milestone 2 viewer
python -m src.main create ./my-parcel-project --name "Village X survey"
python -m src.main info   ./my-parcel-project
python -m src.main selfcheck                          # portability self-check
```

> **Import note:** the source tree is the package `src`, so its sub-package is
> imported as `src.io` — a bare top-level `io` on the path is shadowed by
> Python's standard-library `io` module and is not importable. Everything runs
> from the repo root as `python -m src.main`.

## Architecture rule (non-negotiable)

`src/core/` and `src/io/` contain **zero** PySide6 / UI-framework imports — pure
Python, independently testable. `src/ui/` is a thin layer that only calls into
`core`/`io` and never contains logic. This is what keeps a future mobile/tablet
UI a UI-only rewrite. The rule is enforced by a test
([tests/test_architecture.py](tests/test_architecture.py)), not just convention.

## Layout

```
src/
  main.py            CLI: create / info / selfcheck / gui
  core/              pure Python domain logic
    project_db.py    SQLite schema + read/write for one project folder  (DONE)
    scale.py units.py geometry.py polygon.py templates.py               (stubs)
  io/                pure-Python file loaders (no Qt)
    raster.py        neutral RasterImage type + open_raster dispatcher   (DONE)
    pdf_loader.py    PDF page -> RGBA raster via PyMuPDF                  (DONE)
    image_loader.py  PNG/JPEG -> RGBA raster via Pillow                  (DONE)
    dxf_loader.py    DXF (Milestone 9)                                  (stub)
  ui/                PySide6 layer, no logic
    main_window.py   file-open + display shell                          (DONE)
    canvas_view.py   QGraphicsView pan/zoom canvas                      (DONE)
    scale_dialog.py record_form.py                                      (stubs)
  export/            PDF/CSV/JSON summary generation                    (stub)
config/
  templates/         land-type identification field sets (JSON, editable)
  unit_profiles/     built-in area units (sq m / sq ft / acre / hectare)
tests/               stdlib unittest suite (no third-party install needed)
reference/           prior prototypes, kept for logic reference only
```

## Requirements

- Python 3.11+
- Milestone 1 needs only the standard library. Milestone 2 adds **PySide6**,
  **PyMuPDF**, and **Pillow**:

  ```bash
  pip install PySide6 PyMuPDF Pillow      # or: pip install -r requirements.txt
  ```

  The remaining packages in [requirements.txt](requirements.txt) arrive in later
  milestones. Every dependency works fully offline (no license server /
  phone-home), per the brief's dependency policy.

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
