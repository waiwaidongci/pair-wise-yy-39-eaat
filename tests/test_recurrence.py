import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


def payload(reading, ref, section="3#坝段", point="SP-01", **kw):
    base = {"title": "渗流缺陷", "description": "渗流点反复渗漏", "severity": "major",
            "quantity": reading, "threshold": 10, "external_ref": ref,
            "dam_section": section, "measure_point": point}
    base.update(kw)
    return base


class RecurrenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close(); self.tmp.cleanup()

    def _close(self, item, conclusion=None):
        current = item
        for target in STATES[1:]:
            kw = {"conclusion": conclusion} if target == "closed" and conclusion else {}
            current = self.service.transition(
                current["id"], target, current["version"], "worker",
                TRANSITION_ROLES[target][0], **kw)
        return current

    def test_follow_up_merges_into_first_ticket(self):
        first = self.service.create_item(payload(5.0, "RC-1"), "inspector-a", "inspector")
        self.assertFalse(first["merged"])
        self.assertEqual(first["recurrence_count"], 0)
        dup = self.service.create_item(payload(6.0, "RC-2"), "inspector-b", "inspector")
        self.assertTrue(dup["merged"])
        self.assertEqual(dup["id"], first["id"])
        self.assertEqual(dup["report_count"], 1)
        self.assertEqual(dup["quantity"], 6.0)
        self.assertEqual(dup["last_reading"], 5.0)
        again = self.service.create_item(payload(6.5, "RC-3"), "inspector-b", "inspector")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["report_count"], 2)
        items = self.service.list_items("viewer")
        self.assertEqual(len(items), 1)
        records = self.service.list_records(first["id"], "viewer")
        self.assertEqual([r["kind"] for r in records], ["follow_up", "follow_up"])

    def test_no_merge_across_points_or_sections(self):
        self.service.create_item(payload(5.0, "RC-5"), "a", "inspector")
        other_point = self.service.create_item(
            payload(5.0, "RC-6", point="SP-02"), "a", "inspector")
        other_section = self.service.create_item(
            payload(5.0, "RC-7", section="4#坝段"), "a", "inspector")
        self.assertFalse(other_point["merged"])
        self.assertFalse(other_section["merged"])
        self.assertEqual(len(self.service.list_items("viewer")), 3)

    def test_recurrence_links_last_closed_and_shows_conclusion(self):
        first = self.service.create_item(payload(5.0, "RC-10"), "a", "inspector")
        self._close(first, conclusion="灌浆处理完毕")
        second = self.service.create_item(payload(7.0, "RC-11"), "a", "inspector")
        self.assertFalse(second["merged"])
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(second["previous_item_id"], first["id"])
        self.assertEqual(second["recurrence_count"], 1)
        self.assertEqual(second["last_reading"], 5.0)
        self.assertEqual(second["last_conclusion"], "灌浆处理完毕")
        self.assertEqual(second["blockers"], [])
        listed = [i for i in self.service.list_items("viewer")
                  if i["id"] == second["id"]][0]
        self.assertEqual(listed["last_conclusion"], "灌浆处理完毕")
        self.assertEqual(listed["recurrence_total"], 1)

    def test_escalation_when_recurrence_exceeds_three(self):
        item = self.service.create_item(payload(5.0, "RC-20"), "a", "inspector")
        for i in range(4):
            item = self.service.create_item(
                payload(5.0, f"RC-2{i + 1}"), "a", "inspector")
        self.assertEqual(item["report_count"], 4)
        self.assertEqual(item["recurrence_total"], 4)
        self.assertEqual(item["severity"], "emergency")
        self.assertEqual(item["escalation_status"], "pending")
        self.assertIn("应急升级待应急经理确认", item["blockers"])

    def test_escalation_when_reading_doubles(self):
        self.service.create_item(payload(5.0, "RC-30"), "a", "inspector")
        dup = self.service.create_item(payload(10.0, "RC-31"), "a", "inspector")
        self.assertEqual(dup["severity"], "emergency")
        self.assertEqual(dup["escalation_status"], "pending")
        first = self.service.create_item(
            payload(4.0, "RC-32", point="SP-09"), "a", "inspector")
        self._close(first)
        second = self.service.create_item(
            payload(8.0, "RC-33", point="SP-09"), "a", "inspector")
        self.assertEqual(second["severity"], "emergency")
        self.assertEqual(second["escalation_status"], "pending")

    def test_escalation_confirm_roles_and_close_blocker(self):
        item = self.service.create_item(payload(5.0, "RC-40"), "a", "inspector")
        item = self.service.create_item(payload(11.0, "RC-41"), "a", "inspector")
        self.assertEqual(item["escalation_status"], "pending")
        with self.assertRaises(PermissionDenied):
            self.service.confirm_escalation(item["id"], "mgr", "inspector")
        current = item
        for target in STATES[1:-1]:
            current = self.service.transition(
                current["id"], target, current["version"], "w",
                TRANSITION_ROLES[target][0])
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(current["id"], "closed", current["version"],
                                    "w", "emergency_manager")
        self.assertIn("应急升级待应急经理确认", str(ctx.exception))
        confirmed = self.service.confirm_escalation(item["id"], "mgr", "emergency_manager")
        self.assertEqual(confirmed["escalation_status"], "confirmed")
        with self.assertRaises(ConflictError):
            self.service.confirm_escalation(item["id"], "mgr", "emergency_manager")
        closed = self.service.transition(confirmed["id"], "closed",
                                         confirmed["version"], "w",
                                         "emergency_manager")
        self.assertEqual(closed["status"], "closed")

    def test_section_and_point_must_come_together(self):
        with self.assertRaises(ValidationError):
            self.service.create_item(
                {"title": "t", "description": "d", "severity": "minor",
                 "quantity": 1, "threshold": 2, "dam_section": "3#坝段"},
                "a", "inspector")

    def test_audit_trail_for_recurrence_events(self):
        item = self.service.create_item(payload(5.0, "RC-50"), "a", "inspector")
        item = self.service.create_item(payload(12.0, "RC-51"), "a", "inspector")
        self.service.confirm_escalation(item["id"], "mgr", "emergency_manager")
        actions = [e["action"] for e in self.service.audit("viewer", item["id"])]
        self.assertEqual(actions, ["create", "follow_up", "escalate",
                                   "escalation_confirm"])
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
