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

from .polygon import nearest_vertex_index, SNAP_TOLERANCE_PX
from .units import BUILTIN_AREA_UNITS

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

#: Bump this whenever the DDL below changes. Stored in SQLite's built-in
#: ``PRAGMA user_version``. Every change so far is purely *additive* — new
#: tables (via ``CREATE TABLE IF NOT EXISTS``) and new columns (added with
#: guarded ``ALTER TABLE ADD COLUMN``, see ``_ADDITIVE_COLUMNS``) — so opening
#: an older file just applies the missing pieces and updates the version, with
#: no data migration and no breaking change. v2 added ``source_scales``;
#: v3 added ``parcels.closed``; v4 added ``parcels.owner``. Multiple parcels per
#: source needed no schema change (parcels.source_id already allows many rows).
#: v5 is a *restructuring* (not additive): the per-parcel ``points`` table is
#: replaced by shared ``vertices`` + ``parcel_vertices``, with a dedicated
#: rebuild migration (see ``_migrate_points_to_vertices``). v6 (Milestone 9)
#: adds ``sources.unit_profile_id`` — the local area-unit profile selected for
#: display on that source — a purely additive nullable column.
SCHEMA_VERSION = 6

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
-- Biswa and any regional measure are user-added (Milestone 9). Which profile is
-- active for a given source is recorded in sources.unit_profile_id (an additive
-- column); it is a display/export choice only and never affects stored geometry.
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
    owner           TEXT,                                 -- parcel owner (links parcels for reports)
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

-- Boundary vertices (Milestone 6: topology-aware shared boundaries). A vertex
-- is stored ONCE per source and shared by every parcel whose boundary passes
-- through it, so a shared edge is structurally guaranteed to match. Coordinates
-- live here; lat/lon are present from day one (Phase 2 GPS) but unused today.
CREATE TABLE IF NOT EXISTS vertices (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    pixel_x   REAL    NOT NULL,
    pixel_y   REAL    NOT NULL,
    local_x   REAL,                                      -- real-world metres (X)
    local_y   REAL,                                      -- real-world metres (Y)
    lat       REAL,                                      -- optional, Phase 2
    lon       REAL
);

