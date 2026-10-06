import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from .domain import CapacityFull, ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS occupancies (
                    id TEXT PRIMARY KEY,
                    standard_id TEXT NOT NULL,
                    workorder_id TEXT NOT NULL,
                    slot_start TEXT NOT NULL,
                    slot_end TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    released_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_occupancies_wo_active
                    ON occupancies(workorder_id) WHERE status = 'active';
                CREATE INDEX IF NOT EXISTS idx_occupancies_standard_slot
                    ON occupancies(standard_id, slot_start, slot_end);
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    @staticmethod
    def _occupancy_from_row(row):
        return {
            "id": row["id"],
            "standard_id": row["standard_id"],
            "workorder_id": row["workorder_id"],
            "slot_start": row["slot_start"],
            "slot_end": row["slot_end"],
            "status": row["status"],
            "created_at": row["created_at"],
            "released_at": row["released_at"],
        }

    def get_occupancy(self, occupancy_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM occupancies WHERE id = ?", (occupancy_id,)
            ).fetchone()
        return self._occupancy_from_row(row) if row else None

    def get_active_occupancy(self, workorder_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM occupancies WHERE workorder_id = ? AND status = 'active' "
                "ORDER BY created_at DESC LIMIT 1",
                (workorder_id,),
            ).fetchone()
        return self._occupancy_from_row(row) if row else None

    def list_occupancies(self, standard_id=None, workorder_id=None):
        clauses = []
        params = []
        if standard_id:
            clauses.append("standard_id = ?")
            params.append(standard_id)
        if workorder_id:
            clauses.append("workorder_id = ?")
            params.append(workorder_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM occupancies" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._occupancy_from_row(row) for row in rows]

    def count_overlapping(self, standard_id, slot_start, slot_end):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS c FROM occupancies "
                "WHERE standard_id = ? AND status = 'active' "
                "AND slot_start < ? AND slot_end > ?",
                (standard_id, slot_end, slot_start),
            ).fetchone()
        return int(row["c"])

    def release_active_occupancy_for_workorder(self, workorder_id):
        with self._connect() as connection:
            connection.execute(
                "UPDATE occupancies SET status = 'released', released_at = ? "
                "WHERE workorder_id = ? AND status = 'active'",
                (utcnow(), workorder_id),
            )

    def book_and_schedule(self, workorder_id, standard_id, slot_start, slot_end, capacity, expected_version, wo_data):
        """Atomically reserve capacity for a workorder and mark it scheduled.

        The check-and-insert runs under BEGIN IMMEDIATE so concurrent bookings are
        serialized: the first committer wins, the loser sees the occupied slot and
        raises CapacityFull. A partial unique index enforces one active occupancy
        per workorder, so a retry can never double-book.

        Raises CapacityFull when the standard has no free capacity for the slot.
        Raises ConflictError on optimistic version drift.
        """
        now = utcnow()
        payload = json.dumps(wo_data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM occupancies WHERE workorder_id = ? AND status = 'active'",
                (workorder_id,),
            ).fetchone()
            if not existing:
                row = connection.execute(
                    "SELECT COUNT(*) AS c FROM occupancies "
                    "WHERE standard_id = ? AND status = 'active' "
                    "AND slot_start < ? AND slot_end > ?",
                    (standard_id, slot_end, slot_start),
                ).fetchone()
                if int(row["c"]) >= int(capacity):
                    connection.rollback()
                    raise CapacityFull(
                        "standard %s is fully booked for the requested slot" % standard_id
                    )
                occupancy_id = str(uuid4())
                try:
                    connection.execute(
                        "INSERT INTO occupancies(id, standard_id, workorder_id, slot_start, slot_end, status, created_at) "
                        "VALUES (?, ?, ?, ?, ?, 'active', ?)",
                        (occupancy_id, standard_id, workorder_id, slot_start, slot_end, now),
                    )
                except sqlite3.IntegrityError:
                    connection.rollback()
                    raise ConflictError(
                        "workorder already has an active occupancy: " + workorder_id
                    )
            current = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (workorder_id,)
            ).fetchone()
            if not current:
                connection.rollback()
                raise NotFoundError("workorder not found: " + workorder_id)
            current_version = int(current["version"])
            if expected_version is not None and current_version != int(expected_version):
                connection.rollback()
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            cursor = connection.execute(
                "UPDATE entities SET status = 'scheduled', version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (payload, now, workorder_id, current_version),
            )
            if cursor.rowcount == 0:
                connection.rollback()
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.commit()
        except (CapacityFull, ConflictError, NotFoundError):
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(workorder_id)

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
