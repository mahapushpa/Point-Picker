"""Tests for Milestone 15: DXF support — header-units scale (method 1) and
entity rendering to the neutral RasterImage contract.

The INSUNITS arithmetic is pure (src.core.scale) and always runs; reading and
rendering a real DXF needs ezdxf and is skipped when it isn't installed.
"""

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.scale import (
    DxfHeaderScale, dxf_header_scale, metres_per_unit_from_insunits,
    METHOD_DXF_HEADER,
)

try:
    import ezdxf
    from src.io.dxf_loader import (
        render_dxf, read_dxf_header_units,
        TARGET_M_PER_PX, MIN_MAX_PX, MAX_MAX_PX, UNITLESS_MAX_PX,
    )
    from src.io.raster import open_raster, RasterImage
    _HAVE_EZDXF = True
except Exception:  # pragma: no cover
    _HAVE_EZDXF = False


class InsunitsMathTests(unittest.TestCase):
    def test_known_units(self):
        self.assertAlmostEqual(metres_per_unit_from_insunits(6), 1.0)       # metres
        self.assertAlmostEqual(metres_per_unit_from_insunits(1), 0.0254)    # inches
        self.assertAlmostEqual(metres_per_unit_from_insunits(2), 0.3048)    # feet
        self.assertAlmostEqual(metres_per_unit_from_insunits(4), 0.001)     # mm

    def test_unitless_and_unknown_rejected(self):
        with self.assertRaises(ValueError):
            metres_per_unit_from_insunits(0)     # unitless
        with self.assertRaises(ValueError):
            metres_per_unit_from_insunits(999)   # unknown code

    def test_header_scale_in_metres_equals_units_per_pixel(self):
        cand = dxf_header_scale(6, 0.05)   # metres: 1 unit = 1 m
        self.assertIsInstance(cand, DxfHeaderScale)
        self.assertEqual(cand.method, METHOD_DXF_HEADER)
        self.assertAlmostEqual(cand.metres_per_pixel, 0.05)
        self.assertEqual(cand.unit_name, "metres")

    def test_header_scale_applies_unit_factor(self):
        cand = dxf_header_scale(1, 0.05)   # inches: 1 unit = 0.0254 m
        self.assertAlmostEqual(cand.metres_per_pixel, 0.0254 * 0.05)

    def test_header_scale_rejects_unitless_and_bad_upp(self):
        with self.assertRaises(ValueError):
            dxf_header_scale(0, 0.05)
        with self.assertRaises(ValueError):
            dxf_header_scale(6, 0.0)


