# Land Parcel Measurement & Revenue Records Tool

A fully-offline desktop tool for tracing land-parcel boundaries from scanned or
digital survey documents (PDF / DXF / image) and producing accurate area,
perimeter, and segment-length figures alongside the parcel's revenue-record
identification details.

See [PROJECT_BRIEF.md](PROJECT_BRIEF.md) for the full specification. The prior
browser prototypes whose *logic* seeds this tool — and the earlier, superseded
[DESKTOP_TOOL_BRIEF.md](reference/DESKTOP_TOOL_BRIEF.md) — are kept under
[reference/](reference/) for historical reference only.

## Status — all 19 milestones complete

The GUI is the primary interface and has grown into a full application: a menu
bar and toolbars, dockable **Parcels / Boundary-segment / Location** panels,
several mutually-exclusive canvas modes (**Scale, Trace, Select, Segment,
Locate, Assist**), and dialogs for reports, area-unit profiles, and
identification templates — including **File > New Project** to create a portable
project folder.

- **M1 — portable project store**: create/open a project folder (`project.db` +
  `sources/` + `exports/`); the SQLite schema works identically from a local
  disk or a copied pen drive, verified by `selfcheck`.
- **M2 — open + display + pan/zoom**: render a PDF or image to a native
  `QGraphicsView` canvas; drag to pan, scroll to zoom around the cursor. Loaders
  are pure Python (`src/io/`, no Qt).
- **M3 — manual two-point scale (SI)**: pick two points a known distance apart,
  enter the real-world metres, derive metres-per-pixel (`src/core/scale.py`).
- **M4 — project-aware polygon tracing + measurement**: place a sequence of
  boundary points into a polygon with live segment / perimeter / area readout,
  plus undo / close / clear. Open/closed state is stored explicitly and restored
  exactly. Tracing works **without** a scale (readout shows "(no scale)" pixel
  units and switches to metres once scale is set). Geometry is pure Python
  (`src/core/geometry.py`), SI-canonical.
- **Point-placement refinements**: both scale and trace picks follow
  **place → adjust → confirm** (drag or arrow-key nudge, **Enter** confirms,
  **Esc** fully cancels), with an optional precision crosshair.
- **M5 — multiple parcels per source**: one sheet holds several
  independently-traced parcels, each its own record, in a **Parcels** sidebar
  (add / delete / switch active / edit **owner** — the owner-wise-report
  grouping key). Distinct colour per parcel; active one emphasised.
- **M6 — topology-aware shared boundaries**: parcels reference **shared
  vertices** owned by the source (`vertices` + `parcel_vertices`), so a shared
  edge is structurally identical, not just visually close. Tracing within
  `SNAP_TOLERANCE_PX` of another parcel's vertex snaps onto it (magenta ring
  warns first; a parcel never welds two of its own corners). Snap rule lives in
  `src/core/polygon.py`.
- **M7 — parcel selection (multi-select)**: a **working subset** kept strictly
  separate from the single active parcel. A **Select parcels** mode toggles
  membership by click or **marquee**; three on-canvas states (active / selected /
  context) are visually distinct. Selection is session-only; hit-testing is pure
  Python (`src/core/selection.py`).
- **M8 — image preprocessing (denoise + contrast)**: a **non-destructive,
  display-time** median denoise + CLAHE contrast enhancement, **value-only by
  construction** (never resizes/crops/rotates, so stored pixel coordinates stay
  valid). Pure Python in `src/io/preprocess.py`; the raw raster and the source
  file on disk are never modified.
- **M9 — unit profiles**: areas display in **SI plus an optional local unit,
  side by side**. Four built-in area units (sq m / sq ft / acre / hectare) are
  fixed exact conversions; user local profiles (`{name, sq m per unit}`) are
  managed in a dialog and stored per project. Active unit is **per source**.
  Canonical storage stays SI (`src/core/units.py`).
- **M10 — land-type templates + identification fields**: parcel metadata is
  `{label, value}` pairs (never fixed columns). Built-in templates (Rural-agri /
  Rural-residential / Urban) are seeded per project and protected read-only at
  the DB layer; user templates are editable. Applying a template is **additive**
  (keeps every existing field). Always present: source-document reference,
  owner, and free-text notes.
- **M11 — owner-wise summary report**: one report file **per owner** (every
  parcel under them, identification fields, area/perimeter, and a combined-area
  total), each parcel entry carrying a cropped boundary image. PDF / CSV / JSON
  export (`src/export/report.py`, `src/export/boundary_image.py`).
- **M12 — segment-length / boundary-description report**: a separate report
  type; edges are selected **on the canvas** (contiguous-only), a side table
  builds up per selected segment, feeding the export
  (`src/export/segment_report.py`).
- **M13 — visual confidence overlay**: a review mode drawing traced boundaries
  over the source scan — global hide/show, per-parcel visibility, and adjustable
  opacity — none of which touch stored geometry, the active parcel, or the mode.
- **M14 — PDF metadata scale auto-detection**: read the PDF's physical page size
  as a scale clue, shown alongside manual entry for comparison
  (`src/core/scale.py` + loader metadata).
- **M15 — DXF support**: open a DXF, read header units (`$INSUNITS`) for scale,
  and render entities to a raster at an extent-aware resolution
  (`src/io/dxf_loader.py`).
