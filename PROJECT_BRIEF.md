# Project Brief: Land Parcel Measurement & Revenue Records Tool

## Goal
A **desktop tool** for tracing land parcel boundaries from scanned/digital survey
documents and producing accurate area, perimeter, and segment-length figures,
alongside a summary that combines those measurements with the parcel's
identification details (survey/khasra number, owner/tenant, classification,
etc. — whatever fields the source document carries).

Core capability:
1. **Open a file** — PDF, DXF (CAD), or raster image (PNG/JPEG)
2. **Establish real-world scale** for that file — automatically where possible,
   confirmed or supplied by the user where not
3. **Trace the parcel boundary** — mark points in sequence to build a closed
   polygon (not just isolated points)
4. **Compute measurements** — segment lengths, perimeter, area — in real units
5. **Attach identification details** — let the user enter/paste the parcel's
   revenue-record fields alongside the geometry
6. **Export a summary** — one document per parcel combining geometry results
   and identification details, in a shareable format

This grows out of a manual, chat-based version of this exact workflow
(digitizing a boundary from scanned government survey sheets, establishing
scale from grid spacing / page size / handwritten scale notes, marking
corners, computing area). The working prototypes from that process
(`point_picker.html`, `extract_marked_points.py`) are attached as reference —
their *logic* carries forward into this tool, not the files themselves (see
"Is HTML needed?" below).

## File format scope
- **PDF** — render pages to raster for display; also read the PDF's own
  physical page size (points → inches → mm) as a scale clue when the page was
  generated at true physical scale. In the source project this was
  cross-validated against a page grid and a handwritten scale note and all
  three agreed within ~1%.
- **DXF** (not DWG) — open, documented, text-based, with a mature open library
  (`ezdxf`). No SDK/licensing hurdles, unlike DWG. CAD files often carry
  real-world units/scale in header data — read that directly rather than
  re-deriving it visually. If DWG support becomes a hard requirement later,
  treat it as a separate scope decision, not an assumption.
- **Images (PNG/JPEG)** — no reliable embedded scale in general (DPI metadata
  exists sometimes but is often wrong/missing on scans). This is the case that
  most needs the "ask the user" flow, and will likely be the most common input
  for scanned revenue survey sheets.

## Offline & storage constraints (non-negotiable)
- **Fully offline.** No network calls anywhere in the app — no cloud storage,
  no external API (including no Claude/Anthropic API — this tool doesn't call
  any AI service at runtime), no telemetry, no auto-update checks. Everything
  the app does happens on the local machine.
- **Portable by design.** A project (source files + traced data + summaries)
  must be a self-contained folder that works identically from a local disk or
  a pen drive — no absolute paths baked in, no OS-level install location
  assumed for project data, no background service required to open it.
