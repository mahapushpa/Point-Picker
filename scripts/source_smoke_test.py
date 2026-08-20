#!/usr/bin/env python
"""source_smoke_test.py — run real source documents through the app's loaders
and scale/precision diagnostics, reporting what each one produces.

This is a **generic** diagnostic harness: it takes file paths as command-line
arguments and never hardcodes any path or filename. Point it at whatever
PDF / JPG / PNG / DXF files you want to sanity-check (e.g. real survey sheets
kept locally under a gitignored folder):

    python scripts/source_smoke_test.py path/to/a.pdf path/to/b.jpg ...

For each file it reports: detected type, load time, raster dimensions, any
warnings/flags the loader raises (DXF skipped entity types / curved segments /
precision; PDF page count and physical-size metadata scale; image DPI), and —
crucially — any exception as a full traceback rather than letting the process
crash, so one bad file never hides the results for the rest.

Nothing here is specific to any document; it is safe to commit.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

# Make the `src` package importable when run as `python scripts/source_smoke_test.py`
# from the repository root (the repo root, not scripts/, must be on sys.path).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.io.raster import _DXF_EXTS, _IMAGE_EXTS, _PDF_EXTS  # noqa: E402


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def _diagnose_pdf(path: Path, lines: list[str]) -> None:
    from src.core.scale import metadata_scale_from_page
    from src.io.pdf_loader import load_pdf_page, page_count, read_page_size_points

    n_pages = page_count(path)
    lines.append(f"  pages           : {n_pages}"
                 + ("   (multi-page — app renders page 1 only unless a page is chosen)"
                    if n_pages > 1 else ""))

    t0 = time.perf_counter()
    raster = load_pdf_page(path)          # page 0 at the app's default DPI
    dt = time.perf_counter() - t0
    lines.append(f"  render (page 1) : {raster.width} x {raster.height} px   "
                 f"in {dt * 1000:.0f} ms")

    # Physical-size metadata scale (M14) — plausible or not.
    try:
        w_pt, h_pt = read_page_size_points(path)
        lines.append(f"  page size       : {w_pt:.1f} x {h_pt:.1f} pt "
                     f"({w_pt / 72:.2f} x {h_pt / 72:.2f} in)")
        try:
            ms = metadata_scale_from_page(w_pt, h_pt, raster.width, raster.height)
            lines.append(f"  metadata scale  : {ms.metres_per_pixel:.5f} m/px "
                         f"(page maps to {ms.width_m:.3f} x {ms.height_m:.3f} m if 1:1)")
        except ValueError as exc:
            lines.append(f"  metadata scale  : not usable — {exc}")
    except Exception as exc:  # noqa: BLE001 — report, don't crash
        lines.append(f"  page size       : unavailable — {exc}")

    # Text layer presence (a cheap yes/no; digital vs scanned).
    try:
        _report_pdf_text_layer(path, lines)
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  text layer      : could not check — {exc}")


def _report_pdf_text_layer(path: Path, lines: list[str]) -> None:
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz  # type: ignore
    with fitz.open(str(path)) as doc:
        text = doc.load_page(0).get_text("text") or ""
    stripped = text.strip()
    if stripped:
        lines.append(f"  text layer      : PRESENT ({len(stripped)} chars on page 1 — "
                     "digitally generated, extractable)")
    else:
        lines.append("  text layer      : none on page 1 (looks like a scan / raster-only)")


def _diagnose_image(path: Path, lines: list[str]) -> None:
    from PIL import Image

    from src.io.image_loader import load_image

    t0 = time.perf_counter()
    raster = load_image(path)
    dt = time.perf_counter() - t0
    lines.append(f"  decode          : {raster.width} x {raster.height} px   "
                 f"in {dt * 1000:.0f} ms")

    with Image.open(path) as img:
        fmt = img.format
        mode = img.mode
        dpi = img.info.get("dpi")
    lines.append(f"  format / mode   : {fmt} / {mode}")
    if dpi and all(d > 0 for d in dpi):
        lines.append(f"  DPI metadata    : {dpi[0]:g} x {dpi[1]:g} "
                     "(present — often wrong on scans; treat as a weak clue)")
    else:
        lines.append("  DPI metadata    : none/zero (typical for scans — set scale manually)")


def _diagnose_dxf(path: Path, lines: list[str]) -> None:
    from src.io.dxf_loader import render_dxf

    t0 = time.perf_counter()
    drawing = render_dxf(path)
    dt = time.perf_counter() - t0
    r = drawing.raster
    lines.append(f"  render          : {r.width} x {r.height} px   in {dt * 1000:.0f} ms")
    lines.append(f"  entities drawn  : {drawing.entity_count}   "
                 f"($INSUNITS={drawing.insunits})")
    if drawing.metres_per_pixel is not None:
        lines.append(f"  render scale    : {drawing.metres_per_pixel:.4f} m/px "
                     f"(target {drawing.precision_target_m:g} m/px)")
    if drawing.skipped_entity_types:
        lines.append("  FLAG            : entity types present but not drawn: "
                     + ", ".join(drawing.skipped_entity_types))
    if getattr(drawing, "has_curved_segments", False):
        lines.append("  FLAG            : curved polyline segments drawn as straight "
                     "(bulge dropped) — trace with care")
    if drawing.precision_limited:
        lines.append(f"  FLAG            : precision limited — render coarser than "
                     f"{drawing.precision_target_m:g} m/px target (large drawing)")


def diagnose(path: Path, index: int) -> str:
    ext = path.suffix.lower()
    if ext in _PDF_EXTS:
        kind = "PDF"
    elif ext in _DXF_EXTS:
        kind = "DXF"
    elif ext in _IMAGE_EXTS:
        kind = "image"
    else:
        kind = f"unknown ({ext or 'no extension'})"

    # Generic header — deliberately does NOT print the filename, so pasted
    # output never leaks a real document name.
    lines = [f"file {index} ({kind})"]
    if not path.is_file():
        lines.append("  ERROR           : not a file / does not exist")
        return "\n".join(lines)
    lines.append(f"  size on disk    : {_fmt_bytes(path.stat().st_size)}")

    try:
        if ext in _PDF_EXTS:
            _diagnose_pdf(path, lines)
        elif ext in _DXF_EXTS:
            _diagnose_dxf(path, lines)
        elif ext in _IMAGE_EXTS:
            _diagnose_image(path, lines)
        else:
            lines.append("  skipped         : unsupported extension for any loader")
    except Exception:  # noqa: BLE001 — the whole point: report, never crash the run
        lines.append("  EXCEPTION (full traceback below):")
        lines.append(traceback.format_exc().rstrip())
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Run source documents through the app's loaders + scale/precision "
                    "diagnostics. Takes file paths as arguments; hardcodes nothing.")
    ap.add_argument("paths", nargs="+", help="files to diagnose (PDF / JPG / PNG / DXF)")
    args = ap.parse_args(argv)

    print(f"source_smoke_test — {len(args.paths)} file(s)\n" + "=" * 60)
    for i, raw in enumerate(args.paths, start=1):
        print(diagnose(Path(raw), i))
        print("-" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
