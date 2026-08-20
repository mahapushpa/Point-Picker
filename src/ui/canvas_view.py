"""canvas_view — native QGraphicsView canvas with pan/zoom + point picking (PySide6).

Thin UI: display a RasterImage, pan/zoom, and let the user pick points for the
two shared workflows — two-point scale calibration (M3) and polygon boundary
tracing (M4). No domain logic (scale/geometry math live in ``src.core``).

Point picking is a single shared model used by both modes (M4.5 refinements),
so corrections work identically everywhere:

  * **place** — a left click (press+release under the movement threshold) adds a
    point; a left drag on empty space still pans;
  * **adjust** — press on an already-placed point and drag it, or select it and
    nudge with the arrow keys (1 px, or 10 px with Shift), before finalising;
  * **confirm** — Enter finalises the pick (scale: emit the two points so the
    distance can be entered; polygon: close the boundary);
  * **cancel** — Esc fully resets the in-progress pick with no residual points
    or half-armed mode.

Zoom/pan behaviour is ported from reference/point_picker.html (wheel zoom
anchored under the cursor, drag-to-pan, 0.05x..20x clamp).

A full-window precision crosshair (CAD/GIS style: horizontal + vertical lines
spanning the whole viewport, drawn with a dark core and light halo for contrast
on any map background) can be toggled while picking. It is drawn in viewport
pixels via :meth:`drawForeground`, so it is a fixed on-screen overlay independent
of zoom — the arms always span the window and stay usable at any zoom level.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import QImage, QPixmap, QPainter, QPen, QColor, QBrush
from PySide6.QtGui import QPolygonF
from PySide6.QtWidgets import (
    QGraphicsScene, QGraphicsView, QGraphicsEllipseItem, QGraphicsLineItem,
    QGraphicsSimpleTextItem, QGraphicsPolygonItem, QGraphicsRectItem, QGraphicsItem,
)

from ..core.polygon import nearest_vertex_index, SNAP_TOLERANCE_PX
from ..core.selection import nearest_edge_index, contiguous_edge_toggle
from ..io.raster import RasterImage
from ..io.guardrails import is_duplicate_point

_MARK_COLOR = QColor("#1D9E75")  # scale-calibration markers: accent green (as point_picker.html)
_POLY_COLOR = QColor("#E8770F")  # boundary-tracing markers: orange, visually distinct
_ACTIVE_HALO = QColor("#FFFFFF")  # ring around the currently-selected point
_SNAP_COLOR = QColor("#D6336C")   # snap indicator: magenta, "about to reuse this vertex"
_MARQUEE_COLOR = QColor("#0B7285")  # rubber-band selection rectangle
_SELECTED_FILL_ALPHA = 70         # translucent fill marking a selected (not active) parcel
_SEG_COLOR = QColor("#7048E8")    # selected boundary segment (M12): violet, distinct from all above
_SEG_HOVER_COLOR = QColor("#B197FC")  # edge under the cursor in segment mode (faint violet)
_LOC_REF_COLOR = QColor("#0CA678")    # location reference landmark (M16): teal, filled
_LOC_TARGET_COLOR = QColor("#F76707")  # location target point (M16): orange ring
_LOC_LINK_COLOR = QColor("#495057")   # reference->target connector (M16): grey dashed
_WARN_COLOR = QColor("#E03131")       # missing-corner edge warning (M17): red, dashed
_ASSIST_COLOR = QColor("#1971C2")     # line-follow preview (M18): blue, awaiting confirm


def qimage_from_raster(raster: RasterImage) -> QImage:
    """Build a QImage from a neutral RasterImage.

    ``QImage`` does not copy the buffer it is handed, so we ``.copy()`` to give
    the QImage ownership of an independent copy — otherwise the pixels would be
    freed when the source ``bytes`` is garbage-collected.
    """
    if raster.mode != "RGBA":
        raise ValueError(f"canvas expects RGBA rasters, got mode {raster.mode!r}")
    img = QImage(
        raster.data, raster.width, raster.height, raster.stride,
        QImage.Format.Format_RGBA8888,
    )
    return img.copy()


class CanvasView(QGraphicsView):
    """A pan/zoom viewport for a single rendered document."""

    MIN_SCALE = 0.05
    MAX_SCALE = 20.0
    ZOOM_STEP = 1.15           # matches point_picker.html's wheel step
    CLICK_MOVE_THRESHOLD = 4   # px of movement below which a release is a click
    POINT_HIT_RADIUS = 12      # px around a marker that counts as grabbing it
    NUDGE_STEP = 1.0           # image px per arrow press
    NUDGE_STEP_SHIFT = 10.0    # image px per Shift+arrow press

    #: Emitted (in image pixel coordinates) when a two-point scale is confirmed.
    twoPointsPicked = Signal(QPointF, QPointF)
    #: Emitted when the traced polygon changes structurally (add / undo / clear /
    #: close, or moving a not-yet-persisted point).
    polygonChanged = Signal()
    #: Emitted when the polygon is closed (>= 3 points).
    polygonClosed = Signal()
    #: Emitted (vertex_id, x, y) when an existing shared vertex is moved, so the
    #: move is applied to every parcel referencing it rather than forking it.
    vertexMoved = Signal(int, float, float)
    #: Emitted (scene x, y) when the user clicks in selection mode without
    #: dragging — the window hit-tests it against parcels to toggle one.
    selectionClicked = Signal(QPointF)
    #: Emitted (min_x, min_y, max_x, max_y in scene coords) when a marquee drag
    #: finishes in selection mode.
    marqueeSelected = Signal(float, float, float, float)
    #: Emitted when the boundary-segment selection changes (M12), so the window
    #: rebuilds its side table from :meth:`selected_segment_edges`.
    segmentSelectionChanged = Signal()
    #: Emitted with the edge index under the cursor in segment mode (-1 when none),
    #: for the live "hover an edge to see its length" readout.
    edgeHovered = Signal(int)
    #: Emitted (scene x, y) when the user clicks in location-fixing mode (M16); the
    #: window decides whether it is a reference landmark or a target point.
    locationClicked = Signal(QPointF)
    #: Emitted (point index) when a just-placed tracing point is implausibly close
    #: to the previous one — a probable double-click (Milestone 17). Warning only:
    #: the point is kept; the window surfaces a dismissible message.
    duplicatePointWarning = Signal(int)
    #: Emitted (start, end in scene coords) when the user has marked both ends of a
    #: line-follow (Milestone 18); the window computes the followed path (it holds
    #: the neutral raster) and hands it back via :meth:`set_assist_preview`.
    assistRequested = Signal(QPointF, QPointF)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pixmap_item = None
        self._scale = 1.0  # tracked absolute zoom scale, for clamping

        # Gesture state.
        self._mouse_down = False
        self._moved = False
        self._press_pos = None
        self._last_pan_pos = None
        self._drag_kind: str | None = None   # 'scale' | 'poly' while dragging a marker
        self._drag_index: int | None = None
        self._active: tuple[str, int] | None = None  # selected marker for arrow-nudge

        # Scale-calibration state.
        self._calibrating = False
        self._calib_points: list[QPointF] = []
        self._calib_items: list[QGraphicsItem] = []

        # Polygon-tracing state (the one active, editable boundary). Each point
        # has a parallel vertex id (None until persisted / not snapped).
        self._tracing = False
        self._poly_points: list[QPointF] = []
        self._poly_vertex_ids: list[int | None] = []
        self._poly_closed = False
        self._poly_items: list[QGraphicsItem] = []
        self._active_color = _POLY_COLOR
        # Missing-corner warnings (M17): edge indices flagged as possibly skipping a
        # real corner. Purely advisory highlight, drawn with the active boundary and
        # cleared whenever the boundary is edited (they'd be stale).
        self._edge_warnings: set[int] = set()
        # Other parcels of the same source, drawn for context (not editable).
        # Each is a dict: {points, vertex_ids, closed, color, label}.
        self._bg_polys: list[dict] = []
        self._bg_items: list[QGraphicsItem] = []
        # Shared-vertex snapping.
        self._snap_vertices: list[tuple[int, float, float]] = []  # (id, x, y) of source vertices
        self._snap_indicator: QGraphicsItem | None = None
        self._snap_hover_id: int | None = None

        # Parcel selection (Milestone 7) — a working subset, distinct from the
        # single active/editable parcel. Session state; ids drive how background
        # parcels are drawn (translucent fill) but never which one is editable.
        self._selecting = False
        self._selected_ids: set[int] = set()
        self._sel_dragging = False
        self._sel_moved = False
        self._sel_press_vp = None            # QPointF viewport pos at press
        self._sel_origin_scene: QPointF | None = None
        self._marquee_item: QGraphicsItem | None = None

        # Boundary-segment selection (Milestone 12) — pick a contiguous run of the
        # ACTIVE parcel's edges for the boundary-description report. Selection is
        # an ordered edge-index list kept contiguous by construction.
        self._segmenting = False
        self._seg_selected: list[int] = []
        self._seg_items: list[QGraphicsItem] = []
        self._seg_hover_edge: int | None = None
        self.EDGE_HIT_RADIUS = 8   # px around an edge that counts as clicking it

        # Location-fixing (Milestone 16) — a canvas mode for marking durable
        # reference landmarks and target points; a click emits `locationClicked`
        # and the window drives the reference/target flow. Purely a display +
        # click surface here; the geometry lives in core.location.
        self._locating = False
        self._loc_items: list = []
        self._loc_markers: list = []   # (x, y, label, kind) to draw
        self._loc_links: list = []     # (x1, y1, x2, y2) reference->target links

        # Semi-automated tracing assist (Milestone 18) — a canvas mode: mark two
        # ends of a printed line, the window computes a followed path, and it is
        # shown as a PREVIEW (never auto-accepted). `_assist_start` holds the first
        # mark; `_assist_preview` the path awaiting explicit accept/reject.
        self._assisting = False
        self._assist_start: QPointF | None = None
        self._assist_preview: list | None = None
        self._assist_items: list = []

        # Visual confidence overlay (Milestone 13) — review-time DISPLAY controls
        # only. None of these touch stored geometry, the active parcel, the
        # selection, or the current mode; they change only what is drawn over the
        # scan so a trace can be checked against it:
        #   * `_overlays_visible` — global raw-scan toggle (hide everything drawn);
        #   * `_overlay_opacity`  — fade all boundary overlays (0..1);
        #   * `_hidden_parcel_ids`— genuinely drop individual parcels (active or
        #     not), distinct from the muted background look and from selection;
        #   * `_active_parcel_id` — which parcel the editable boundary belongs to,
        #     so per-parcel hiding can apply to it too (backgrounds carry their ids).
        self._overlays_visible = True
        self._overlay_opacity = 1.0
        self._hidden_parcel_ids: set[int] = set()
        self._active_parcel_id: int | None = None

        # Precision crosshair.
        self._crosshair_enabled = False
        self._cursor_vp_pos = None  # QPoint in viewport coords, or None

        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setRenderHints(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)  # we pan manually
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setBackgroundBrush(Qt.GlobalColor.lightGray)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # for Enter/Esc/arrow keys
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)  # crosshair follows the free cursor

    # -- image display ------------------------------------------------------

    def set_image(self, raster: RasterImage) -> None:
        """Display *raster*, replacing anything already shown, and fit it."""
        pixmap = QPixmap.fromImage(qimage_from_raster(raster))
        self._scene.clear()  # also drops any calibration / polygon marker items
        self._reset_calibration_state()
        self._reset_polygon_state()
        self._bg_items.clear()  # scene.clear() already removed them
        self._bg_polys.clear()
        self._snap_vertices.clear()
        self._snap_indicator = None
        self._snap_hover_id = None
        self._selecting = False
        self._selected_ids.clear()
        self._sel_dragging = False
        self._marquee_item = None
        self._segmenting = False
        self._seg_selected.clear()
        self._seg_items.clear()
        self._seg_hover_edge = None
        self._edge_warnings.clear()
        self._locating = False
        self._loc_items.clear()
        self._assisting = False
        self._assist_start = None
        self._assist_preview = None
        self._assist_items.clear()
        # Reset the review-overlay display to defaults for the new source.
        self._overlays_visible = True
        self._overlay_opacity = 1.0
        self._hidden_parcel_ids.clear()
        self._active_parcel_id = None
        self._active = None
        self._drag_kind = self._drag_index = None
        self._pixmap_item = self._scene.addPixmap(pixmap)
        self._scene.setSceneRect(QRectF(pixmap.rect()))
        self.reset_view()

    def set_display_pixels(self, raster: RasterImage) -> None:
        """Replace only the displayed pixels, keeping the scene rect, zoom/pan,
        and every marker / boundary / selection item exactly as-is. Used for the
        non-destructive preprocessing preview toggle: because preprocessing never
        changes dimensions, all stored pixel coordinates stay valid, so nothing
        else needs to move. Requires the same dimensions as the current image."""
        if self._pixmap_item is None:
            return
        if (raster.width, raster.height) != (int(self._scene.sceneRect().width()),
                                             int(self._scene.sceneRect().height())):
            raise ValueError(
                "set_display_pixels requires identical dimensions "
                f"({raster.width}x{raster.height} vs scene "
                f"{int(self._scene.sceneRect().width())}x{int(self._scene.sceneRect().height())})")
        self._pixmap_item.setPixmap(QPixmap.fromImage(qimage_from_raster(raster)))

    def has_image(self) -> bool:
        return self._pixmap_item is not None

    def reset_view(self) -> None:
        """Fit the whole image in the viewport (the prototype's 'Reset view')."""
        if self._pixmap_item is None:
            return
        self.resetTransform()
        self.fitInView(self._pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)
        self._scale = self.transform().m11()
        self._update_cursor()

    def zoom_in(self) -> None:
        self._zoom_by(self.ZOOM_STEP, anchor_center=True)

    def zoom_out(self) -> None:
        self._zoom_by(1 / self.ZOOM_STEP, anchor_center=True)

    # -- scale calibration --------------------------------------------------

    def start_scale_calibration(self) -> bool:
        """Arm two-point calibration: clear any previous markers and wait for two
        clicks (then Enter to confirm). Returns False if there is no image."""
        if self._pixmap_item is None:
            return False
        self._reset_polygon_mode()          # modes are mutually exclusive
        self._reset_selection_mode()
        self._reset_segment_mode()
        self._reset_location_mode()
        self._reset_assist_mode()
        self._redraw_segments()
        self.clear_scale_markers()
        self._calibrating = True
        self._crosshair_enabled = True       # crosshair on by default for scale
        self.setFocus()
        self._update_cursor()
        self.viewport().update()
        return True

    def cancel_scale_calibration(self) -> None:
        self._reset_calibration_state()
        self._update_cursor()
        self.viewport().update()

    def clear_scale_markers(self) -> None:
        """Remove calibration markers/line and forget picked points."""
        self._remove_items(self._calib_items)
        self._calib_points.clear()
        if self._active is not None and self._active[0] == "scale":
            self._active = None

    def is_calibrating(self) -> bool:
        return self._calibrating

    # -- polygon tracing ----------------------------------------------------

    def start_polygon(self) -> bool:
        """Enter boundary-tracing mode. Keeps any existing polygon so tracing can
        resume; use :meth:`clear_polygon` to start over. Returns False if there
        is no image."""
        if self._pixmap_item is None:
            return False
        self._reset_calibration_mode()       # modes are mutually exclusive
        self._reset_selection_mode()
        self._reset_segment_mode()
        self._reset_location_mode()
        self._reset_assist_mode()
        self._redraw_segments()
        if self._poly_closed:
            self._poly_closed = False        # re-open to continue editing
        self._tracing = True
        self._crosshair_enabled = False      # off by default for polygon (many vertices)
        self.setFocus()
        self._redraw_polygon()
        self._update_cursor()
        self.viewport().update()
        return True

    def stop_polygon(self) -> None:
        self._tracing = False
        self._clear_snap_indicator()
        self._update_cursor()
        self.viewport().update()

    def undo_last_point(self) -> None:
        if not self._poly_points:
            return
        self._poly_points.pop()
        if self._poly_vertex_ids:
            self._poly_vertex_ids.pop()
        self._poly_closed = False
        self._edge_warnings.clear()   # geometry changed; warnings are stale (M17)
        if self._active is not None and self._active[0] == "poly":
            self._active = ("poly", len(self._poly_points) - 1) if self._poly_points else None
        self._redraw_polygon()
        self.polygonChanged.emit()

    def clear_polygon(self) -> None:
        had_any = bool(self._poly_points)
        self._poly_points.clear()
        self._poly_vertex_ids.clear()
        self._poly_closed = False
        self._edge_warnings.clear()
        if self._active is not None and self._active[0] == "poly":
            self._active = None
        self._redraw_polygon()
        if had_any:
            self.polygonChanged.emit()

    def close_polygon(self) -> bool:
        """Close the boundary (last point joins the first). Needs >= 3 points."""
        if len(self._poly_points) < 3 or self._poly_closed:
            return False
        self._poly_closed = True
        self._tracing = False
        self._clear_snap_indicator()
        self._redraw_polygon()
        self._update_cursor()
        self.polygonChanged.emit()
        self.polygonClosed.emit()
        return True

    def set_polygon(self, pixel_points, closed: bool = True, vertex_ids=None) -> None:
        """Replace the active polygon with points restored from storage (image
        pixels), optionally with their vertex ids. Does not emit change signals —
        the caller is loading, not editing."""
        self._poly_points = [QPointF(float(x), float(y)) for x, y in pixel_points]
        if vertex_ids is not None:
            self._poly_vertex_ids = [None if v is None else int(v) for v in vertex_ids]
        else:
            self._poly_vertex_ids = [None] * len(self._poly_points)
        self._poly_closed = closed and len(self._poly_points) >= 3
        self._tracing = False
        self._active = None
        # A different active boundary invalidates any segment selection (edge
        # indices are per-parcel); drop it silently (the caller is loading).
        self._seg_selected.clear()
        self._seg_hover_edge = None
        self._edge_warnings.clear()   # warnings belong to the previous boundary (M17)
        self._redraw_polygon()
        self._redraw_segments()
        self._update_cursor()

    def set_active_vertex_ids(self, vertex_ids) -> None:
        """Attach vertex ids to the current active points (e.g. after a save that
        created vertices), without touching coordinates or redrawing points."""
        ids = [None if v is None else int(v) for v in vertex_ids]
        # Keep aligned with the point list.
        if len(ids) < len(self._poly_points):
            ids += [None] * (len(self._poly_points) - len(ids))
        self._poly_vertex_ids = ids[:len(self._poly_points)]

    def active_vertex_ids(self) -> list[int | None]:
        return list(self._poly_vertex_ids)

    def set_snap_vertices(self, vertices) -> None:
        """Provide the source's vertices as (id, x, y) so tracing can snap new
        points onto existing ones (shared boundaries)."""
        self._snap_vertices = [(int(vid), float(x), float(y)) for vid, x, y in vertices]

    def polygon_points(self) -> list[tuple[float, float]]:
        """Current boundary as ordered (x, y) image-pixel tuples."""
        return [(p.x(), p.y()) for p in self._poly_points]

    def is_polygon_closed(self) -> bool:
        return self._poly_closed

    def is_tracing(self) -> bool:
        return self._tracing

    def set_active_color(self, color: QColor) -> None:
        """Colour of the active (editable) boundary. Set per parcel so each is
        visually distinct from the others shown in the background."""
        self._active_color = QColor(color)
        self._redraw_polygon()

    def set_background_polygons(self, polygons) -> None:
        """Draw other parcels of the same source for context, non-interactively.
        *polygons* is a list of ``(parcel_id, points, vertex_ids, closed, color,
        label)`` where points are (x, y) image-pixel tuples. ``parcel_id`` lets a
        selected background parcel draw in its distinct selected state; vertex ids
        let a shared vertex move in lock-step when edited via the active parcel.
        Visuals only — hit-testing and editing always target the active boundary."""
        self._bg_polys = []
        for parcel_id, points, vertex_ids, closed, color, label in polygons:
            self._bg_polys.append({
                "parcel_id": None if parcel_id is None else int(parcel_id),
                "points": [QPointF(float(x), float(y)) for x, y in points],
                "vertex_ids": [None if v is None else int(v) for v in (vertex_ids or [])],
                "closed": bool(closed),
                "color": QColor(color),
                "label": label,
            })
        self._redraw_backgrounds()

    def set_selected_ids(self, ids) -> None:
        """Record which parcels are in the current selection (a working subset,
        separate from the active parcel) so background parcels can render their
        selected state. Purely visual; does not change the active parcel."""
        self._selected_ids = {int(i) for i in ids}
        self._redraw_backgrounds()

    def selected_ids(self) -> set[int]:
        return set(self._selected_ids)

    def _redraw_backgrounds(self) -> None:
        self._remove_items(self._bg_items)
        if not self._overlays_visible:
            return
        for poly in self._bg_polys:
            qpts = poly["points"]
            if not qpts:
                continue
            if poly["parcel_id"] is not None and poly["parcel_id"] in self._hidden_parcel_ids:
                continue   # per-parcel overlay hidden (Milestone 13), regardless of state
            selected = poly["parcel_id"] is not None and poly["parcel_id"] in self._selected_ids
            if selected and len(qpts) >= 3:
                # Selected-but-not-active: a translucent fill (unique to this
                # state — the active parcel has vertex dots, unselected ones are
                # a bare outline), plus a solid outline in the parcel colour.
                fill = QColor(poly["color"])
                fill.setAlpha(_SELECTED_FILL_ALPHA)
                shape = QGraphicsPolygonItem(QPolygonF(qpts))
                shape.setBrush(QBrush(fill))
                shape.setPen(QPen(Qt.PenStyle.NoPen))
                shape.setZValue(0.5)  # above the pixmap, below the outlines
                self._scene.addItem(shape)
                self._bg_items.append(shape)
            qcolor = QColor(poly["color"])
            qcolor.setAlpha(255 if selected else 210)  # muted unless selected
            pen = QPen(qcolor)
            pen.setWidth(3 if selected else 2)          # selected reads heavier
            pen.setCosmetic(True)
            segments = list(zip(qpts, qpts[1:]))
            if poly["closed"] and len(qpts) >= 3:
                segments.append((qpts[-1], qpts[0]))
            for a, b in segments:
                line = QGraphicsLineItem(a.x(), a.y(), b.x(), b.y())
                line.setPen(pen)
                line.setZValue(1)  # above the pixmap, below the active boundary
                self._scene.addItem(line)
                self._bg_items.append(line)
            if poly["label"]:
                text = QGraphicsSimpleTextItem(poly["label"])
                text.setBrush(QColor(poly["color"]))
                text.setPos(qpts[0])
                text.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
                text.setZValue(2)
                self._scene.addItem(text)
                self._bg_items.append(text)
        self._apply_opacity(self._bg_items)

    # -- boundary-segment selection (Milestone 12) --------------------------
    #
    # A view operation on the ACTIVE parcel: click an edge to add/remove it from a
    # contiguous run (colour change), for the boundary-description report. Edge i
    # is between active point i and i+1 (the closing edge, index n-1, joins the
    # last point back to the first when the boundary is closed) — the same edge
    # numbering the export uses, so indices line up end-to-end.

    def start_segment_selection(self) -> bool:
        """Enter segment-selection mode on the active boundary. Returns False if
        there is no image or the active parcel has no edges to select."""
        if self._pixmap_item is None or len(self._edge_list()) == 0:
            return False
        self._reset_calibration_mode()
        self._reset_polygon_mode()
        self._reset_selection_mode()
        self._reset_location_mode()
        self._reset_assist_mode()
        self._clear_snap_indicator()
        self._segmenting = True
        self.setFocus()
        self._redraw_segments()
        self._update_cursor()
        return True

    def stop_segment_selection(self) -> None:
        self._segmenting = False
        self._seg_hover_edge = None
        self._redraw_segments()
        self._update_cursor()
        self.edgeHovered.emit(-1)

    def is_segment_selecting(self) -> bool:
        return self._segmenting

    def clear_segment_selection(self) -> None:
        if self._seg_selected:
            self._seg_selected.clear()
            self._redraw_segments()
            self.segmentSelectionChanged.emit()

    def selected_segment_edges(self) -> list[int]:
        """The current ordered, contiguous run of selected edge indices."""
        return list(self._seg_selected)

    def set_segment_selection(self, edges) -> None:
        """Set the selected edge run programmatically (restore, or drive from a
        test), then redraw and notify. Indices must refer to the active boundary."""
        self._seg_selected = [int(e) for e in edges]
        self._redraw_segments()
        self.segmentSelectionChanged.emit()

    def _reset_segment_mode(self) -> None:
        """Leave segment interaction; keep the selection so a re-entry resumes it."""
        self._segmenting = False
        self._seg_hover_edge = None

    def _edge_list(self):
        """The active boundary's edges as ``(edge_index, a, b)`` with QPointF
        endpoints, including the closing edge when closed. Matches the export's
        edge numbering."""
        pts = self._poly_points
        edges = [(i, pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
        if self._poly_closed and len(pts) >= 3:
            edges.append((len(pts) - 1, pts[-1], pts[0]))
        return edges

    def _edge_at(self, view_pos: QPointF):
        """Index of the active-boundary edge under *view_pos* (viewport coords)
        within EDGE_HIT_RADIUS, else None. Hit-tested in viewport pixels so the
        tolerance is constant at any zoom."""
        edges = self._edge_list()
        if not edges:
            return None
        segs = []
        for _idx, a, b in edges:
            va, vb = self.mapFromScene(a), self.mapFromScene(b)
            segs.append(((va.x(), va.y()), (vb.x(), vb.y())))
        pos = (view_pos.x(), view_pos.y())
        local = nearest_edge_index(pos, segs, self.EDGE_HIT_RADIUS)
        return edges[local][0] if local is not None else None

    def _toggle_edge_at(self, view_pos: QPointF) -> None:
        edge = self._edge_at(view_pos)
        if edge is None:
            return
        n_edges = len(self._edge_list())
        self._seg_selected = contiguous_edge_toggle(
            self._seg_selected, edge, n_edges, self._poly_closed)
        self._redraw_segments()
        self.segmentSelectionChanged.emit()

    def _redraw_segments(self) -> None:
        self._remove_items(self._seg_items)
        if not self._segmenting:
            return
        if not self._overlays_visible or self._active_overlay_hidden():
            return   # segments belong to the active parcel; hidden with it (M13)
        by_index = {i: (a, b) for i, a, b in self._edge_list()}
        # Hovered (not-yet-selected) edge: a faint highlight cue.
        if self._seg_hover_edge is not None and self._seg_hover_edge not in self._seg_selected \
                and self._seg_hover_edge in by_index:
            a, b = by_index[self._seg_hover_edge]
            self._add_segment_line(a, b, _SEG_HOVER_COLOR, width=5)
        # Selected edges: bold highlight + running sequence number.
        for seq, idx in enumerate(self._seg_selected, 1):
            if idx not in by_index:
                continue
            a, b = by_index[idx]
            self._add_segment_line(a, b, _SEG_COLOR, width=5)
            mid = QPointF((a.x() + b.x()) / 2, (a.y() + b.y()) / 2)
            label = QGraphicsSimpleTextItem(str(seq))
            label.setBrush(_SEG_COLOR)
            label.setPos(mid)
            label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
            label.setZValue(8)
            self._scene.addItem(label)
            self._seg_items.append(label)
        self._apply_opacity(self._seg_items)

    def _add_segment_line(self, a: QPointF, b: QPointF, color: QColor, width: int) -> None:
        pen = QPen(color)
        pen.setWidth(width)
        pen.setCosmetic(True)
        line = QGraphicsLineItem(a.x(), a.y(), b.x(), b.y())
        line.setPen(pen)
        line.setZValue(7)   # above the active boundary (5), below markers (10)
        self._scene.addItem(line)
        self._seg_items.append(line)

    # -- location-fixing (Milestone 16) -------------------------------------
    #
    # Own canvas mode, mutually exclusive with scale/trace/select/segment (same
    # pattern). A left click emits `locationClicked` in scene (pixel) coordinates;
    # the window decides whether it lands as a reference landmark or a target, and
    # hands back markers to draw via `set_location_overlay`.

    def start_location_fix(self) -> bool:
        """Enter location-fixing mode. Independent of tracing state — usable before,
        after, or without a traced boundary. Returns False if there is no image."""
        if self._pixmap_item is None:
            return False
        self._reset_calibration_mode()
        self._reset_polygon_mode()
        self._reset_selection_mode()
        self._reset_segment_mode()
        self._reset_assist_mode()
        self._redraw_segments()
        self._clear_snap_indicator()
        self._locating = True
        self.setFocus()
        self._update_cursor()
        self.viewport().update()
        return True

    def stop_location_fix(self) -> None:
        self._locating = False
        self._update_cursor()
        self.viewport().update()

    def is_locating(self) -> bool:
        return self._locating

    def _reset_location_mode(self) -> None:
        """Leave location interaction; keep the drawn markers (another tool may be
        used, then location resumed)."""
        self._locating = False

    def set_location_overlay(self, markers, links=()) -> None:
        """Draw location-fix markers and reference->target links. *markers* is a
        list of ``(x, y, label, kind)`` (kind 'ref' or 'target'); *links* a list of
        ``(x1, y1, x2, y2)``. Visual only — the window owns the data."""
        self._loc_markers = [(float(x), float(y), str(lb), str(k)) for x, y, lb, k in markers]
        self._loc_links = [(float(a), float(b), float(c), float(d)) for a, b, c, d in links]
        self._redraw_location()

    def _redraw_location(self) -> None:
        self._remove_items(self._loc_items)
        if not self._overlays_visible:
            return   # honour the M13 global overlay hide
        for a, b, c, d in self._loc_links:
            pen = QPen(_LOC_LINK_COLOR)
            pen.setWidth(1)
            pen.setCosmetic(True)
            pen.setStyle(Qt.PenStyle.DashLine)
            line = QGraphicsLineItem(a, b, c, d)
            line.setPen(pen)
            line.setZValue(6)
            self._scene.addItem(line)
            self._loc_items.append(line)
        for x, y, label, kind in self._loc_markers:
            color = _LOC_REF_COLOR if kind == "ref" else _LOC_TARGET_COLOR
            r = 6.0
            pen = QPen(color)
            pen.setWidth(2)
            pen.setCosmetic(True)
            dot = QGraphicsEllipseItem(-r, -r, 2 * r, 2 * r)
            dot.setPen(pen)
            if kind == "ref":
                dot.setBrush(QBrush(color))   # landmark: filled; target: open ring
            dot.setPos(QPointF(x, y))
            dot.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
            dot.setZValue(12)
            self._scene.addItem(dot)
            self._loc_items.append(dot)
            if label:
                text = QGraphicsSimpleTextItem(label)
                text.setBrush(color)
                text.setPos(QPointF(x, y))
                text.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
                text.moveBy(r + 3, -(r + 13))
                text.setZValue(12)
                self._scene.addItem(text)
                self._loc_items.append(text)

    # -- semi-automated tracing assist (Milestone 18) -----------------------
    #
    # Own canvas mode. The user marks two ends of a printed line; the second mark
    # emits `assistRequested`, the window computes the followed path and hands it
    # back via `set_assist_preview`. The preview is NEVER auto-accepted: Enter
    # accepts it (appending through the same snap + guard-rail path as manual
    # points), Esc rejects it (boundary untouched).

    def start_assist(self) -> bool:
        """Enter line-follow mode. Returns False if there is no image."""
        if self._pixmap_item is None:
            return False
        self._reset_calibration_mode()
        self._reset_polygon_mode()
        self._reset_selection_mode()
        self._reset_segment_mode()
        self._reset_location_mode()
        self._reset_assist_mode()
        self._redraw_segments()
        self._clear_snap_indicator()
        self._assisting = True
        self._assist_start = None
        self._assist_preview = None
        self._redraw_assist()
        self.setFocus()
        self._update_cursor()
        self.viewport().update()
        return True

    def stop_assist(self) -> None:
        self._assisting = False
        self._assist_start = None
        self._assist_preview = None
        self._redraw_assist()
        self._update_cursor()
        self.viewport().update()

    def is_assisting(self) -> bool:
        return self._assisting

    def has_assist_preview(self) -> bool:
        return bool(self._assist_preview)

    def _reset_assist_mode(self) -> None:
        """Leave line-follow interaction, dropping any in-progress mark/preview."""
        self._assisting = False
        self._assist_start = None
        self._assist_preview = None
        self._redraw_assist()

    def _assist_click(self, scene_pt: QPointF) -> None:
        if self._assist_preview is not None:
            return   # a preview is up: accept (Enter) or reject (Esc) first
        if self._assist_start is None:
            self._assist_start = scene_pt
            self._redraw_assist()
        else:
            self.assistRequested.emit(self._assist_start, scene_pt)

    def set_assist_preview(self, points) -> None:
        """Show a followed path awaiting confirmation (window computes it)."""
        self._assist_preview = [QPointF(float(x), float(y)) for x, y in points]
        self._redraw_assist()

    def clear_assist_preview(self) -> None:
        """Reject the preview and any pending start — the boundary is unchanged."""
        self._assist_preview = None
        self._assist_start = None
        self._redraw_assist()

    def accept_assist_preview(self) -> bool:
        """Append the previewed path to the active boundary through the SAME path
        manual points take — so M6 snap-to-existing-vertex and the M17 duplicate
        check apply identically (the assist gets no exemption). Returns False if
        there was no preview."""
        if not self._assist_preview:
            return False
        dups = []
        for p in self._assist_preview:
            if self._append_boundary_point(p):
                dups.append(len(self._poly_points) - 1)
        self._assist_preview = None
        self._assist_start = None
        self._clear_snap_indicator()
        self._redraw_assist()
        self._redraw_polygon()
        self.polygonChanged.emit()
        for idx in dups:
            self.duplicatePointWarning.emit(idx)
        return True

    def _redraw_assist(self) -> None:
        self._remove_items(self._assist_items)
        if not self._overlays_visible:
            return
        if self._assist_start is not None and not self._assist_preview:
            self._add_marker(self._assist_items, self._assist_start, "A", _ASSIST_COLOR,
                             filled=False, active=False)
        if self._assist_preview:
            pen = QPen(_ASSIST_COLOR)
            pen.setWidth(3)
            pen.setCosmetic(True)
            pen.setStyle(Qt.PenStyle.DashLine)
            pts = self._assist_preview
            for a, b in zip(pts, pts[1:]):
                line = QGraphicsLineItem(a.x(), a.y(), b.x(), b.y())
                line.setPen(pen)
                line.setZValue(8)
                self._scene.addItem(line)
                self._assist_items.append(line)
            for p in (pts[0], pts[-1]):
                self._add_marker(self._assist_items, p, "", _ASSIST_COLOR,
                                 filled=True, active=False)
        self._apply_opacity(self._assist_items)

    # -- visual confidence overlay (Milestone 13) ---------------------------
    #
    # Review-only display controls: a way to compare what's traced against the
    # raw scan underneath. Every method here re-renders overlay layers and
    # nothing else — the traced points, vertex ids, active parcel, selection, and
    # the current mode (calibrating / tracing / selecting / segmenting) are left
    # exactly as they were, so these are safe to toggle mid-edit.

    def set_overlays_visible(self, visible: bool) -> None:
        """Global show/hide of everything drawn over the scan (all parcels, the
        active boundary, segment highlights, calibration markers, crosshair) so the
        bare scan can be compared against the trace. A pure display toggle: it does
        not exit any mode or drop any state."""
        self._overlays_visible = bool(visible)
        self._apply_overlay_display()

    def overlays_visible(self) -> bool:
        return self._overlays_visible

    def toggle_overlays(self) -> None:
        self.set_overlays_visible(not self._overlays_visible)

    def set_overlay_opacity(self, factor: float) -> None:
        """Opacity (0..1) of the boundary overlays, so a trace can be faded to
        spot misalignment against faint scan detail. Visual only."""
        self._overlay_opacity = max(0.0, min(1.0, float(factor)))
        self._apply_overlay_display()

    def overlay_opacity(self) -> float:
        return self._overlay_opacity

    def set_active_parcel_id(self, parcel_id) -> None:
        """Tell the canvas which parcel the active/editable boundary belongs to, so
        per-parcel hiding can apply to it as well as to background parcels."""
        self._active_parcel_id = None if parcel_id is None else int(parcel_id)
        self._redraw_polygon()
        self._redraw_segments()

    def set_parcel_hidden(self, parcel_id, hidden: bool) -> None:
        """Genuinely hide/show one parcel's overlay — distinct from the muted
        background look and independent of its active/selected state. Visual only."""
        pid = int(parcel_id)
        if hidden:
            self._hidden_parcel_ids.add(pid)
        else:
            self._hidden_parcel_ids.discard(pid)
        self._apply_overlay_display()

    def set_hidden_parcel_ids(self, ids) -> None:
        self._hidden_parcel_ids = {int(i) for i in ids}
        self._apply_overlay_display()

    def is_parcel_hidden(self, parcel_id) -> bool:
        return int(parcel_id) in self._hidden_parcel_ids

    def hidden_parcel_ids(self) -> set[int]:
        return set(self._hidden_parcel_ids)

    def _active_overlay_hidden(self) -> bool:
        return (self._active_parcel_id is not None
                and self._active_parcel_id in self._hidden_parcel_ids)

    def _apply_overlay_display(self) -> None:
        """Re-render every overlay layer honouring the current visibility / opacity
        / per-parcel-hidden settings. Touches no stored geometry or mode."""
        self._redraw_backgrounds()
        self._redraw_polygon()
        self._redraw_segments()
        self._redraw_calibration()
        self._redraw_location()
        self._redraw_assist()
        self.viewport().update()   # crosshair is painted in drawForeground

    def _apply_opacity(self, bucket: list) -> None:
        """Fade the just-drawn items in *bucket* to the review opacity (a no-op at
        full opacity, so normal editing is pixel-for-pixel unchanged)."""
        if self._overlay_opacity != 1.0:
            for item in bucket:
                item.setOpacity(self._overlay_opacity)

    # -- shared pick: confirm / cancel / crosshair --------------------------

    def confirm_pick(self) -> bool:
        """Finalise the in-progress pick (Enter). Scale: emit the two points.
        Polygon: close the boundary. No-op (returns False) if not enough points."""
        if self._calibrating and len(self._calib_points) == 2:
            self._calibrating = False
            self._active = None
            self._update_cursor()
            self.viewport().update()
            self.twoPointsPicked.emit(self._calib_points[0], self._calib_points[1])
            return True
        if self._tracing and len(self._poly_points) >= 3:
            return self.close_polygon()
        return False

    def cancel_pick(self) -> bool:
        """Fully reset whatever pick is in progress (Esc). Returns True if there
        was something to cancel. Leaves no residual points or half-armed mode."""
        if self._calibrating:
            self._reset_calibration_state()
            self._update_cursor()
            self.viewport().update()
            return True
        if self._tracing:
            self.clear_polygon()             # drops all points + emits change
            self._tracing = False
            self._clear_snap_indicator()
            self._update_cursor()
            self.viewport().update()
            return True
        return False

    def set_crosshair_enabled(self, enabled: bool) -> None:
        self._crosshair_enabled = bool(enabled)
        self._update_cursor()
        self.viewport().update()

    def is_crosshair_enabled(self) -> bool:
        return self._crosshair_enabled

    def toggle_crosshair(self) -> None:
        self.set_crosshair_enabled(not self._crosshair_enabled)

    def is_picking(self) -> bool:
        return self._calibrating or self._tracing

    # -- parcel selection (Milestone 7) -------------------------------------

    def start_selection(self) -> bool:
        """Enter selection mode: click a parcel to toggle it, or drag a marquee to
        catch several. Leaves any traced boundary intact (a view operation, like
        scale). Returns False if there is no image."""
        if self._pixmap_item is None:
            return False
        self._reset_calibration_mode()   # drop an in-progress calibration
        self._reset_polygon_mode()       # stop tracing, keep the polygon
        self._reset_segment_mode()
        self._reset_location_mode()
        self._reset_assist_mode()
        self._redraw_segments()
        self._clear_snap_indicator()
        self._selecting = True
        self.setFocus()
        self._update_cursor()
        self.viewport().update()
        return True

    def stop_selection(self) -> None:
        self._selecting = False
        self._clear_marquee()
        self._update_cursor()
        self.viewport().update()

    def is_selecting(self) -> bool:
        return self._selecting

    def _reset_selection_mode(self) -> None:
        """Leave selection interaction without discarding the selection itself —
        selected parcels stay highlighted while another tool is used."""
        self._selecting = False
        self._sel_dragging = False
        self._clear_marquee()

    def _update_marquee(self, rect: QRectF) -> None:
        if self._marquee_item is None:
            pen = QPen(_MARQUEE_COLOR)
            pen.setWidth(1)
            pen.setStyle(Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            fill = QColor(_MARQUEE_COLOR)
            fill.setAlpha(40)
            item = QGraphicsRectItem()
            item.setPen(pen)
            item.setBrush(QBrush(fill))
            item.setZValue(30)
            self._scene.addItem(item)
            self._marquee_item = item
        self._marquee_item.setRect(rect)

    def _clear_marquee(self) -> None:
        if self._marquee_item is not None and self._marquee_item.scene() is self._scene:
            self._scene.removeItem(self._marquee_item)
        self._marquee_item = None

    # -- zoom ---------------------------------------------------------------

    def wheelEvent(self, event) -> None:
        if self._pixmap_item is None:
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        self._zoom_by(self.ZOOM_STEP if delta > 0 else 1 / self.ZOOM_STEP)

    def _zoom_by(self, factor: float, *, anchor_center: bool = False) -> None:
        if self._pixmap_item is None:
            return
        new_scale = self._scale * factor
        if new_scale < self.MIN_SCALE or new_scale > self.MAX_SCALE:
            return
        self._scale = new_scale
        if anchor_center:
            prev = self.transformationAnchor()
            self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
            self.scale(factor, factor)
            self.setTransformationAnchor(prev)
        else:
            self.scale(factor, factor)
        self.viewport().update()

    # -- mouse --------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pixmap_item is not None \
                and self._selecting:
            # Selection mode: left-drag draws a marquee, a plain click toggles a
            # parcel. Both resolve on release.
            pos = event.position()
            self._sel_dragging = True
            self._sel_moved = False
            self._sel_press_vp = pos
            self._sel_origin_scene = self.mapToScene(pos.toPoint())
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._pixmap_item is not None:
            pos = event.position()
            hit = self._marker_at(pos) if self.is_picking() else None
            if hit is not None:
                # Grab an existing point to fine-tune it (drag), not pan/place.
                self._drag_kind, self._drag_index = hit
                self._active = hit
                self._redraw_active()
                event.accept()
                return
            # Ambiguous: becomes a pan if it moves, a placement if it doesn't.
            self._mouse_down = True
            self._moved = False
            self._press_pos = pos
            self._last_pan_pos = pos
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        self._cursor_vp_pos = pos.toPoint()
        if self._crosshair_enabled and self.is_picking():
            self.viewport().update()

        if self._selecting and self._sel_dragging:
            if not self._sel_moved and \
                    (pos - self._sel_press_vp).manhattanLength() > self.CLICK_MOVE_THRESHOLD:
                self._sel_moved = True
            if self._sel_moved:
                rect = QRectF(self._sel_origin_scene,
                              self.mapToScene(pos.toPoint())).normalized()
                self._update_marquee(rect)
            event.accept()
            return

        if self._drag_index is not None:
            self._set_marker_position(self._drag_kind, self._drag_index,
                                      self.mapToScene(pos.toPoint()))
            event.accept()
            return

        # Free hover while tracing: show when the cursor is about to snap.
        if self._tracing and not self._mouse_down:
            self._update_snap_indicator(self.mapToScene(pos.toPoint()))

        # Free hover in segment mode: highlight the edge under the cursor and
        # report it (drives the live "hover an edge to see its length" readout).
        if self._segmenting and not self._mouse_down:
            edge = self._edge_at(pos)
            if edge != self._seg_hover_edge:
                self._seg_hover_edge = edge
                self._redraw_segments()
                self.edgeHovered.emit(-1 if edge is None else edge)

        if self._mouse_down and self._last_pan_pos is not None:
            if not self._moved and (pos - self._press_pos).manhattanLength() > self.CLICK_MOVE_THRESHOLD:
                self._moved = True
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            if self._moved:
                dx = pos.x() - self._last_pan_pos.x()
                dy = pos.y() - self._last_pan_pos.y()
                h, v = self.horizontalScrollBar(), self.verticalScrollBar()
                h.setValue(h.value() - int(dx))
                v.setValue(v.value() - int(dy))
            self._last_pan_pos = pos
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._selecting and self._sel_dragging:
            self._sel_dragging = False
            if self._sel_moved and self._sel_origin_scene is not None:
                rect = QRectF(self._sel_origin_scene,
                              self.mapToScene(event.position().toPoint())).normalized()
                self._clear_marquee()
                self.marqueeSelected.emit(rect.left(), rect.top(), rect.right(), rect.bottom())
            elif self._sel_origin_scene is not None:
                self.selectionClicked.emit(self._sel_origin_scene)
            self._sel_origin_scene = None
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            if self._drag_index is not None:
                # Finished fine-tuning a point; persist the final position.
                if self._drag_kind == "poly":
                    self.polygonChanged.emit()
                self._drag_kind = self._drag_index = None
                self._update_cursor()
                event.accept()
                return
            if self._mouse_down:
                self._mouse_down = False
                was_click = not self._moved
                self._update_cursor()
                if was_click:
                    if self._locating:
                        self.locationClicked.emit(self.mapToScene(event.position().toPoint()))
                    elif self._assisting:
                        self._assist_click(self.mapToScene(event.position().toPoint()))
                    elif self._segmenting:
                        self._toggle_edge_at(event.position())
                    else:
                        self._place_point(self.mapToScene(event.position().toPoint()))
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:
        self._cursor_vp_pos = None
        self._clear_snap_indicator()
        self.viewport().update()
        super().leaveEvent(event)

    # -- keyboard: confirm / cancel / nudge ---------------------------------

    def keyPressEvent(self, event) -> None:
        # Line-follow preview (M18): Enter accepts the followed path, Esc rejects it
        # (or cancels a pending first mark) — never auto-accepted.
        if self._assisting:
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._assist_preview:
                self.accept_assist_preview()
                event.accept()
                return
            if key == Qt.Key.Key_Escape and (self._assist_preview or self._assist_start):
                self.clear_assist_preview()
                event.accept()
                return
        if self.is_picking():
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.confirm_pick()
                event.accept()
                return
            if key == Qt.Key.Key_Escape:
                self.cancel_pick()
                event.accept()
                return
            if key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
                if self._active is not None:
                    step = (self.NUDGE_STEP_SHIFT
                            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                            else self.NUDGE_STEP)
                    dx = (-step if key == Qt.Key.Key_Left else step if key == Qt.Key.Key_Right else 0.0)
                    dy = (-step if key == Qt.Key.Key_Up else step if key == Qt.Key.Key_Down else 0.0)
                    self._nudge_active(dx, dy)
                    event.accept()
                    return
        super().keyPressEvent(event)

    # -- shared point editing (scene coordinates) ---------------------------

    def _place_point(self, scene_pt: QPointF) -> None:
        """Add a point at *scene_pt* for the active pick mode. While tracing, a
        point within SNAP_TOLERANCE_PX of an existing source vertex (not one this
        parcel already uses) snaps onto it, sharing that vertex."""
        if self._calibrating:
            if len(self._calib_points) >= 2:
                return  # two points already placed; adjust or confirm/cancel
            self._calib_points.append(scene_pt)
            self._active = ("scale", len(self._calib_points) - 1)
            self._redraw_calibration()
        elif self._tracing:
            duplicate = self._append_boundary_point(scene_pt)
            self._clear_snap_indicator()
            self._redraw_polygon()
            self.polygonChanged.emit()
            if duplicate:
                self.duplicatePointWarning.emit(len(self._poly_points) - 1)

    def _append_boundary_point(self, scene_pt: QPointF) -> bool:
        """Append one point to the active boundary with M6 snap-to-existing-vertex,
        returning True if it is an M17 likely-duplicate of the previous point.
        Shared by manual tracing and accepted assist paths (M18) so both get
        identical snapping and guard-rail behaviour — the assist is not exempted."""
        self._edge_warnings.clear()   # geometry changed; any warnings are stale
        snap = self._snap_target((scene_pt.x(), scene_pt.y()))
        duplicate = False
        if snap is not None:
            vid, sx, sy = snap
            self._poly_points.append(QPointF(sx, sy))
            self._poly_vertex_ids.append(vid)
        else:
            if self._poly_points and is_duplicate_point(
                    (scene_pt.x(), scene_pt.y()),
                    (self._poly_points[-1].x(), self._poly_points[-1].y())):
                duplicate = True
            self._poly_points.append(scene_pt)
            self._poly_vertex_ids.append(None)
        self._active = ("poly", len(self._poly_points) - 1)
        return duplicate

    def _set_marker_position(self, kind: str, index: int, scene_pt: QPointF,
                            *, emit: bool = True) -> None:
        if kind == "scale" and 0 <= index < len(self._calib_points):
            self._calib_points[index] = scene_pt
            self._active = ("scale", index)
            self._redraw_calibration()
        elif kind == "poly" and 0 <= index < len(self._poly_points):
            self._poly_points[index] = scene_pt
            self._active = ("poly", index)
            self._edge_warnings.clear()   # moved a vertex; warnings are stale (M17)
            vid = self._poly_vertex_ids[index] if index < len(self._poly_vertex_ids) else None
            if vid is not None:
                # Moving an existing shared vertex: move it everywhere it's used.
                self._apply_vertex_move(vid, scene_pt)
            self._redraw_polygon()
            if emit:
                if vid is not None:
                    self.vertexMoved.emit(vid, scene_pt.x(), scene_pt.y())
                else:
                    self.polygonChanged.emit()

    def _apply_vertex_move(self, vertex_id: int, scene_pt: QPointF) -> None:
        """Reflect a shared vertex's new position in every background parcel that
        references it, and in the live snap set, so the shared edge stays in
        lock-step on screen."""
        changed = False
        for poly in self._bg_polys:
            for i, vid in enumerate(poly["vertex_ids"]):
                if vid == vertex_id:
                    poly["points"][i] = scene_pt
                    changed = True
        self._snap_vertices = [(vid, scene_pt.x(), scene_pt.y()) if vid == vertex_id else (vid, x, y)
                               for (vid, x, y) in self._snap_vertices]
        if changed:
            self._redraw_backgrounds()

    def _snap_target(self, point):
        """Return (vertex_id, x, y) to snap *point* onto, or None. Excludes
        vertices this parcel already uses (never weld its own corners)."""
        if not self._snap_vertices:
            return None
        exclude = {v for v in self._poly_vertex_ids if v is not None}
        idx = nearest_vertex_index(point, self._snap_vertices, SNAP_TOLERANCE_PX,
                                   exclude_ids=exclude)
        return self._snap_vertices[idx] if idx is not None else None

    def _update_snap_indicator(self, scene_pt: QPointF) -> None:
        """Show a magenta ring on the vertex the next click would snap to, so the
        snap is never a surprise. Hidden when no candidate is in range."""
        if not self._overlays_visible:
            self._clear_snap_indicator()
            return
        snap = self._snap_target((scene_pt.x(), scene_pt.y()))
        if snap is None:
            self._clear_snap_indicator()
            return
        vid, sx, sy = snap
        if self._snap_hover_id == vid and self._snap_indicator is not None:
            return
        self._clear_snap_indicator()
        r = 9.0
        pen = QPen(_SNAP_COLOR)
        pen.setWidth(2)
        pen.setCosmetic(True)
        ring = QGraphicsEllipseItem(-r, -r, 2 * r, 2 * r)
        ring.setPen(pen)
        ring.setPos(QPointF(sx, sy))
        ring.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        ring.setZValue(20)
        self._scene.addItem(ring)
        self._snap_indicator = ring
        self._snap_hover_id = vid

    def _clear_snap_indicator(self) -> None:
        if self._snap_indicator is not None and self._snap_indicator.scene() is self._scene:
            self._scene.removeItem(self._snap_indicator)
        self._snap_indicator = None
        self._snap_hover_id = None

    def _nudge_active(self, dx: float, dy: float) -> None:
        if self._active is None:
            return
        kind, index = self._active
        pts = self._calib_points if kind == "scale" else self._poly_points
        if 0 <= index < len(pts):
            p = pts[index]
            self._set_marker_position(kind, index, QPointF(p.x() + dx, p.y() + dy))

    def _marker_at(self, view_pos: QPointF):
        """Return ('scale'|'poly', index) of the nearest adjustable marker within
        POINT_HIT_RADIUS of *view_pos* (viewport coords), else None."""
        pts = self._calib_points if self._calibrating else self._poly_points
        kind = "scale" if self._calibrating else "poly"
        best = None
        best_d = self.POINT_HIT_RADIUS
        for i, p in enumerate(pts):
            vp = self.mapFromScene(p)
            d = (QPointF(vp) - view_pos).manhattanLength()
            if d <= best_d:
                best_d = d
                best = (kind, i)
        return best

    # -- drawing ------------------------------------------------------------

    def _redraw_calibration(self) -> None:
        self._remove_items(self._calib_items)
        if not self._overlays_visible:
            return   # review overlay hidden (Milestone 13); picked points are kept
        pts = self._calib_points
        if len(pts) == 2:
            pen = QPen(_MARK_COLOR)
            pen.setWidth(2)
            pen.setCosmetic(True)
            line = QGraphicsLineItem(pts[0].x(), pts[0].y(), pts[1].x(), pts[1].y())
            line.setPen(pen)
            self._scene.addItem(line)
            self._calib_items.append(line)
        for i, p in enumerate(pts):
            active = self._active == ("scale", i)
            self._add_marker(self._calib_items, p, str(i + 1), _MARK_COLOR,
                             filled=False, active=active)

    def _redraw_polygon(self) -> None:
        self._remove_items(self._poly_items)
        if not self._overlays_visible or self._active_overlay_hidden():
            return   # review overlay hidden (Milestone 13); geometry is untouched
        pts = self._poly_points
        color = self._active_color
        edge = QPen(color)
        edge.setWidth(3)          # the active boundary is drawn a touch thicker
        edge.setCosmetic(True)
        for a, b in zip(pts, pts[1:]):
            line = QGraphicsLineItem(a.x(), a.y(), b.x(), b.y())
            line.setPen(edge)
            line.setZValue(5)
            self._scene.addItem(line)
            self._poly_items.append(line)
        if self._poly_closed and len(pts) >= 3:
            closing = QPen(color)
            closing.setWidth(3)
            closing.setCosmetic(True)
            closing.setStyle(Qt.PenStyle.DashLine)
            line = QGraphicsLineItem(pts[-1].x(), pts[-1].y(), pts[0].x(), pts[0].y())
            line.setPen(closing)
            line.setZValue(5)
            self._scene.addItem(line)
            self._poly_items.append(line)
        # Missing-corner warnings (M17): overlay flagged edges in dashed red so the
        # suspect run stands out for review. Advisory only — the boundary is unchanged.
        if self._edge_warnings:
            by_index = {i: (a, b) for i, a, b in self._edge_list()}
            warn_pen = QPen(_WARN_COLOR)
            warn_pen.setWidth(5)
            warn_pen.setCosmetic(True)
            warn_pen.setStyle(Qt.PenStyle.DashLine)
            for idx in self._edge_warnings:
                if idx not in by_index:
                    continue
                a, b = by_index[idx]
                line = QGraphicsLineItem(a.x(), a.y(), b.x(), b.y())
                line.setPen(warn_pen)
                line.setZValue(6)   # above the boundary edges (5), below markers (10)
                self._scene.addItem(line)
                self._poly_items.append(line)
        for i, p in enumerate(pts):
            active = self._active == ("poly", i)
            self._add_marker(self._poly_items, p, str(i + 1), color,
                             filled=True, active=active)
        self._apply_opacity(self._poly_items)

    def set_edge_warnings(self, edge_indices) -> None:
        """Highlight edges flagged as possibly skipping a corner (Milestone 17).
        Pass an empty iterable to clear. Advisory overlay only; never edits points."""
        self._edge_warnings = {int(i) for i in edge_indices}
        self._redraw_polygon()

    def edge_warnings(self) -> set[int]:
        return set(self._edge_warnings)

    def _redraw_active(self) -> None:
        """Refresh only the highlight after selection changes (cheap enough to
        just rebuild the relevant layer)."""
        if self._calibrating:
            self._redraw_calibration()
        else:
            self._redraw_polygon()

    def _add_marker(self, bucket: list, scene_pt: QPointF, number: str,
                    color: QColor, *, filled: bool, active: bool) -> None:
        r = 5.0 if filled else 6.0
        pen = QPen(color)
        pen.setWidth(2)
        pen.setCosmetic(True)
        dot = QGraphicsEllipseItem(-r, -r, 2 * r, 2 * r)
        dot.setPen(pen)
        if filled:
            dot.setBrush(QBrush(color))
        dot.setPos(scene_pt)
        dot.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        dot.setZValue(10)
        self._scene.addItem(dot)
        bucket.append(dot)

        if active:
            # A white ring marks the point the arrow keys will nudge.
            rr = r + 3
            ring = QGraphicsEllipseItem(-rr, -rr, 2 * rr, 2 * rr)
            hpen = QPen(_ACTIVE_HALO)
            hpen.setWidth(2)
            hpen.setCosmetic(True)
            ring.setPen(hpen)
            ring.setPos(scene_pt)
            ring.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
            ring.setZValue(9)
            self._scene.addItem(ring)
            bucket.append(ring)

        label = QGraphicsSimpleTextItem(number)
        label.setBrush(color)
        label.setPos(scene_pt)
        label.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        label.moveBy(r + 3, -(r + 13))
        label.setZValue(10)
        self._scene.addItem(label)
        bucket.append(label)

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:
        super().drawForeground(painter, rect)
        if not (self._overlays_visible and self._crosshair_enabled and self.is_picking()
                and self._cursor_vp_pos is not None):
            return
        x, y = self._cursor_vp_pos.x(), self._cursor_vp_pos.y()
        w, h = self.viewport().width(), self.viewport().height()
        painter.save()
        painter.resetTransform()  # draw in viewport pixels: fixed on-screen size
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        # Light halo first (so the line reads on dark map areas), dark core on top.
        halo = QPen(QColor(255, 255, 255, 200))
        halo.setWidth(3)
        painter.setPen(halo)
        painter.drawLine(0, y, w, y)
        painter.drawLine(x, 0, x, h)
        core = QPen(QColor(20, 20, 20))
        core.setWidth(1)
        painter.setPen(core)
        painter.drawLine(0, y, w, y)
        painter.drawLine(x, 0, x, h)
        painter.restore()

    # -- helpers ------------------------------------------------------------

    def _remove_items(self, bucket: list) -> None:
        for item in bucket:
            if item.scene() is self._scene:
                self._scene.removeItem(item)
        bucket.clear()

    def _reset_calibration_state(self) -> None:
        self._remove_items(self._calib_items)
        self._calib_points.clear()
        self._calibrating = False
        if self._active is not None and self._active[0] == "scale":
            self._active = None

    def _reset_calibration_mode(self) -> None:
        """Leave calibration mode and drop its in-progress points (used when
        switching to polygon mode)."""
        self._reset_calibration_state()

    def _reset_polygon_state(self) -> None:
        self._remove_items(self._poly_items)
        self._poly_points.clear()
        self._poly_closed = False
        self._tracing = False
        if self._active is not None and self._active[0] == "poly":
            self._active = None

    def _reset_polygon_mode(self) -> None:
        """Leave tracing mode without discarding the polygon (used when switching
        to scale mode — the boundary should survive)."""
        self._tracing = False

    def _update_cursor(self) -> None:
        if self._pixmap_item is None:
            self.unsetCursor()
        elif self.is_picking():
            # Hide the OS cursor when the drawn crosshair is showing, else a cross.
            self.setCursor(Qt.CursorShape.BlankCursor if self._crosshair_enabled
                           else Qt.CursorShape.CrossCursor)
        elif self._selecting or self._segmenting or self._locating or self._assisting:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
