from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, EMERGENCY_CONFIRM_ROLES, ENTITY,
                    RECORD_ROLES, TERMINAL_STATES, TITLE, VIEW_ROLES,
                    completion_blockers, current_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        dam_section = require_text(payload.get("dam_section"), "dam_section", 100)
        measure_point = require_text(payload.get("measure_point"), "measure_point", 100)
        reading = require_number(payload.get("reading", 0), "reading")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item, outcome = self.repository.create_item(
            title, description, severity, quantity, threshold, external_ref,
            dam_section, measure_point, reading, actor)
        if outcome["merged"]:
            self.repository.append_audit("report", ENTITY, item["id"], actor, {
                "reading": reading, "report_count": item["report_count"],
                "escalated": outcome["escalated"],
            })
        else:
            self.repository.append_audit("create", ENTITY, item["id"], actor, {
                "title": title, "severity": item["severity"], "quantity": quantity,
                "dam_section": dam_section, "measure_point": measure_point,
                "reading": reading, "recurrence_count": item["recurrence_count"],
                "previous_item_id": item["previous_item_id"],
                "escalated": outcome["escalated"],
                "priority": priority_score(item["severity"], quantity, threshold),
            })
        result = self.enrich(item)
        result["merged"] = outcome["merged"]
        return result

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
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str, conclusion: Optional[str] = None) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        if item.get("escalation_pending"):
            raise ConflictError("应急升级待应急经理确认")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        stored = None
        if target in TERMINAL_STATES and conclusion is not None:
            stored = require_text(conclusion, "conclusion")
        updated = self.repository.transition_item(item_id, target, expected_version,
                                                  actor, stored)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target, "conclusion": stored,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def confirm_escalation(self, item_id: int, expected_version: int, actor: str,
                           role: str) -> Dict[str, Any]:
        ensure_role(role, EMERGENCY_CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        item = self.repository.get_item(item_id)
        if not item.get("escalation_pending"):
            raise ConflictError("无待确认的应急升级")
        updated = self.repository.confirm_escalation(item_id, expected_version, actor)
        self.repository.append_audit("confirm_escalation", ENTITY, item_id, actor, {
            "severity": updated["severity"],
            "recurrence_count": updated["recurrence_count"],
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
        result["escalation_pending"] = bool(item.get("escalation_pending"))
        result["blockers"] = current_blockers(
            item, self.repository.open_record_count(item["id"]))
        return result
