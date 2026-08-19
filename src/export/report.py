"""report — owner-wise summary report (Milestone 11).

Pure Python. **No PySide6 or any UI-framework imports** (see PROJECT_BRIEF.md's
architecture rule): this is real logic and must stay callable with no GUI. It
reads a project through :class:`src.core.project_db.ProjectDB`'s public methods
and produces the first of the brief's three report types — the **owner-wise
summary**: every parcel grouped by owner, its identification fields plus area /
perimeter, and a per-owner **total** area, with a grand total across all owners.

Three export paths share one built model (:func:`build_owner_report`):
  * **PDF**  — primary, shareable (uses PyMuPDF, imported lazily so CSV/JSON
    export and the model itself never depend on it);
  * **CSV / JSON** — record-keeping / import elsewhere.

Correctness rules taken straight from the brief:
  * **Scale-first** — no real-world number is produced for a parcel whose source
    has no scale set. Such parcels still appear (honesty over silence), marked
    "(no scale)", and are excluded from area totals rather than counted as zero.
  * **SI is canonical** — every area is stored/summed in square metres; hectare
    and acre (fixed, universally-correct built-ins) are always shown as derived
    units. A source's selected local profile (e.g. a Bigha) is shown *alongside*
    SI per parcel, never instead of it, and a group's local total is only given
    when every scaled parcel in the group shares one local unit (otherwise it
    would be ambiguous, so it is omitted).
  * Each report carries, per parcel, the **scale-determination method** and its
    confidence / cross-check **note**, and the **source-file reference**.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from ..core.geometry import measure_polygon
from ..core import units

#: Label used for parcels with no owner set (grouped together, shown last).
NO_OWNER_LABEL = "(no owner)"

#: Fixed derived-area factors (square metres per unit) — universally correct.
_HECTARE = units.BUILTIN_AREA_UNITS["hectare"]
_ACRE = units.BUILTIN_AREA_UNITS["acre"]

REPORT_KIND = "owner-wise-summary"


# ---------------------------------------------------------------------------
# Data model (pure, serialisable)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceRef:
    """The always-present source-document reference for a parcel."""
    source_id: int | None
    relative_path: str | None
    original_name: str | None
    page: int | None
    doc_date: str | None

    def describe(self) -> str:
        name = self.original_name or self.relative_path or "unknown source"
        parts = [name]
        if self.page is not None:
            parts.append(f"p.{self.page}")
        if self.doc_date:
            parts.append(f"({self.doc_date})")
        return " ".join(parts)


@dataclass(frozen=True)
class ParcelRow:
    """One parcel in the report. Real-world figures are ``None`` when the
    parcel's source has no scale set, or the boundary is too incomplete to
    measure (area needs 3+ points, perimeter 2+)."""
    parcel_id: int
    label: str                     # name if set, else "Parcel <id>"
    owner: str | None              # normalised: empty/whitespace -> None
    fields: list                   # identification [(label, value), ...]
    notes: str | None
    point_count: int
    closed: bool
    has_scale: bool
    metres_per_pixel: float | None
    scale_method: str | None
    scale_note: str | None
    perimeter_m: float | None
    area_sq_m: float | None
    area_hectare: float | None
    area_acre: float | None
    local_unit: str | None
    local_area: float | None
    source: SourceRef


@dataclass(frozen=True)
class OwnerGroup:
    """All parcels for one owner, with their combined (SI) area total."""
    owner: str | None
    display_owner: str
    parcels: list
    parcel_count: int
    scaled_count: int              # parcels with a usable area
    missing_scale_count: int       # parcels excluded from the total (no scale)
    total_area_sq_m: float | None
    total_area_hectare: float | None
    total_area_acre: float | None
    total_local_unit: str | None
    total_local_area: float | None


@dataclass(frozen=True)
class OwnerReport:
    """The whole owner-wise summary."""
    kind: str
    project_name: str
    generated_at: str
    parcel_count: int
    owner_count: int
    missing_scale_count: int
    scale_methods: list
    grand_total_area_sq_m: float | None
    grand_total_area_hectare: float | None
    grand_total_area_acre: float | None
    groups: list


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def _norm_owner(owner):
    """Empty / whitespace-only owner -> None (so it groups under NO_OWNER)."""
    if owner is None:
        return None
    owner = str(owner).strip()
    return owner or None


def _build_parcel_row(project, parcel, source) -> ParcelRow:
    pid = parcel["id"]
    points = project.get_parcel_polygon(pid)
    closed = bool(parcel.get("closed"))
    scale = project.get_source_scale(source["id"])
    mpp = scale["metres_per_pixel"] if scale else None

    m = measure_polygon(points, mpp, closed=closed)
    n = m.point_count
    has_scale = m.has_scale

    # Scale-first: real-world figures only with a scale, and only where the
    # geometry supports them (area needs a ring of 3+, perimeter a path of 2+).
    area = m.area_sq_m if (has_scale and n >= 3) else None
    perim = m.perimeter_m if (has_scale and n >= 2) else None
    ha = units.area_in_unit(area, _HECTARE) if area is not None else None
    acre = units.area_in_unit(area, _ACRE) if area is not None else None

    local_unit = local_area = None
    profile = project.get_source_unit_profile(source["id"])
    if area is not None and profile is not None and profile["name"] != units.SI_AREA_UNIT:
        local_unit = profile["name"]
        local_area = units.area_in_unit(area, profile["sq_m_per_unit"])

    fields = [(f["label"], f["value"] or "") for f in project.get_parcel_fields(pid)]
    full = project.get_parcel(pid) or {}
    label = parcel.get("name") or full.get("name") or f"Parcel {pid}"

    return ParcelRow(
        parcel_id=pid,
        label=label,
        owner=_norm_owner(parcel.get("owner", full.get("owner"))),
        fields=fields,
        notes=full.get("notes"),
        point_count=n,
        closed=closed,
        has_scale=has_scale,
        metres_per_pixel=mpp,
        scale_method=(scale["method"] if scale else None),
        scale_note=(scale.get("note") if scale else None),
        perimeter_m=perim,
        area_sq_m=area,
        area_hectare=ha,
        area_acre=acre,
        local_unit=local_unit,
        local_area=local_area,
        source=SourceRef(
            source_id=source["id"],
            relative_path=source.get("relative_path"),
            original_name=source.get("original_name"),
            page=source.get("page"),
            doc_date=source.get("doc_date"),
        ),
    )


def _group_total(parcels):
    """(total_sq_m, ha, acre, local_unit, local_area) over a group's parcels.

    Only parcels with a usable area contribute. Local total is given only when
    every scaled parcel shares one local unit; otherwise it is ambiguous and
    omitted (SI + ha + acre remain, always unambiguous)."""
    scaled = [p for p in parcels if p.area_sq_m is not None]
    if not scaled:
        return None, None, None, None, None
    total = sum(p.area_sq_m for p in scaled)
    ha = units.area_in_unit(total, _HECTARE)
    acre = units.area_in_unit(total, _ACRE)
    local_units = [p.local_unit for p in scaled]
    if local_units[0] is not None and all(u == local_units[0] for u in local_units):
        local_unit = local_units[0]
        local_area = sum(p.local_area for p in scaled)
    else:
        local_unit = local_area = None
    return total, ha, acre, local_unit, local_area


def build_owner_report(project, parcel_ids=None, *, project_name=None) -> OwnerReport:
    """Build the owner-wise summary for *project* (a :class:`ProjectDB`).

    *parcel_ids*: optional iterable to scope the report to a chosen subset of
    parcels (e.g. the current on-canvas selection, per the brief); ``None`` means
    every parcel in the project. Parcels are gathered across all sources, since
    one owner commonly has parcels on several sheets.
    """
    id_filter = set(parcel_ids) if parcel_ids is not None else None

    rows = []
    for source in project.list_sources():
        for parcel in project.list_parcels(source["id"]):
            if id_filter is not None and parcel["id"] not in id_filter:
                continue
            rows.append(_build_parcel_row(project, parcel, source))

    # Group by (normalised) owner. Named owners sort case-insensitively; the
    # no-owner bucket always sorts last.
    buckets: dict = {}
    for row in rows:
        buckets.setdefault(row.owner, []).append(row)

    def _sort_key(owner):
        return (owner is None, (owner or "").casefold())

    groups = []
    for owner in sorted(buckets, key=_sort_key):
        parcels = buckets[owner]
        total, ha, acre, lunit, larea = _group_total(parcels)
        missing = sum(1 for p in parcels if not p.has_scale)
        groups.append(OwnerGroup(
            owner=owner,
            display_owner=(owner if owner is not None else NO_OWNER_LABEL),
            parcels=parcels,
            parcel_count=len(parcels),
            scaled_count=sum(1 for p in parcels if p.area_sq_m is not None),
            missing_scale_count=missing,
            total_area_sq_m=total,
            total_area_hectare=ha,
            total_area_acre=acre,
            total_local_unit=lunit,
            total_local_area=larea,
        ))

    grand = sum((g.total_area_sq_m for g in groups if g.total_area_sq_m is not None),
                start=0.0)
    any_area = any(g.total_area_sq_m is not None for g in groups)
    grand_sq_m = grand if any_area else None
    grand_ha = units.area_in_unit(grand_sq_m, _HECTARE) if grand_sq_m is not None else None
    grand_acre = units.area_in_unit(grand_sq_m, _ACRE) if grand_sq_m is not None else None

    methods = sorted({p.scale_method for p in rows if p.scale_method})

    if project_name is None:
        try:
            project_name = project.get_meta("project_name") or "project"
        except Exception:  # pragma: no cover - defensive; get_meta is simple
            project_name = "project"

    return OwnerReport(
        kind=REPORT_KIND,
        project_name=project_name,
        generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        parcel_count=len(rows),
        owner_count=sum(1 for g in groups if g.owner is not None),
        missing_scale_count=sum(1 for p in rows if not p.has_scale),
        scale_methods=methods,
        grand_total_area_sq_m=grand_sq_m,
        grand_total_area_hectare=grand_ha,
        grand_total_area_acre=grand_acre,
        groups=groups,
    )


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _round_floats(obj, ndigits=6):
    """Recursively round floats for tidy record-keeping output (the canonical
    model keeps full precision; only serialised copies are rounded)."""
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, dict):
        return {k: _round_floats(v, ndigits) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_floats(v, ndigits) for v in obj]
    return obj


def _num(value, decimals):
    """Format a number for CSV/PDF, or '' for a missing (no-scale) value."""
    return "" if value is None else f"{value:.{decimals}f}"


def _owner_slug(display_owner) -> str:
    """A filesystem-safe, readable slug for an owner's report filename. Free-text
    owner names (and the "(no owner)" bucket) are reduced to word characters; a
    group's sequence number (added by the caller) guarantees uniqueness even if
    two different owners slugify the same way."""
    slug = re.sub(r"[^\w\-]+", "-", str(display_owner), flags=re.UNICODE).strip("-_")
    return slug.lower() or "owner"


# One report file is produced PER OWNER (not one combined multi-owner document),
# so each writer below takes a single OwnerGroup plus the report-level metadata.
# `crops` (optional) maps parcel_id -> boundary_image.ParcelCrop for the boundary
# thumbnails; a writer references/embeds them where available and never fails if
# a crop is missing.

# ---------------------------------------------------------------------------
# JSON — one owner per file.
# ---------------------------------------------------------------------------

def write_json(report: OwnerReport, group, path, crops=None) -> None:
    crops = crops or {}
    gdict = asdict(group)
    for pdict, parcel in zip(gdict["parcels"], group.parcels):
        crop = crops.get(parcel.parcel_id)
        pdict["boundary_image"] = crop.external_filename if (crop and crop.is_external) else None
        pdict["boundary_image_embedded"] = bool(crop and crop.png_bytes is not None)
    data = {
        "kind": report.kind,
        "project_name": report.project_name,
        "generated_at": report.generated_at,
        "scale_methods": report.scale_methods,
        "owner": group.owner,
        "display_owner": group.display_owner,
        "group": gdict,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(_round_floats(data), fh, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CSV — one owner per file: a row per parcel, then a TOTAL row for that owner.
# ---------------------------------------------------------------------------

def _field_label_union(parcels) -> list:
    """Distinct identification-field labels across *parcels*, in first-seen order,
    so every parcel's fields land in stable, shared columns."""
    seen, seen_set = [], set()
    for parcel in parcels:
        for label, _value in parcel.fields:
            if label not in seen_set:
                seen_set.add(label)
                seen.append(label)
    return seen


