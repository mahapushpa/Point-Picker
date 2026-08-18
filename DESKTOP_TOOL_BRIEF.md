# Project brief: Desktop tool for scaled measurement from PDF / CAD / image files

## Origin
This grows out of a long manual workflow: digitizing a land parcel boundary from
scanned government survey sheets (PDF and JPEG), establishing real-world scale
from clues in the document (grid spacing, page physical size, handwritten scale
notes), marking corner points by hand, and computing area/perimeter. That whole
process — done manually turn-by-turn in a chat — is the blueprint for what this
tool should do natively.

A prototype of the "mark points on an image, get pixel coordinates" piece
already exists and works (see attached `point_picker.html` /
`extract_marked_points.py`). This project generalizes that into a proper
desktop application covering more file types and adding real scale/measurement.

## Goal
A **desktop tool** that can:
1. **Open a file** — PDF, one popular CAD format, or a raster image (PNG/JPEG)
2. **Establish real-world scale** for that file — asking the user for input where
   needed, and reading it automatically from the file where possible
3. **Let the user mark points** on the rendered document and get back both pixel
   coordinates and real-world coordinates
4. **Compute measurements** — distances, perimeters, areas — in real units

## File format scope
- **PDF** — render pages to raster for display; also read the PDF's own physical
  page size (points → inches → mm), which is a strong, free scale clue when the
  page was generated at true physical scale (this worked well in the source
  project — cross-validated against a grid drawn on the page and a handwritten
  scale note, all agreed within ~1%).
- **One CAD format** — recommend starting with **DXF**, not DWG. DXF is an open,
  documented, text-based format with mature open libraries (e.g. `ezdxf` in
  Python) and no licensing hurdles. DWG requires either a proprietary SDK or
  lossy conversion. If DWG support becomes a hard requirement later, that's a
  separate, bigger scope decision — flag it explicitly rather than assume it.
  CAD files often already carry real-world units/scale in their header data —
  that should be read directly rather than re-derived visually.
- **Images (PNG/JPEG)** — no reliable embedded scale in general (occasionally
  DPI metadata, but it's frequently wrong or missing for scans). This is the
  case that most needs the "ask the user" flow.

## Scale-determination workflow (the core hard problem)
Lean on multiple methods and cross-validate when more than one is available,
same approach used successfully in the source project:

1. **From file metadata** — PDF physical page size, DXF units/header, image DPI
   if present and plausible.
2. **From visible reference content** — if the document has a printed scale bar,
   a ruled reference grid, or a written scale note (e.g. "1 cm = 40 m"), detect
   or let the user confirm it. Grid detection worked well via peak-detection on
   pixel-darkness sums across rows/columns to find ruled line spacing.
3. **From user-supplied reference** — let the user mark two points a known
   real-world distance apart (or trace a known-area shape) and derive scale
   from that. This is the universal fallback when nothing else is available.
4. **Cross-check, don't just trust one source** — when two+ methods are
   available, compare them and surface disagreement to the user rather than
   silently picking one. In the source project, three independent methods
   agreeing within ~1% is what made the calibration trustworthy.
5. **Be honest about uncertainty** — if only one weak method is available, say
   so rather than presenting a confident-looking number.

## Point-marking / digitizing
Build on the existing `point_picker.html` concept:
- Pan/zoom a rendered page or image
- Click to mark points, with labels
- Once scale is established, show live real-world coordinates and running
  distance/area as points are placed
- Support marking a **polygon/path**, not just isolated points (connect points
  in sequence to define a boundary) — this was a manual, error-prone, and
  requested-but-never-fully-built feature in the source project; worth
  designing in from the start here
- Export both pixel and real-world coordinates (JSON/CSV/GeoJSON-ish)

## Hard lessons from the source project (avoid repeating these)
- **Don't embed large images as base64 inside a single generated artifact/page**
  — it silently failed at a few hundred KB in one environment. A real desktop
  app loading local files directly doesn't have this problem, but keep it in
  mind for any web-based companion view.
- **Multi-resolution/multi-source coordinate mapping is error-prone by hand.**
  When the same document exists at two different render resolutions (e.g. a
  288 DPI render vs. a 300 DPI export), a simple uniform scale factor works —
  but it must be derived and verified against known matching points, not
  assumed.
- **Automated corner/vertex detection in dense, nearly-collinear clusters is
  unreliable** — sampling straight-line "directness" between candidate points
  gives false positives when points are nearly collinear. If offering
  auto-detected vertices (e.g. from a user's hand-traced overlay), a
  skeletonize + branch-point approach worked well for finding *vertex
  locations*, but connecting them into a correct *path order* automatically
  did not work reliably — treat vertex detection and path-ordering as separate
  problems, and let the user confirm path order.
- **Prefer user-in-the-loop confirmation over silent automation** for anything
  where being wrong is costly (a wrong scale or a wrong boundary corner is much
  worse than a slower, confirmed-correct workflow).

## Suggested tech directions (starting point, not a fixed decision)
- Python-based is a reasonable default given the working prototypes so far:
  - PDF rendering: `PyMuPDF` (already used successfully in the source project)
  - DXF reading: `ezdxf`
  - Image handling: `Pillow` / `OpenCV`
  - Desktop GUI: `PySide6` (Qt) is a solid choice for a real cross-platform
    desktop app with good canvas/graphics-view support for pan/zoom/click; an
    Electron + web-tech UI is the alternative if reusing the existing HTML
    point-picker code directly is preferred over rewriting the UI in Qt.
- Keep the scale-detection logic and the point-marking UI as separate,
  testable modules — the source project's most reliable moments were when
  small, single-purpose scripts did one thing (grid detection, marker
  extraction, coordinate transform) and were verified independently.

## Suggested first milestones
1. Open a PDF and an image file, render to screen, pan/zoom
2. Manual scale entry (two-point known-distance method) — get end-to-end
   measurement working with the simplest possible calibration first
3. Point marking with live distance/area readout
4. PDF metadata-based scale auto-detection, shown alongside manual entry for
   comparison
5. DXF support
6. Grid/reference-content auto-detection (harder, do last)

## Attached reference files
- `point_picker.html` — working browser-based point-marking prototype
- `extract_marked_points.py` — working CLI for extracting marked-circle
  coordinates from an annotated image
- `README.md` — notes from that prototype, including extension ideas
