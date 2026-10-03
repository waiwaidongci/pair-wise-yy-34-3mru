from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import BATCH_ENTITY, BATCH_ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        # 测试钩子：置为True时审计写入失败，用于验证审核失败后的可恢复性
        self.audit_fail = False
        self._create_schema()

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
                    updated_at TEXT NOT NULL,
                    scene TEXT,
                    shift TEXT,
                    injury TEXT
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
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    measure_scope TEXT,
                    verified_status TEXT
                        CHECK(verified_status IS NULL OR verified_status IN ('verified','reopened')),
                    reopen_reason TEXT,
                    UNIQUE(item_id, external_ref)
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
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    scene TEXT NOT NULL,
                    shift TEXT NOT NULL,
                    primary_item_id INTEGER NOT NULL REFERENCES items(id),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    item_id INTEGER NOT NULL REFERENCES items(id),
                    role TEXT NOT NULL CHECK(role IN ('primary','linked')),
                    join_seq INTEGER NOT NULL,
                    idempotency_key TEXT,
                    request_json TEXT,
                    join_state TEXT NOT NULL DEFAULT 'joined'
                        CHECK(join_state IN ('joined','pending_audit','rejected')),
                    reject_reason TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(batch_id, item_id),
                    UNIQUE(batch_id, idempotency_key)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_batch_members_item
                    ON batch_members(item_id) WHERE join_state IN ('joined','pending_audit');
                CREATE TABLE IF NOT EXISTS batch_recomputations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    trigger_item_id INTEGER NOT NULL REFERENCES items(id),
                    seq INTEGER NOT NULL,
                    before_json TEXT NOT NULL,
                    after_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_measure_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    item_id INTEGER NOT NULL REFERENCES items(id),
                    review_type TEXT NOT NULL CHECK(review_type IN ('reopened','verified')),
                    measure_scope TEXT NOT NULL,
                    covered_by_scope TEXT,
                    reason TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    flushed INTEGER NOT NULL DEFAULT 0
                );
            """)
            # 旧库迁移：没有批次字段的旧事故按单事故继续使用，列全部可空
            self._ensure_column("items", "scene", "TEXT")
            self._ensure_column("items", "shift", "TEXT")
            self._ensure_column("items", "injury", "TEXT")
            self._ensure_column("records", "measure_scope", "TEXT")
            self._ensure_column(
                "records", "verified_status",
                "TEXT CHECK(verified_status IS NULL OR verified_status IN ('verified','reopened'))")
            self._ensure_column("records", "reopen_reason", "TEXT")

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        cols = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    # ---------------------------------------------------------------- items
    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str, scene: Optional[str] = None,
                    shift: Optional[str] = None,
                    injury: Optional[str] = None) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at,
                       scene, shift, injury)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now, scene, shift, injury),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("事故不存在")
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
                    raise NotFoundError("事故不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    # -------------------------------------------------------------- records
    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str,
                   measure_scope: Optional[str] = None) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at, measure_scope) VALUES(?,?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now, measure_scope),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def get_record(self, record_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFoundError("事项不存在")
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

    def mark_record_verified(self, record_id: int, actor: str,
                             reason: Optional[str]) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE records SET status='closed', verified_status='verified',
                   reopen_reason=NULL WHERE id=?""",
                (record_id,),
            )
            if cur.rowcount == 0:
                raise NotFoundError("事项不存在")
        return self.get_record(record_id)

    def reopen_record(self, record_id: int, reason: str, actor: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE records SET status='open', verified_status='reopened',
                   reopen_reason=? WHERE id=?""",
                (reason, record_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("事项不存在")
        return self.get_record(record_id)

    # -------------------------------------------------------------- batches
    def create_batch(self, scene: str, shift: str, primary_item_id: int,
                     actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            self.get_item(primary_item_id)
            n = self.conn.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"]
            batch_no = f"{BATCH_ID_PREFIX}-{n + 1:06d}"
            cur = self.conn.execute(
                """INSERT INTO batches(batch_no, scene, shift, primary_item_id, version,
                   created_by, created_at, updated_at) VALUES(?,?,?,?,1,?,?,?)""",
                (batch_no, scene, shift, primary_item_id, actor, now, now),
            )
            batch_id = int(cur.lastrowid)
            self.conn.execute(
                """INSERT INTO batch_members(batch_id, item_id, role, join_seq,
                   join_state, created_by, created_at)
                   VALUES(?,?,'primary',1,'joined',?,?)""",
                (batch_id, primary_item_id, actor, now),
            )
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFoundError("批次不存在")
        return dict(row)

    def list_batches(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM batches ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

    def active_member_row(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM batch_members WHERE item_id=?
                   AND join_state IN ('joined','pending_audit')""",
                (item_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_member_rows(self, batch_id: int,
                         states=("joined", "pending_audit")) -> List[Dict[str, Any]]:
        placeholders = ",".join("?" for _ in states)
        with self._lock:
            rows = self.conn.execute(
                f"""SELECT * FROM batch_members WHERE batch_id=?
                    AND join_state IN ({placeholders}) ORDER BY join_seq""",
                (batch_id, *states),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_member_items(self, batch_id: int,
                          states=("joined", "pending_audit")) -> List[Dict[str, Any]]:
        placeholders = ",".join("?" for _ in states)
        with self._lock:
            rows = self.conn.execute(
                f"""SELECT i.* FROM items i JOIN batch_members m ON m.item_id=i.id
                    WHERE m.batch_id=? AND m.join_state IN ({placeholders})
                    ORDER BY m.join_seq""",
                (batch_id, *states),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_member_row(self, batch_id: int, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM batch_members WHERE batch_id=? AND item_id=?",
                (batch_id, item_id),
            ).fetchone()
        return dict(row) if row else None

    def verified_records_for_batch(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT r.* FROM records r JOIN batch_members m ON m.item_id=r.item_id
                   WHERE m.batch_id=? AND m.join_state IN ('joined','pending_audit')
                   AND r.verified_status='verified' AND r.measure_scope IS NOT NULL
                   ORDER BY r.id""",
                (batch_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def apply_batch_merge(self, batch_id: int, item: Dict[str, Any],
                          new_severity: str, new_quantity: float,
                          recompute_before: Dict[str, Any],
                          recompute_after: Dict[str, Any],
                          covered: List[Dict[str, Any]],
                          idempotency_key: Optional[str],
                          request_json: Dict[str, Any], actor: str) -> Dict[str, Any]:
        """在单个事务内完成并入、重算与措施退回，并写入审计outbox。

        业务表与outbox同事务提交，保证审核写入失败后可按原请求恢复。
        返回 {batch, member, recomputation, reviews}。
        """
        now = utc_now()
        item_id = item["id"]
        with self._lock, self.conn:
            batch = self.get_batch(batch_id)
            # 同批次重复提交（并发时唯一索引的前置检查）
            dup = self.conn.execute(
                """SELECT * FROM batch_members WHERE batch_id=? AND item_id=?
                   AND join_state IN ('joined','pending_audit')""",
                (batch_id, item_id),
            ).fetchone()
            if dup is not None:
                raise ConflictError("该关联事故已在本批次中")
            other = self.conn.execute(
                """SELECT batch_id FROM batch_members WHERE item_id=?
                   AND join_state IN ('joined','pending_audit') AND batch_id<>?""",
                (item_id, batch_id),
            ).fetchone()
            if other is not None:
                raise ConflictError("该事故已属于其他调查批次")
            seq_row = self.conn.execute(
                "SELECT COALESCE(MAX(join_seq),0)+1 AS seq FROM batch_members WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            join_seq = int(seq_row["seq"])
            try:
                cur = self.conn.execute(
                    """INSERT INTO batch_members(batch_id, item_id, role, join_seq,
                       idempotency_key, request_json, join_state, created_by, created_at)
                       VALUES(?,?,'linked',?,?,?,'pending_audit',?,?)""",
                    (batch_id, item_id, join_seq, idempotency_key,
                     json.dumps(request_json, ensure_ascii=False, sort_keys=True), actor, now),
                )
                member_id = int(cur.lastrowid)
            except sqlite3.IntegrityError as exc:
                raise ConflictError("该关联事故或幂等键已提交过") from exc
            # 重算主事故（保留原编号、状态；刷新严重度/伤害指数/版本）
            self.conn.execute(
                """UPDATE items SET severity=?, quantity=?, version=version+1, updated_at=?
                   WHERE id=?""",
                (new_severity, new_quantity, now, batch["primary_item_id"]),
            )
            rec_row = self.conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS seq FROM batch_recomputations WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            cur = self.conn.execute(
                """INSERT INTO batch_recomputations(batch_id, trigger_item_id, seq,
                   before_json, after_json, created_at) VALUES(?,?,?,?,?,?)""",
                (batch_id, item_id, int(rec_row["seq"]),
                 json.dumps(recompute_before, ensure_ascii=False, sort_keys=True),
                 json.dumps(recompute_after, ensure_ascii=False, sort_keys=True), now),
            )
            recomputation_id = int(cur.lastrowid)
            reviews: List[Dict[str, Any]] = []
            for measure, hit_scopes in covered:
                reason = (
                    f"并入事故{item_id}的新证据范围{','.join(hit_scopes)}"
                    f"覆盖已验证措施原范围{measure['measure_scope']}，退回复核"
                )
                self.conn.execute(
                    """UPDATE records SET status='open', verified_status='reopened',
                       reopen_reason=? WHERE id=?""",
                    (reason, measure["id"]),
                )
                cur = self.conn.execute(
                    """INSERT INTO batch_measure_reviews(batch_id, record_id, item_id,
                       review_type, measure_scope, covered_by_scope, reason, created_by,
                       created_at) VALUES(?,?,?,'reopened',?,?,?,?,?)""",
                    (batch_id, measure["id"], measure["item_id"],
                     measure["measure_scope"], ",".join(hit_scopes), reason, actor, now),
                )
                reviews.append({"id": int(cur.lastrowid), "record_id": measure["id"],
                                "reason": reason})
            self.conn.execute(
                "UPDATE batches SET version=version+1, updated_at=? WHERE id=?",
                (now, batch_id),
            )
            events = [
                ("batch_merge", batch_id, {
                    "batch_no": batch["batch_no"], "linked_item_id": item_id,
                    "join_seq": join_seq,
                    "severity": {"before": recompute_before["severity"],
                                 "after": recompute_after["severity"]},
                    "deadline_hours": {"before": recompute_before["deadline_hours"],
                                       "after": recompute_after["deadline_hours"]},
                    "open_items": {"before": recompute_before["open_items"],
                                   "after": recompute_after["open_items"]},
                    "idempotency_key": idempotency_key,
                }),
                ("batch_recompute", batch_id, {
                    "trigger_item_id": item_id,
                    "before": recompute_before, "after": recompute_after,
                }),
            ]
            for measure, hit_scopes in covered:
                events.append(("measure_reopened", batch_id, {
                    "record_id": measure["id"], "item_id": measure["item_id"],
                    "measure_scope": measure["measure_scope"],
                    "covered_by_scope": hit_scopes,
                    "reason": next(r["reason"] for r in reviews
                                  if r["record_id"] == measure["id"]),
                }))
            for action, entity_id, detail in events:
                self._enqueue_outbox(action, BATCH_ENTITY, entity_id, actor, detail, now)
        member = self.get_member_row(batch_id, item_id)
        batch = self.get_batch(batch_id)
        return {"batch": batch, "member": member, "recomputation_id": recomputation_id,
                "reviews": reviews}

    def mark_member_joined(self, batch_id: int, item_id: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """UPDATE batch_members SET join_state='joined'
                   WHERE batch_id=? AND item_id=? AND join_state='pending_audit'""",
                (batch_id, item_id),
            )

    def reject_member(self, member_row_id: int, reason: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE batch_members SET join_state='rejected', reject_reason=? WHERE id=?",
                (reason, member_row_id),
            )

    def pending_member(self, batch_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                """SELECT * FROM batch_members WHERE batch_id=?
                   AND join_state='pending_audit' ORDER BY id LIMIT 1""",
                (batch_id,),
            ).fetchone()
        return dict(row) if row else None

    def add_measure_review(self, batch_id: int, record_id: int, item_id: int,
                           review_type: str, measure_scope: str,
                           covered_by_scope: Optional[str], reason: Optional[str],
                           actor: str) -> int:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO batch_measure_reviews(batch_id, record_id, item_id,
                   review_type, measure_scope, covered_by_scope, reason, created_by,
                   created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (batch_id, record_id, item_id, review_type, measure_scope,
                 covered_by_scope, reason, actor, now),
            )
            return int(cur.lastrowid)

    def list_recomputations(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM batch_recomputations WHERE batch_id=? ORDER BY id",
                (batch_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["before_json"] = json.loads(item["before_json"])
            item["after_json"] = json.loads(item["after_json"])
            result.append(item)
        return result

    def list_measure_reviews(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM batch_measure_reviews WHERE batch_id=? ORDER BY id",
                (batch_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ outbox
    def _enqueue_outbox(self, action: str, entity_type: str, entity_id: int,
                        actor: str, detail: dict, now: Optional[str] = None) -> None:
        self.conn.execute(
            """INSERT INTO audit_outbox(action, entity_type, entity_id, actor, detail,
               created_at, flushed) VALUES(?,?,?,?,?,?,0)""",
            (action, entity_type, entity_id, actor,
             json.dumps(detail, ensure_ascii=False, sort_keys=True), now or utc_now()),
        )

    def flush_audit_outbox(self) -> int:
        """按入队顺序把未发送的审计事件补写入审计链。失败时整体保留待重试。"""
        flushed = 0
        if self.audit_fail:
            raise RuntimeError("审计写入失败(注入)")
        while True:
            with self._lock:
                row = self.conn.execute(
                    "SELECT * FROM audit_outbox WHERE flushed=0 ORDER BY id LIMIT 1"
                ).fetchone()
            if row is None:
                break
            detail = json.loads(row["detail"])
            try:
                with self._lock, self.conn:
                    last = self.conn.execute(
                        "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                    previous = last["entry_hash"] if last else "GENESIS"
                    event = make_entry(row["action"], row["entity_type"], row["entity_id"],
                                       row["actor"], detail, previous)
                    event["created_at"] = row["created_at"]
                    from .audit import calculate_hash
                    event["entry_hash"] = calculate_hash(previous, {
                        "action": event["action"], "entity_type": event["entity_type"],
                        "entity_id": event["entity_id"], "actor": event["actor"],
                        "detail": detail, "created_at": event["created_at"],
                    })
                    self.conn.execute(
                        """INSERT INTO audit_events(action, entity_type, entity_id, actor,
                           detail, previous_hash, entry_hash, created_at)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (event["action"], event["entity_type"], event["entity_id"],
                         event["actor"],
                         json.dumps(detail, ensure_ascii=False, sort_keys=True),
                         event["previous_hash"], event["entry_hash"],
                         event["created_at"]),
                    )
                    self.conn.execute(
                        "UPDATE audit_outbox SET flushed=1 WHERE id=?", (row["id"],))
                    flushed += 1
            except sqlite3.Error:
                raise
        return flushed

    # ------------------------------------------------------------- audit
    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        if self.audit_fail:
            raise RuntimeError("审计写入失败(注入)")
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

    def list_audit(self, entity_id: Optional[int] = None,
                   entity_type: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events WHERE 1=1"
        params: List[Any] = []
        if entity_id is not None:
            sql += " AND entity_id=?"
            params.append(entity_id)
        if entity_type is not None:
            sql += " AND entity_type=?"
            params.append(entity_type)
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