@unittest.skipUnless(_HAVE_EZDXF, "ezdxf not available")
class DxfRenderTests(unittest.TestCase):
    def _square_dxf(self, insunits=6, side=10.0, extras=None):
        d = Path(tempfile.mkdtemp()) / "sq.dxf"
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = insunits
        msp = doc.modelspace()
        for a, b in [((0, 0), (side, 0)), ((side, 0), (side, side)),
                     ((side, side), (0, side)), ((0, side), (0, 0))]:
            msp.add_line(a, b)
        if extras:
            extras(msp)
        doc.saveas(str(d))
        return d

    def test_read_header_units(self):
        self.assertEqual(read_dxf_header_units(self._square_dxf(insunits=6)), 6)
        self.assertEqual(read_dxf_header_units(self._square_dxf(insunits=1)), 1)

    def test_render_dimensions_and_bbox_and_line_count(self):
        r = render_dxf(self._square_dxf(side=10.0))
        # A neutral RGBA RasterImage with self-consistent byte length.
        self.assertIsInstance(r.raster, RasterImage)
        self.assertEqual(r.raster.mode, "RGBA")
        self.assertEqual(len(r.raster.data), r.raster.width * r.raster.height * 4)
        self.assertGreater(r.raster.width, 0)
        self.assertGreater(r.raster.height, 0)
        # Sanity: four LINE entities, square bounding box.
        self.assertEqual(r.entity_count, 4)
        self.assertEqual(r.bbox, (0.0, 0.0, 10.0, 10.0))
        # A square drawing renders (near-)square.
        self.assertAlmostEqual(r.raster.width, r.raster.height, delta=1)

    def test_recognizable_placement(self):
        r = render_dxf(self._square_dxf(side=10.0))
        w, h, data = r.raster.width, r.raster.height, r.raster.data

        def pixel(x, y):
            i = (y * w + x) * 4
            return data[i], data[i + 1], data[i + 2]

        # The outline is drawn (some black pixels) but the interior is empty (the
        # centre stays white) — i.e. it's a box, not a filled blob.
        black = sum(1 for y in range(h) for x in range(w) if pixel(x, y) == (0, 0, 0))
        self.assertGreater(black, 0)
        self.assertEqual(pixel(w // 2, h // 2), (255, 255, 255))

    def test_y_axis_is_flipped_orientation_not_mirrored(self):
        # DXF is Y-up; raster images are Y-down. A symmetric shape (or a bbox /
        # line-count check) would pass even if the render were mirrored — which
        # would silently flip every compass bearing in an M12 boundary report from
        # a DXF source. Use a right triangle whose right-angle corner is at world
        # (0,0): its horizontal leg runs along the BOTTOM (y=0) and its vertical leg
        # along the LEFT (x=0), so a correct render puts the long horizontal line in
        # the image's bottom half and the long vertical line in its left half.
        d = Path(tempfile.mkdtemp()) / "tri.dxf"
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 6
        msp = doc.modelspace()
        for a, b in [((0, 0), (10, 0)), ((0, 0), (0, 10)), ((10, 0), (0, 10))]:
            msp.add_line(a, b)
        doc.saveas(str(d))

        r = render_dxf(d)
        w, h, data = r.raster.width, r.raster.height, r.raster.data

        def is_black(x, y):
            i = (y * w + x) * 4
            return data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 0

        row_black = [sum(1 for x in range(w) if is_black(x, y)) for y in range(h)]
        col_black = [sum(1 for y in range(h) if is_black(x, y)) for x in range(w)]
        # The full-width horizontal leg is the row with the most black pixels; the
        # full-height vertical leg is the column with the most.
        h_leg_row = max(range(h), key=lambda y: row_black[y])
        v_leg_col = max(range(w), key=lambda x: col_black[x])

        # World y=0 (bottom) must land in the image's BOTTOM half (Y flipped), not
        # the top; world x=0 (left) in the LEFT half (X not flipped).
        self.assertGreater(h_leg_row, h * 0.5,
                           "horizontal leg (world y=0) should render at the image bottom")
        self.assertLess(v_leg_col, w * 0.5,
                        "vertical leg (world x=0) should render at the image left")
        # And the far corner opposite the right angle — world (10,10), outside the
        # triangle — must be the image's TOP-RIGHT, confirming both axes at once.
        self.assertFalse(is_black(w - 1 - 2, 2))    # top-right stays blank

    def test_unsupported_entities_are_flagged_not_dropped_silently(self):
        def extras(msp):
            msp.add_circle((5, 5), 2)
            msp.add_text("K-123").set_placement((1, 1))
        r = render_dxf(self._square_dxf(extras=extras))
        self.assertEqual(r.entity_count, 4)                 # only the 4 lines drawn
        self.assertIn("CIRCLE", r.skipped_entity_types)
        self.assertIn("TEXT", r.skipped_entity_types)

    def test_lwpolyline_supported(self):
        d = Path(tempfile.mkdtemp()) / "poly.dxf"
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 6
        doc.modelspace().add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True)
        doc.saveas(str(d))
        r = render_dxf(d)
        self.assertEqual(r.entity_count, 1)
        self.assertEqual(r.bbox, (0.0, 0.0, 10.0, 10.0))

    def test_straight_polyline_has_no_curved_flag(self):
        d = Path(tempfile.mkdtemp()) / "poly.dxf"
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 6
        doc.modelspace().add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True)
        doc.saveas(str(d))
        r = render_dxf(d)
        self.assertFalse(r.has_curved_segments)

    def test_bulged_lwpolyline_flags_curved_segments(self):
        # An LWPOLYLINE is a *supported* type, so a bulged (arc) segment inside one
        # is drawn as a straight chord and would otherwise be silent. Format is
        # (x, y, start_width, end_width, bulge); a non-zero bulge = arc.
        d = Path(tempfile.mkdtemp()) / "bulge.dxf"
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 6
        doc.modelspace().add_lwpolyline(
            [(0, 0, 0, 0, 0.5), (10, 0, 0, 0, 0), (10, 10, 0, 0, 0), (0, 10, 0, 0, 0)],
            format="xyseb", close=True)
        doc.saveas(str(d))
        r = render_dxf(d)
        self.assertEqual(r.entity_count, 1)            # still drawn
        self.assertTrue(r.has_curved_segments)         # but flagged as curved

    def test_bulged_polyline_flags_curved_segments(self):
        # The old-style POLYLINE path stores bulge on each vertex.
        d = Path(tempfile.mkdtemp()) / "bulge2.dxf"
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = 6
        pl = doc.modelspace().add_polyline2d([(0, 0), (10, 0), (10, 10), (0, 10)], close=True)
        pl.vertices[0].dxf.bulge = 0.4
        doc.saveas(str(d))
        r = render_dxf(d)
        self.assertTrue(r.has_curved_segments)

    def test_no_drawable_entities_raises(self):
        d = Path(tempfile.mkdtemp()) / "empty.dxf"
        doc = ezdxf.new()
        doc.modelspace().add_circle((0, 0), 1)   # unsupported only
        doc.saveas(str(d))
        with self.assertRaises(ValueError):
            render_dxf(d)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            render_dxf(Path(tempfile.mkdtemp()) / "nope.dxf")

    def test_open_raster_dispatches_dxf(self):
        # The io<->ui contract: open_raster returns the same RasterImage the rest
        # of the pipeline consumes, unchanged, for a DXF source.
        d = self._square_dxf()
        raster = open_raster(d)
        direct = render_dxf(d).raster
        self.assertEqual((raster.width, raster.height), (direct.width, direct.height))
        self.assertEqual(raster.mode, "RGBA")

    def test_header_scale_end_to_end(self):
        # Known units (metres) + the render's units-per-pixel -> candidate mpp.
        r = render_dxf(self._square_dxf(insunits=6, side=10.0))
        cand = dxf_header_scale(r.insunits, r.units_per_pixel)
        self.assertAlmostEqual(cand.metres_per_pixel, r.units_per_pixel)   # 1 m/unit
        # Inches would scale the same render down by 0.0254.
        cand_in = dxf_header_scale(1, r.units_per_pixel)
        self.assertAlmostEqual(cand_in.metres_per_pixel, 0.0254 * r.units_per_pixel)


