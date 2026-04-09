"""SQLite-backed canonical store with FTS5 lexical index.

Schema (v3):
  memory_units   — typed durable knowledge (+ embedded_at for vec tracking)
  memory_fts     — FTS5 virtual table over title + body + tags
  memory_vec     — sqlite-vec virtual table (optional, loaded if extension available)
  relations      — directed edges between memory units (graph view)
  events         — append-only raw events from hooks/tools
  sessions       — session metadata
  meta           — key/value runtime info
  search_log     — retrieval telemetry

Migration path:
  v1 → v2  ALTER TABLE memory_units ADD COLUMN embedded_at TEXT
  v2 → v3  CREATE TABLE relations + indexes

V1 deliberately uses only stdlib sqlite3. The vec virtual table is layered on
via the optional sqlite-vec extension; its absence degrades gracefully.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

from .models import Event, MemoryType, MemoryUnit, Relation


# ---------------------------------------------------------------------------
# TTL by type
# ---------------------------------------------------------------------------

TTL_BY_TYPE: dict[MemoryType, timedelta] = {
    MemoryType.code_change: timedelta(days=14),
}


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# Base tables — always created for new databases (v3 baseline).
# Migrations handle the delta for existing v1/v2 databases.
_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_units (
    id            TEXT PRIMARY KEY,
    project       TEXT NOT NULL,
    type          TEXT NOT NULL,
    title         TEXT NOT NULL,
    body          TEXT NOT NULL,
    tags          TEXT NOT NULL DEFAULT '[]',
    file_paths    TEXT NOT NULL DEFAULT '[]',
    source_refs   TEXT NOT NULL DEFAULT '[]',
    created_at    TEXT NOT NULL,
    valid_from    TEXT NOT NULL,
    valid_to      TEXT,
    confidence    REAL NOT NULL DEFAULT 0.8,
    checksum      TEXT,
    embedded_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_mu_project_type ON memory_units(project, type);
CREATE INDEX IF NOT EXISTS idx_mu_valid_to    ON memory_units(valid_to);
CREATE INDEX IF NOT EXISTS idx_mu_checksum    ON memory_units(checksum);
CREATE INDEX IF NOT EXISTS idx_mu_embedded    ON memory_units(embedded_at);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    title, body, tags,
    content='memory_units',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS memory_units_ai AFTER INSERT ON memory_units BEGIN
    INSERT INTO memory_fts(rowid, title, body, tags)
    VALUES (new.rowid, new.title, new.body, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS memory_units_ad AFTER DELETE ON memory_units BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, title, body, tags)
    VALUES('delete', old.rowid, old.title, old.body, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS memory_units_au AFTER UPDATE ON memory_units BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, title, body, tags)
    VALUES('delete', old.rowid, old.title, old.body, old.tags);
    INSERT INTO memory_fts(rowid, title, body, tags)
    VALUES (new.rowid, new.title, new.body, new.tags);
END;

CREATE TABLE IF NOT EXISTS relations (
    id            TEXT PRIMARY KEY,
    project       TEXT NOT NULL,
    from_id       TEXT NOT NULL,
    to_id         TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    weight        REAL NOT NULL DEFAULT 1.0,
    source        TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rel_from ON relations(project, from_id);
CREATE INDEX IF NOT EXISTS idx_rel_to   ON relations(project, to_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rel_dedupe
    ON relations(project, from_id, to_id, relation_type);

CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    project     TEXT NOT NULL,
    session_id  TEXT,
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    processed   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_events_unprocessed
    ON events(project, processed, created_at);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    project     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    summary     TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL,
    query       TEXT NOT NULL,
    top_k       INTEGER NOT NULL,
    hit_ids     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_search_log_project_time
    ON search_log(project, created_at);
"""

SCHEMA_VERSION = "3"