def _crop_ref(crop) -> str:
    """CSV cell for a parcel's boundary image: the external PNG filename if it was
    saved separately, a marker if embedded in the PDF, else blank."""
    if crop is None:
        return ""
    if crop.is_external:
        return crop.external_filename
    if crop.png_bytes is not None:
        return "(embedded in PDF)"
    return ""


def write_csv(report: OwnerReport, group, path, crops=None) -> None:
    crops = crops or {}
    labels = _field_label_union(group.parcels)
    fixed_tail = ["Points", "Closed", "Area (m²)", "Area (hectare)",
                  "Area (acre)", "Local area", "Local unit", "Perimeter (m)",
                  "Scale method", "Scale note", "Source file", "Page", "Doc date",
                  "Boundary image"]
    header = ["Owner", "Parcel"] + labels + fixed_tail

    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for parcel in group.parcels:
            fmap = dict(parcel.fields)
            row = [group.display_owner, parcel.label]
            row += [fmap.get(lbl, "") for lbl in labels]
            row += [
                parcel.point_count,
                "yes" if parcel.closed else "no",
                _num(parcel.area_sq_m, 2),
                _num(parcel.area_hectare, 4),
                _num(parcel.area_acre, 4),
                _num(parcel.local_area, 4),
                parcel.local_unit or "",
                _num(parcel.perimeter_m, 2),
                parcel.scale_method or "" if parcel.has_scale else "(no scale)",
                parcel.scale_note or "",
                parcel.source.original_name or parcel.source.relative_path or "",
                "" if parcel.source.page is None else parcel.source.page,
                parcel.source.doc_date or "",
                _crop_ref(crops.get(parcel.parcel_id)),
            ]
            w.writerow(row)
        # Per-owner total row (no grand total — this file is one owner).
        total = [group.display_owner, f"TOTAL — {group.parcel_count} parcel(s)"]
        total += ["" for _ in labels]
        total += [
            "", "",
            _num(group.total_area_sq_m, 2),
            _num(group.total_area_hectare, 4),
            _num(group.total_area_acre, 4),
            _num(group.total_local_area, 4),
            group.total_local_unit or "",
            "", "", "", "", "", "",
        ]
        w.writerow(total)


