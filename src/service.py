from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (NotFoundError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, ESCALATION_CONFIRM_ROLES,
                    RECORD_ROLES, TERMINAL_STATES, TITLE, VIEW_ROLES,
                    completion_blockers, escalation_reason, escalation_required,
                    priority_score, recurrence_escalation,
                    response_deadline_hours, role_for_transition,
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
        raw_quantity = payload.get("quantity")
        quantity = require_number(raw_quantity if raw_quantity is not None else 0,
                                  "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        dam_section = payload.get("dam_section")
        if dam_section is not None:
            dam_section = require_text(dam_section, "dam_section", 100)
        measure_point = payload.get("measure_point")
        if measure_point is not None:
            measure_point = require_text(measure_point, "measure_point", 100)
        if (dam_section is None) != (measure_point is None):
            raise ValidationError("dam_section和measure_point需同时登记")
        if dam_section is not None:
            open_item = self.repository.find_open_by_point(dam_section, measure_point)
            if open_item is not None:
                follow_quantity = (require_number(raw_quantity, "quantity")
                                   if raw_quantity is not None
                                   else open_item["quantity"])
                return self._follow_up(open_item, follow_quantity, actor)
        previous = None
        recurrence_count = 0
        last_reading = None
        if dam_section is not None:
            previous = self.repository.latest_by_point(dam_section, measure_point)
            if previous is not None:
                recurrence_count = (int(previous["recurrence_count"] or 0)
                                    + int(previous["report_count"] or 0) + 1)
                last_reading = previous["quantity"]
        item = self.repository.create_item(
            title, description, severity, quantity, threshold, external_ref, actor,
            dam_section=dam_section, measure_point=measure_point,
            previous_item_id=previous["id"] if previous else None,
            recurrence_count=recurrence_count, last_reading=last_reading)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
            "dam_section": dam_section, "measure_point": measure_point,
            "recurrence_count": recurrence_count,
            "previous_item_id": previous["id"] if previous else None,
        })
        item = self._maybe_escalate(item, actor)
        return self.enrich(item, merged=False)

    def _follow_up(self, open_item: Dict[str, Any], quantity: float,
                   actor: str) -> Dict[str, Any]:
        item = self.repository.merge_follow_up(open_item["id"], quantity, actor)
        record = self.repository.add_record(
            open_item["id"], "follow_up",
            f"续报：本次读数{quantity}，累计续报{item['report_count']}次",
            "closed", None, actor)
        self.repository.append_audit("follow_up", ENTITY, open_item["id"], actor, {
            "record_id": record["id"], "report_count": item["report_count"],
            "quantity": quantity, "last_reading": item["last_reading"],
        })
        item = self._maybe_escalate(item, actor)
        return self.enrich(item, merged=True)

    def _maybe_escalate(self, item: Dict[str, Any], actor: str) -> Dict[str, Any]:
        recurrence_total = (int(item["recurrence_count"] or 0)
                            + int(item["report_count"] or 0))
        if not recurrence_escalation(recurrence_total, item["quantity"],
                                     item["last_reading"]):
            return item
        if item["escalation_status"] == "pending":
            return item
        updated = self.repository.set_escalation_pending(item["id"])
        self.repository.append_audit("escalate", ENTITY, item["id"], actor, {
            "reason": escalation_reason(recurrence_total, item["quantity"],
                                        item["last_reading"]),
            "recurrence_total": recurrence_total,
            "quantity": item["quantity"], "last_reading": item["last_reading"],
        })
        return updated

    def confirm_escalation(self, item_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, ESCALATION_CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.confirm_escalation(item_id)
        self.repository.append_audit("escalation_confirm", ENTITY, item_id, actor, {
            "severity": item["severity"],
            "escalation_status": item["escalation_status"],
        })
        return self.enrich(item)

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
                   actor: str, role: str,
                   conclusion: Optional[str] = None) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        if conclusion is not None:
            conclusion = require_text(conclusion, "conclusion")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id),
                                       item["escalation_status"])
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(
            item_id, target, expected_version, actor,
            conclusion if target == "closed" else None)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "conclusion": conclusion if target == "closed" else None,
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

    def enrich(self, item: Dict[str, Any],
               merged: Optional[bool] = None) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["recurrence_total"] = (int(item.get("recurrence_count") or 0)
                                      + int(item.get("report_count") or 0))
        result["last_conclusion"] = self._last_conclusion(item)
        result["blockers"] = self._blockers(item)
        if merged is not None:
            result["merged"] = merged
        return result

    def _last_conclusion(self, item: Dict[str, Any]) -> Optional[str]:
        previous_id = item.get("previous_item_id")
        if not previous_id:
            return None
        try:
            previous = self.repository.get_item(previous_id)
        except NotFoundError:
            return None
        return previous.get("conclusion") or self.repository.latest_conclusion(previous_id)

    def _blockers(self, item: Dict[str, Any]) -> list:
        if item["status"] in TERMINAL_STATES:
            return []
        return completion_blockers(
            "closed", self.repository.open_record_count(item["id"]),
            item.get("escalation_status") or "none")