@unittest.skipUnless(_HAVE_EZDXF, "ezdxf not available")
class ExtentAwareResolutionTests(unittest.TestCase):
    def _square(self, side, insunits=6):
        d = Path(tempfile.mkdtemp()) / f"sq{side}.dxf"
        doc = ezdxf.new()
        doc.header["$INSUNITS"] = insunits
        msp = doc.modelspace()
        for a, b in [((0, 0), (side, 0)), ((side, 0), (side, side)),
                     ((side, side), (0, side)), ((0, side), (0, 0))]:
            msp.add_line(a, b)
        doc.saveas(str(d))
        return d

    def _longest(self, r):
        return max(r.raster.width, r.raster.height)

    def test_small_extent_renders_finer_than_target(self):
        # A 20 m plot: resolution is chosen (floored at MIN_MAX_PX) so each pixel is
        # well finer than the target, and precision is NOT flagged as limited.
        r = render_dxf(self._square(20.0, insunits=6))
        self.assertEqual(self._longest(r), MIN_MAX_PX)
        self.assertLess(r.metres_per_pixel, TARGET_M_PER_PX)   # finer than target
        self.assertFalse(r.precision_limited)

    def test_medium_extent_hits_target_below_ceiling(self):
        # A 200 m sheet needs more than the floor but less than the ceiling, so it
        # lands right around the target precision without being flagged.
        r = render_dxf(self._square(200.0, insunits=6))
        self.assertGreater(self._longest(r), MIN_MAX_PX)
        self.assertLess(self._longest(r), MAX_MAX_PX)
        self.assertLessEqual(r.metres_per_pixel, TARGET_M_PER_PX + 1e-9)
        self.assertFalse(r.precision_limited)

    def test_large_extent_capped_at_ceiling_and_flagged(self):
        # A 3 km village-scale sheet can't hit the target even at the ceiling: it
        # renders at MAX_MAX_PX and reports precision_limited so the user is warned.
        r = render_dxf(self._square(3000.0, insunits=6))
        self.assertEqual(self._longest(r), MAX_MAX_PX)
        self.assertGreater(r.metres_per_pixel, TARGET_M_PER_PX)
        self.assertTrue(r.precision_limited)

    def test_unitless_uses_fixed_default_and_is_never_flagged(self):
        # No real-world unit -> a metres target is meaningless: fixed default size,
        # no precision figure, never flagged.
        r = render_dxf(self._square(50.0, insunits=0))
        self.assertEqual(self._longest(r), UNITLESS_MAX_PX)
        self.assertIsNone(r.metres_per_pixel)
        self.assertFalse(r.precision_limited)

    def test_explicit_max_px_override_is_honoured(self):
        # Forcing max_px overrides the extent-aware logic exactly...
        r = render_dxf(self._square(10.0, insunits=6), max_px=500)
        self.assertEqual(self._longest(r), 500)
        # ...and a forced-too-small value on a large drawing is still honestly
        # reported as precision-limited.
        big = render_dxf(self._square(3000.0, insunits=6), max_px=1000)
        self.assertEqual(self._longest(big), 1000)
        self.assertTrue(big.precision_limited)


if __name__ == "__main__":
    unittest.main()