# ---------------------------------------------------------------------------
# PDF (primary, shareable). PyMuPDF is imported lazily so the model and the
# CSV/JSON writers never depend on it.
# ---------------------------------------------------------------------------

def _import_fitz():
    try:
        import pymupdf as fitz  # PyMuPDF >= 1.24
    except ImportError:
        try:
            import fitz  # older wheels
        except ImportError as exc:  # pragma: no cover - depends on install
            raise RuntimeError(
                "PDF export needs PyMuPDF (pip install pymupdf); "
                "export CSV or JSON instead.") from exc
    return fitz


class _PdfWriter:
    """A tiny top-to-bottom text layout over PyMuPDF pages (A4, auto page-break).

    Kept minimal on purpose — a readable typed document, not a pixel-perfect
    grid — so PDF generation stays dependency-light and predictable."""

    WIDTH, HEIGHT = 595.0, 842.0        # A4 in points
    LEFT, RIGHT, TOP, BOTTOM = 50.0, 545.0, 55.0, 800.0

    def __init__(self, fitz):
        self._fitz = fitz
        self.doc = fitz.open()
        self._new_page()

    def _new_page(self):
        self.page = self.doc.new_page(width=self.WIDTH, height=self.HEIGHT)
        self.y = self.TOP

    def _ensure(self, height):
        if self.y + height > self.BOTTOM:
            self._new_page()

    def line(self, text, size=10, bold=False, indent=0.0, gap=3.0, color=(0, 0, 0)):
        font = "hebo" if bold else "helv"
        lh = size + gap
        for chunk in self._wrap(text, size, self.RIGHT - self.LEFT - indent):
            self._ensure(lh)
            self.page.insert_text((self.LEFT + indent, self.y + size), chunk,
                                  fontsize=size, fontname=font, color=color)
            self.y += lh

    def rule(self, gap_before=4.0, gap_after=6.0):
        self.y += gap_before
        self._ensure(1.0)
        self.page.draw_line((self.LEFT, self.y), (self.RIGHT, self.y),
                            color=(0.6, 0.6, 0.6), width=0.5)
        self.y += gap_after

    def space(self, amount=4.0):
        self.y += amount

    def image(self, png_bytes, pixel_w, pixel_h, indent=18.0,
              max_w=250.0, max_h=170.0, gap=6.0):
        """Embed a PNG (given its pixel size) scaled to fit a box, never upscaled,
        page-breaking if it would overflow the current page."""
        scale = min(max_w / pixel_w, max_h / pixel_h, 1.0)
        dw, dh = pixel_w * scale, pixel_h * scale
        self._ensure(dh + gap)
        x0 = self.LEFT + indent
        rect = self._fitz.Rect(x0, self.y, x0 + dw, self.y + dh)
        self.page.insert_image(rect, stream=png_bytes)
        self.y += dh + gap

    def _wrap(self, text, size, max_width):
        """Greedy word-wrap using PyMuPDF's text-length metric."""
        get_len = self._fitz.get_text_length
        words = str(text).split()
        if not words:
            return [""]
        lines, current = [], words[0]
        for word in words[1:]:
            trial = current + " " + word
            if get_len(trial, fontname="helv", fontsize=size) <= max_width:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return lines

    def save(self, path):
        self.doc.save(str(path))
        self.doc.close()


