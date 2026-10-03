import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from src.http_api import make_handler
from src.repository import Repository
from src.rules import SEVERITIES
from src.service import Service


class BatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _create(self, ref, severity="minor", site="S1", shift="day",
                quantity=0, threshold=1, role="reporter"):
        payload = {"title": f"accident {ref}", "description": f"desc {ref}",
                   "severity": severity, "quantity": quantity, "threshold": threshold}
        if ref is not None:
            payload["external_ref"] = ref
        if site is not None:
            payload["site"] = site
        if shift is not None:
            payload["shift"] = shift
        return self.service.create_item(payload, "creator", role)

    def _batch_for(self, item):
        self.assertIsNotNone(item["batch"], "事故应归入批次")
        return self.service.get_batch(item["batch"]["id"], "viewer")

    # 同一现场、同班次的事故组成调查批次；主事故保留原编号与状态。
    def test_grouping_and_main_preserved(self):
        main = self._create("A-1", severity="serious")
        self.assertEqual(main["status"], "reported")
        assoc = self._create("A-2", severity="moderate")
        batch = self._batch_for(main)
        self.assertEqual(batch["member_count"], 2)
        self.assertEqual(batch["main_item_id"], main["id"])
        self.assertEqual(batch["main_item"]["id"], main["id"])
        self.assertEqual(batch["main_item"]["status"], "reported")
        self.assertEqual(batch["main_item"]["external_ref"], "A-1")
        assoc_ids = [a["id"] for a in batch["associated_items"]]
        self.assertIn(assoc["id"], assoc_ids)
        # 关联事故保留各自经过与伤情（描述不被改写）。
        self.assertEqual(self.service.get_item(assoc["id"], "viewer")["description"], "desc A-2")

    # 并入后重算主事故严重度、响应期限与未关闭事项。
    def test_recalc_severity_deadline_open_items(self):
        main = self._create("B-1", severity="minor")
        self.assertEqual(main["deadline_hours"], 72)
        assoc = self._create("B-2", severity="fatal", quantity=0, threshold=1)
        main_after = self.service.get_item(main["id"], "viewer")
        self.assertEqual(main_after["severity"], "fatal")
        self.assertEqual(main_after["deadline_hours"], 4)
        batch = self._batch_for(main)
        self.assertGreaterEqual(len(batch["recalculations"]), 1)
        recalc = batch["recalculations"][-1]["detail"]
        self.assertEqual(recalc["severity"]["before"], "minor")
        self.assertEqual(recalc["severity"]["after"], "fatal")
        self.assertEqual(recalc["deadline_hours"]["after"], 4)
        # 未关闭事项跨批次合计。
        self.service.add_record(assoc["id"], {"kind": "action", "detail": "open measure",
                                              "status": "open"}, "recorder", "investigator")
        batch = self._batch_for(main)
        self.assertEqual(batch["open_items"], 1)
        self.assertEqual(self.service.get_item(main["id"], "viewer")["open_items"], 1)

    # 通过验证的措施被新证据覆盖范围时退回复核并记录原因。
    def test_measure_returned_on_covering_evidence(self):
        main = self._create("C-1", severity="moderate")
        self.service.add_record(main["id"], {"kind": "action", "detail": "verified fix",
                                              "status": "closed", "scope": "area-A"},
                                "recorder", "investigator")
        assoc = self._create("C-2", severity="moderate")
        self.service.add_record(assoc["id"], {"kind": "evidence", "detail": "new finding",
                                              "status": "closed", "scope": "area-A"},
                                "recorder", "investigator")
        records = self.service.list_records(main["id"], "viewer")
        measure = next(r for r in records if r["kind"] == "action")
        self.assertEqual(measure["status"], "open")
        batch = self._batch_for(main)
        self.assertEqual(len(batch["returns"]), 1)
        ret = batch["returns"][0]["detail"]
        self.assertIn("退回复核", ret["reason"])
        self.assertEqual(ret["measure_scope"], "area-A")
        self.assertEqual(ret["evidence_scope"], "area-A")

    # 新证据未覆盖原范围时，已验证措施仍算有效。
    def test_measure_remains_valid_when_not_covered(self):
        main = self._create("D-1", severity="moderate")
        self.service.add_record(main["id"], {"kind": "action", "detail": "verified fix",
                                              "status": "closed", "scope": "area-A"},
                                "recorder", "investigator")
        assoc = self._create("D-2", severity="moderate")
        self.service.add_record(assoc["id"], {"kind": "evidence", "detail": "unrelated",
                                              "status": "closed", "scope": "area-B"},
                                "recorder", "investigator")
        records = self.service.list_records(main["id"], "viewer")
        measure = next(r for r in records if r["kind"] == "action")
        self.assertEqual(measure["status"], "closed")
        self.assertEqual(self._batch_for(main)["returns"], [])

    # 两个上报入口同时提交同一关联事故只接受一次，后到拿到当前批次版本。
    def test_idempotent_duplicate_submission(self):
        first = self._create("E-1", severity="moderate")
        second = self._create("E-1", severity="moderate")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["batch"]["id"], second["batch"]["id"])
        items = self.service.list_items("viewer")
        self.assertEqual(len([i for i in items if i["external_ref"] == "E-1"]), 1)
        batch = self._batch_for(first)
        self.assertEqual(batch["member_count"], 1)
        # 重复提交不重复写归并审计。
        merges = [e for e in self.service.audit("viewer") if e["action"] == "batch_merge"]
        self.assertEqual(merges, [])

    # 审核写入失败后按原请求恢复：重放不产生重复归并。
    def test_recovery_replay_no_duplicate_merge(self):
        main = self._create("F-1", severity="minor")
        assoc = self._create("F-2", severity="serious")
        batch_before = self._batch_for(main)
        self.assertEqual(batch_before["member_count"], 2)
        # 用同一请求重放关联事故。
        replay = self._create("F-2", severity="serious")
        self.assertEqual(replay["id"], assoc["id"])
        batch_after = self._batch_for(main)
        self.assertEqual(batch_after["member_count"], 2)
        merges = [e for e in self.service.audit("viewer") if e["action"] == "batch_merge"]
        self.assertEqual(len(merges), 1)
        self.assertTrue(self.repo.verify_audit_chain())

    # 旧事故没有批次字段时按单事故继续使用。
    def test_legacy_item_without_batch(self):
        legacy = self._create("G-1", site=None, shift=None, severity="minor")
        self.assertIsNone(legacy["batch"])
        self.assertIsNone(legacy["batch_id"])
        self.assertEqual(legacy["open_items"], 0)
        self.service.add_record(legacy["id"], {"kind": "action", "detail": "x",
                                               "status": "open"}, "recorder", "investigator")
        self.assertEqual(self.service.get_item(legacy["id"], "viewer")["open_items"], 1)
        # 单事故流转不受批次影响。
        current = legacy
        for target in ["investigating", "corrective_action", "verification"]:
            current = self.service.transition(current["id"], target, current["version"],
                                              "reviewer", "investigator" if target != "verification" else "safety_manager")
        self.assertEqual(current["status"], "verification")

    # 批次详情可查到归并来源、重算结果与退回记录。
    def test_batch_detail_audit_sources(self):
        main = self._create("H-1", severity="minor")
        assoc = self._create("H-2", severity="fatal")
        detail = self._batch_for(main)
        self.assertGreaterEqual(len(detail["merges"]), 1)
        self.assertEqual(detail["merges"][0]["detail"]["merged_item_id"], assoc["id"])
        self.assertGreaterEqual(len(detail["recalculations"]), 1)
        actions = {e["action"] for e in self.service.audit("viewer")}
        self.assertIn("batch_created", actions)
        self.assertIn("batch_merge", actions)
        self.assertIn("batch_recalc", actions)
        self.assertTrue(self.repo.verify_audit_chain())

    # 批次内未关闭事项阻止主事故关闭。
    def test_batch_open_items_block_closure(self):
        main = self._create("I-1", severity="serious", quantity=12, threshold=6)
        assoc = self._create("I-2", severity="serious")
        self.service.add_record(assoc["id"], {"kind": "action", "detail": "open",
                                               "status": "open"}, "recorder", "investigator")
        current = main
        for target in ["investigating", "corrective_action", "verification"]:
            current = self.service.transition(current["id"], target, current["version"],
                                              "reviewer", "investigator" if target != "verification" else "safety_manager")
        with self.assertRaises(Exception):
            self.service.transition(current["id"], "closed", current["version"],
                                    "reviewer", "safety_manager")

    # 显式把游离事故并入批次。
    def test_explicit_merge_endpoint_logic(self):
        main = self._create("J-1", severity="moderate")
        standalone = self._create("J-2", site=None, shift=None, severity="moderate")
        self.assertIsNone(standalone["batch"])
        batch = self._batch_for(main)
        merged = self.service.merge_item(batch["id"], standalone["id"], "reviewer",
                                         "investigator")
        self.assertEqual(merged["member_count"], 2)
        self.assertEqual(self.service.get_item(standalone["id"], "viewer")["batch"]["id"],
                         batch["id"])

    # 并发：两个上报入口同时提交同一事故，只接受一次，后到拿到当前批次版本。
    def test_concurrent_same_accident_idempotent(self):
        results = []
        errors = []

        def worker():
            try:
                results.append(self._create("CONC-SAME", severity="moderate"))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 4)
        self.assertEqual(len({r["id"] for r in results}), 1)
        batch = self._batch_for(results[0])
        self.assertEqual(batch["member_count"], 1)
        self.assertIsNotNone(results[0]["batch"]["version"])

    # 并发：不同事故同现场同班次，并入同一批次。
    def test_concurrent_different_accidents_same_batch(self):
        results = []

        def worker(i):
            results.append(self._create(f"CONC-DIFF-{i}", severity="moderate"))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len({r["id"] for r in results}), 4)
        batch = self._batch_for(results[0])
        self.assertEqual(batch["member_count"], 4)
        self.assertTrue(self.repo.verify_audit_chain())


class BatchHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "http.db"))
        self.service = Service(self.repo)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0),
                                          make_handler(self.service, str(Path(__file__).resolve().parent.parent / "static")))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.repo.close()
        self.tmp.cleanup()

    def _req(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                      headers={"Content-Type": "application/json",
                               "X-Actor": "tester", "X-Role": "reporter"})
        with urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_batch_http_flow(self):
        status, body = self._req("POST", "/api/items", {"title": "m", "description": "d",
                                                        "severity": "minor",
                                                        "external_ref": "K-1",
                                                        "site": "S1", "shift": "day"})
        self.assertEqual(status, 201)
        self.assertIsNotNone(body["batch"])
        status, body2 = self._req("POST", "/api/items", {"title": "m2", "description": "d2",
                                                         "severity": "fatal",
                                                         "external_ref": "K-2",
                                                         "site": "S1", "shift": "day"})
        self.assertEqual(status, 201)
        status, listing = self._req("GET", "/api/batches")
        self.assertEqual(status, 200)
        self.assertEqual(len(listing["batches"]), 1)
        self.assertEqual(listing["batches"][0]["member_count"], 2)
        batch_id = listing["batches"][0]["id"]
        status, detail = self._req("GET", f"/api/batches/{batch_id}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["main_item"]["severity"], "fatal")
        self.assertGreaterEqual(len(detail["recalculations"]), 1)
        # 重复上报同一 external_ref 只接受一次。
        status, replay = self._req("POST", "/api/items", {"title": "m", "description": "d",
                                                          "severity": "minor",
                                                          "external_ref": "K-1",
                                                          "site": "S1", "shift": "day"})
        self.assertEqual(replay["external_ref"], "K-1")
        status, listing2 = self._req("GET", "/api/batches")
        self.assertEqual(listing2["batches"][0]["member_count"], 2)


if __name__ == "__main__":
    unittest.main()
