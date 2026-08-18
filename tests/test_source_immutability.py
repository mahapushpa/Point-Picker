"""Source-file immutability guard (PROJECT_BRIEF "Offline & storage constraints").

Once a file is copied into ``sources/`` it is the canonical original and must
never be modified in place by anything the app does. This test drives the full
user flow — open a document into a project, enhance it (M8 preview), trace a
parcel, and let the project autosave — then asserts the imported source file's
bytes AND modification time are unchanged, and that the enhancement lives only in
memory (no ``*_enhanced.*`` or other derived file appears in ``sources/``).

Runs offscreen; skipped if PySide6/Pillow are unavailable.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    import numpy as np
    from PySide6.QtWidgets import QApplication, QMessageBox, QInputDialog
    from PySide6.QtCore import QPointF
    from PIL import Image
    from src.ui.main_window import MainWindow
    from src.core.project_db import ProjectDB
    _HAVE_QT = True
except Exception:  # pragma: no cover
    _HAVE_QT = False

_app = None


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])
        QMessageBox.information = staticmethod(lambda *a, **k: None)
        QMessageBox.warning = staticmethod(lambda *a, **k: None)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipUnless(_HAVE_QT, "PySide6/Pillow not available")
class SourceImmutabilityTests(unittest.TestCase):
    def test_source_file_unchanged_after_open_enhance_trace_save(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)

        # A real scan placed OUTSIDE the project, then opened into it.
        yy, xx = np.mgrid[0:200, 0:260]
        g = np.clip(128 + (np.sin(xx / 8.0) + np.cos(yy / 10.0)) * 10, 0, 255).astype(np.uint8)
        external = root / "original_sheet.png"
        Image.fromarray(g, mode="L").convert("RGB").save(external)
        external_sha = _sha256(external)

        proj_dir = root / "proj"
        proj_dir.mkdir()

        w = MainWindow()
        self.addCleanup(w.close)
        w._set_project(ProjectDB.create(str(proj_dir)))
        w.load_path(str(external))     # copies the file into sources/

        # Locate the canonical copy inside sources/ and fingerprint it.
        srcs = w._project.list_sources()
        self.assertEqual(len(srcs), 1)
        copied = w._project.resolve(srcs[0]["relative_path"])
        self.assertTrue(copied.is_file())
        self.assertEqual(copied.parent.name, "sources")
        sha_before = _sha256(copied)
        mtime_before = copied.stat().st_mtime_ns
        listing_before = sorted(p.name for p in copied.parent.iterdir())

        # Enhance (M8 preview), trace a parcel, autosave happens on polygon change.
        w.set_preprocess_enabled(True)
        QInputDialog.getDouble = staticmethod(lambda *a, **k: (10.0, True))
        w.begin_scale_calibration()
        w.canvas._place_point(QPointF(40, 40))
        w.canvas._place_point(QPointF(140, 40))
        w.canvas.confirm_pick()

        w.new_parcel()
        for x, y in [(20, 20), (120, 20), (120, 120), (20, 120)]:
            w.canvas._place_point(QPointF(x, y))
        w.close_polygon()
        w.set_preprocess_enabled(False)  # toggle back for good measure

        # The imported source is byte-for-byte and mtime identical...
        self.assertEqual(_sha256(copied), sha_before, "sources/ copy bytes changed")
        self.assertEqual(copied.stat().st_mtime_ns, mtime_before, "sources/ copy mtime changed")
        # ...and the enhancement created no derived file in sources/.
        self.assertEqual(sorted(p.name for p in copied.parent.iterdir()), listing_before,
                         "a derived/extra file appeared in sources/")
        # The external original is likewise untouched.
        self.assertEqual(_sha256(external), external_sha, "external original changed")

        # And the traced work really did persist (so 'save' genuinely happened).
        parcels = w._project.list_parcels(srcs[0]["id"])
        self.assertEqual(len(parcels), 1)
        self.assertEqual(parcels[0]["point_count"], 4)


if __name__ == "__main__":
    unittest.main()