def _area_phrase(area_sq_m, ha, acre, local_unit, local_area):
    if area_sq_m is None:
        # ASCII placeholder: the PDF's base font can't encode an em dash.
        return "(no scale set)"
    text = f"{area_sq_m:.2f} m² ({ha:.4f} ha, {acre:.4f} acre)"
    if local_unit is not None:
        text += f", {local_area:.4f} {local_unit}"
    return text


def write_pdf(report: OwnerReport, group, path, crops=None) -> None:
    """Write one owner's report as a PDF, embedding each parcel's boundary crop
    (or referencing it when saved separately)."""
    crops = crops or {}
    fitz = _import_fitz()
    pdf = _PdfWriter(fitz)

    pdf.line("Owner-wise Summary Report", size=16, bold=True)
    pdf.line(f"Owner: {group.display_owner}", size=12, bold=True)
    pdf.line(f"Project: {report.project_name}", size=10)
    pdf.line(f"Generated: {report.generated_at}", size=9, color=(0.3, 0.3, 0.3))
    pdf.line(f"Parcels for this owner: {group.parcel_count}", size=9,
             color=(0.3, 0.3, 0.3))
    if report.scale_methods:
        pdf.line("Scale method(s): " + ", ".join(report.scale_methods),
                 size=9, color=(0.3, 0.3, 0.3))
    if group.missing_scale_count:
        pdf.line(f"Note: {group.missing_scale_count} parcel(s) have no scale set "
                 "and are excluded from the area total.",
                 size=9, color=(0.6, 0.2, 0.0))
    pdf.rule()

    for parcel in group.parcels:
        closed = "closed" if parcel.closed else "open"
        pdf.line(f"{parcel.label}   [{parcel.point_count} pts, {closed}]",
                 size=10, bold=True, indent=10, gap=2.0)
        if parcel.fields:
            pdf.line("   ".join(f"{lbl}: {val}" for lbl, val in parcel.fields
                                if val) or "(no identification values)",
                     size=9, indent=18, gap=2.0, color=(0.2, 0.2, 0.2))
        pdf.line("Area: " + _area_phrase(parcel.area_sq_m, parcel.area_hectare,
                                         parcel.area_acre, parcel.local_unit,
                                         parcel.local_area)
                 + (f"    Perimeter: {parcel.perimeter_m:.2f} m"
                    if parcel.perimeter_m is not None else ""),
                 size=9, indent=18, gap=2.0)
        if parcel.has_scale:
            note = f" - {parcel.scale_note}" if parcel.scale_note else ""
            pdf.line(f"Scale: {parcel.scale_method or 'set'}{note}",
                     size=8, indent=18, gap=2.0, color=(0.35, 0.35, 0.35))
        pdf.line(f"Source: {parcel.source.describe()}",
                 size=8, indent=18, gap=2.0, color=(0.35, 0.35, 0.35))
        if parcel.notes:
            pdf.line(f"Notes: {parcel.notes}", size=8, indent=18, gap=2.0,
                     color=(0.35, 0.35, 0.35))
        _emit_parcel_crop(pdf, crops.get(parcel.parcel_id))
        pdf.space(3.0)

    total_phrase = _area_phrase(group.total_area_sq_m, group.total_area_hectare,
                                group.total_area_acre, group.total_local_unit,
                                group.total_local_area)
    pdf.rule()
    pdf.line(f"Total area for {group.display_owner} "
             f"({group.scaled_count} of {group.parcel_count} scaled): {total_phrase}",
             size=11, bold=True)
    pdf.save(path)