-- A parcel's boundary as an ORDERED list of vertex references (not raw coords).
CREATE TABLE IF NOT EXISTS parcel_vertices (
    parcel_id INTEGER NOT NULL REFERENCES parcels(id)  ON DELETE CASCADE,
    seq       INTEGER NOT NULL,                          -- order around the boundary
    vertex_id INTEGER NOT NULL REFERENCES vertices(id) ON DELETE CASCADE,
    PRIMARY KEY (parcel_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_vertices_source ON vertices(source_id);
CREATE INDEX IF NOT EXISTS idx_parcel_vertices_vertex ON parcel_vertices(vertex_id);
CREATE INDEX IF NOT EXISTS idx_parcel_vertices_parcel ON parcel_vertices(parcel_id, seq);
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
#: Single source of truth for the factors is ``core.units`` so the DB seed and
#: the conversion logic can never drift apart.
BUILTIN_UNIT_PROFILES = tuple(BUILTIN_AREA_UNITS.items())

#: Columns added to existing tables after their first release. Applied
#: additively via guarded ``ALTER TABLE ADD COLUMN`` (CREATE TABLE IF NOT EXISTS
#: cannot add a column to a table that already exists). Each entry is
#: ``(table, column, column_definition)``; the definition must carry a DEFAULT so
#: existing rows get a value. v3 added parcels.closed.
_ADDITIVE_COLUMNS = (
    ("parcels", "closed", "INTEGER NOT NULL DEFAULT 0"),
    ("parcels", "owner", "TEXT"),
    # v6: the local area-unit profile chosen for display on a source. Plain
    # INTEGER (no DB-level FK, since ALTER ADD COLUMN can't carry ON DELETE);
    # delete_unit_profile() clears any references in application code.
    ("sources", "unit_profile_id", "INTEGER"),
)

#: Sentinel for "argument not supplied" in partial updates (distinct from None,
#: which is a meaningful value — e.g. clearing an owner).
_UNSET = object()


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
            _seed_builtin_units(conn)
            conn.commit()
        except Exception:
            conn.close()
            raise
        return cls(root, conn)

    @classmethod
    def open(cls, root) -> "ProjectDB":
        """Open an existing project folder.

        A file written by a *newer* code version than this one is rejected
        loudly. A file from an *older* version is upgraded in place: additive
        changes (new tables/columns for v2-v4) apply idempotently, and the v5
        restructuring rebuilds any per-parcel ``points`` into shared vertices.
        The stored version is then bumped to match.
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
            conn.executescript(SCHEMA_SQL)   # new tables (source_scales, vertices, parcel_vertices)
            _ensure_additive_columns(conn)   # new columns (parcels.closed / owner, sources.unit_profile_id)
            _seed_builtin_units(conn)        # ensure built-in units exist post-upgrade
            if _table_exists(conn, "points"):
                # v5 restructuring: rebuild per-parcel points as shared vertices,
                # then drop the old table. Safe as a clean rebuild since only
                # test parcels exist at this milestone.
                _migrate_points_to_vertices(conn)
                conn.execute("DROP TABLE points")
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
    #
    # SOURCE-FILE IMMUTABILITY (non-negotiable, PROJECT_BRIEF "Offline & storage
    # constraints"): once a file is copied into ``sources/`` it is the canonical
    # original and MUST NEVER be overwritten in place by anything the app does —
    # not preprocessing, not an auto-trace cache, not any derived artifact. The
    # only writes into ``sources/`` are the create-only copies below, each
    # guarded so it can only ever create a new file, never replace an existing
    # one. Any *derived* version of a source (e.g. an enhanced scan) must be a
    # distinctly-named new file (``sheet_enhanced.png`` beside ``sheet.png``) or
    # kept in memory only — as M8's preprocessing preview does — so it is always
    # unambiguous which file is the untouched original. Future milestones that
    # touch source files must preserve this invariant.

    def import_source(self, src_file, file_type: str | None = None,
                      *, doc_date: str | None = None, page: int | None = None) -> int:
        """Copy *src_file* into ``sources/`` and register it by relative path.

        Returns the new source id. The file is copied (kept as a file, never a
        blob) and only its project-relative path is stored, so the project
        stays portable. Create-only: if a same-named file already exists in
        ``sources/`` this raises rather than overwriting it (immutability rule).
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
            # Create-only: guarded by ``not dest.exists()`` so an existing
            # original is never overwritten (source-file immutability rule).
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

    # -- unit profiles (Milestone 9) ----------------------------------------
    #
    # SI is canonical; a profile is only a factor to square metres, applied at
    # display/export time and never to stored geometry. Built-ins (sq m / sq ft /
    # acre / hectare) are fixed and cannot be edited or deleted; local measures
    # (Bigha, ...) are user-added. Which profile is active is per-source
    # (``sources.unit_profile_id``), so different sheets can use different local
    # units. "No profile" is a valid state — the UI still shows SI.

    def list_unit_profiles(self) -> list[dict]:
        """All unit profiles, built-ins first (in their seed order) then
        user-defined by name. Each: id, name, sq_m_per_unit, is_builtin."""
        rows = self.conn.execute(
            "SELECT id, name, sq_m_per_unit, is_builtin FROM unit_profiles "
            "ORDER BY is_builtin DESC, "
            # built-ins keep their canonical order; user ones sort by name
            "CASE WHEN is_builtin = 1 THEN id ELSE 0 END, name COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_unit_profile(self, profile_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT id, name, sq_m_per_unit, is_builtin FROM unit_profiles WHERE id = ?",
            (profile_id,),
        ).fetchone()
        return dict(row) if row else None

    def create_unit_profile(self, name: str, sq_m_per_unit: float) -> int:
        """Add a user-defined local area unit (factor to square metres). Names are
        unique (case-sensitively, as stored); raises on a duplicate name or a
        non-positive factor."""
        name = (name or "").strip()
        if not name:
            raise ProjectError("Unit profile name must not be empty.")
        if not (sq_m_per_unit > 0):
            raise ProjectError(f"Conversion factor must be positive, got {sq_m_per_unit!r}.")
        try:
            cur = self.conn.execute(
                "INSERT INTO unit_profiles (name, sq_m_per_unit, is_builtin, created_at) "
                "VALUES (?, ?, 0, ?)",
                (name, float(sq_m_per_unit), _utcnow()),
            )
        except sqlite3.IntegrityError:
            raise ProjectError(f"A unit profile named {name!r} already exists.")
        self.conn.commit()
        return int(cur.lastrowid)

    def update_unit_profile(self, profile_id: int, *, name: str | None = None,
                            sq_m_per_unit: float | None = None) -> None:
        """Rename and/or re-factor a user-defined profile. Built-ins are fixed and
        cannot be changed."""
        existing = self.get_unit_profile(profile_id)
        if existing is None:
            raise ProjectError(f"No unit profile with id {profile_id}.")
        if existing["is_builtin"]:
            raise ProjectError(f"The built-in unit {existing['name']!r} cannot be edited.")
        new_name = existing["name"] if name is None else (name or "").strip()
        if not new_name:
            raise ProjectError("Unit profile name must not be empty.")
        new_factor = existing["sq_m_per_unit"] if sq_m_per_unit is None else float(sq_m_per_unit)
        if not (new_factor > 0):
            raise ProjectError(f"Conversion factor must be positive, got {new_factor!r}.")
        try:
            self.conn.execute(
                "UPDATE unit_profiles SET name = ?, sq_m_per_unit = ? WHERE id = ?",
                (new_name, new_factor, profile_id),
            )
        except sqlite3.IntegrityError:
            raise ProjectError(f"A unit profile named {new_name!r} already exists.")
        self.conn.commit()

    def delete_unit_profile(self, profile_id: int) -> None:
        """Delete a user-defined profile and clear it from any source that had it
        active. Built-ins cannot be deleted."""
        existing = self.get_unit_profile(profile_id)
        if existing is None:
            raise ProjectError(f"No unit profile with id {profile_id}.")
        if existing["is_builtin"]:
            raise ProjectError(f"The built-in unit {existing['name']!r} cannot be deleted.")
        # No DB-level FK on sources.unit_profile_id, so clear references here.
        self.conn.execute(
            "UPDATE sources SET unit_profile_id = NULL WHERE unit_profile_id = ?", (profile_id,))
        self.conn.execute("DELETE FROM unit_profiles WHERE id = ?", (profile_id,))
        self.conn.commit()

    def set_source_unit_profile(self, source_id: int, profile_id: int | None) -> None:
        """Set (or clear, with None) the active display unit profile for a source."""
        if self.conn.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone() is None:
            raise ProjectError(f"No source with id {source_id} in this project.")
        if profile_id is not None and self.get_unit_profile(profile_id) is None:
            raise ProjectError(f"No unit profile with id {profile_id}.")
        self.conn.execute(
            "UPDATE sources SET unit_profile_id = ? WHERE id = ?", (profile_id, source_id))
        self.conn.commit()

    def get_source_unit_profile(self, source_id: int) -> dict | None:
        """The active unit profile for a source as a dict, or None if unset (or
        the source is unknown)."""
        row = self.conn.execute(
            "SELECT p.id, p.name, p.sq_m_per_unit, p.is_builtin "
            "FROM sources s JOIN unit_profiles p ON p.id = s.unit_profile_id "
            "WHERE s.id = ?", (source_id,),
        ).fetchone()
        return dict(row) if row else None

    # -- parcels & shared vertices (Milestone 6) ----------------------------
    #
    # A source can carry several parcels; a parcel's boundary is an ordered list
    # of references into the source's shared `vertices`. Two parcels reference
    # the same vertex where their boundaries meet, so a shared edge is
    # structurally identical, and moving that vertex moves it for both. Snapping
    # (dedup) on save reuses an existing vertex when a point lands within
    # SNAP_TOLERANCE_PX of it — but never one already used by the SAME parcel.
    # `owner` links a source's parcels for owner-wise reporting.

    def create_parcel(self, source_id: int, *, name: str | None = None,
                    owner: str | None = None) -> int:
        """Create a new (empty) parcel on *source_id* and return its id."""
        if self.conn.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone() is None:
            raise ProjectError(f"No source with id {source_id} in this project.")
        now = _utcnow()
        cur = self.conn.execute(
            "INSERT INTO parcels (name, source_id, owner, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, source_id, owner, now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_parcels(self, source_id: int) -> list[dict]:
        """All parcels for a source, in creation order, each with its live vertex
        count (for a list/sidebar)."""
        rows = self.conn.execute(
            "SELECT p.id, p.source_id, p.name, p.owner, p.closed, p.created_at, p.updated_at, "
            "  (SELECT COUNT(*) FROM parcel_vertices WHERE parcel_id = p.id) AS point_count "
            "FROM parcels p WHERE p.source_id = ? ORDER BY p.id", (source_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_parcel(self, parcel_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT id, source_id, name, owner, closed, scale_m_per_px, notes, "
            "created_at, updated_at FROM parcels WHERE id = ?", (parcel_id,)
        ).fetchone()
        return dict(row) if row else None

    def update_parcel(self, parcel_id: int, *, name=_UNSET, owner=_UNSET) -> None:
        """Update the given parcel fields (only those passed). ``owner=None``
        clears the owner; omitting it leaves the owner unchanged."""
        sets, params = [], []
        if name is not _UNSET:
            sets.append("name = ?")
            params.append(name)
        if owner is not _UNSET:
            sets.append("owner = ?")
            params.append(owner)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.append(_utcnow())
        params.append(parcel_id)
        self.conn.execute(f"UPDATE parcels SET {', '.join(sets)} WHERE id = ?", params)
        self.conn.commit()

    def delete_parcel(self, parcel_id: int) -> None:
        """Delete a parcel and its vertex references (ON DELETE CASCADE), then
        prune any vertices left unreferenced by any parcel."""
        source_id = None
        row = self.conn.execute("SELECT source_id FROM parcels WHERE id = ?", (parcel_id,)).fetchone()
        if row is not None:
            source_id = row["source_id"]
        self.conn.execute("DELETE FROM parcels WHERE id = ?", (parcel_id,))
        if source_id is not None:
            self._prune_orphan_vertices(source_id)
        self.conn.commit()

    def save_parcel_polygon(self, parcel_id: int, pixel_points: list[tuple[float, float]],
                            *, closed: bool = False,
                            metres_per_pixel: float | None = None) -> None:
        """Replace *parcel_id*'s boundary with *pixel_points* (in order) and
        record its *closed* state. Each point snaps to an existing source vertex
        within SNAP_TOLERANCE_PX (never one already used by this parcel) or
        creates a new one. Orphaned vertices are pruned. An empty list clears the
        boundary but keeps the parcel row."""
        parcel = self.conn.execute(
            "SELECT source_id FROM parcels WHERE id = ?", (parcel_id,)).fetchone()
        if parcel is None:
            raise ProjectError(f"No parcel with id {parcel_id} in this project.")
        source_id = parcel["source_id"]

        self.conn.execute("DELETE FROM parcel_vertices WHERE parcel_id = ?", (parcel_id,))
        # Candidate vertices for snapping: all of the source's vertices, growing
        # as we create new ones this call. `used` excludes this parcel's own.
        candidates = [(v["id"], v["pixel_x"], v["pixel_y"])
                      for v in self.list_vertices(source_id)]
        used_ids: set[int] = set()
        for seq, (px, py) in enumerate(pixel_points):
            idx = nearest_vertex_index((px, py), candidates, SNAP_TOLERANCE_PX,
                                       exclude_ids=used_ids)
            if idx is not None:
                vid = candidates[idx][0]                 # reuse a shared/unclaimed vertex
            else:
                vid = self._create_vertex(source_id, px, py, metres_per_pixel)
                candidates.append((vid, float(px), float(py)))
            self.conn.execute(
                "INSERT INTO parcel_vertices (parcel_id, seq, vertex_id) VALUES (?, ?, ?)",
                (parcel_id, seq, vid),
            )
            used_ids.add(vid)

        self.conn.execute(
            "UPDATE parcels SET updated_at = ?, closed = ? WHERE id = ?",
            (_utcnow(), 1 if closed else 0, parcel_id),
        )
        self._prune_orphan_vertices(source_id)
        self.conn.commit()

    def get_parcel_points(self, parcel_id: int) -> list[dict]:
        """Ordered boundary points of a parcel, resolved through its vertices."""
        rows = self.conn.execute(
            "SELECT pv.seq AS seq, pv.vertex_id AS vertex_id, "
            "  v.pixel_x, v.pixel_y, v.local_x, v.local_y, v.lat, v.lon "
            "FROM parcel_vertices pv JOIN vertices v ON pv.vertex_id = v.id "
            "WHERE pv.parcel_id = ? ORDER BY pv.seq", (parcel_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_parcel_polygon(self, parcel_id: int) -> list[tuple[float, float]]:
        """The parcel's boundary as ordered (pixel_x, pixel_y) tuples."""
        return [(p["pixel_x"], p["pixel_y"]) for p in self.get_parcel_points(parcel_id)]

    def get_parcel_vertex_ids(self, parcel_id: int) -> list[int]:
        """The ordered vertex ids of a parcel's boundary (so callers know which
        vertices are shared with other parcels)."""
        rows = self.conn.execute(
            "SELECT vertex_id FROM parcel_vertices WHERE parcel_id = ? ORDER BY seq",
            (parcel_id,)
        ).fetchall()
        return [r["vertex_id"] for r in rows]

    def get_parcel_closed(self, parcel_id: int) -> bool:
        """The parcel's stored closed/open state (the *saved* state, not inferred
        from point count — an open 3+-point boundary reloads as open)."""
        row = self.conn.execute("SELECT closed FROM parcels WHERE id = ?", (parcel_id,)).fetchone()
        return bool(row["closed"]) if row is not None else False

    # -- vertices (shared across a source's parcels) ------------------------

    def list_vertices(self, source_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, pixel_x, pixel_y, local_x, local_y, lat, lon "
            "FROM vertices WHERE source_id = ? ORDER BY id", (source_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def move_vertex(self, vertex_id: int, pixel_x: float, pixel_y: float, *,
                    metres_per_pixel: float | None = None) -> None:
        """Move a vertex — this moves it for **every** parcel referencing it."""
        lx = ly = None
        if metres_per_pixel is not None:
            lx, ly = pixel_x * metres_per_pixel, pixel_y * metres_per_pixel
        self.conn.execute(
            "UPDATE vertices SET pixel_x = ?, pixel_y = ?, local_x = ?, local_y = ? WHERE id = ?",
            (float(pixel_x), float(pixel_y), lx, ly, vertex_id),
        )
        now = _utcnow()
        for pid in self.parcels_referencing_vertex(vertex_id):
            self.conn.execute("UPDATE parcels SET updated_at = ? WHERE id = ?", (now, pid))
        self.conn.commit()

    def parcels_referencing_vertex(self, vertex_id: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT DISTINCT parcel_id FROM parcel_vertices WHERE vertex_id = ? ORDER BY parcel_id",
            (vertex_id,)
        ).fetchall()
        return [r["parcel_id"] for r in rows]

    def refresh_source_vertices_si(self, source_id: int, metres_per_pixel: float | None) -> None:
        """Recompute every vertex's SI (local_x/local_y) for the source when the
        scale changes. Pixel coordinates are unchanged."""
        if metres_per_pixel is None:
            self.conn.execute(
                "UPDATE vertices SET local_x = NULL, local_y = NULL WHERE source_id = ?",
                (source_id,))
        else:
            self.conn.execute(
                "UPDATE vertices SET local_x = pixel_x * ?, local_y = pixel_y * ? "
                "WHERE source_id = ?",
                (metres_per_pixel, metres_per_pixel, source_id))
        self.conn.commit()

    def _create_vertex(self, source_id: int, px: float, py: float,
                    metres_per_pixel: float | None) -> int:
        lx = ly = None
        if metres_per_pixel is not None:
            lx, ly = px * metres_per_pixel, py * metres_per_pixel
        cur = self.conn.execute(
            "INSERT INTO vertices (source_id, pixel_x, pixel_y, local_x, local_y) "
            "VALUES (?, ?, ?, ?, ?)",
            (source_id, float(px), float(py), lx, ly),
        )
        return int(cur.lastrowid)

    def _prune_orphan_vertices(self, source_id: int) -> None:
        """Delete vertices of a source no longer referenced by any parcel."""
        self.conn.execute(
            "DELETE FROM vertices WHERE source_id = ? AND id NOT IN "
            "(SELECT vertex_id FROM parcel_vertices)", (source_id,))

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


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)
    ).fetchone() is not None


def _seed_builtin_units(conn: sqlite3.Connection) -> None:
    """Insert the fixed built-in area units if absent. Idempotent (``INSERT OR
    IGNORE`` on the UNIQUE name), so it is safe on both create and upgrade."""
    now = _utcnow()
    for uname, factor in BUILTIN_UNIT_PROFILES:
        conn.execute(
            "INSERT OR IGNORE INTO unit_profiles "
            "(name, sq_m_per_unit, is_builtin, created_at) VALUES (?, ?, 1, ?)",
            (uname, factor, now),
        )


def _migrate_points_to_vertices(conn: sqlite3.Connection) -> None:
    """v4 -> v5 rebuild: turn each parcel's independent `points` into references
    into a shared per-source `vertices` set, deduplicating points within
    SNAP_TOLERANCE_PX so adjacent parcels that traced the same corner end up
    sharing one vertex. A parcel never merges two of its *own* points (they are
    excluded from its snap candidates), so a parcel's shape is preserved."""
    parcels = conn.execute("SELECT id, source_id FROM parcels ORDER BY id").fetchall()
    created: dict[int, list[tuple[int, float, float]]] = {}  # source_id -> [(vid, x, y)]
    for parcel in parcels:
        pid, sid = parcel["id"], parcel["source_id"]
        source_verts = created.setdefault(sid, [])
        pts = conn.execute(
            "SELECT pixel_x, pixel_y, local_x, local_y, lat, lon "
            "FROM points WHERE parcel_id = ? ORDER BY seq", (pid,)
        ).fetchall()
        used_ids: set[int] = set()
        for seq, pt in enumerate(pts):
            point = (pt["pixel_x"], pt["pixel_y"])
            idx = nearest_vertex_index(point, source_verts, SNAP_TOLERANCE_PX,
                                       exclude_ids=used_ids)
            if idx is not None:
                vid = source_verts[idx][0]
            else:
                cur = conn.execute(
                    "INSERT INTO vertices (source_id, pixel_x, pixel_y, local_x, local_y, lat, lon) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (sid, pt["pixel_x"], pt["pixel_y"], pt["local_x"], pt["local_y"],
                     pt["lat"], pt["lon"]),
                )
                vid = int(cur.lastrowid)
                source_verts.append((vid, pt["pixel_x"], pt["pixel_y"]))
            conn.execute(
                "INSERT INTO parcel_vertices (parcel_id, seq, vertex_id) VALUES (?, ?, ?)",
                (pid, seq, vid),
            )
            used_ids.add(vid)


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