# Migrations applied incrementally to older databases.
_MIGRATIONS: dict[int, list[str]] = {
    2: [
        "ALTER TABLE memory_units ADD COLUMN embedded_at TEXT",
        "CREATE INDEX IF NOT EXISTS idx_mu_embedded ON memory_units(embedded_at)",
    ],
    3: [
        """CREATE TABLE IF NOT EXISTS relations (
            id            TEXT PRIMARY KEY,
            project       TEXT NOT NULL,
            from_id       TEXT NOT NULL,
            to_id         TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            weight        REAL NOT NULL DEFAULT 1.0,
            source        TEXT NOT NULL,
            created_at    TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_rel_from ON relations(project, from_id)",
        "CREATE INDEX IF NOT EXISTS idx_rel_to   ON relations(project, to_id)",
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_rel_dedupe
            ON relations(project, from_id, to_id, relation_type)""",
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _checksum(title: str, body: str) -> str:
    h = hashlib.sha256()
    h.update(title.strip().lower().encode())
    h.update(b"\x00")
    h.update(body.strip().lower().encode())
    return h.hexdigest()[:16]


def _try_load_vec(conn: sqlite3.Connection) -> bool:
    """Load the sqlite-vec extension if available. Returns True on success."""
    try:
        import sqlite_vec  # type: ignore[import-not-found]

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception:
        return False


def _ensure_vec_table(conn: sqlite3.Connection, dim: int = 384) -> None:
    """Create the memory_vec virtual table if not already present."""
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec "
        f"USING vec0(embedding FLOAT[{dim}])"
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class Store:
    def __init__(self, conn: sqlite3.Connection, project: str, *, vec_enabled: bool = False) -> None:
        self.conn = conn
        self.project = project
        self._vec_enabled = vec_enabled

    @property
    def vec_enabled(self) -> bool:
        return self._vec_enabled

    # ---------- lifecycle ----------

    @classmethod
    def open(cls, db_path: Path, project: str) -> "Store":
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")

        # Optionally load sqlite-vec before creating the schema.
        vec_enabled = _try_load_vec(conn)

        # Apply base schema (idempotent CREATE IF NOT EXISTS).
        conn.executescript(_BASE_SCHEMA)

        # Run incremental migrations for existing databases.
        current_version = _get_meta(conn, "schema_version", "1")
        for ver in sorted(v for v in _MIGRATIONS if v > int(current_version)):
            for sql in _MIGRATIONS[ver]:
                try:
                    conn.execute(sql)
                except sqlite3.OperationalError as exc:
                    # "duplicate column name" is fine — column already exists.
                    if "duplicate column name" not in str(exc).lower():
                        raise
            _set_meta(conn, "schema_version", str(ver))
            conn.commit()

        # Make sure schema_version is at least SCHEMA_VERSION for fresh DBs.
        _set_meta(conn, "schema_version", SCHEMA_VERSION)
        conn.commit()

        # Create the vector table if the extension was loaded.
        if vec_enabled:
            _ensure_vec_table(conn)

        return cls(conn, project, vec_enabled=vec_enabled)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---------- memory units ----------

    @staticmethod
    def _now_iso() -> str:
        from datetime import timezone

        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def upsert_memory(self, unit: MemoryUnit) -> tuple[MemoryUnit, bool]:
        """Insert a memory unit, deduping by (project, type, checksum).

        Returns (unit, created). If an equivalent unit exists, returns the
        existing one with created=False.

        When sqlite-vec is enabled, also stores an embedding for the unit.
        """
        if unit.checksum is None:
            unit = unit.model_copy(update={"checksum": _checksum(unit.title, unit.body)})
        if unit.valid_to is None:
            ttl = TTL_BY_TYPE.get(unit.type)
            if ttl is not None:
                try:
                    expires = _parse_iso(unit.valid_from) + ttl
                    unit = unit.model_copy(
                        update={"valid_to": expires.isoformat(timespec="seconds")}
                    )
                except ValueError:
                    pass
        now_iso = self._now_iso()
        with self.tx() as c:
            row = c.execute(
                "SELECT * FROM memory_units WHERE project=? AND type=? AND checksum=? "
                "AND (valid_to IS NULL OR valid_to > ?)",
                (unit.project, unit.type.value, unit.checksum, now_iso),
            ).fetchone()
            if row:
                return self._row_to_unit(row), False
            cur = c.execute(
                """INSERT INTO memory_units
                   (id, project, type, title, body, tags, file_paths, source_refs,
                    created_at, valid_from, valid_to, confidence, checksum)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    unit.id,
                    unit.project,
                    unit.type.value,
                    unit.title,
                    unit.body,
                    json.dumps(unit.tags),
                    json.dumps(unit.file_paths),
                    json.dumps(unit.source_refs),
                    unit.created_at,
                    unit.valid_from,
                    unit.valid_to,
                    unit.confidence,
                    unit.checksum,
                ),
            )
            rowid = cur.lastrowid

        # Embed outside the transaction (can be slow for real models).
        if self._vec_enabled and rowid is not None:
            self._embed_unit_by_rowid(unit, rowid)

        return unit, True

    def _embed_unit_by_rowid(self, unit: MemoryUnit, rowid: int) -> None:
        """Compute and store the embedding for a unit. Non-fatal on failure."""
        from ..retrieval.embeddings import get_embedder

        embedder = get_embedder()
        if embedder is None:
            return
        try:
            text = f"{unit.title}\n\n{unit.body}"
            vec = embedder.embed_one(text)
            blob = embedder.serialize(vec)
            with self.tx() as c:
                c.execute(
                    "INSERT OR REPLACE INTO memory_vec(rowid, embedding) VALUES (?, ?)",
                    (rowid, blob),
                )
                c.execute(
                    "UPDATE memory_units SET embedded_at=? WHERE id=?",
                    (self._now_iso(), unit.id),
                )
        except Exception:
            pass  # embedding failure is always non-fatal

    def get_memory(self, unit_id: str) -> MemoryUnit | None:
        row = self.conn.execute(
            "SELECT * FROM memory_units WHERE id=?", (unit_id,)
        ).fetchone()
        return self._row_to_unit(row) if row else None

    def invalidate_memory(self, unit_id: str, when: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE memory_units SET valid_to=? WHERE id=?", (when, unit_id))

    def list_memory(
        self,
        types: Iterable[MemoryType] | None = None,
        limit: int = 50,
        active_only: bool = True,
    ) -> list[MemoryUnit]:
        sql = "SELECT * FROM memory_units WHERE project=?"
        params: list[Any] = [self.project]
        if active_only:
            sql += " AND (valid_to IS NULL OR valid_to > ?)"
            params.append(self._now_iso())
        if types:
            type_list = list(types)
            placeholders = ",".join(["?"] * len(type_list))
            sql += f" AND type IN ({placeholders})"
            params.extend(t.value for t in type_list)
        sql += " ORDER BY datetime(created_at) DESC LIMIT ?"
        params.append(limit)
        return [self._row_to_unit(r) for r in self.conn.execute(sql, params).fetchall()]

    def units_needing_embedding(self, limit: int = 100) -> list[tuple[MemoryUnit, int]]:
        """Return (unit, rowid) pairs for units without an embedding."""
        rows = self.conn.execute(
            "SELECT *, rowid FROM memory_units "
            "WHERE project=? AND embedded_at IS NULL "
            "AND (valid_to IS NULL OR valid_to > ?) "
            "LIMIT ?",
            (self.project, self._now_iso(), limit),
        ).fetchall()
        return [(self._row_to_unit(r), r["rowid"]) for r in rows]

    def count_units(self) -> dict[str, int]:
        """Return total and embedded unit counts for this project."""
        total = self.conn.execute(
            "SELECT COUNT(*) FROM memory_units WHERE project=?", (self.project,)
        ).fetchone()[0]
        embedded = self.conn.execute(
            "SELECT COUNT(*) FROM memory_units WHERE project=? AND embedded_at IS NOT NULL",
            (self.project,),
        ).fetchone()[0]
        return {"total": total, "embedded": embedded, "pending_embed": total - embedded}

    # ---------- FTS5 search ----------

    def search(self, query: str, top_k: int = 8) -> list[tuple[MemoryUnit, float]]:
        """FTS5 lexical search over active memory units in this project."""
        q = _sanitize_fts(query)
        if not q:
            return []
        rows = self.conn.execute(
            """SELECT mu.*, bm25(memory_fts) AS score
               FROM memory_fts
               JOIN memory_units mu ON mu.rowid = memory_fts.rowid
               WHERE memory_fts MATCH ?
                 AND mu.project = ?
                 AND (mu.valid_to IS NULL OR mu.valid_to > ?)
               ORDER BY score
               LIMIT ?""",
            (q, self.project, self._now_iso(), top_k),
        ).fetchall()
        out: list[tuple[MemoryUnit, float]] = []
        for r in rows:
            unit = self._row_to_unit(r)
            score = 1.0 / (1.0 + max(0.0, float(r["score"])))
            out.append((unit, score))
        return out

    # ---------- vector search ----------

    def search_vec(
        self, query_vec: list[float], top_k: int = 8
    ) -> list[tuple[MemoryUnit, float]]:
        """Cosine-similarity search over stored embeddings.

        Returns (unit, score) pairs ordered by similarity (higher = better).
        Returns empty list if the vec extension is not loaded.
        """
        if not self._vec_enabled:
            return []
        from ..retrieval.embeddings import Embedder

        blob = Embedder.serialize(query_vec)
        now = self._now_iso()
        try:
            rows = self.conn.execute(
                """SELECT mu.*, v.distance
                   FROM memory_vec v
                   JOIN memory_units mu ON mu.rowid = v.rowid
                   WHERE v.embedding MATCH ?
                     AND k = ?
                     AND mu.project = ?
                     AND (mu.valid_to IS NULL OR mu.valid_to > ?)
                   ORDER BY v.distance""",
                (blob, top_k * 2, self.project, now),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        out: list[tuple[MemoryUnit, float]] = []
        for r in rows:
            unit = self._row_to_unit(r)
            # distance is cosine distance [0, 2]; convert to similarity [1, -1]
            dist = float(r["distance"])
            score = 1.0 - dist / 2.0
            out.append((unit, score))
        return out[:top_k]

    # ---------- telemetry ----------

    def log_search(self, query: str, top_k: int, hit_ids: list[str]) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO search_log (project, query, top_k, hit_ids, created_at)
                   VALUES (?,?,?,?,?)""",
                (
                    self.project,
                    query,
                    top_k,
                    json.dumps(hit_ids),
                    self._now_iso(),
                ),
            )

    # ---------- relations (graph) ----------

    def upsert_relations(self, relations: Iterable[Relation]) -> int:
        """Insert relations, ignoring duplicates. Returns count inserted."""
        inserted = 0
        with self.tx() as c:
            for r in relations:
                try:
                    cur = c.execute(
                        """INSERT OR IGNORE INTO relations
                           (id, project, from_id, to_id, relation_type, weight, source, created_at)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (r.id, r.project, r.from_id, r.to_id, r.relation_type,
                         r.weight, r.source, r.created_at),
                    )
                    if cur.rowcount > 0:
                        inserted += 1
                except sqlite3.IntegrityError:
                    pass
        return inserted

    def neighbors(
        self, unit_id: str, depth: int = 1, max_nodes: int = 20
    ) -> list[tuple[MemoryUnit, float]]:
        """Return direct neighbor units reachable via any edge from unit_id.

        Only depth=1 is implemented here; multi-hop is handled by
        retrieval.graph.expand_with_graph.
        """
        now = self._now_iso()
        rows = self.conn.execute(
            """SELECT mu.*, r.weight
               FROM relations r
               JOIN memory_units mu ON mu.id = r.to_id
               WHERE r.project = ? AND r.from_id = ?
                 AND (mu.valid_to IS NULL OR mu.valid_to > ?)
               UNION
               SELECT mu.*, r.weight
               FROM relations r
               JOIN memory_units mu ON mu.id = r.from_id
               WHERE r.project = ? AND r.to_id = ?
                 AND mu.id != ?
                 AND (mu.valid_to IS NULL OR mu.valid_to > ?)
               ORDER BY weight DESC
               LIMIT ?""",
            (self.project, unit_id, now, self.project, unit_id, unit_id, now, max_nodes),
        ).fetchall()
        result: list[tuple[MemoryUnit, float]] = []
        seen: set[str] = set()
        for r in rows:
            unit = self._row_to_unit(r)
            if unit.id not in seen:
                seen.add(unit.id)
                result.append((unit, float(r["weight"])))
        return result

    def get_relations(self, unit_id: str) -> list[dict[str, Any]]:
        """Return all edges from or to unit_id in this project."""
        rows = self.conn.execute(
            "SELECT * FROM relations WHERE project=? AND (from_id=? OR to_id=?)",
            (self.project, unit_id, unit_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_relations(self) -> dict[str, int]:
        """Return edge counts grouped by relation_type for this project."""
        rows = self.conn.execute(
            "SELECT relation_type, COUNT(*) as n FROM relations WHERE project=? GROUP BY relation_type",
            (self.project,),
        ).fetchall()
        return {r["relation_type"]: r["n"] for r in rows}

    # ---------- Graph-helper queries (for edge extraction) ----------

    def units_sharing_files(
        self, file_paths: list[str], exclude_id: str, limit: int = 20
    ) -> list[MemoryUnit]:
        """Return active units that share at least one file_path."""
        if not file_paths:
            return []
        placeholders = ",".join(["?"] * len(file_paths))
        rows = self.conn.execute(
            f"""SELECT DISTINCT mu.*
               FROM memory_units mu, json_each(mu.file_paths) je
               WHERE mu.project = ?
                 AND mu.id != ?
                 AND je.value IN ({placeholders})
                 AND (mu.valid_to IS NULL OR mu.valid_to > ?)
               LIMIT ?""",
            [self.project, exclude_id, *file_paths, self._now_iso(), limit],
        ).fetchall()
        return [self._row_to_unit(r) for r in rows]

    def units_sharing_tags(
        self, tags: list[str], exclude_id: str, limit: int = 20
    ) -> list[MemoryUnit]:
        """Return active units that share at least one tag."""
        if not tags:
            return []
        placeholders = ",".join(["?"] * len(tags))
        rows = self.conn.execute(
            f"""SELECT DISTINCT mu.*
               FROM memory_units mu, json_each(mu.tags) je
               WHERE mu.project = ?
                 AND mu.id != ?
                 AND je.value IN ({placeholders})
                 AND (mu.valid_to IS NULL OR mu.valid_to > ?)
               LIMIT ?""",
            [self.project, exclude_id, *tags, self._now_iso(), limit],
        ).fetchall()
        return [self._row_to_unit(r) for r in rows]

    def recent_code_changes_on_files(
        self, file_paths: list[str], minutes: int, exclude_id: str, limit: int = 10
    ) -> list[MemoryUnit]:
        """Return code_change units on the given files created within the last N minutes."""
        if not file_paths:
            return []
        from datetime import timezone

        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=minutes)
        ).isoformat(timespec="seconds")
        placeholders = ",".join(["?"] * len(file_paths))
        rows = self.conn.execute(
            f"""SELECT DISTINCT mu.*
               FROM memory_units mu, json_each(mu.file_paths) je
               WHERE mu.project = ?
                 AND mu.type = 'code_change'
                 AND mu.id != ?
                 AND je.value IN ({placeholders})
                 AND mu.created_at >= ?
                 AND (mu.valid_to IS NULL OR mu.valid_to > ?)
               LIMIT ?""",
            [self.project, exclude_id, *file_paths, cutoff, self._now_iso(), limit],
        ).fetchall()
        return [self._row_to_unit(r) for r in rows]

    # ---------- per-project settings ----------

    def get_setting(self, key: str, default: str = "") -> str:
        """Read a per-project setting from the meta table."""
        row = self.conn.execute(
            "SELECT v FROM meta WHERE k=?", (f"setting:{key}",)
        ).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        """Write a per-project setting to the meta table."""
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO meta (k, v) VALUES (?,?)",
                (f"setting:{key}", value),
            )

    # ---------- events ----------

    def append_event(self, event: Event) -> Event:
        if event.id is None:
            event = event.model_copy(update={"id": str(uuid.uuid4())})
        with self.tx() as c:
            c.execute(
                """INSERT INTO events (id, project, session_id, type, payload, created_at, processed)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    event.id,
                    event.project,
                    event.session_id,
                    event.type,
                    json.dumps(event.payload),
                    event.created_at,
                    int(event.processed),
                ),
            )
        return event

    def unprocessed_events(self, limit: int = 200) -> list[Event]:
        rows = self.conn.execute(
            """SELECT * FROM events
               WHERE project=? AND processed=0
               ORDER BY datetime(created_at) ASC LIMIT ?""",
            (self.project, limit),
        ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def mark_processed(self, ids: Iterable[str]) -> None:
        ids_list = list(ids)
        if not ids_list:
            return
        with self.tx() as c:
            c.executemany(
                "UPDATE events SET processed=1 WHERE id=?", [(i,) for i in ids_list]
            )

    # ---------- sessions ----------

    def start_session(self, session_id: str, started_at: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO sessions (id, project, started_at) VALUES (?,?,?)",
                (session_id, self.project, started_at),
            )

    def end_session(self, session_id: str, ended_at: str, summary: str | None) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE sessions SET ended_at=?, summary=? WHERE id=?",
                (ended_at, summary, session_id),
            )

    # ---------- tag-based lookup ----------

    def find_by_tag(self, tag: str, limit: int = 5) -> list[MemoryUnit]:
        """Return active units whose tags JSON array contains *tag*."""
        rows = self.conn.execute(
            """SELECT mu.*
               FROM memory_units mu, json_each(mu.tags) je
               WHERE mu.project = ?
                 AND je.value = ?
                 AND (mu.valid_to IS NULL OR mu.valid_to > ?)
               ORDER BY mu.created_at DESC
               LIMIT ?""",
            (self.project, tag, self._now_iso(), limit),
        ).fetchall()
        return [self._row_to_unit(r) for r in rows]

    # ---------- helpers ----------

    @staticmethod
    def _row_to_unit(row: sqlite3.Row) -> MemoryUnit:
        return MemoryUnit(
            id=row["id"],
            project=row["project"],
            type=MemoryType(row["type"]),
            title=row["title"],
            body=row["body"],
            tags=json.loads(row["tags"]),
            file_paths=json.loads(row["file_paths"]),
            source_refs=json.loads(row["source_refs"]),
            created_at=row["created_at"],
            valid_from=row["valid_from"],
            valid_to=row["valid_to"],
            confidence=row["confidence"],
            checksum=row["checksum"],
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        return Event(
            id=row["id"],
            project=row["project"],
            session_id=row["session_id"],
            type=row["type"],
            payload=json.loads(row["payload"]),
            created_at=row["created_at"],
            processed=bool(row["processed"]),
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def open_store(db_path: Path, project: str) -> Store:
    return Store.open(db_path, project)


def _get_meta(conn: sqlite3.Connection, key: str, default: str) -> str:
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row[0] if row else default


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?,?)", (key, value))


_FTS_SPECIAL = set('"():*^')


def _sanitize_fts(q: str) -> str:
    """Build a forgiving FTS5 query that ORs all tokens."""
    cleaned = "".join(" " if ch in _FTS_SPECIAL else ch for ch in q)
    tokens = [t for t in cleaned.split() if len(t) > 1]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)
