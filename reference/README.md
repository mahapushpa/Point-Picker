# Point Picker

Two small tools for one job: **getting precise pixel coordinates out of points marked on an image.** Built out of a real need — digitizing corners on a scanned map — but generically useful for anything similar (calibration points, annotation datasets, digitizing plans/diagrams, etc).

## The two workflows

### 1. `point_picker.html` — interactive, click-to-mark
Open the file directly in any browser (double-click it, no server or install needed). Load an image from your computer, click points on it, label them, export as JSON/CSV. Pan by dragging, zoom with the scroll wheel.

Everything runs client-side. The image is never uploaded anywhere — it's loaded via the browser's local file API. This matters: an earlier version of this tool tried to *embed* the image as base64 inside the page itself, which hit a size limit and silently failed. Loading the image locally at runtime instead of embedding it sidesteps that entirely, and scales to images of any size.

**Why this version exists:** in the original back-and-forth this was developed in, an AI assistant tried to build this exact tool as an in-chat interactive widget and it failed to render. Rebuilding it as a plain standalone HTML file fixes the root cause and makes it independently useful.

### 2. `extract_marked_points.py` — annotate-then-extract
For workflows where you can't run a live interactive tool (e.g. handing an image back and forth with an AI assistant that can only see static uploads): mark points by hand in any image editor (Paint, Preview, GIMP...) using small colored circles, then run this script to extract their exact centers.

```bash
pip install opencv-python numpy
python3 extract_marked_points.py my_marked_image.png --color red --offset 1900 1350
```

The `--offset` flag matters when your marked image is a *crop* of a larger original — the script adds the offset back in, so the output coordinates map directly onto the full-size image.

This was the more reliable of the two approaches in practice: a person marks a point exactly where they want it (using their own judgment about what's a "real" corner vs. noise), and extraction is purely mechanical and precise. The interactive tool is faster when it works, but this fallback needs nothing except image-editing software everyone already has.

## Data format
Both tools use the same simple JSON shape:
```json
[
  { "label": "1", "x": 2755.5, "y": 1665.0 },
  { "label": "T1", "x": 2752.5, "y": 1458.0 }
]
```

## Ideas for extending this (good starting points for Claude Code)
- **Snap-to-line**: given the source image, offer to snap a click to the nearest strong edge/line within a few pixels — useful when marking corners on line drawings.
- **Multi-image sessions**: load several crops of the same larger image, keep one running point list across all of them (this project needed exactly that — the map didn't fit in one reasonable-resolution view).
- **Connect-the-dots mode**: after marking points, click pairs (or drag between them) to define which points are connected, building up a polygon/path, not just a point set. Export as GeoJSON-style line/polygon in addition to bare points.
- **Undo/redo history** beyond just "undo last" — a full stack.
- **Color-coded categories**: right now the HTML tool has one marker style; the Python script supports red/blue/green presets. Worth unifying — e.g. a category dropdown in the HTML tool that changes marker color and feeds into the same category logic the Python script uses.
- **Keyboard shortcuts**: arrow keys to nudge the last-placed point by 1px for fine correction, number keys to quick-set the label.
- **Auto-detect circles for the Python script**: currently it expects the user to specify a color; could add a "detect all plausible marker colors" mode.
- **Scale calibration built in**: let the user click two points a known real-world distance apart, enter that distance, and have the tool auto-report all subsequent coordinates in real units alongside pixels.

## Files
- `point_picker.html` — the interactive tool, single file, zero dependencies
- `extract_marked_points.py` — the CLI extraction script
- `README.md` — this file