- **Database: SQLite, single file, stdlib only.** `sqlite3` ships with Python
  — no server process, no separate install, and the `.db` file is just a file
  that copies/moves with the rest of the project. This is the simplest option
  that still supports querying across parcels (e.g. "all parcels in Tehsil
  X") once there's more than a handful — plain JSON/CSV becomes awkward past
  that point, but a full client-server DB (Postgres/MySQL) is unnecessary
  weight for a single-user local tool.
- **Source files stay as files, not DB blobs.** PDFs/images/DXFs referenced
  by a project live in the project folder and are referenced by relative
  path from the SQLite DB — never embedded as binary blobs in the database.
  Keeps the DB small and the source documents directly openable/verifiable
  outside the tool.
- One project = one folder = one `.db` file + a `sources/` subfolder + any
  exported summaries. Moving, backing up, or copying to a pen drive is just
  copying that folder.

## Land identification & revenue-record fields
These aren't computed — they're metadata the user attaches to a traced
parcel, and they feed straight into the summary output. Field *names* vary by
land type and region (Khasra vs. Plot vs. House number are the same role —
"local parcel identifier" — under different labels), so don't hardcode field
names. Model it as:

- **Land-type templates** — starting field sets for the three known cases,
  fully user-editable (rename, add, remove fields per parcel):
  - **Rural — agricultural**: Khasra number, Khata number, Village, Tehsil,
    District, Owner/tenant/patta-holder name(s), Land classification
  - **Rural — residential**: Plot number (replaces Khasra), Village, Tehsil,
    District, Owner name(s)
  - **Urban**: Plot/House number, Colony/Locality name, Ward, City, District,
    Owner name(s)
- **Under the hood, every field is a `{label, value}` pair**, not a fixed
  column — so a parcel can carry multiple identifiers at once (e.g. an old
  Khasra number *and* a new Plot number after conversion) and multiple
  address levels (village/tehsil/district *or* colony/ward/city, or both)
  without the data model needing to change.
- Templates are saved/reusable per land-type and are a starting point only —
  editing a field on one parcel never touches the template.
- Also always: source document reference (file name, page, date) and a
  free-text notes field, regardless of template.

Store templates as simple JSON/YAML under `config/templates/` so new land
types or regional variants can be added without touching code.

## Scale-determination workflow (the core hard problem)
Use multiple methods and cross-validate when more than one is available:

1. **From file metadata** — PDF physical page size, DXF units/header, image
   DPI if present and plausible.
2. **From visible reference content** — a printed scale bar, a ruled
   reference grid, or a written scale note (e.g. "1 cm = 40 m"). Grid
   detection worked well via peak-detection on pixel-darkness sums across
   rows/columns to find ruled line spacing.
3. **From user-supplied reference** — the user marks two points a known
   real-world distance apart (or traces a known-area shape) and scale is
   derived from that. This is the universal fallback and should be the
   *first milestone* to implement — get one method working end-to-end before
   adding auto-detection.
4. **Cross-check, don't trust one source blindly** — when two+ methods are
   available, compare them and surface disagreement rather than silently
   picking one. Three independent methods agreeing within ~1% is what made
   calibration trustworthy in the source project.
5. **Be honest about uncertainty** — if only one weak method is available,
   say so rather than presenting a confident-looking number.

## Point-marking / digitizing
- Pan/zoom a rendered page or image (native canvas, ported from the
  `point_picker.html` pan/zoom/click logic)
- Click to mark points in sequence, building a **closed polygon** — this was
  requested but never fully built in the source project; design it in from
  day one rather than bolting it on later
- Once scale is established, show live real-world segment length and running
  perimeter/area as points are placed
- Support undo, point removal, and re-ordering
- Export both pixel and real-world coordinates (JSON/CSV/GeoJSON-ish)
- **Multiple parcels per source (confirmed requirement).** One sheet
  commonly contains several khasra boundaries under a single owner. The
  tool must support tracing and independently measuring several polygons
  on the same loaded source, each becoming its own parcel record, not one
  parcel per file. This also means "owner" becomes a linking field across
  multiple parcels, used for owner-wise reporting (see Reporting below).

## Measurement & unit handling
- Perimeter = sum of segment distances between consecutive polygon points
- Area = shoelace formula on the closed polygon (real-world coordinates, not
  pixels)
- **Canonical storage is always SI** (metres, sq. metres) — nothing
  region-specific ever gets hardcoded into the geometry engine.
- **Local units are user-defined profiles, not a built-in table.** Bigha and
  Biswa aren't uniform across states/districts, so there's no safe hardcoded
  conversion. Instead:
  - A unit profile = `{name, conversion factor to sq. m}` (e.g.
    "Bigha — Jaipur" → some sq. m value the user supplies or confirms)
  - Profiles are saved and reusable, selectable per document/region
  - The summary always shows SI (sq. m, and hectare/acre as standard
    derived units) alongside the selected local profile, so there's always a
    verifiable baseline even if a local conversion factor turns out wrong
  - Ship with sq. m, sq. ft, acre, hectare built in as fixed, universally
    correct units; everything else (Bigha, Biswa, or any other local measure)
    is a user-added profile from day one

## Reporting
Three distinct outputs, not one generic report — each serves a different
purpose:

1. **Owner-wise summary report.** Grouped by owner: every khasra/parcel
   under that owner, its identification fields, area and perimeter, and a
   **total row** (combined area across all their parcels). This is the
   standard format revenue-land summaries are expected in, and is the
   primary deliverable most sessions will end with.
2. **Segment-length / boundary-description report — a separate, specific
   report type, not a byproduct of the summary.** Traditional boundary
   descriptions list each edge with direction and length (e.g. "North:
   bounded by [X], 45 m"). The user should be able to **select which
   segments to include** (not forced to include every edge) — e.g. only
   boundary edges shared with a named neighboring feature, excluding
   internal/construction lines. This needs its own export path, since its
   structure (one row per selected segment) differs from the parcel-summary
   structure (one row per parcel).
3. **Visual confidence overlay.** Before trusting a report, the user should
   be able to see the traced boundary drawn directly over the source scan,
   selectable/toggleable per parcel, so overlap/misalignment is visible at
   a glance rather than trusted blindly. This is a review step, not a
   report format — likely a canvas mode rather than an export.

All exports: PDF (primary, shareable) and CSV/JSON (record-keeping /
import elsewhere). Each report also carries: scale-determination method
used and its confidence/cross-check note, and source file reference.

## Location fixing (local georeferencing)
A second core hard problem, structurally parallel to scale-determination
but solving for *position* instead of *size*. The user marks well-defined,
durable reference features already visible on the sheet — tubewell,
building, or other long-lived structures (school, water tank) — the same
way scale-calibration points are marked. Two modes, since what's known
about those reference points varies by survey age:

1. **GPS-anchored mode** (modern surveys, or older surveys where the
   landmark still exists and can be located today). The user supplies known
   real-world GPS coordinates for each marked reference point. With 2+
   points, compute a coordinate transform between sheet-pixel space and
   real-world GPS space, giving the traced parcel an absolute location —
   and as a side benefit, this also cross-checks against the scale factor
   from Milestone 3, since GPS-anchored reference points imply a scale too.
2. **Distance/trigonometry mode** (older surveys, no GPS available). The
   user supplies a known real-world distance (and bearing, if known) from a
   reference point to the parcel. Position is computed by trigonometry —
   this doesn't require the reference point's own absolute coordinates,
   only the relationship between it and the parcel, so it still works when
   nothing on the sheet has ever been GPS-surveyed.
3. **Cross-validation** — same principle as scale-determination: when 3+
   reference points are available, compare the position each one implies
   and surface disagreement rather than trusting one silently. With only
   one weak reference, say so explicitly rather than presenting a
   confident-looking location.

Output: the parcel's location description (GPS coordinates where available,
otherwise "X m from [landmark] bearing Y°"), attached to the parcel record
and included in reports where location was requested, not just area.

## Data quality / noise handling
Two distinct pieces, different risk profiles:

1. **Image preprocessing** (denoise/contrast enhancement on the scan before
   display/tracing). Standard, mechanical image processing — low risk,
   directly helps manual tracing precision on faint or degraded scans, and
   is also a prerequisite for decent results from the tracing-assist
   feature below. Worth doing early since it's a broadly useful, low-risk
   improvement.
2. **Point-data guard rails** — flag likely-accidental input (e.g. a new
   point placed implausibly close to the previous one, suggesting a
   double-click) at the moment of placement. This is deliberately a
   **warning, not automated correction**: the source project's own hard
   lessons (see below) already showed that automated "is this point a real
   corner or noise" judgment is unreliable — a simple flag the user can
   dismiss or act on is the right level of automation here, not a silent
   fix.

## Semi-automated tracing assist (boundary line-following)
User marks two points on a printed/drawn boundary line; the tool follows
that line automatically between them, rather than just connecting the two
points with a straight line. This is the same class of technique as
"magnetic lasso" / "intelligent scissors" in tools like Photoshop and
GIMP — a cost-minimizing path search over an edge/gradient map between the
two points — and should be built the same way: adopt the established
technique rather than inventing one.

**This carries the same caution flagged in the source project's hard
lessons**: automated path-following in dense, faint, or nearly-collinear
regions is genuinely unreliable, and a wrong auto-path is worse than a
slower manual one on a document that may feed into an official land
record. So: the followed path is always shown for confirmation before
being accepted into the polygon, never applied silently, and manual
point-by-point tracing remains available as the fallback for any segment
where the auto-follow gets it wrong. This is a harder feature — sequence
it after image preprocessing (above) and treat it as one of the later
milestones, alongside grid auto-detection.

## Is HTML needed? (answered)
No. For a true standalone desktop app, build the canvas natively
(`QGraphicsView` in PySide6 handles pan/zoom/click well, including on large
images). Port the *logic* from `point_picker.html` — the pan/zoom math, click
→ image-coordinate conversion, offset handling, point/label data model — into
native Python/Qt code. Don't wrap a webview around the HTML file; that adds a
dependency and a coordinate-mapping layer for no benefit once you're not
constrained to a browser context.

## Hard lessons from the source project (avoid repeating these)
- **Don't embed large images as base64** inside a single generated
  artifact/page — it silently failed at a few hundred KB in the earlier
  chat-based prototype. A real desktop app loading local files directly
  doesn't have this problem, but keep it in mind for any web-based companion
  view if one is ever added.
- **Multi-resolution/multi-source coordinate mapping is error-prone by
  hand.** If the same document is ever handled at two different render
  resolutions, a uniform scale factor works but must be derived and verified
  against known matching points, not assumed.
- **Automated corner/vertex detection in dense, nearly-collinear clusters is
  unreliable** — sampling straight-line "directness" between candidate points
  gives false positives when points are nearly collinear. If auto-detected
  vertices are ever offered (e.g. from a hand-traced overlay), a
  skeletonize + branch-point approach worked for finding vertex *locations*,
  but connecting them into a correct *path order* automatically did not work
  reliably. Treat vertex detection and path-ordering as separate problems;
  let the user confirm path order.
- **Prefer user-in-the-loop confirmation over silent automation** anywhere
  being wrong is costly — a wrong scale or a wrong boundary corner is worse
  than a slower, confirmed-correct workflow. This applies doubly here since
  outputs feed into official-looking land records.

## Tech stack (decided)
- PDF rendering: `PyMuPDF`
- DXF reading: `ezdxf`
- Image handling: `Pillow` / `OpenCV`
- Desktop GUI: **`PySide6` (Qt)** — native `QGraphicsView` for
  pan/zoom/click, good cross-platform packaging via `PyInstaller`. Chosen
  over a web-based UI for speed and simplicity of the initial build.
- **Storage: `sqlite3`** (Python stdlib — no extra dependency) for project
  metadata, parcel records, points/geometry, and field templates. See
  "Offline & storage constraints" above.
- PDF summary export: `PyMuPDF` (write) or `reportlab`
- **Dependency policy**: every library above must work fully offline with no
  license server / phone-home behavior — worth a quick check per library
  before adding it, not just assumed.

**Non-negotiable architectural rule given PySide6 was chosen over a
web-based UI:** because the mobile/tablet/field-use goal is still the longer
-term target (see Phase 2 below), `src/core/` and `src/io/` **must have zero
PySide6 or any UI-framework imports.** Every piece of real logic — scale
detection, geometry/unit conversion, template handling, file loaders — lives
in plain Python and is independently testable and callable with no GUI
running. `src/ui/` is a thin layer that only calls into `core`/`io` and
renders results; it never contains logic itself. This is what makes a
Phase 2 UI rewrite (mobile or otherwise) a UI-only rewrite rather than a
full-project rebuild — treat any logic that leaks into `ui/` as a defect to
fix immediately, not later.

## Phase 2 (not in scope now, but the reason for the rule above)
- Mobile/tablet field-use UI, built separately once the core is proven —
  likely reusing `src/core`/`src/io` behind either a rewritten native mobile
  UI or a browser-based UI (the existing `point_picker.html` already uses
  pointer events and `touch-action: none`, so that approach remains
  available as a reference if a web-based mobile UI is chosen later)
- GPS-based real-point capture during survey — the point data model should
  carry **optional `lat`/`lon` fields** alongside pixel/local coordinates
  from day one (unused for now, populated by nothing today), so this doesn't
  require a breaking schema change when it lands

Keep scale-detection logic, geometry/unit-conversion logic, and the
point-marking UI as separate, independently testable modules — the source
project's most reliable moments were small, single-purpose components
(grid detection, marker extraction, coordinate transform) verified on
their own.

## Suggested folder structure
```
land-measure-tool/                # the application code itself
├── src/
│   ├── main.py
│   ├── ui/                      # PySide6 only — no logic lives here
│   │   ├── main_window.py
│   │   ├── canvas_view.py       # pan/zoom + point marking (ported from point_picker.html logic)
│   │   ├── scale_dialog.py
│   │   └── record_form.py       # land-identification field form, template-driven
│   ├── io/                      # pure Python, no UI imports
│   │   ├── pdf_loader.py
│   │   ├── dxf_loader.py
│   │   └── image_loader.py
│   ├── core/                    # pure Python, no UI imports — reusable in Phase 2
│   │   ├── scale.py             # 4-method scale detection & cross-validation
│   │   ├── geometry.py          # segment/perimeter/area calc
│   │   ├── units.py             # SI-canonical storage + user-defined local unit profiles
│   │   ├── polygon.py           # point/path/polygon data model (incl. optional lat/lon)
│   │   ├── templates.py         # land-record field templates, load/save/apply
│   │   └── project_db.py        # SQLite schema + read/write for one project folder
│   └── export/
│       └── report.py            # PDF/CSV/JSON summary generation
├── config/
│   ├── templates/                # rural-agri.json, rural-residential.json, urban.json
│   └── unit_profiles/            # default/global unit conversion profiles
├── tests/
├── reference/                   # prior prototypes, kept for logic reference only
│   ├── point_picker.html
│   └── extract_marked_points.py
├── requirements.txt
├── PROJECT_BRIEF.md
└── README.md

# a USER'S PROJECT (created/opened by the app, portable to a pen drive)
some-parcel-project/
├── project.db                    # single SQLite file: parcels, points, fields, unit profiles used
├── sources/                      # original PDFs/images/DXFs, referenced by relative path from project.db
└── exports/                      # generated PDF/CSV/JSON summaries
```

## Development workflow
- **Claude Code**: implements each milestone in the VS Code project, module
  by module, following this brief.
- **Claude (chat/review)**: reviews diffs/architecture against this brief —
  particularly the scale cross-validation logic and unit conversion, where
  correctness matters most — and flags scope creep or deviation.
- **You**: verifies against real survey sheets at each milestone, especially
  scale accuracy (compare tool output against a known/manually-checked
  distance or area before trusting a new document type).

## Suggested milestones
1. ✅ Project folder structure + SQLite schema — done
2. ✅ Open a PDF and an image file, render to screen, pan/zoom — done
3. ✅ Manual two-point scale entry — done
4. ✅ Polygon point-marking with live segment/perimeter/area readout, saved
   to the project DB, incl. fine-tune/nudge, reliable cancel, precision
   crosshair, and the explicit `closed` state — done
5. **Multi-parcel per source** — several khasra boundaries traced and
   measured independently on one loaded sheet, each its own parcel record;
   owner as a linking field across a source's parcels
6. Unit profiles: sq m / sq ft / acre / hectare built in, plus user-defined
   local unit profiles (save/select, e.g. a "Bigha — Jaipur" profile)
7. Land-type templates (rural-agri / rural-residential / urban) + editable
   identification-fields form, tied to a traced parcel, incl. owner field
8. Owner-wise summary report (all parcels for an owner + total), PDF/CSV/
   JSON export
9. Segment-length / boundary-description report — separate report type,
   user-selectable segments
10. Visual confidence overlay — traced boundary shown over the source scan,
    toggleable per parcel
11. PDF metadata-based scale auto-detection, shown alongside manual entry
    for comparison
12. DXF support (open file, read header units/scale, render entities)
13. Location-fixing (local georeferencing): mark on-sheet reference points
    (tubewell, building, other durable structures), GPS-anchored mode and
    distance/trigonometry mode, cross-validated when 3+ references are used
14. Image preprocessing (denoise/contrast) for scans — helps manual tracing
    precision now, and is a prerequisite for milestone 16
15. Point-data guard rails — flag (don't auto-correct) likely-accidental
    duplicate/near points at placement time
16. Semi-automated tracing assist — follow a printed/drawn boundary line
    between two user-marked points, always shown for confirmation before
    being accepted, manual tracing remains the fallback
17. Grid/reference-content auto-detection (hardest, do last)

## Attached reference files
- `point_picker.html` — working browser-based point-marking prototype
  (logic reference only — see "Is HTML needed?")
- `extract_marked_points.py` — working CLI for extracting marked-circle
  coordinates from an annotated image
- `README.md` — original prototype notes, including extension ideas
