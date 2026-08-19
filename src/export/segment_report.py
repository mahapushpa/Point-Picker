"""segment_report — segment-length / boundary-description report (Milestone 12).

A DIFFERENT shape from the owner-wise summary (report.py): **one row per
selected boundary edge**, not one row per parcel. For a single parcel the user
picks a contiguous run of its boundary edges on the canvas; each becomes a row
with the two vertex labels, the edge length (real units), a compass bearing, and
a free-text neighbouring-feature name — producing the traditional boundary
description, e.g. "North: bounded by [Ramesh's field], 45 m".

Pure Python, no UI-framework imports (export layer; may use core/io, both
UI-free). Reuses report.py's ``SourceRef`` and never-overwrite ``unique_export_path``.

Rules carried over:
  * **Scale-first** — a boundary description needs real lengths, so a parcel
    whose source has no scale cannot be reported; :func:`build_segment_report`
    refuses rather than fabricating or silently dropping the length.
  * **Units** — lengths are LINEAR, so M9's *areal* local profiles (Bigha, ...)
    do not apply; per M9's rule lengths report in SI (metres) plus the built-in
    linear unit (feet). Never raw pixels.
  * Bearing is plain geometry (geometry.bearing_deg, North-up/clockwise), not
    M16's georeferenced bearing.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass

from ..core.geometry import segment_length, bearing_deg, compass_label
from ..core import units
from .report import SourceRef, unique_export_path, _owner_slug as _slug, _round_floats

REPORT_KIND = "boundary-description"

_FOOT = units.BUILTIN_LENGTH_UNITS["foot"]

#: Honesty note carried by every boundary-description report: the chosen bearing
#: convention (screen-up = North) is a reasonable default for a north-up scan, but
#: an ASSUMPTION, not a verified fact — in the same spirit as the Scale-first rule
#: (don't imply precision that doesn't exist). Geodetically-verified bearings come
#: with Location-fixing (M16). ASCII-only so it renders in the PDF's base font.
BEARING_CAVEAT = ("Bearings are relative to the sheet as loaded (assumed "
                  "north-up); geodetically-verified bearing will be available "
                  "via Location-fixing (M16).")


class SegmentReportError(Exception):
    """Raised when a boundary-description report cannot be produced (no scale,
    too few points, or an empty segment selection)."""


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EdgeInfo:
    """One candidate boundary edge of a parcel (all edges, before selection).

    This is exactly what the live side-table shows; the report is built from the
    subset the user selects — there is no separate "UI vs report" data model."""
    edge_index: int                 # stable index into the parcel's edge list
    vertex_a_label: str             # per-parcel ordinal, e.g. "V3"
    vertex_b_label: str
    vertex_a_id: int
    vertex_b_id: int
    length_m: float | None          # None only if the source has no scale
    length_ft: float | None
    bearing_deg: float
    compass: str


@dataclass(frozen=True)
class SegmentRow:
    """A selected edge as it appears in the report (with its neighbour name)."""
    seq: int                        # 1-based position within the report
    edge_index: int
    vertex_a_label: str
    vertex_b_label: str
    length_m: float
    length_ft: float
    bearing_deg: float
    compass: str
    neighbour: str                  # free text; may be blank


@dataclass(frozen=True)
class SegmentReport:
    kind: str
    project_name: str
    generated_at: str
    parcel_id: int
    parcel_label: str
    owner: str | None
    scale_method: str | None
    scale_note: str | None
    source: SourceRef
    rows: list
    segment_count: int
    total_length_m: float
    total_length_ft: float
    bearing_note: str               # honesty caveat about the bearing convention


# ---------------------------------------------------------------------------
# Edge enumeration (the side-table's contents)
# ---------------------------------------------------------------------------

def _edge_endpoints(points, closed):
    """Yield (edge_index, i, j) for each boundary edge: consecutive point index
    pairs, plus the closing edge (last->first) when the polygon is closed."""
    n = len(points)
    for i in range(n - 1):
        yield i, i, i + 1
    if closed and n >= 3:
        yield n - 1, n - 1, 0


def list_parcel_edges(project, parcel_id):
    """Return ``(edges, context)`` for a parcel: every boundary edge as an
    :class:`EdgeInfo`, plus a context dict (parcel label/owner, scale info, source
    reference). Lengths are ``None`` when the source has no scale — the caller
    must enforce Scale-first before offering generation."""
    parcel = project.get_parcel(parcel_id)
    if parcel is None:
        raise SegmentReportError(f"No parcel with id {parcel_id}.")
    points = project.get_parcel_points(parcel_id)   # ordered, with vertex ids
    if len(points) < 2:
        raise SegmentReportError("Parcel has no traced boundary to describe.")
    closed = project.get_parcel_closed(parcel_id)

    scale = project.get_source_scale(parcel["source_id"])
    mpp = scale["metres_per_pixel"] if scale else None

    edges = []
    for edge_index, i, j in _edge_endpoints(points, closed):
        a, b = points[i], points[j]
        length_px = segment_length((a["pixel_x"], a["pixel_y"]),
                                   (b["pixel_x"], b["pixel_y"]))
        length_m = length_px * mpp if mpp is not None else None
        length_ft = units.length_in_unit(length_m, _FOOT) if length_m is not None else None
        brg = bearing_deg((a["pixel_x"], a["pixel_y"]), (b["pixel_x"], b["pixel_y"]))
        edges.append(EdgeInfo(
            edge_index=edge_index,
            vertex_a_label=f"V{i + 1}", vertex_b_label=f"V{j + 1}",
            vertex_a_id=a["vertex_id"], vertex_b_id=b["vertex_id"],
            length_m=length_m, length_ft=length_ft,
            bearing_deg=brg, compass=compass_label(brg),
        ))

    source = project.get_source(parcel["source_id"]) or {}
    context = {
        "parcel_id": parcel_id,
        "parcel_label": parcel.get("name") or f"Parcel {parcel_id}",
        "owner": parcel.get("owner"),
        "closed": closed,
        "has_scale": mpp is not None,
        "metres_per_pixel": mpp,
        "scale_method": scale["method"] if scale else None,
        "scale_note": scale.get("note") if scale else None,
        "source": SourceRef(
            source_id=parcel["source_id"],
            relative_path=source.get("relative_path"),
            original_name=source.get("original_name"),
            page=source.get("page"),
            doc_date=source.get("doc_date"),
        ),
    }
    return edges, context


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_segment_report(project, parcel_id, edge_indices, neighbours=None, *,
                         project_name=None) -> SegmentReport:
    """Build the boundary-description report for *parcel_id* over the ordered,
    user-selected *edge_indices* (a contiguous run). *neighbours* maps an edge
    index to its neighbouring-feature name (missing -> blank).

    Raises :class:`SegmentReportError` on no scale (Scale-first), an unknown edge
    index, or an empty selection."""
    neighbours = neighbours or {}
    edges, ctx = list_parcel_edges(project, parcel_id)
    if not ctx["has_scale"]:
        raise SegmentReportError(
            "This parcel's source has no scale set — set a scale before generating "
            "a boundary-description report (lengths would otherwise be meaningless).")
    if not edge_indices:
        raise SegmentReportError("No segments selected for the report.")

    by_index = {e.edge_index: e for e in edges}
    rows = []
    total_m = 0.0
    for seq, idx in enumerate(edge_indices, 1):
        edge = by_index.get(idx)
        if edge is None:
            raise SegmentReportError(f"Edge index {idx} is not part of this parcel.")
        total_m += edge.length_m
        rows.append(SegmentRow(
            seq=seq, edge_index=idx,
            vertex_a_label=edge.vertex_a_label, vertex_b_label=edge.vertex_b_label,
            length_m=edge.length_m, length_ft=edge.length_ft,
            bearing_deg=edge.bearing_deg, compass=edge.compass,
            neighbour=str(neighbours.get(idx, "") or "").strip(),
        ))

    if project_name is None:
        try:
            project_name = project.get_meta("project_name") or "project"
        except Exception:  # pragma: no cover
            project_name = "project"

    from datetime import datetime, timezone
    return SegmentReport(
        kind=REPORT_KIND,
        project_name=project_name,
        generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        parcel_id=parcel_id,
        parcel_label=ctx["parcel_label"],
        owner=ctx["owner"],
        scale_method=ctx["scale_method"],
        scale_note=ctx["scale_note"],
        source=ctx["source"],
        rows=rows,
        segment_count=len(rows),
        total_length_m=total_m,
        total_length_ft=units.length_in_unit(total_m, _FOOT),
        bearing_note=BEARING_CAVEAT,
    )


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def _description_line(row: SegmentRow) -> str:
    """The traditional one-line description for an edge. Uses an ASCII placeholder
    for an un-named neighbour (the PDF's base font can't encode an em dash)."""
    neighbour = row.neighbour or "(not specified)"
    return f"{row.compass}: bounded by {neighbour}, {row.length_m:.1f} m"


def write_json(report: SegmentReport, path) -> None:
    data = asdict(report)
    for row_dict, row in zip(data["rows"], report.rows):
        row_dict["description"] = _description_line(row)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(_round_floats(data), fh, indent=2, ensure_ascii=False)


def write_csv(report: SegmentReport, path) -> None:
    header = ["Seq", "From", "To", "Length (m)", "Length (ft)", "Bearing (deg)",
              "Direction", "Neighbour", "Description"]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for row in report.rows:
            w.writerow([
                row.seq, row.vertex_a_label, row.vertex_b_label,
                f"{row.length_m:.2f}", f"{row.length_ft:.2f}",
                f"{row.bearing_deg:.1f}", row.compass, row.neighbour,
                _description_line(row),
            ])
        w.writerow(["", "", "TOTAL", f"{report.total_length_m:.2f}",
                    f"{report.total_length_ft:.2f}", "", "", "", ""])
        # Honesty caveat about the bearing convention (a trailing metadata row).
        w.writerow([])
        w.writerow(["Bearing note", report.bearing_note])


def write_pdf(report: SegmentReport, path) -> None:
    from .report import _import_fitz, _PdfWriter
    fitz = _import_fitz()
    pdf = _PdfWriter(fitz)

    pdf.line("Boundary Description Report", size=16, bold=True)
    pdf.line(f"Parcel: {report.parcel_label}"
             + (f"   Owner: {report.owner}" if report.owner else ""), size=11, bold=True)
    pdf.line(f"Project: {report.project_name}", size=10)
    pdf.line(f"Generated: {report.generated_at}", size=9, color=(0.3, 0.3, 0.3))
    if report.scale_method:
        note = f" - {report.scale_note}" if report.scale_note else ""
        pdf.line(f"Scale: {report.scale_method}{note}", size=9, color=(0.3, 0.3, 0.3))
    pdf.line(f"Source: {report.source.describe()}", size=9, color=(0.3, 0.3, 0.3))
    pdf.rule()

    # Primary output: the traditional boundary description, one line per edge.
    for row in report.rows:
        pdf.line(f"{row.seq}. {_description_line(row)}", size=11, indent=6, gap=3.0)
        pdf.line(f"[{row.vertex_a_label}->{row.vertex_b_label}, bearing "
                 f"{row.bearing_deg:.1f} deg, {row.length_ft:.1f} ft]",
                 size=8, indent=16, gap=3.0, color=(0.4, 0.4, 0.4))
    pdf.rule()
    pdf.line(f"Total length ({report.segment_count} segment(s)): "
             f"{report.total_length_m:.2f} m ({report.total_length_ft:.2f} ft)",
             size=11, bold=True)
    pdf.space(4.0)
    pdf.line(f"Note: {report.bearing_note}", size=8, color=(0.4, 0.4, 0.4))
    pdf.save(path)


# ---------------------------------------------------------------------------
# Dispatch — one report for one parcel, per requested format.
# ---------------------------------------------------------------------------

_WRITERS = {"pdf": write_pdf, "csv": write_csv, "json": write_json}


def export_segment_report(project, parcel_id, exports_dir, *, edge_indices,
                          neighbours=None, formats=("pdf",), timestamp=None,
                          project_name=None):
    """Build and write the boundary-description report for one parcel to
    *exports_dir*, one file per requested format. Returns ``(report, paths)``."""
    from pathlib import Path as _Path
    from datetime import datetime
    exports_dir = _Path(exports_dir)
    formats = list(formats)
    unknown = [f for f in formats if f not in _WRITERS]
    if unknown:
        raise ValueError(f"Unsupported report format(s) {unknown}; "
                         f"expected any of {sorted(_WRITERS)}.")

    report = build_segment_report(project, parcel_id, edge_indices, neighbours,
                                  project_name=project_name)
    when = timestamp or datetime.now()
    stem = f"boundary-description_{_slug(report.parcel_label)}"
    paths = []
    for fmt in formats:
        out = unique_export_path(exports_dir, stem, fmt, timestamp=when)
        _WRITERS[fmt](report, out)
        paths.append(out)
    return report, paths
