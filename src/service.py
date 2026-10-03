from __future__ import annotations

from typing import Any, Dict, List, Optional

from .domain import (ConflictError, ensure_role, normalize_severity, require_number,
                     require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, BATCH_ENTITY, CREATE_ROLES, ENTITY, EVIDENCE_KINDS,
                    MEASURE_KINDS, MERGE_ROLES, RECORD_ROLES, TITLE, VIEW_ROLES,
                    completion_blockers, escalation_required, max_severity,
                    priority_score, response_deadline_hours, role_for_transition,
                    scope_covers, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    @staticmethod
    def _optional_text(value: Any, field: str, max_length: int) -> Optional[str]:
        if value is None:
            return None
        return require_text(value, field, max_length)

    # ------------------------------------------------------------------ #
    # 事故上报
    # ------------------------------------------------------------------ #
    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        site = self._optional_text(payload.get("site"), "site", 100)
        shift = self._optional_text(payload.get("shift"), "shift", 100)
        # 幂等：同一 external_ref 重复上报只接受一次，后到请求拿到当前事故（含批次）状态。
        if external_ref is not None:
            existing = self.repository.get_item_by_external_ref(external_ref)
            if existing is not None:
                if site and shift:
                    self._ensure_batch(existing, site, shift, actor)
                return self.enrich(self.repository.get_item(existing["id"]))
        try:
            item = self.repository.create_item(title, description, severity, quantity,
                                               threshold, external_ref, actor)
        except ConflictError:
            # 并发下另一入口已用同一 external_ref 建单：回退为幂等重放。
            if external_ref is None:
                raise
            existing = self.repository.get_item_by_external_ref(external_ref)
            if existing is None:
                raise
            if site and shift:
                self._ensure_batch(existing, site, shift, actor)
            return self.enrich(self.repository.get_item(existing["id"]))
        if site and shift:
            self._ensure_batch(item, site, shift, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(self.repository.get_item(item["id"]))

    # ------------------------------------------------------------------ #
    # 调查批次
    # ------------------------------------------------------------------ #
    def _ensure_batch(self, item: Dict[str, Any], site: str, shift: str,
                      actor: str) -> Dict[str, Any]:
        """按现场+班次把事故并入调查批次；幂等，失败后可按原请求恢复。"""
        if item.get("batch_id"):
            batch = self.repository.get_batch(item["batch_id"])
            if batch is not None:
                self._recalc_batch(batch, actor)
                self._check_measures(batch, item, actor)
            return batch
        batch = self.repository.get_or_create_batch(site, shift, item["id"], actor)
        is_founder = (batch["main_item_id"] == item["id"])
        if self.repository.set_item_batch(item["id"], batch["id"]):
            if is_founder:
                self.repository.append_audit("batch_created", BATCH_ENTITY, batch["id"], actor, {
                    "site": site, "shift": shift, "main_item_id": item["id"],
                })
            elif not self.repository.audit_exists("batch_merge", batch["id"],
                                                  "merged_item_id", item["id"]):
                self.repository.append_audit("batch_merge", BATCH_ENTITY, batch["id"], actor, {
                    "merged_item_id": item["id"],
                    "external_ref": item.get("external_ref"),
                    "site": site, "shift": shift,
                })
            self._recalc_batch(batch, actor)
            self._check_measures(batch, item, actor)
            self.repository.increment_batch_version(batch["id"])
        return batch

    def merge_item(self, batch_id: int, item_id: int, actor: str, role: str) -> Dict[str, Any]:
        """把游离事故显式并入指定批次。"""
        ensure_role(role, MERGE_ROLES)
        actor = require_text(actor, "actor", 100)
        batch = self.repository.get_batch(batch_id)
        if batch is None:
            raise NotFoundError("批次不存在")
        item = self.repository.get_item(item_id)
        if item.get("batch_id"):
            if item["batch_id"] == batch_id:
                return self._batch_detail(batch)
            raise ConflictError("事故已属于其他批次")
        if self.repository.set_item_batch(item_id, batch_id):
            if not self.repository.audit_exists("batch_merge", batch_id,
                                                  "merged_item_id", item_id):
                self.repository.append_audit("batch_merge", BATCH_ENTITY, batch_id, actor, {
                    "merged_item_id": item_id,
                    "external_ref": item.get("external_ref"),
                    "site": batch["site"], "shift": batch["shift"],
                })
            self._recalc_batch(batch, actor)
            self._check_measures(batch, item, actor)
            self.repository.increment_batch_version(batch_id)
        return self._batch_detail(batch)

    def _recalc_batch(self, batch: Dict[str, Any], actor: str) -> None:
        """重算主事故严重度、响应期限与未关闭事项；幂等，无变化不重复写审计。"""
        members = self.repository.list_batch_members(batch["id"])
        if not members:
            return
        main = self.repository.get_item(batch["main_item_id"])
        new_severity = max_severity([m["severity"] for m in members])
        old_severity = main["severity"]
        old_deadline = response_deadline_hours(old_severity, main["quantity"], main["threshold"])
        old_open = self.repository.batch_open_record_count(batch["id"])
        if new_severity != old_severity:
            self.repository.update_item_severity(main["id"], new_severity)
        new_deadline = response_deadline_hours(new_severity, main["quantity"], main["threshold"])
        new_open = self.repository.batch_open_record_count(batch["id"])
        latest = self.repository.latest_audit("batch_recalc", batch["id"])
        if (latest and latest["detail"].get("severity", {}).get("after") == new_severity
                and latest["detail"].get("deadline_hours", {}).get("after") == new_deadline
                and latest["detail"].get("open_items", {}).get("after") == new_open):
            return
        self.repository.append_audit("batch_recalc", BATCH_ENTITY, batch["id"], actor, {
            "main_item_id": main["id"],
            "member_count": len(members),
            "severity": {"before": old_severity, "after": new_severity},
            "deadline_hours": {"before": old_deadline, "after": new_deadline},
            "open_items": {"before": old_open, "after": new_open},
        })

    def _check_measures(self, batch: Dict[str, Any], new_item: Dict[str, Any],
                        actor: str) -> None:
        """新证据覆盖已验证措施原范围时，退回复核并记录原因；幂等。"""
        evidence = [r for r in self.repository.list_records(new_item["id"])
                    if r["kind"] in EVIDENCE_KINDS and r.get("scope")]
        if not evidence:
            return
        measures = [r for r in self.repository.list_batch_records(batch["id"])
                    if r["kind"] in MEASURE_KINDS and r["status"] == "closed" and r.get("scope")]
        for measure in measures:
            for ev in evidence:
                if scope_covers(ev["scope"], measure["scope"]):
                    if self.repository.reopen_record(measure["id"]):
                        self.repository.append_audit("measure_returned", BATCH_ENTITY,
                                                     batch["id"], actor, {
                            "measure_record_id": measure["id"],
                            "evidence_record_id": ev["id"],
                            "measure_scope": measure["scope"],
                            "evidence_scope": ev["scope"],
                            "reason": (f"新证据#{ev['id']}范围覆盖措施#{measure['id']}原范围，"
                                       f"措施退回复核"),
                        })
                    break

    def list_batches(self, role: str) -> List[Dict[str, Any]]:
        self._view(role)
        return [self._batch_summary(b) for b in self.repository.list_batches()]

    def get_batch(self, batch_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        batch = self.repository.get_batch(batch_id)
        if batch is None:
            raise NotFoundError("批次不存在")
        return self._batch_detail(batch)

    def _batch_summary(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": batch["id"], "site": batch["site"], "shift": batch["shift"],
            "status": batch["status"], "version": batch["version"],
            "main_item_id": batch["main_item_id"],
            "member_count": len(self.repository.list_batch_members(batch["id"])),
            "open_items": self.repository.batch_open_record_count(batch["id"]),
        }

    def _batch_detail(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        members = self.repository.list_batch_members(batch["id"])
        main = self.enrich(self.repository.get_item(batch["main_item_id"]))
        associated = [self.enrich(m) for m in members if m["id"] != batch["main_item_id"]]
        events = self.repository.list_audit(entity_id=batch["id"])
        return {
            "id": batch["id"], "site": batch["site"], "shift": batch["shift"],
            "status": batch["status"], "version": batch["version"],
            "main_item_id": batch["main_item_id"],
            "main_item": main,
            "associated_items": associated,
            "member_count": len(members),
            "open_items": self.repository.batch_open_record_count(batch["id"]),
            "merges": [e for e in events if e["action"] == "batch_merge"],
            "recalculations": [e for e in events if e["action"] == "batch_recalc"],
            "returns": [e for e in events if e["action"] == "measure_returned"],
            "created_at": batch["created_at"], "updated_at": batch["updated_at"],
        }

    # ------------------------------------------------------------------ #
    # 记录与流转
    # ------------------------------------------------------------------ #
    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        scope = self._optional_text(payload.get("scope"), "scope", 500)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor, scope)
        item = self.repository.get_item(item_id)
        # 新证据并入后，重算批次并复核已验证措施是否被覆盖。
        if kind in EVIDENCE_KINDS and scope and item.get("batch_id"):
            batch = self.repository.get_batch(item["batch_id"])
            if batch is not None:
                self._recalc_batch(batch, actor)
                self._check_measures(batch, item, actor)
                self.repository.increment_batch_version(batch["id"])
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def _open_count(self, item: Dict[str, Any]) -> int:
        if item.get("batch_id"):
            return self.repository.batch_open_record_count(item["batch_id"])
        return self.repository.open_record_count(item["id"])

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self._open_count(item))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["open_items"] = self._open_count(item)
        batch_id = item.get("batch_id")
        if batch_id:
            batch = self.repository.get_batch(batch_id)
            result["batch"] = None if batch is None else {
                "id": batch["id"], "site": batch["site"], "shift": batch["shift"],
                "version": batch["version"], "main_item_id": batch["main_item_id"],
                "status": batch["status"],
            }
        else:
            result["batch"] = None
        return result
