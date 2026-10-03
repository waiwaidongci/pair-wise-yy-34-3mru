from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()
        self._migrate()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    scope TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    site TEXT NOT NULL,
                    shift TEXT NOT NULL,
                    main_item_id INTEGER NOT NULL REFERENCES items(id),
                    status TEXT NOT NULL DEFAULT 'open',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(site, shift)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)

    def _migrate(self) -> None:
        """为旧库补充批次相关列，保证没有批次字段的旧事故仍按单事故使用。"""
        def columns(table: str) -> set:
            rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            return {r[1] for r in rows}
        with self._lock, self.conn:
            item_cols = columns("items")
            if "site" not in item_cols:
                self.conn.execute("ALTER TABLE items ADD COLUMN site TEXT")
            if "shift" not in item_cols:
                self.conn.execute("ALTER TABLE items ADD COLUMN shift TEXT")
            if "batch_id" not in item_cols:
                self.conn.execute("ALTER TABLE items ADD COLUMN batch_id INTEGER REFERENCES items(id)")
            record_cols = columns("records")
            if "scope" not in record_cols:
                self.conn.execute("ALTER TABLE records ADD COLUMN scope TEXT")

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str,
                   scope: Optional[str] = None) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       scope, created_by, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, scope, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def get_item_by_external_ref(self, external_ref: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM items WHERE external_ref=?", (external_ref,)
            ).fetchone()
        return None if row is None else dict(row)

    def get_or_create_batch(self, site: str, shift: str, main_item_id: int,
                            actor: str) -> Dict[str, Any]:
        """按现场+班次查找批次，不存在则以主事故身份创建（幂等）。"""
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO batches(site, shift, main_item_id, status, version,
                   created_by, created_at, updated_at) VALUES(?,?,?,'open',1,?,?,?)
                   ON CONFLICT(site, shift) DO NOTHING""",
                (site, shift, main_item_id, actor, now, now),
            )
            row = self.conn.execute(
                "SELECT * FROM batches WHERE site=? AND shift=?", (site, shift)
            ).fetchone()
        return dict(row)

    def get_batch(self, batch_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM batches WHERE id=?", (batch_id,)
            ).fetchone()
        return None if row is None else dict(row)

    def list_batches(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM batches ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def list_batch_members(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM items WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def list_batch_records(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT r.* FROM records r JOIN items i ON r.item_id=i.id
                   WHERE i.batch_id=? ORDER BY r.id""",
                (batch_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def batch_open_record_count(self, batch_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                """SELECT COUNT(*) AS n FROM records r JOIN items i ON r.item_id=i.id
                   WHERE i.batch_id=? AND r.status='open'""",
                (batch_id,),
            ).fetchone()
        return int(row["n"])

    def set_item_batch(self, item_id: int, batch_id: int) -> bool:
        """把事故并入批次，仅当尚未入批次时生效；返回是否本次新并入（幂等）。"""
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE items SET batch_id=? WHERE id=? AND batch_id IS NULL",
                (batch_id, item_id),
            )
            return cur.rowcount == 1

    def update_item_severity(self, item_id: int, severity: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE items SET severity=?, updated_at=? WHERE id=?",
                (severity, utc_now(), item_id),
            )

    def increment_batch_version(self, batch_id: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE batches SET version=version+1, updated_at=? WHERE id=?",
                (utc_now(), batch_id),
            )

    def reopen_record(self, record_id: int) -> bool:
        """把已验证措施退回未关闭状态，仅当当前为closed时生效（幂等）。"""
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE records SET status='open' WHERE id=? AND status='closed'",
                (record_id,),
            )
            return cur.rowcount == 1

    def audit_exists(self, action: str, entity_id: int,
                     detail_key: str, detail_value: Any) -> bool:
        with self._lock:
            rows = self.conn.execute(
                "SELECT detail FROM audit_events WHERE action=? AND entity_id=?",
                (action, entity_id),
            ).fetchall()
        for row in rows:
            try:
                detail = json.loads(row["detail"])
            except (TypeError, ValueError):
                continue
            if detail.get(detail_key) == detail_value:
                return True
        return False

    def latest_audit(self, action: str, entity_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM audit_events WHERE action=? AND entity_id=?
                   ORDER BY id DESC LIMIT 1""",
                (action, entity_id),
            ).fetchone()
        if row is None:
            return None
        event = dict(row)
        event["detail"] = json.loads(event["detail"])
        return event

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
