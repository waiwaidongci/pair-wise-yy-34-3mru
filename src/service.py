from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .domain import (ConflictError, NotFoundError, ValidationError, ensure_role,
                     normalize_severity, optional_text, require_int, require_number,
                     require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, BATCH_CREATE_ROLES, BATCH_ENTITY, BATCH_MERGE_ROLES,
                    BATCH_VIEW_ROLES, CREATE_ROLES, ENTITY, MEASURE_REOPEN_ROLES,
                    MEASURE_VERIFY_ROLES, RECORD_ROLES, VIEW_ROLES, aggregate_quantity,
                    aggregate_severity, completion_blockers, covered_measures,
                    escalation_required, normalize_scope, priority_score,
                    response_deadline_hours, role_for_transition, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    # ----------------------------------------------------------------- items
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
        scene = optional_text(payload.get("scene"), "scene", 100)
        shift = optional_text(payload.get("shift"), "shift", 50)
        injury = optional_text(payload.get("injury"), "injury")
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor, scene, shift,
                                           injury)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
            "scene": scene, "shift": shift,
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        require_int(item_id, "item_id")
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValidationError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        measure_scope = optional_text(payload.get("measure_scope"), "measure_scope", 200)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor, measure_scope)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
            "measure_scope": measure_scope,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        require_int(item_id, "item_id")
        target = require_text(target, "target", 50)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        expected_version = require_int(expected_version, "expected_version")
        # 主事故关闭时按整批未关闭事项校验；无批次字段/未归并的旧事故按单事故处理
        members = None
        member_row = self.repository.active_member_row(item_id)
        if member_row is not None:
            batch = self.repository.get_batch(member_row["batch_id"])
            if batch["primary_item_id"] == item_id:
                members = self.repository.list_member_rows(batch["id"])
        if members is not None:
            open_items = sum(self.repository.open_record_count(m["item_id"])
                             for m in members)
        else:
            open_items = self.repository.open_record_count(item_id)
        blockers = completion_blockers(target, open_items)
        if blockers:
            raise ConflictError("；".join(blockers) + f"(未关闭事项{open_items}件)")
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
            "batch_id": member_row["batch_id"] if member_row else None,
            "scope": "batch" if members is not None else "single",
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

    def audit(self, role: str, item_id: Optional[int] = None,
              entity_type: Optional[str] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id, entity_type)

    # --------------------------------------------------------------- batches
    def create_batch(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        primary_id = require_int(payload.get("primary_item_id"), "primary_item_id")
        primary = self.repository.get_item(primary_id)
        member_row = self.repository.active_member_row(primary_id)
        if member_row is not None:
            raise ConflictError("主事故已属于调查批次")
        scene = optional_text(payload.get("scene"), "scene", 100) or primary["scene"]
        shift = optional_text(payload.get("shift"), "shift", 50) or primary["shift"]
        if not scene:
            raise ValidationError("scene不能为空（事故本身未登记现场时必须显式提供）")
        if not shift:
            raise ValidationError("shift不能为空（事故本身未登记班次时必须显式提供）")
        batch = self.repository.create_batch(scene, shift, primary_id, actor)
        self.repository.append_audit("batch_create", BATCH_ENTITY, batch["id"], actor, {
            "batch_no": batch["batch_no"], "scene": scene, "shift": shift,
            "primary_item_id": primary_id, "primary_external_ref": primary["external_ref"],
        })
        return self.batch_detail(batch["id"], role)

    def merge_item(self, batch_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        """把同现场、同班次的事故并入调查批次。

        - 同批次同事故/同幂等键只接受一次，后到请求返回当前批次版本；
        - 已在其他批次（含待审计状态）的事故拒绝并说明；
        - 业务数据与审计outbox同事务，审计写入失败可按原请求重放恢复。
        """
        require_int(batch_id, "batch_id")
        ensure_role(role, BATCH_MERGE_ROLES)
        actor = require_text(actor, "actor", 100)
        batch = self.repository.get_batch(batch_id)
        item_id = require_int(payload.get("item_id"), "item_id")
        idempotency_key = optional_text(payload.get("idempotency_key"),
                                        "idempotency_key", 100)
        evidence_scopes = payload.get("evidence_scopes", [])
        if not isinstance(evidence_scopes, list) or not all(
                isinstance(s, str) and s.strip() for s in evidence_scopes):
            raise ValidationError("evidence_scopes必须是非空字符串数组")
        evidence_scopes = [normalize_scope(s) for s in evidence_scopes]

        # 1) 已有成员（joined 或 pending_audit）：只接受一次，后到请求拿当前批次版本
        existing = self.repository.get_member_row(batch_id, item_id)
        if existing is not None and existing["join_state"] in ("joined", "pending_audit"):
            recovered_now = False
            # pending_audit 表示审核写入曾失败：按原请求补写审计（恢复）
            if existing["join_state"] == "pending_audit":
                try:
                    self.repository.flush_audit_outbox()
                    self.repository.mark_member_joined(batch_id, item_id)
                    recovered_now = True
                except Exception as exc:
                    current = self.batch_detail(batch_id, role)
                    current["duplicate"] = True
                    current["accepted"] = False
                    current["recovered"] = False
                    current["member_state"] = "pending_audit"
                    current["audit_warning"] = str(exc)
                    return current
            else:
                self._safe_flush()
            current = self.batch_detail(batch_id, role)
            # 恢复性重放：原请求被接受（补完审核）；普通重复提交不重复接受
            current["duplicate"] = not recovered_now
            current["accepted"] = recovered_now
            current["recovered"] = True
            current["member_state"] = "joined"
            return current

        # 2) 前置校验：现场/班次一致、不能是主事故、不能已属于其他批次
        item = self.repository.get_item(item_id)
        if batch["primary_item_id"] == item_id:
            raise ConflictError("该事故是本批次的主事故，无需并入")
        other = self.repository.active_member_row(item_id)
        if other is not None and other["batch_id"] != batch_id:
            raise ConflictError("该事故已属于其他调查批次")
        if (item["scene"] or batch["scene"]) != batch["scene"]:
            raise ConflictError("现场与批次不一致，不能组成同一调查批次")
        if (item["shift"] or batch["shift"]) != batch["shift"]:
            raise ConflictError("班次与批次不一致，不能组成同一调查批次")

        # 3) 重算：严重度取最高级、伤害指数求和，期限/优先级随主事故重算
        members = self.repository.list_member_items(batch_id)
        before = self._recompute_snapshot(batch, members)
        after_members = members + [item]
        new_severity = aggregate_severity([m["severity"] for m in after_members])
        new_quantity = aggregate_quantity([m["quantity"] for m in after_members])
        primary = next(m for m in after_members if m["id"] == batch["primary_item_id"])
        primary_after = dict(primary)
        primary_after["severity"] = new_severity
        primary_after["quantity"] = new_quantity
        after = self._snapshot_from(batch, after_members, primary_after)

        # 4) 已验证措施：仅新证据覆盖原范围才退回复核，否则继续有效
        verified = self.repository.verified_records_for_batch(batch_id)
        covered = covered_measures(verified, evidence_scopes) if evidence_scopes else []
        covered_ids = {measure["id"] for measure, _ in covered}

        request_json = {"item_id": item_id, "evidence_scopes": evidence_scopes,
                        "idempotency_key": idempotency_key, "actor": actor, "role": role}
        try:
            result = self.repository.apply_batch_merge(
                batch_id, item, new_severity, new_quantity, before, after,
                covered, idempotency_key, request_json, actor)
        except ConflictError:
            # 两个入口并发提交同一关联事故：唯一索引保证只接受一次
            race = self.repository.get_member_row(batch_id, item_id)
            if race is not None and race["join_state"] in ("joined", "pending_audit"):
                self._safe_flush()
                current = self.batch_detail(batch_id, role)
                current["duplicate"] = True
                current["accepted"] = False
                current["member_state"] = race["join_state"]
                return current
            raise

        # 5) 审计outbox写入：失败时成员保持pending_audit，可按原请求恢复
        audit_error = None
        try:
            self.repository.flush_audit_outbox()
            self.repository.mark_member_joined(batch_id, item_id)
        except Exception as exc:  # 审计写入失败：业务已提交，等待原请求重放
            audit_error = str(exc)

        detail = self.batch_detail(batch_id, role)
        detail["accepted"] = audit_error is None
        detail["audit_warning"] = audit_error
        detail["covered_record_ids"] = sorted(covered_ids)
        detail["valid_verified_record_ids"] = sorted(
            m["id"] for m in verified if m["id"] not in covered_ids)
        if audit_error is not None:
            detail["member_state"] = "pending_audit"
            detail["recovery_hint"] = "审核写入失败，使用相同请求（含幂等键）重试即可恢复"
        else:
            detail["member_state"] = "joined"
        return detail

    def recover_merge(self, batch_id: int, payload: Dict[str, Any], actor: str,
                      role: str) -> Dict[str, Any]:
        """按原请求恢复：重放待审核(pending_audit)并入的审计写入。"""
        require_int(batch_id, "batch_id")
        ensure_role(role, BATCH_MERGE_ROLES)
        batch = self.repository.get_batch(batch_id)
        item_id = require_int(payload.get("item_id"), "item_id")
        member = self.repository.get_member_row(batch_id, item_id)
        if member is None:
            raise NotFoundError("该并入请求不存在")
        if member["join_state"] == "joined":
            current = self.batch_detail(batch_id, role)
            current["duplicate"] = True
            current["accepted"] = False
            current["recovered"] = False
            current["member_state"] = "joined"
            return current
        if member["join_state"] != "pending_audit":
            raise ConflictError(f"该并入请求状态为{member['join_state']}，无法恢复")
        self.repository.flush_audit_outbox()
        self.repository.mark_member_joined(batch_id, item_id)
        detail = self.batch_detail(batch_id, role)
        detail["accepted"] = True
        detail["recovered"] = True
        detail["member_state"] = "joined"
        return detail

    def verify_measure(self, item_id: int, record_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        """验证纠正措施：通过后措施有效，后续只在被同范围新证据覆盖时退回。"""
        require_int(item_id, "item_id")
        require_int(record_id, "record_id")
        ensure_role(role, MEASURE_VERIFY_ROLES)
        actor = require_text(actor, "actor", 100)
        record = self.repository.get_record(record_id)
        if record["item_id"] != item_id:
            raise ValidationError("事项不属于该事故")
        if not record.get("measure_scope"):
            raise ValidationError("纠正措施必须先登记measure_scope才能验证")
        reason = optional_text(payload.get("reason"), "reason") if payload else None
        updated = self.repository.mark_record_verified(record_id, actor, reason)
        member_row = self.repository.active_member_row(item_id)
        batch_id = member_row["batch_id"] if member_row else None
        if batch_id is not None:
            self.repository.add_measure_review(
                batch_id, record_id, item_id, "verified",
                record["measure_scope"], None, reason, actor)
        self.repository.append_audit("measure_verified", ENTITY, item_id, actor, {
            "record_id": record_id, "measure_scope": record["measure_scope"],
            "batch_id": batch_id,
        })
        return updated

    def reopen_measure(self, item_id: int, record_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        """人工退回复核（记录原因），重新计入未关闭事项。"""
        require_int(item_id, "item_id")
        require_int(record_id, "record_id")
        ensure_role(role, MEASURE_REOPEN_ROLES)
        actor = require_text(actor, "actor", 100)
        reason = require_text((payload or {}).get("reason"), "reason")
        record = self.repository.get_record(record_id)
        if record["item_id"] != item_id:
            raise ValidationError("事项不属于该事故")
        if record.get("verified_status") != "verified":
            raise ConflictError("只有已验证通过的措施可以退回复核")
        updated = self.repository.reopen_record(record_id, reason, actor)
        member_row = self.repository.active_member_row(item_id)
        batch_id = member_row["batch_id"] if member_row else None
        if batch_id is not None:
            self.repository.add_measure_review(
                batch_id, record_id, item_id, "reopened",
                record["measure_scope"], None, reason, actor)
        self.repository.append_audit("measure_reopened", ENTITY, item_id, actor, {
            "record_id": record_id, "measure_scope": record["measure_scope"],
            "reason": reason, "batch_id": batch_id, "manual": True,
        })
        return updated

    def list_batches(self, role: str) -> list:
        ensure_role(role, BATCH_VIEW_ROLES)
        result = []
        for batch in self.repository.list_batches():
            members = self.repository.list_member_rows(batch["id"])
            result.append({
                "id": batch["id"], "batch_no": batch["batch_no"],
                "scene": batch["scene"], "shift": batch["shift"],
                "primary_item_id": batch["primary_item_id"],
                "version": batch["version"],
                "member_count": len(members),
                "created_at": batch["created_at"], "updated_at": batch["updated_at"],
            })
        return result

    def batch_detail(self, batch_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_VIEW_ROLES)
        batch = self.repository.get_batch(batch_id)
        members = self.repository.list_member_rows(batch["id"])
        items_by_id = {m["item_id"]: self.repository.get_item(m["item_id"])
                       for m in members}
        member_payload: List[Dict[str, Any]] = []
        open_items = 0
        for m in members:
            item = items_by_id[m["item_id"]]
            records = self.repository.list_records(m["item_id"])
            open_items += sum(1 for r in records if r["status"] == "open")
            source_request = None
            if m.get("request_json"):
                try:
                    source_request = json.loads(m["request_json"])
                except (TypeError, ValueError):
                    source_request = m["request_json"]
            member_payload.append({
                "item_id": m["item_id"], "role": m["role"], "join_seq": m["join_seq"],
                "join_state": m["join_state"], "reject_reason": m.get("reject_reason"),
                "idempotency_key": m["idempotency_key"],
                "source_request": source_request,
                "external_ref": item["external_ref"],
                "title": item["title"],
                "narrative": item["description"],
                "injury": item.get("injury"),
                "severity": item["severity"], "status": item["status"],
                "version": item["version"],
                "records": records,
            })
        primary = items_by_id[batch["primary_item_id"]]
        recomputations = self.repository.list_recomputations(batch["id"])
        recomputation_payload = [{
            "id": r["id"], "seq": r["seq"], "trigger_item_id": r["trigger_item_id"],
            "before": r["before_json"], "after": r["after_json"],
            "created_at": r["created_at"],
        } for r in recomputations]
        reviews = self.repository.list_measure_reviews(batch["id"])
        return {
            "id": batch["id"], "batch_no": batch["batch_no"],
            "scene": batch["scene"], "shift": batch["shift"], "version": batch["version"],
            "primary_item_id": batch["primary_item_id"],
            "primary": self.enrich(dict(primary)),
            "members": member_payload,
            "severity": primary["severity"],
            "deadline_hours": response_deadline_hours(
                primary["severity"], primary["quantity"], primary["threshold"]),
            "priority": priority_score(
                primary["severity"], primary["quantity"], primary["threshold"], open_items),
            "open_items": open_items,
            "recomputations": recomputation_payload,
            "measure_reviews": reviews,
            "created_by": batch["created_by"],
            "created_at": batch["created_at"], "updated_at": batch["updated_at"],
        }

    def _recompute_snapshot(self, batch: Dict[str, Any],
                            members: List[Dict[str, Any]]) -> Dict[str, Any]:
        primary = next(m for m in members if m["id"] == batch["primary_item_id"])
        return self._snapshot_from(batch, members, primary)

    def _snapshot_from(self, batch: Dict[str, Any], members: List[Dict[str, Any]],
                       primary: Dict[str, Any]) -> Dict[str, Any]:
        open_items = sum(self.repository.open_record_count(m["id"]) for m in members)
        return {
            "severity": primary["severity"],
            "quantity": aggregate_quantity([m["quantity"] for m in members]),
            "priority": priority_score(primary["severity"],
                                       aggregate_quantity([m["quantity"] for m in members]),
                                       primary["threshold"], open_items),
            "deadline_hours": response_deadline_hours(
                primary["severity"],
                aggregate_quantity([m["quantity"] for m in members]),
                primary["threshold"]),
            "open_items": open_items,
            "member_count": len(members),
        }

    def _safe_flush(self) -> None:
        try:
            self.repository.flush_audit_outbox()
        except Exception:
            pass

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