def _emit_parcel_crop(pdf, crop) -> None:
    """Embed a parcel's boundary crop, or reference the separately-saved PNG, or
    note why it is unavailable — never silently omit it."""
    if crop is None:
        return
    if crop.png_bytes is not None:
        pdf.line("Boundary image:", size=8, indent=18, gap=1.0,
                 color=(0.35, 0.35, 0.35))
        pdf.image(crop.png_bytes, crop.width, crop.height)
    elif crop.is_external:
        pdf.line(f"Boundary image: {crop.external_filename} "
                 f"(saved separately - {crop.reason})",
                 size=8, indent=18, gap=2.0, color=(0.2, 0.35, 0.6))
    elif crop.reason:
        pdf.line(f"Boundary image: unavailable ({crop.reason})",
                 size=8, indent=18, gap=2.0, color=(0.5, 0.5, 0.5))


# ---------------------------------------------------------------------------
# Dispatch — one file PER OWNER per requested format.
# ---------------------------------------------------------------------------

#: Supported export formats -> per-owner writer.
_WRITERS = {"pdf": write_pdf, "csv": write_csv, "json": write_json}


def unique_export_path(exports_dir, stem, ext, *, timestamp=None):
    """A never-overwriting path inside *exports_dir* (the brief's storage rule).

    Names are timestamped (``<stem>_YYYYMMDD-HHMMSS.<ext>``) so regenerating the
    same report never clobbers an earlier one; on the rare same-second collision
    an index (``_2``, ``_3``, ...) is appended. Pass a shared *timestamp* to give
    several formats / owners of one generation the same timestamp."""
    from pathlib import Path as _Path
    exports_dir = _Path(exports_dir)
    ext = ext.lstrip(".").lower()
    when = timestamp or datetime.now()
    base = f"{stem}_{when.strftime('%Y%m%d-%H%M%S')}"
    candidate = exports_dir / f"{base}.{ext}"
    index = 2
    while candidate.exists():
        candidate = exports_dir / f"{base}_{index}.{ext}"
        index += 1
    return candidate


