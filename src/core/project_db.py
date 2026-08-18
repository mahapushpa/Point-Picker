"""project_db.py — SQLite schema and read/write for one project folder.

Pure Python, standard library only. **No PySide6 or any UI-framework imports.**
This module is the whole of Milestone 1: it can create and open a portable
project folder and prove its SQLite schema works, with nothing region-specific
or GUI-specific baked in.

A project is one self-contained folder (see PROJECT_BRIEF.md "Offline & storage
constraints")::

    some-parcel-project/
    |-- project.db        single SQLite file (this module owns its schema)
    |-- sources/          original PDFs / images / DXFs, referenced by
    |                     *relative* path from project.db (never DB blobs)
    |-- exports/          generated PDF / CSV / JSON summaries

Portability is the point: nothing absolute is ever stored inside the .db. The
file is opened by whatever absolute path the folder happens to live at right
now (local disk, a copied folder, or a mounted pen drive); every path the DB
stores is relative to the project root and kept in POSIX form so it survives a
move between machines and operating systems.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import shutil

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

#: Bump this whenever the DDL below changes. Stored in SQLite's built-in
#: ``PRAGMA user_version``. Every change so far is purely *additive* — new
#: tables (via ``CREATE TABLE IF NOT EXISTS``) and new columns (added with
#: guarded ``ALTER TABLE ADD COLUMN``, see ``_ADDITIVE_COLUMNS``) — so opening
#: an older file just applies the missing pieces and updates the version, with
#: no data migration and no breaking change. v2 added ``source_scales``;
#: v3 added ``parcels.closed``.
SCHEMA_VERSION = 3

DB_FILENAME = "project.db"
SOURCES_DIRNAME = "sources"
EXPORTS_DIRNAME = "exports"

#: Recognised source-file categories. Loaders arrive in Milestone 2; the schema
#: only needs to record which kind a file is.
SOURCE_TYPES = ("pdf", "dxf", "image")

# The schema is deliberately forward-compatible with later milestones so that a
# project created today does not need a migration when point-marking (M4), unit
# profiles (M5) and identification templates (M6) land. Nothing here is logic;
# it is only the shape of the store.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Source documents. Stored by RELATIVE path (POSIX form) from the project
-- root, never as a binary blob, so the file stays directly openable and the
-- project stays portable.
CREATE TABLE IF NOT EXISTS sources (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    relative_path TEXT    NOT NULL UNIQUE,   -- e.g. 'sources/sheet1.pdf'
    file_type     TEXT    NOT NULL,          -- one of SOURCE_TYPES
    original_name TEXT,                      -- original filename as imported
    page          INTEGER,                   -- page index for multi-page PDFs
    doc_date      TEXT,                       -- date on the source document
    added_at      TEXT    NOT NULL           -- ISO-8601 UTC
);

-- Local area-unit profiles. SI is canonical; every profile is just a factor to
-- square metres. sq m / sq ft / acre / hectare are seeded as built-ins; Bigha,
-- Biswa and any regional measure are user-added (Milestone 5).
CREATE TABLE IF NOT EXISTS unit_profiles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL UNIQUE,
    sq_m_per_unit REAL    NOT NULL,          -- 1 unit == this many square metres
    is_builtin    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL
);

-- One traced land parcel.
CREATE TABLE IF NOT EXISTS parcels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT,                                 -- optional human label
    source_id       INTEGER REFERENCES sources(id)       ON DELETE SET NULL,
    land_type       TEXT,                                 -- template key started from
    scale_m_per_px  REAL,                                 -- established scale (metres/pixel)
    scale_method    TEXT,                                 -- 'two-point' | 'metadata' | ...
    scale_note      TEXT,                                 -- confidence / cross-check note
    unit_profile_id INTEGER REFERENCES unit_profiles(id) ON DELETE SET NULL,
    notes           TEXT,                                 -- always-present free text
    closed          INTEGER NOT NULL DEFAULT 0,           -- 1 if the boundary is closed
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

-- Identification / revenue-record metadata as {label, value} pairs, so field
-- names are never hardcoded and a parcel can carry any number of identifiers
-- and address levels (PROJECT_BRIEF.md "Land identification & revenue-record
-- fields").
CREATE TABLE IF NOT EXISTS parcel_fields (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    parcel_id INTEGER NOT NULL REFERENCES parcels(id) ON DELETE CASCADE,
    seq       INTEGER NOT NULL,                          -- display order
    label     TEXT    NOT NULL,
    value     TEXT
);

-- Ordered boundary points forming a closed polygon. lat/lon are present from
-- day one (Phase 2 GPS capture) but unused today.
CREATE TABLE IF NOT EXISTS points (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    parcel_id INTEGER NOT NULL REFERENCES parcels(id) ON DELETE CASCADE,
    seq       INTEGER NOT NULL,                          -- order around the boundary
    label     TEXT,
    pixel_x   REAL    NOT NULL,
    pixel_y   REAL    NOT NULL,
    local_x   REAL,                                      -- real-world metres (X)
    local_y   REAL,                                      -- real-world metres (Y)
    lat       REAL,                                      -- optional, Phase 2
    lon       REAL
);

CREATE INDEX IF NOT EXISTS idx_points_parcel ON points(parcel_id, seq);
CREATE INDEX IF NOT EXISTS idx_fields_parcel ON parcel_fields(parcel_id, seq);

-- Established real-world scale for a source file (Milestone 3). One row per
-- source (latest calibration wins). SI-canonical: metres per rendered pixel.
-- The two calibration points and the entered distance are kept so the scale
-- can be redone or cross-checked, and `method` records how it was derived
-- ('two-point' for now; metadata / reference-content methods come later).
CREATE TABLE IF NOT EXISTS source_scales (
    source_id        INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    metres_per_pixel REAL    NOT NULL,
    method           TEXT    NOT NULL,
    p1x REAL, p1y REAL, p2x REAL, p2y REAL,   -- calibration points (pixels)
    real_distance_m  REAL,                     -- the entered real-world distance
    note             TEXT,                      -- confidence / cross-check note
    updated_at       TEXT    NOT NULL
);
"""

