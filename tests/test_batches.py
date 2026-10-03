import json
import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES, scope_covers


def make_item(service, ref, severity="minor", quantity=1.0, scene="A车间",
              shift="夜班", injury="擦伤", actor="rpt"):
    return service.create_item({
        "title": f"事故{ref}", "description": f"经过{ref}", "severity": severity,
        "quantity": quantity, "threshold": 5, "external_ref": ref,
        "scene": scene, "shift": shift, "injury": injury,
    }, actor, "reporter")


class BatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.primary = make_item(self.service, "OI-P", "moderate", 2.0)
        self.batch = self.service.create_batch({
            "primary_item_id": self.primary["id"],
        }, "sm", "safety_manager")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _batch(self):
        return self.service.batch_detail(self.batch["id"], "viewer")

    def test_create_batch_keeps_primary_identity(self):
        detail = self._batch()
        self.assertEqual(detail["primary_item_id"], self.primary["id"])
        self.assertEqual(detail["primary"]["external_ref"], "OI-P")
        self.assertEqual(detail["primary"]["status"], STATES[0])
        self.assertEqual(detail["version"], 1)
        self.assertEqual(detail["members"][0]["role"], "primary")
        self.assertEqual(detail["members"][0]["injury"], "擦伤")

    def test_merge_recomputes_severity_deadline_and_open_items(self):
        linked = make_item(self.service, "OI-L1", "fatal", 3.0, injury="骨折")
        before_deadline = self._batch()["deadline_hours"]
        result = self.service.merge_item(self.batch["id"], {
            "item_id": linked["id"], "evidence_scopes": ["A车间:压机"],
        }, "rpt", "reporter")
        self.assertTrue(result["accepted"])
        detail = self._batch()
        # 主事故保留原编号与状态，严重度提升到fatal，伤害指数求和
        self.assertEqual(detail["primary"]["id"], self.primary["id"])
        self.assertEqual(detail["primary"]["external_ref"], "OI-P")
        self.assertEqual(detail["primary"]["status"], STATES[0])
        self.assertEqual(detail["severity"], "fatal")
        self.assertEqual(detail["primary"]["quantity"], 5.0)
        self.assertLess(detail["deadline_hours"], before_deadline)
        self.assertEqual(detail["primary"]["version"], self.primary["version"] + 1)
        self.assertEqual(detail["version"], 2)
        # 关联事故保留各自经过与伤情
        linked_member = [m for m in detail["members"] if m["item_id"] == linked["id"]][0]
        self.assertEqual(linked_member["injury"], "骨折")
        self.assertEqual(linked_member["narrative"], "经过OI-L1")
        self.assertEqual(linked_member["status"], STATES[0])
        # 重算结果留痕
        rec = detail["recomputations"][-1]
        self.assertEqual(rec["before"]["severity"], "moderate")
        self.assertEqual(rec["after"]["severity"], "fatal")

    def test_duplicate_merge_accepted_once_returns_current_version(self):
        linked = make_item(self.service, "OI-D", "minor", 1.0)
        payload = {"item_id": linked["id"], "evidence_scopes": [],
                   "idempotency_key": "KEY-1"}
        first = self.service.merge_item(self.batch["id"], payload, "rpt", "reporter")
        self.assertTrue(first["accepted"])
        self.assertNotIn("duplicate", first)
        # 同请求（第二个上报入口）再提交：拒绝接受，返回当前批次版本
        second = self.service.merge_item(self.batch["id"], payload, "rpt", "reporter")
        self.assertTrue(second["duplicate"])
        self.assertFalse(second["accepted"])
        self.assertEqual(second["id"], self.batch["id"])
        self.assertEqual(second["version"], first["version"])
        detail = self._batch()
        self.assertEqual(len(detail["members"]), 2)

    def test_scene_or_shift_mismatch_rejected(self):
        other_scene = make_item(self.service, "OI-S", "minor", 1.0,
                                scene="B车间", shift="夜班")
        with self.assertRaises(ConflictError):
            self.service.merge_item(self.batch["id"],
                                    {"item_id": other_scene["id"], "evidence_scopes": []},
                                    "rpt", "reporter")
        other_shift = make_item(self.service, "OI-T", "minor", 1.0,
                                scene="A车间", shift="白班")
        with self.assertRaises(ConflictError):
            self.service.merge_item(self.batch["id"],
                                    {"item_id": other_shift["id"], "evidence_scopes": []},
                                    "rpt", "reporter")

    def test_item_cannot_join_two_batches(self):
        linked = make_item(self.service, "OI-X", "minor", 1.0)
        self.service.merge_item(self.batch["id"],
                                {"item_id": linked["id"], "evidence_scopes": []},
                                "rpt", "reporter")
        other_primary = make_item(self.service, "OI-Q", "minor", 1.0)
        other_batch = self.service.create_batch(
            {"primary_item_id": other_primary["id"]}, "sm", "safety_manager")
        with self.assertRaises(ConflictError):
            self.service.merge_item(other_batch["id"],
                                    {"item_id": linked["id"], "evidence_scopes": []},
                                    "rpt", "reporter")

    def test_verified_measure_valid_until_scope_covered(self):
        linked = make_item(self.service, "OI-M", "serious", 1.0)
        # 主事故上登记并验证一条措施
        rec = self.service.add_record(self.primary["id"], {
            "kind": "corrective_action", "detail": "加装防护罩",
            "measure_scope": "A车间:压机", "external_ref": "CA-1",
        }, "inv", "investigator")
        self.service.verify_measure(self.primary["id"], rec["id"],
                                    {"reason": "现场验证通过"}, "sm", "safety_manager")
        verified = self.repo.get_record(rec["id"])
        self.assertEqual(verified["verified_status"], "verified")
        self.assertEqual(verified["status"], "closed")

        # 新证据范围不覆盖原范围：措施仍有效
        r1 = self.service.merge_item(self.batch["id"], {
            "item_id": linked["id"], "evidence_scopes": ["A车间:仓库"],
        }, "rpt", "reporter")
        self.assertEqual(r1["valid_verified_record_ids"], [rec["id"]])
        self.assertEqual(r1["covered_record_ids"], [])
        self.assertEqual(self.repo.get_record(rec["id"])["status"], "closed")

        # 后续事故带来覆盖原范围的新证据：退回复核、重新打开、记录原因
        linked2 = make_item(self.service, "OI-M2", "minor", 1.0)
        r2 = self.service.merge_item(self.batch["id"], {
            "item_id": linked2["id"], "evidence_scopes": ["A车间:压机:急停"],
        }, "rpt", "reporter")
        self.assertEqual(r2["covered_record_ids"], [rec["id"]])
        reopened = self.repo.get_record(rec["id"])
        self.assertEqual(reopened["status"], "open")
        self.assertEqual(reopened["verified_status"], "reopened")
        self.assertIn("覆盖", reopened["reopen_reason"])
        review = self._batch()["measure_reviews"][-1]
        self.assertEqual(review["review_type"], "reopened")
        self.assertEqual(review["record_id"], rec["id"])
        self.assertIn("A车间:压机:急停", review["covered_by_scope"])
        # 退回后重新计入未关闭事项
        self.assertEqual(self._batch()["open_items"], 1)

    def test_audit_failure_then_replay_original_request_recovers(self):
        linked = make_item(self.service, "OI-R", "serious", 4.0)
        payload = {"item_id": linked["id"], "evidence_scopes": ["A车间:压机"],
                   "idempotency_key": "REC-1"}
        # 审核写入失败：业务已提交，成员处于pending_audit
        self.repo.audit_fail = True
        result = self.service.merge_item(self.batch["id"], payload, "rpt", "reporter")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["member_state"], "pending_audit")
        self.assertTrue(self.repo.verify_audit_chain())
        self.assertEqual(
            self.service.list_batches("viewer")[0]["member_count"], 2)
        # 恢复后用同一原请求重放
        self.repo.audit_fail = False
        recovered = self.service.merge_item(self.batch["id"], payload, "rpt", "reporter")
        self.assertTrue(recovered["accepted"])
        self.assertEqual(recovered["member_state"], "joined")
        member = [m for m in recovered["members"] if m["item_id"] == linked["id"]][0]
        self.assertEqual(member["join_state"], "joined")
        self.assertTrue(self.repo.verify_audit_chain())
        events = self.service.audit("safety_manager", self.batch["id"])
        actions = [e["action"] for e in events]
        self.assertIn("batch_merge", actions)
        self.assertIn("batch_recompute", actions)
        # 再重放只算重复
        again = self.service.merge_item(self.batch["id"], payload, "rpt", "reporter")
        self.assertTrue(again["duplicate"])

    def test_explicit_recover_endpoint(self):
        linked = make_item(self.service, "OI-E", "minor", 1.0)
        payload = {"item_id": linked["id"], "evidence_scopes": [],
                   "idempotency_key": "EXP-1"}
        self.repo.audit_fail = True
        self.service.merge_item(self.batch["id"], payload, "rpt", "reporter")
        self.repo.audit_fail = False
        out = self.service.recover_merge(self.batch["id"],
                                         {"item_id": linked["id"]}, "sm",
                                         "safety_manager")
        self.assertTrue(out["recovered"])

    def test_batch_closing_requires_all_members_records_closed(self):
        linked = make_item(self.service, "OI-C", "serious", 6.0)
        self.service.merge_item(self.batch["id"], {
            "item_id": linked["id"], "evidence_scopes": [],
        }, "rpt", "reporter")
        # 关联事故上有未关闭事项
        self.service.add_record(linked["id"], {
            "kind": "action", "detail": "待整改", "status": "open",
            "external_ref": "OPEN-1",
        }, "inv", "investigator")
        current = self.service.get_item(self.primary["id"], "viewer")
        # 主事故推进到verification
        current = self.service.transition(current["id"], "investigating",
                                          current["version"], "inv", "investigator")
        current = self.service.transition(current["id"], "corrective_action",
                                          current["version"], "inv", "investigator")
        current = self.service.transition(current["id"], "verification",
                                          current["version"], "sm", "safety_manager")
        with self.assertRaises(ConflictError):
            self.service.transition(current["id"], "closed", current["version"],
                                    "sm", "safety_manager")

    def test_audit_shows_merge_source_recompute_and_return(self):
        linked = make_item(self.service, "OI-A", "fatal", 8.0)
        rec = self.service.add_record(self.primary["id"], {
            "kind": "corrective_action", "detail": "围栏",
            "measure_scope": "A车间", "external_ref": "CA-9",
        }, "inv", "investigator")
        self.service.verify_measure(self.primary["id"], rec["id"],
                                    {"reason": "ok"}, "sm", "safety_manager")
        self.service.merge_item(self.batch["id"], {
            "item_id": linked["id"], "evidence_scopes": ["*"],
        }, "rpt", "reporter")
        events = self.service.audit("safety_manager", self.batch["id"])
        merge_event = [e for e in events if e["action"] == "batch_merge"][0]
        self.assertEqual(merge_event["detail"]["linked_item_id"], linked["id"])
        self.assertEqual(merge_event["detail"]["severity"]["after"], "fatal")
        reopen_event = [e for e in events if e["action"] == "measure_reopened"][0]
        self.assertEqual(reopen_event["detail"]["record_id"], rec["id"])
        self.assertTrue(self.repo.verify_audit_chain())

    def test_legacy_item_without_batch_works_as_single(self):
        # 旧事故：只有item字段、无任何批次关联，完整流转不变
        legacy = self.service.create_item({
            "title": "旧事故", "description": "历史数据", "severity": "minor",
            "quantity": 1, "threshold": 5,
        }, "old", "reporter")
        self.service.add_record(legacy["id"], {
            "kind": "note", "detail": "已处理", "status": "closed",
        }, "inv", "investigator")
        current = legacy
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"], "rv",
                TRANSITION_ROLES[target][0])
        self.assertEqual(current["status"], "closed")
        self.assertIsNone(self.repo.active_member_row(legacy["id"]))

    def test_schema_migration_on_old_database(self):
        # 模拟旧库（无scene/injury/measure_scope列、无批次表）
        path = str(Path(self.tmp.name) / "legacy.db")
        import sqlite3
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE items(id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT, description TEXT, severity TEXT, quantity REAL, threshold REAL,
            status TEXT, version INTEGER, external_ref TEXT, created_by TEXT,
            created_at TEXT, updated_at TEXT)""")
        conn.execute("""CREATE TABLE records(id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER, kind TEXT, detail TEXT, status TEXT, external_ref TEXT,
            created_by TEXT, created_at TEXT)""")
        conn.execute("""CREATE TABLE audit_events(id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT, entity_type TEXT, entity_id INTEGER, actor TEXT, detail TEXT,
            previous_hash TEXT, entry_hash TEXT UNIQUE, created_at TEXT)""")
        conn.execute("""INSERT INTO items(title,description,severity,quantity,threshold,
            status,version,created_by,created_at,updated_at)
            VALUES('旧','描述','minor',1,5,'reported',1,'old','2024-01-01T00:00:00+00:00',
            '2024-01-01T00:00:00+00:00')""")
        conn.commit()
        conn.close()
        repo = Repository(path)
        try:
            row = repo.get_item(1)
            self.assertIsNone(row["scene"])
            self.assertIsNone(row["injury"])
            svc = Service(repo)
            got = svc.get_item(1, "viewer")
            self.assertEqual(got["title"], "旧")
            # 旧事故仍可单事故使用
            svc.add_record(1, {"kind": "n", "detail": "d", "status": "closed"},
                           "inv", "investigator")
        finally:
            repo.close()


class ScopeRulesTest(unittest.TestCase):
    def test_scope_coverage(self):
        self.assertTrue(scope_covers("*", "A车间:压机"))
        self.assertTrue(scope_covers("A车间:压机", "A车间:压机"))
        self.assertTrue(scope_covers("A车间:压机:急停", "A车间:压机"))
        self.assertFalse(scope_covers("A车间:仓库", "A车间:压机"))
        self.assertFalse(scope_covers("A车间:压机2", "A车间:压机"))


class ConcurrentMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.primary = make_item(self.service, "OI-CP", "minor", 1.0)
        self.batch = self.service.create_batch(
            {"primary_item_id": self.primary["id"]}, "sm", "safety_manager")
        self.linked = make_item(self.service, "OI-CL", "minor", 1.0)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_two_entrances_submit_concurrently_only_one_accepted(self):
        results = []
        errors = []

        def submit(key):
            try:
                out = self.service.merge_item(self.batch["id"], {
                    "item_id": self.linked["id"], "evidence_scopes": [],
                    "idempotency_key": key,
                }, "rpt", "reporter")
                results.append(out)
            except ConflictError as exc:
                errors.append(str(exc))

        t1 = threading.Thread(target=submit, args=("K1",))
        t2 = threading.Thread(target=submit, args=("K2",))
        t1.start(); t2.start(); t1.join(); t2.join()
        accepted = [r for r in results if r.get("accepted")]
        duplicates = [r for r in results if r.get("duplicate")]
        self.assertEqual(len(accepted), 1, results)
        self.assertEqual(len(accepted) + len(duplicates) + len(errors), 2)
        detail = self.service.batch_detail(self.batch["id"], "viewer")
        self.assertEqual(len(detail["members"]), 2)


if __name__ == "__main__":
    unittest.main()