def _build_parcel_crops(project, report, exports_dir, when):
    """Compute a boundary crop for every parcel in *report*. Crops that fit are
    kept in memory for PDF embedding; awkward ones are saved as separate PNGs in
    *exports_dir* and referenced. Returns ``(crops_by_parcel_id, png_paths)``."""
    from . import boundary_image as bimg
    crops, png_paths = {}, []
    for seq, group in enumerate(report.groups, 1):
        stem = f"owner-summary_{seq:02d}_{_owner_slug(group.display_owner)}"
        for parcel in group.parcels:
            pid = parcel.parcel_id
            polygon = project.get_parcel_polygon(pid)
            if len(polygon) < 2:
                crops[pid] = bimg.ParcelCrop(pid, reason="no boundary traced")
                continue
            source = (project.get_source(parcel.source.source_id)
                      if parcel.source.source_id is not None else None)
            if source is None:
                crops[pid] = bimg.ParcelCrop(pid, reason="source unavailable")
                continue
            open_kwargs = {}
            if source.get("file_type") == "pdf" and source.get("page") is not None:
                open_kwargs["page"] = source["page"]
            try:
                image = bimg.render_crop(
                    project.resolve(parcel.source.relative_path), polygon,
                    closed=parcel.closed, open_kwargs=open_kwargs)
            except Exception as exc:   # unreadable/unsupported source, etc.
                crops[pid] = bimg.ParcelCrop(pid, reason=f"crop unavailable: {exc}")
                continue
            w, h = image.size
            placement, reason = bimg.classify_crop(w, h)
            if placement == "external":
                png_path = unique_export_path(
                    exports_dir, f"{stem}_parcel{pid}", "png", timestamp=when)
                image.save(png_path)
                png_paths.append(png_path)
                crops[pid] = bimg.ParcelCrop(
                    pid, external_filename=png_path.name, width=w, height=h,
                    reason=reason)
            else:
                crops[pid] = bimg.ParcelCrop(
                    pid, png_bytes=bimg.to_png_bytes(image), width=w, height=h)
    return crops, png_paths