#: Seeded on project creation. Universally correct, region-neutral units only.
BUILTIN_UNIT_PROFILES = (
    ("square metre", 1.0),
    ("square foot", 0.09290304),
    ("acre", 4046.8564224),
    ("hectare", 10000.0),
)

#: Columns added to existing tables after their first release. Applied
#: additively via guarded ``ALTER TABLE ADD COLUMN`` (CREATE TABLE IF NOT EXISTS
#: cannot add a column to a table that already exists). Each entry is
#: ``(table, column, column_definition)``; the definition must carry a DEFAULT so
#: existing rows get a value. v3 added parcels.closed.
_ADDITIVE_COLUMNS = (
    ("parcels", "closed", "INTEGER NOT NULL DEFAULT 0"),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    """Timezone-aware ISO-8601 timestamp in UTC (no machine-local assumptions)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ProjectError(Exception):
    """Raised for project create/open problems (missing folder, wrong schema...)."""


# ---------------------------------------------------------------------------
# ProjectDB
# ---------------------------------------------------------------------------

class ProjectDB:
    """A handle to one project folder and its ``project.db``.

    Construct with :meth:`create` (new folder) or :meth:`open` (existing one),
    or use as a context manager. All paths exposed are absolute and derived
    from *root* at runtime; nothing absolute is ever written into the database.
    """

    def __init__(self, root: Path, conn: sqlite3.Connection):
        self.root = root
        self.conn = conn

    # -- construction -------------------------------------------------------

    @classmethod
    def create(cls, root, name: str | None = None, exist_ok: bool = False) -> "ProjectDB":
        """Create a new project folder (with ``sources/`` and ``exports/``),
        initialise ``project.db`` with the schema, and seed built-in units.

        Raises :class:`ProjectError` if the folder already contains a project
        and *exist_ok* is False.
        """
        root = Path(root).resolve()
        db_path = root / DB_FILENAME
        if db_path.exists() and not exist_ok:
            raise ProjectError(f"A project already exists at {root} (found {DB_FILENAME}).")

        root.mkdir(parents=True, exist_ok=True)
        (root / SOURCES_DIRNAME).mkdir(exist_ok=True)
        (root / EXPORTS_DIRNAME).mkdir(exist_ok=True)

        conn = _connect(db_path)
        try:
            conn.executescript(SCHEMA_SQL)
            _ensure_additive_columns(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            now = _utcnow()
            meta = {
                "schema_version": str(SCHEMA_VERSION),
                "project_name": name or root.name,
                "created_at": now,
                "app": "land-measure-tool",
            }
            conn.executemany(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                list(meta.items()),
            )
            for uname, factor in BUILTIN_UNIT_PROFILES:
                conn.execute(
                    "INSERT OR IGNORE INTO unit_profiles "
                    "(name, sq_m_per_unit, is_builtin, created_at) VALUES (?, ?, 1, ?)",
                    (uname, factor, now),
                )
            conn.commit()
        except Exception:
            conn.close()
            raise
        return cls(root, conn)

    @classmethod
    def open(cls, root) -> "ProjectDB":
        """Open an existing project folder.

        A file written by a *newer* code version than this one is rejected
        loudly. A file from an *older* version is upgraded in place: because
        every schema change is additive (new tables and new columns only),
        re-running the idempotent schema script and adding any missing columns
        brings the file up to date without touching existing data, and the
        stored version is bumped to match.
        """
        root = Path(root).resolve()
        db_path = root / DB_FILENAME
        if not db_path.exists():
            raise ProjectError(f"No {DB_FILENAME} found in {root}.")

        conn = _connect(db_path)
        found = conn.execute("PRAGMA user_version").fetchone()[0]
        if found > SCHEMA_VERSION:
            conn.close()
            raise ProjectError(
                f"Project schema version {found} is newer than this app supports "
                f"(version {SCHEMA_VERSION}); update the application to open it."
            )
        if found < SCHEMA_VERSION:
            # Additive-only upgrade: new tables (v2 source_scales) and new
            # columns (v3 parcels.closed).
            conn.executescript(SCHEMA_SQL)
            _ensure_additive_columns(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()
        # Ensure the runtime folders exist even if the folder was hand-copied.
        (root / SOURCES_DIRNAME).mkdir(exist_ok=True)
        (root / EXPORTS_DIRNAME).mkdir(exist_ok=True)
        return cls(root, conn)

    # -- paths --------------------------------------------------------------

    @property
    def db_path(self) -> Path:
        return self.root / DB_FILENAME

    @property
    def sources_dir(self) -> Path:
        return self.root / SOURCES_DIRNAME

    @property
    def exports_dir(self) -> Path:
        return self.root / EXPORTS_DIRNAME

    def resolve(self, relative_path: str) -> Path:
        """Turn a stored relative path into an absolute path under this root."""
        return self.root / Path(*PurePosixPath(relative_path).parts)

    # -- meta ---------------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )
        self.conn.commit()

    @property
    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    # -- sources ------------------------------------------------------------

    def import_source(self, src_file, file_type: str | None = None,
                      *, doc_date: str | None = None, page: int | None = None) -> int:
        """Copy *src_file* into ``sources/`` and register it by relative path.

        Returns the new source id. The file is copied (kept as a file, never a
        blob) and only its project-relative path is stored, so the project
        stays portable.
        """
        src_file = Path(src_file)
        if not src_file.is_file():
            raise ProjectError(f"Source file not found: {src_file}")
        if file_type is None:
            file_type = _guess_file_type(src_file)
        if file_type not in SOURCE_TYPES:
            raise ProjectError(f"Unsupported source type {file_type!r}; expected one of {SOURCE_TYPES}.")

        dest = self.sources_dir / src_file.name
        if dest.exists():
            raise ProjectError(f"A source named {src_file.name!r} already exists in this project.")
        shutil.copy2(src_file, dest)
        rel = PurePosixPath(SOURCES_DIRNAME) / src_file.name
        return self.register_source(str(rel), file_type,
                                    original_name=src_file.name, doc_date=doc_date, page=page)

    def register_source(self, relative_path: str, file_type: str, *,
                        original_name: str | None = None,
                        doc_date: str | None = None, page: int | None = None) -> int:
        """Record a source that already lives inside the project by its
        project-relative path (POSIX form). Used by :meth:`import_source`, or
        directly for files placed in ``sources/`` by other means."""
        if file_type not in SOURCE_TYPES:
            raise ProjectError(f"Unsupported source type {file_type!r}; expected one of {SOURCE_TYPES}.")
        rel = PurePosixPath(relative_path).as_posix()
        cur = self.conn.execute(
            "INSERT INTO sources (relative_path, file_type, original_name, page, doc_date, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (rel, file_type, original_name, page, doc_date, _utcnow()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_sources(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, relative_path, file_type, original_name, page, doc_date, added_at "
            "FROM sources ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_source_by_relative_path(self, relative_path: str) -> dict | None:
        rel = PurePosixPath(relative_path).as_posix()
        row = self.conn.execute(
            "SELECT id, relative_path, file_type, original_name, page, doc_date, added_at "
            "FROM sources WHERE relative_path = ?", (rel,)
        ).fetchone()
        return dict(row) if row else None

    def import_or_get_source(self, src_file, file_type: str | None = None,
                            *, doc_date: str | None = None,
                            page: int | None = None) -> tuple[int, bool]:
        """Register *src_file* into the project, or return the existing row if a
        source with the same relative path (``sources/<name>``) is already
        registered. Returns ``(source_id, already_existed)``. Idempotent so a
        file can be re-opened / re-attached without duplicating or re-copying.
        """
        src_file = Path(src_file)
        rel = PurePosixPath(SOURCES_DIRNAME) / src_file.name
        existing = self.get_source_by_relative_path(str(rel))
        if existing is not None:
            # If the copy is missing (e.g. sources/ was cleared), restore it.
            dest = self.resolve(existing["relative_path"])
            if not dest.exists() and src_file.is_file():
                shutil.copy2(src_file, dest)
            return existing["id"], True
        return self.import_source(src_file, file_type, doc_date=doc_date, page=page), False

    # -- source scale (Milestone 3) -----------------------------------------

    def set_source_scale(self, source_id: int, metres_per_pixel: float, *,
                        method: str = "two-point",
                        p1: tuple[float, float] | None = None,
                        p2: tuple[float, float] | None = None,
                        real_distance_m: float | None = None,
                        note: str | None = None) -> None:
        """Store (or replace) the established scale for a source, SI-canonical
        as metres per rendered pixel. One row per source; re-calibrating simply
        overwrites it."""
        if metres_per_pixel <= 0:
            raise ProjectError(f"metres_per_pixel must be positive, got {metres_per_pixel}")
        if self.conn.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone() is None:
            raise ProjectError(f"No source with id {source_id} in this project.")
        p1x, p1y = (p1 if p1 is not None else (None, None))
        p2x, p2y = (p2 if p2 is not None else (None, None))
        self.conn.execute(
            "INSERT OR REPLACE INTO source_scales "
            "(source_id, metres_per_pixel, method, p1x, p1y, p2x, p2y, real_distance_m, note, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source_id, float(metres_per_pixel), method, p1x, p1y, p2x, p2y,
             real_distance_m, note, _utcnow()),
        )
        self.conn.commit()

    def get_source_scale(self, source_id: int) -> dict | None:
        """Return the stored scale for a source as a dict, or None if unset."""
        row = self.conn.execute(
            "SELECT source_id, metres_per_pixel, method, p1x, p1y, p2x, p2y, "
            "real_distance_m, note, updated_at FROM source_scales WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        return dict(row) if row else None

    def clear_source_scale(self, source_id: int) -> None:
        """Remove any stored scale for a source (used when re-calibrating)."""
        self.conn.execute("DELETE FROM source_scales WHERE source_id = ?", (source_id,))
        self.conn.commit()

    # -- parcels & polygon points (Milestone 4) -----------------------------
    #
    # Milestone 4 keeps one parcel (one traced boundary) per source. The parcel
    # is created lazily the first time a polygon is saved. Points are stored in
    # boundary order; pixel coordinates are canonical, and local_x/local_y are
    # populated in SI when a scale is available so the DB always carries the
    # metric coordinates too (refreshed whenever the polygon or scale changes).

    def get_parcel_for_source(self, source_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT id, name, source_id, scale_m_per_px, notes, closed, created_at, updated_at "
            "FROM parcels WHERE source_id = ? ORDER BY id LIMIT 1", (source_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_or_create_parcel_for_source(self, source_id: int, name: str | None = None) -> int:
        existing = self.get_parcel_for_source(source_id)
        if existing is not None:
            return existing["id"]
        if self.conn.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone() is None:
            raise ProjectError(f"No source with id {source_id} in this project.")
        now = _utcnow()
        cur = self.conn.execute(
            "INSERT INTO parcels (name, source_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, source_id, now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def save_polygon(self, source_id: int, pixel_points: list[tuple[float, float]],
                    *, closed: bool = False, metres_per_pixel: float | None = None,
                    name: str | None = None) -> int:
        """Replace the traced boundary for *source_id* with *pixel_points* (in
        order) and record its *closed* state. Returns the parcel id. Local (SI)
        coordinates are stored when a scale is given. Passing an empty list
        clears the boundary but keeps the parcel row."""
        parcel_id = self.get_or_create_parcel_for_source(source_id, name=name)
        self.conn.execute("DELETE FROM points WHERE parcel_id = ?", (parcel_id,))
        for i, (px, py) in enumerate(pixel_points):
            lx = ly = None
            if metres_per_pixel is not None:
                lx, ly = px * metres_per_pixel, py * metres_per_pixel
            self.conn.execute(
                "INSERT INTO points (parcel_id, seq, label, pixel_x, pixel_y, local_x, local_y) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (parcel_id, i, str(i + 1), float(px), float(py), lx, ly),
            )
        self.conn.execute(
            "UPDATE parcels SET updated_at = ?, closed = ? WHERE id = ?",
            (_utcnow(), 1 if closed else 0, parcel_id),
        )
        self.conn.commit()
        return parcel_id

    def get_parcel_points(self, parcel_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT seq, label, pixel_x, pixel_y, local_x, local_y, lat, lon "
            "FROM points WHERE parcel_id = ? ORDER BY seq", (parcel_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_polygon(self, source_id: int) -> list[tuple[float, float]]:
        """Return the source's boundary as ordered (pixel_x, pixel_y) tuples,
        or an empty list if none is stored."""
        parcel = self.get_parcel_for_source(source_id)
        if parcel is None:
            return []
        return [(p["pixel_x"], p["pixel_y"]) for p in self.get_parcel_points(parcel["id"])]

    def get_polygon_closed(self, source_id: int) -> bool:
        """Return the stored closed/open state of the source's boundary. False
        when there is no parcel yet — this is the *saved* state, not inferred
        from the point count, so an open 3+-point boundary reloads as open."""
        parcel = self.get_parcel_for_source(source_id)
        return bool(parcel["closed"]) if parcel is not None else False

    def clear_polygon(self, source_id: int) -> None:
        """Delete the stored boundary points for a source's parcel (if any) and
        reset its closed flag."""
        parcel = self.get_parcel_for_source(source_id)
        if parcel is not None:
            self.conn.execute("DELETE FROM points WHERE parcel_id = ?", (parcel["id"],))
            self.conn.execute("UPDATE parcels SET updated_at = ?, closed = 0 WHERE id = ?",
                              (_utcnow(), parcel["id"]))
            self.conn.commit()

    # -- unit profiles ------------------------------------------------------

    def add_unit_profile(self, name: str, sq_m_per_unit: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO unit_profiles (name, sq_m_per_unit, is_builtin, created_at) "
            "VALUES (?, ?, 0, ?)",
            (name, float(sq_m_per_unit), _utcnow()),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_unit_profiles(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, name, sq_m_per_unit, is_builtin, created_at FROM unit_profiles ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    # -- lifecycle ----------------------------------------------------------

    def table_names(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "ProjectDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_additive_columns(conn: sqlite3.Connection) -> None:
    """Add any columns in ``_ADDITIVE_COLUMNS`` that a pre-existing table is
    missing. Idempotent: columns already present are left untouched, so this is
    safe to run on both a freshly-created schema and an older file being
    upgraded."""
    for table, column, decl in _ADDITIVE_COLUMNS:
        present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _guess_file_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext == ".dxf":
        return "dxf"
    if ext in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"):
        return "image"
    raise ProjectError(f"Cannot infer source type from extension {ext!r}.")


def create_project(root, name: str | None = None, exist_ok: bool = False) -> ProjectDB:
    """Convenience wrapper for :meth:`ProjectDB.create`."""
    return ProjectDB.create(root, name=name, exist_ok=exist_ok)


def open_project(root) -> ProjectDB:
    """Convenience wrapper for :meth:`ProjectDB.open`."""
    return ProjectDB.open(root)
