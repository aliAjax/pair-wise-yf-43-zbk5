import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


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
                CREATE TABLE IF NOT EXISTS occupancy (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    work_order_id TEXT NOT NULL,
                    standard_id TEXT NOT NULL,
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    released_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_occupancy_one_active
                    ON occupancy(work_order_id) WHERE state = 'held';
                CREATE INDEX IF NOT EXISTS idx_occupancy_standard_time
                    ON occupancy(standard_id, start_at, end_at);
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

    @staticmethod
    def _occupancy_from_row(row):
        return {
            "id": row["id"],
            "work_order_id": row["work_order_id"],
            "standard_id": row["standard_id"],
            "start_at": row["start_at"],
            "end_at": row["end_at"],
            "state": row["state"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "released_at": row["released_at"],
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

    def get_entity(self, entity_id, connection=None):
        sql = "SELECT * FROM entities WHERE id = ?"
        params = (entity_id,)
        if connection is None:
            with self._connect() as conn:
                row = conn.execute(sql, params).fetchone()
        else:
            row = connection.execute(sql, params).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None, connection=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = "SELECT * FROM entities" + where + " ORDER BY created_at, id"
        if connection is None:
            with self._connect() as conn:
                rows = conn.execute(sql, params).fetchall()
        else:
            rows = connection.execute(sql, params).fetchall()
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

    # -- 占用账事务原语 -----------------------------------------------------

    def _append_audit_tx(self, connection, entity_id, actor, action, from_status,
                         to_status, detail=None):
        connection.execute(
            "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, "
            "to_status, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_id,
                actor.user_id,
                actor.role,
                action,
                from_status,
                to_status,
                json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
                utcnow(),
            ),
        )

    @staticmethod
    def _set_entity_tx(connection, entity_id, version, status, data):
        connection.execute(
            "UPDATE entities SET status = ?, version = ?, data = ?, updated_at = ? WHERE id = ?",
            (status, version, json.dumps(data, ensure_ascii=False, sort_keys=True),
             utcnow(), entity_id),
        )

    def _held_rows(self, connection, standard_id, start_at, end_at):
        return connection.execute(
            "SELECT * FROM occupancy WHERE state = 'held' AND standard_id = ? "
            "AND start_at < ? AND end_at > ?",
            (standard_id, end_at, start_at),
        ).fetchall()

    def hold_work_order(self, work_order, standard, actor, action="schedule", detail=None):
        """先到先占：容量内写入 held，容量满抛 ConflictError；整体一事务。"""
        order_id = work_order["id"]
        start_at = work_order["data"]["start_at"]
        end_at = work_order["data"]["end_at"]
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            locked = self.get_entity(order_id, connection=connection)
            if locked is None:
                raise NotFoundError("entity not found: " + order_id)
            std = self.get_entity(standard["id"], connection=connection)
            if std is None or std["kind"] != "standard":
                raise NotFoundError("standard not found: " + standard["id"])
            if locked["status"] not in ("pending", "queued"):
                raise ConflictError(
                    "work order %s is not waiting for a slot (status=%s)"
                    % (order_id, locked["status"])
                )
            if std["status"] != "available":
                raise ConflictError("standard %s is not available" % std["id"])
            if not (std["data"]["valid_from"] <= start_at and end_at <= std["data"]["valid_until"]):
                raise ConflictError("standard validity does not cover the whole work window")
            concurrent = self._held_rows(connection, std["id"], start_at, end_at)
            capacity = int(std["data"].get("capacity", 1))
            if len(concurrent) >= capacity:
                ids = ", ".join(row["work_order_id"] for row in concurrent)
                raise ConflictError(
                    "standard capacity full for the window; held by: " + ids
                )
            now = utcnow()
            connection.execute(
                "INSERT INTO occupancy(work_order_id, standard_id, start_at, end_at, "
                "state, created_by, created_at) VALUES (?, ?, ?, ?, 'held', ?, ?)",
                (order_id, std["id"], start_at, end_at, actor.user_id, now),
            )
            next_version = int(locked["version"]) + 1
            merged = dict(locked["data"])
            merged["scheduled_by"] = actor.user_id
            merged["scheduled_at"] = now
            self._set_entity_tx(connection, order_id, next_version, "scheduled", merged)
            self._append_audit_tx(
                connection, order_id, actor, action, locked["status"], "scheduled",
                detail or {"standard_id": std["id"], "start_at": start_at,
                           "end_at": end_at},
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(order_id)

    def enqueue_work_order(self, work_order, standard, actor):
        order_id = work_order["id"]
        start_at = work_order["data"]["start_at"]
        end_at = work_order["data"]["end_at"]
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            locked = self.get_entity(order_id, connection=connection)
            std = self.get_entity(standard["id"], connection=connection)
            if locked is None or std is None:
                raise NotFoundError("entity not found")
            if locked["status"] != "pending":
                raise ConflictError(
                    "work order %s cannot be queued from status %s"
                    % (order_id, locked["status"])
                )
            if std["status"] != "available":
                raise ConflictError("standard %s is not available" % std["id"])
            if not (std["data"]["valid_from"] <= start_at and end_at <= std["data"]["valid_until"]):
                raise ConflictError("standard validity does not cover the whole work window")
            now = utcnow()
            merged = dict(locked["data"])
            merged["queued_by"] = actor.user_id
            merged["queued_at"] = now
            self._set_entity_tx(
                connection, order_id, int(locked["version"]) + 1, "queued", merged
            )
            self._append_audit_tx(
                connection, order_id, actor, "enqueue", "pending", "queued",
                {"standard_id": std["id"]},
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(order_id)

    def release_occupancy(self, connection, order_id, reason):
        now = utcnow()
        connection.execute(
            "UPDATE occupancy SET state = 'released', released_at = ? "
            "WHERE work_order_id = ? AND state = 'held'",
            (now, order_id),
        )
        return now

    def finish_work_order(self, work_order, actor, result, completed_at):
        order_id = work_order["id"]
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            locked = self.get_entity(order_id, connection=connection)
            if locked is None:
                raise NotFoundError("entity not found: " + order_id)
            if locked["status"] != "scheduled":
                raise ConflictError("only scheduled work orders can complete")
            held = connection.execute(
                "SELECT * FROM occupancy WHERE work_order_id = ? AND state = 'held'",
                (order_id,),
            ).fetchone()
            if held is None:
                raise ConflictError("work order %s has no active occupancy" % order_id)
            now = utcnow()
            connection.execute(
                "UPDATE occupancy SET state = 'completed', released_at = ? "
                "WHERE work_order_id = ? AND state = 'held'",
                (now, order_id),
            )
            merged = dict(locked["data"])
            merged["result"] = result
            merged["completed_at"] = completed_at
            merged["completed_by"] = actor.user_id
            self._set_entity_tx(
                connection, order_id, int(locked["version"]) + 1, "completed", merged
            )
            self._append_audit_tx(
                connection, order_id, actor, "complete", "scheduled", "completed",
                {"result": result, "completed_at": completed_at},
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(order_id)

    def cancel_work_order(self, work_order, actor, reason):
        order_id = work_order["id"]
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            locked = self.get_entity(order_id, connection=connection)
            if locked is None:
                raise NotFoundError("entity not found: " + order_id)
            if locked["status"] not in ("scheduled", "queued"):
                raise ConflictError("only scheduled/queued work orders can be cancelled")
            now = self.release_occupancy(connection, order_id, "cancelled")
            merged = dict(locked["data"])
            merged["cancel_reason"] = reason
            merged["cancelled_by"] = actor.user_id
            merged["cancelled_at"] = now
            self._set_entity_tx(
                connection, order_id, int(locked["version"]) + 1, "cancelled", merged
            )
            self._append_audit_tx(
                connection, order_id, actor, "cancel", locked["status"], "cancelled",
                {"reason": reason},
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(order_id)

    def void_work_order(self, work_order, actor, reason):
        """作废退回待排并释放占用（与标准器联动作废同一条路径）。"""
        order_id = work_order["id"]
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            locked = self.get_entity(order_id, connection=connection)
            if locked is None:
                raise NotFoundError("entity not found: " + order_id)
            if locked["status"] not in ("scheduled", "queued"):
                raise ConflictError("only scheduled/queued work orders can be voided")
            now = self.release_occupancy(connection, order_id, "void")
            merged = dict(locked["data"])
            merged["void_reason"] = reason
            merged["voided_at"] = now
            self._set_entity_tx(
                connection, order_id, int(locked["version"]) + 1, "pending", merged
            )
            self._append_audit_tx(
                connection, order_id, actor, "void", locked["status"], "pending",
                {"reason": reason},
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(order_id)

    def change_standard_status(self, standard, next_status, actor, action, patch):
        """标准器状态变化：未完成工单一律作废退回待排；已完成校准保留。"""
        std_id = standard["id"]
        connection = self._connect()
        affected = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            locked = self.get_entity(std_id, connection=connection)
            if locked is None:
                raise NotFoundError("standard not found: " + std_id)
            merged = dict(locked["data"])
            merged.update(patch)
            self._set_entity_tx(
                connection, std_id, int(locked["version"]) + 1, next_status, merged
            )
            self._append_audit_tx(
                connection, std_id, actor, action, locked["status"], next_status,
                {"patch": patch},
            )
            open_orders = connection.execute(
                "SELECT * FROM entities WHERE kind = 'work_order' "
                "AND status IN ('scheduled', 'queued') ORDER BY created_at, id"
            ).fetchall()
            now = utcnow()
            for row in open_orders:
                order = self._entity_from_row(row)
                if order["data"].get("standard_id") != std_id:
                    continue
                self.release_occupancy(connection, order["id"], "standard:" + action)
                order_data = dict(order["data"])
                order_data["void_reason"] = "standard %s" % action
                order_data["voided_at"] = now
                self._set_entity_tx(
                    connection, order["id"], int(order["version"]) + 1,
                    "pending", order_data,
                )
                self._append_audit_tx(
                    connection, order["id"], actor, "void",
                    order["status"], "pending",
                    {"reason": "standard %s" % action, "standard_id": std_id},
                )
                affected.append(order["id"])
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(std_id), affected

    def renew_standard(self, standard, actor, patch):
        std_id = standard["id"]
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            locked = self.get_entity(std_id, connection=connection)
            if locked is None:
                raise NotFoundError("standard not found: " + std_id)
            merged = dict(locked["data"])
            merged.update(patch)
            self._set_entity_tx(
                connection, std_id, int(locked["version"]) + 1, locked["status"], merged
            )
            self._append_audit_tx(
                connection, std_id, actor, "renew", locked["status"],
                locked["status"], {"patch": patch},
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(std_id)

    def list_occupancy(self, standard_id=None, state=None):
        clauses = []
        params = []
        if standard_id:
            clauses.append("standard_id = ?")
            params.append(standard_id)
        if state:
            clauses.append("state = ?")
            params.append(state)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM occupancy" + where + " ORDER BY start_at, id", params
            ).fetchall()
        return [self._occupancy_from_row(row) for row in rows]

    def list_queued_for_standard(self, standard_id):
        return [
            order
            for order in self.list_entities(kind="work_order", status="queued")
            if order["data"].get("standard_id") == standard_id
        ]

    def reconcile_occupancy(self):
        """启动恢复：held 占用必须对应 scheduled 工单；孤儿占用退回释放，
        防止“排程写入失败后重复占位”。"""
        connection = self._connect()
        repaired = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM occupancy WHERE state = 'held'"
            ).fetchall()
            now = utcnow()
            for row in rows:
                order = self.get_entity(row["work_order_id"], connection=connection)
                if order is not None and order["status"] == "scheduled":
                    continue
                connection.execute(
                    "UPDATE occupancy SET state = 'released', released_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                repaired.append(row["work_order_id"])
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return repaired

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

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