def export_owner_reports(project, exports_dir, *, formats=("pdf",),
                         parcel_ids=None, timestamp=None, project_name=None):
    """Generate the owner-wise summary as **one file per owner** for each requested
    format, into *exports_dir*. Filenames carry the owner (slug + sequence number,
    so same-named or blank owners never collide) and a shared timestamp.

    Returns ``(report, report_paths, image_paths)`` — the built model, the report
    files written, and any separately-saved boundary PNGs.
    """
    from pathlib import Path as _Path
    exports_dir = _Path(exports_dir)
    formats = list(formats)
    unknown = [f for f in formats if f not in _WRITERS]
    if unknown:
        raise ValueError(f"Unsupported report format(s) {unknown}; "
                         f"expected any of {sorted(_WRITERS)}.")

    report = build_owner_report(project, parcel_ids=parcel_ids,
                                project_name=project_name)
    when = timestamp or datetime.now()
    crops, image_paths = _build_parcel_crops(project, report, exports_dir, when)

    report_paths = []
    for seq, group in enumerate(report.groups, 1):
        stem = f"owner-summary_{seq:02d}_{_owner_slug(group.display_owner)}"
        for fmt in formats:
            out = unique_export_path(exports_dir, stem, fmt, timestamp=when)
            _WRITERS[fmt](report, group, out, crops)
            report_paths.append(out)
    return report, report_paths, image_paths