- **M16 — location-fixing (distance/trigonometry mode)**: mark on-sheet
  reference points for a selected subset of parcels and compute a
  distance + bearing description (encroachment-detection use case). Reuses the
  per-source M3 scale; GPS-anchored mode documented but deferred
  (`src/core/location.py`).
- **M17 — point-data guard rails**: warn (never auto-correct) on a
  likely-accidental duplicate/near point at placement time
  (`src/io/guardrails.py`).
- **M18 — semi-automated tracing assist**: follow a printed boundary line
  between two user-marked points via a bounded cost-minimising path search,
  always shown for confirmation before acceptance; manual tracing stays the
  fallback (`src/io/tracing_assist.py`).
- **M19 — grid/reference-content auto-detection**: detect ruled grid spacing via
  numpy peak-detection on pixel-darkness sums, offered as a final scale method
  (`src/io/grid_detect.py`).

### Run commands

The GUI is the primary interface. Invoke as a module from the repository root
(`python src/main.py …` also works):

```bash
python -m src.main                                    # launch the GUI (default)
python -m src.main gui [optional-file.pdf|png|jpg|dxf]# same, optionally opening a file
python -m src.main info   ./my-parcel-project         # diagnostics: schema + contents
python -m src.main selfcheck                           # portability self-check
```

New projects are created from the GUI (**File > New Project**).

> **Import note:** the source tree is the package `src`, so its sub-package is
> imported as `src.io` — a bare top-level `io` on the path is shadowed by
> Python's standard-library `io` module. Everything runs from the repo root as
> `python -m src.main`.

## Architecture rule (non-negotiable)

`src/core/` and `src/io/` contain **zero** PySide6 / UI-framework imports — pure
Python, independently testable. `src/ui/` is a thin layer that only calls into
`core`/`io` and never contains logic. This is what keeps a future mobile/tablet
UI a UI-only rewrite. The rule is enforced by a test
([tests/test_architecture.py](tests/test_architecture.py)), not just convention.

## Layout

```
src/
  main.py            CLI: gui (default) / info / selfcheck
  core/              pure Python domain logic
    project_db.py    SQLite schema + read/write for one project folder
    scale.py         two-point + metadata + grid scale math (metres per pixel)
    geometry.py      segment / perimeter / shoelace-area (SI)
    polygon.py       shared-vertex snap rule + tolerance
    selection.py     parcel hit-testing (click + marquee)
    units.py         built-in + local area-unit conversions (SI-canonical)
    location.py      distance/bearing location-fixing (trig mode)
    templates.py     land-type template defaults (storage lives in project_db)
  io/                pure-Python file loaders + processing (no Qt)
    raster.py        neutral RasterImage type + open_raster dispatcher
    pdf_loader.py    PDF page -> RGBA raster + physical page size (PyMuPDF)
    image_loader.py  PNG/JPEG -> RGBA raster + DPI metadata (Pillow)
    dxf_loader.py    DXF render + header-units scale (ezdxf)
    preprocess.py    denoise (median) + contrast (CLAHE), value-only
    guardrails.py    likely-duplicate point warning
    tracing_assist.py line-following path search
    grid_detect.py   ruled-grid spacing peak-detection
  ui/                PySide6 layer, no logic
    main_window.py canvas_view.py scale_dialog.py record_form.py
    identification_dialog.py templates_dialog.py unit_profiles_dialog.py
    report_dialog.py
  export/            report generation
    report.py        owner-wise summary (PDF/CSV/JSON); PDF text writer
    segment_report.py boundary-description report
    boundary_image.py cropped-boundary image for reports
    assets/fonts/    bundled Noto Sans Devanagari (SIL OFL) for Unicode PDF text
scripts/
  source_smoke_test.py  run real documents through the loaders + diagnostics (path args)
tests/               stdlib unittest suite (no third-party install needed to run)
reference/           prior prototypes + superseded brief, kept for reference only
```

## Requirements

- Python 3.11+
- Runtime dependencies (see [requirements.txt](requirements.txt)): **PySide6**,
  **PyMuPDF**, **ezdxf**, **Pillow**, **numpy**. Every dependency works fully
  offline (no license server / phone-home), per the brief's dependency policy.

```bash
pip install -r requirements.txt
```

The pure-Python `src/core` and much of `src/io` are independently testable, but
running the app and the full suite needs the packages above.

## Portability & source-file immutability (non-negotiable)

A project folder is self-contained: moving, backing up, or copying it to a pen
drive is just copying the folder. Source documents stay as files under
`sources/` and are referenced by relative path from `project.db` — never
embedded as database blobs.

Once a file is copied into `sources/` it is the canonical original and is never
overwritten in place by anything the app does — not the M8 preprocessing preview
(kept in memory only), nor any derived artifact (which must be a
distinctly-named new file). The rule is guarded by a test
([tests/test_source_immutability.py](tests/test_source_immutability.py)) that
checksums the imported file across an open → enhance → trace → save flow.

## Tests

```bash
python -m unittest discover -s tests -v
# or, if you have pytest installed:
pytest tests
```

GUI tests run headless under Qt's offscreen platform
(`QT_QPA_PLATFORM=offscreen`).
