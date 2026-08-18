# Land Parcel Measurement & Revenue Records Tool

A fully-offline desktop tool for tracing land-parcel boundaries from scanned or
digital survey documents (PDF / DXF / image) and producing accurate area,
perimeter, and segment-length figures alongside the parcel's revenue-record
identification details.

See [PROJECT_BRIEF.md](PROJECT_BRIEF.md) for the full specification. The prior
browser prototypes whose *logic* seeds this tool are kept under
[reference/](reference/) for reference only.

## Status — Milestones 1–4 complete

- **Milestone 1 — portable project store**: create/open a project folder
  (`project.db` + `sources/` + `exports/`); its SQLite schema works identically
  from a local disk or a copied/mounted pen drive (confirmed on real hardware).
- **Milestone 2 — open + display + pan/zoom**: open a PDF or image, render it to
  a native `QGraphicsView` canvas, pan by dragging and zoom around the cursor
  with the scroll wheel. Loaders live in `src/io/` (pure Python, no Qt); the
  window and canvas are the only Qt code.
- **Milestone 3 — manual two-point scale (SI only)**: click "Set scale", click
  two points a known distance apart, enter the real-world distance in metres,
  and the tool derives and displays metres-per-pixel. Redo any time. The scale
  math is pure Python (`src/core/scale.py`).
- **Milestone 4 — project-aware polygon tracing + measurement**: File > New/Open
  Project registers the loaded file into the project's `sources/` (copied,
  referenced by relative path) and persists the scale and traced boundary to
  `project.db`; reopening restores them. "Trace" places a sequence of boundary
  points forming a polygon (orange, distinct from the green scale markers),
  with live segment / perimeter / area readout, plus undo / close / clear. The
  boundary's open/closed state is stored explicitly (`parcels.closed`) and
  restored exactly on reload — an open 3+-point boundary reloads open, not
  auto-closed. Geometry is pure Python (`src/core/geometry.py`), SI only. Tracing
  works **without** a scale too — the readout shows pixel units with a "(no
  scale)" indicator and switches to metres the moment a scale is set.

- **Point-placement refinements (shared by scale + tracing)**: both picks follow
  **place → adjust → confirm**. After placing a point you can drag it, or select
  it and nudge with the arrow keys (1 px, or 10 px with Shift), before
  finalising. **Enter** confirms (scale → distance prompt; polygon → close);
  **Esc** fully cancels an in-progress pick with no residual points. A toggleable
  full-window precision crosshair (dark core + light halo, CAD/GIS style) is on
  by default while calibrating scale, off by default while tracing.
- **Milestone 5 — multiple parcels per source**: one sheet can hold several
  independently-traced parcels, each its own record. A **Parcels** sidebar lists
  them, lets you add / delete / switch the active one, and edit its **owner**
  (the field parcels are grouped by for owner-wise reporting later). Each parcel
  draws in a distinct colour; the active (editable) one is emphasised and the
  others show as context. Points, closed-state, and owner persist and restore
  per parcel. No structural schema change was needed (`parcels.source_id` already
  allows many); `owner` was added as an additive column (v4).
- **Milestone 6 — topology-aware shared boundaries**: parcel boundaries are now
  ordered references to **shared vertices** owned by the source (`vertices` +
  `parcel_vertices`, replacing the per-parcel `points` table — schema v5, with a
  rebuild-and-dedup migration). Tracing a point within `SNAP_TOLERANCE_PX` of an
  existing vertex (of a *different* parcel) snaps onto it — a magenta ring warns
  before it snaps, and a parcel never welds two of its *own* corners. Moving a
  shared vertex (drag or arrow-nudge) moves it for **every** parcel referencing
  it, so a shared edge is structurally identical, not just visually close. The
  snap rule lives in `src/core/polygon.py` and is used by both the canvas and the
  DB.

Later milestones (parcel multi-select, image preprocessing, unit profiles,
identification templates, reports, DXF, location-fixing) are scaffolded or
pending and will be filled in one milestone at a time.

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
    scale.py         two-point scale math (metres per pixel)            (DONE)
    geometry.py      segment / perimeter / shoelace-area (SI)           (DONE)
    polygon.py       shared-vertex snap rule + tolerance                (DONE)
    units.py templates.py                                               (stubs)
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
