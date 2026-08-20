"""main.py — command-line entry point.

Preferred invocation is ``python -m src.main`` from the repository root;
``python src/main.py`` also works (this module repairs sys.path so the ``src``
package resolves either way — necessary because ``src.io`` must be imported
qualified, as a bare top-level ``io`` is shadowed by the stdlib).

**The GUI is the primary interface** — running with no subcommand launches it
directly. The actual measurement work lives there, and it has grown well beyond
a viewer into a full application: a menu bar and toolbars; dockable Parcels /
Boundary-segment / Location panels; several interaction modes (Scale, Trace,
Select, Segment, Locate, Assist); and dialogs for owner / boundary reports,
area-unit profiles, and identification templates — including File > New Project
to create a portable project folder. None of that is discoverable from a CLI
usage string, so the CLI keeps only two diagnostic commands.

Commands:
    (none)     launch the desktop application — the default action
    gui        the same, but can also open a file on startup
    info       print a project's schema version, unit profiles, and sources
    selfcheck  portability self-test (build, copy elsewhere, reopen intact) —
               the pen-drive check from Milestone 1

Usage:
    python -m src.main                       # launch the GUI (primary use)
    python -m src.main gui [<file>]          # GUI, optionally opening a file
    python -m src.main info   <folder>       # diagnostics
    python -m src.main selfcheck [<scratch-folder>]
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

# Make the `src` package importable whether launched as `python -m src.main`
# (already fine) or `python src/main.py` (repo root not yet on the path).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.core.project_db import ProjectDB, ProjectError, SCHEMA_VERSION  # noqa: E402


def cmd_info(args) -> int:
    try:
        proj = ProjectDB.open(args.folder)
    except ProjectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    with proj:
        print(f"Project : {proj.get_meta('project_name')}")
        print(f"Root    : {proj.root}")
        print(f"Created : {proj.get_meta('created_at')}")
        print(f"Schema  : v{proj.schema_version}")
        print(f"Tables  : {', '.join(proj.table_names())}")
        print("Unit profiles:")
        for u in proj.list_unit_profiles():
            tag = "built-in" if u["is_builtin"] else "custom"
            print(f"  - {u['name']:<14} 1 unit = {u['sq_m_per_unit']} m^2  ({tag})")
        sources = proj.list_sources()
        print(f"Sources : {len(sources)}")
        for s in sources:
            print(f"  - [{s['file_type']}] {s['relative_path']}")
    return 0


def cmd_selfcheck(args) -> int:
    """Prove portability: build a project, copy the whole folder to a second
    location (standing in for a pen drive / different machine path), open it
    from there, and confirm the data and relative source path survive intact.
    Nothing absolute is stored, so opening from any path must work."""
    scratch = Path(args.scratch) if args.scratch else Path(tempfile.mkdtemp(prefix="lmt_selfcheck_"))
    made_temp = args.scratch is None
    original = scratch / "original-project"
    moved = scratch / "usb" / "copied-project"
    ok = True
    try:
        # Build an original project with a real (copied) source file.
        proj = ProjectDB.create(original, name="Self-check project")
        sample = scratch / "sample_sheet.png"
        sample.write_bytes(b"\x89PNG\r\n\x1a\n fake image bytes for the portability test")
        with proj:
            sid = proj.import_source(sample, "image", doc_date="2026-08-18")
            proj.set_meta("checkpoint", "written-before-copy")
        print(f"[1] created project at {original} (source id {sid})")

        # Copy the ENTIRE folder elsewhere, simulating a move to a pen drive.
        moved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(original, moved)
        print(f"[2] copied whole folder to {moved}")

        # Open strictly from the new path and verify everything came across.
        with ProjectDB.open(moved) as p2:
            assert p2.schema_version == SCHEMA_VERSION, "schema version mismatch after copy"
            assert p2.get_meta("checkpoint") == "written-before-copy", "meta lost in copy"
            srcs = p2.list_sources()
            assert len(srcs) == 1, "source row lost in copy"
            rel = srcs[0]["relative_path"]
            assert rel == "sources/sample_sheet.png", f"unexpected relative path {rel!r}"
            resolved = p2.resolve(rel)
            assert resolved.is_file(), f"source file missing at new location: {resolved}"
            assert resolved.parent.parent == moved.resolve(), "resolved path not under new root"
        print(f"[3] opened from new path; source resolves to {resolved}")

        # Guard the core promise: no absolute path text is stored in the DB.
        leaked = _scan_for_absolute_paths(moved, original)
        assert not leaked, f"absolute path leaked into DB: {leaked}"
        print("[4] no absolute paths stored in project.db")
        print("PORTABILITY SELF-CHECK: PASS")
    except AssertionError as exc:
        ok = False
        print(f"PORTABILITY SELF-CHECK: FAIL - {exc}", file=sys.stderr)
    finally:
        if made_temp:
            shutil.rmtree(scratch, ignore_errors=True)
    return 0 if ok else 1


def cmd_gui(args) -> int:
    """Launch the desktop application. Imported lazily so the CLI and the
    pure-Python tests never require PySide6 to be installed."""
    from src.ui.main_window import run
    return run(file=args.file)


def _scan_for_absolute_paths(project_root: Path, original_root: Path) -> str | None:
    """Return the first stored value that looks like an absolute path from the
    original location, or None if clean. A portable DB stores none."""
    import sqlite3

    needles = {str(original_root).lower(), str(original_root.resolve()).lower(),
               str(project_root).lower()}
    conn = sqlite3.connect(str(project_root / "project.db"))
    try:
        conn.row_factory = sqlite3.Row
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            for row in conn.execute(f"SELECT * FROM {t}"):
                for val in tuple(row):
                    if isinstance(val, str):
                        low = val.lower()
                        if any(n in low for n in needles) or (":\\" in low) or low.startswith("/"):
                            return f"{t}: {val!r}"
    finally:
        conn.close()
    return None


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="land-measure-tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Land Parcel Measurement & Revenue Records Tool — a fully "
                    "offline desktop app for tracing land-parcel boundaries "
                    "from scanned/PDF/DXF surveys and computing area, perimeter, "
                    "and segment lengths in real units.",
        epilog="With no command this launches the GUI — the primary interface, "
               "where all the measurement work happens (Scale / Trace / Select / "
               "Segment / Locate / Assist modes, reports, and File > New Project). "
               "`info` and `selfcheck` are diagnostic helpers for the portable "
               "project folder; explore everything else from the GUI's menus and "
               "toolbars.")
    # No subcommand is valid: it means "launch the GUI" (handled in main()).
    sub = ap.add_subparsers(dest="command")

    g = sub.add_parser("gui", help="launch the desktop application (the default action)")
    g.add_argument("file", nargs="?", help="optional PDF / image / DXF to open on startup")
    g.set_defaults(func=cmd_gui)

    i = sub.add_parser("info", help="show a project's schema and contents")
    i.add_argument("folder")
    i.set_defaults(func=cmd_info)

    s = sub.add_parser("selfcheck", help="run the portability self-check (local-vs-copied)")
    s.add_argument("scratch", nargs="?", help="scratch folder to use (default: a temp dir)")
    s.set_defaults(func=cmd_selfcheck)

    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # The GUI is the primary interface: no subcommand launches it directly
    # (so `python -m src.main`, or VS Code's "Run Python File", opens the app
    # rather than erroring on a missing command).
    if getattr(args, "func", None) is None:
        return cmd_gui(argparse.Namespace(file=None))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
