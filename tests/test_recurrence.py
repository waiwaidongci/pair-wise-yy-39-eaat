import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


def payload(point, reading, ref=None, section="坝段甲"):
    data = {"title": "渗流缺陷", "description": "渗流点缺陷登记", "severity": "minor",
            "quantity": reading, "threshold": 10, "dam_section": section,
            "measure_point": point, "reading": reading}
    if ref:
        data["external_ref"] = ref
    return data


class RecurrenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close(); self.tmp.cleanup()

    def close_item(self, item, conclusion=None):
        current = item
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0],
                conclusion=conclusion if target == STATES[-1] else None)
        return current

    def test_registration_requires_section_point_reading(self):
        with self.assertRaises(ValidationError):
            self.service.create_item({"title": "t", "description": "d", "severity": "minor",
                                      "dam_section": "坝段甲", "reading": 1.0}, "a", "inspector")
        item = self.service.create_item(payload("SP-1", 1.2), "a", "inspector")
        self.assertEqual(item["dam_section"], "坝段甲")
        self.assertEqual(item["measure_point"], "SP-1")
        self.assertEqual(item["reading"], 1.2)
        self.assertEqual(item["recurrence_count"], 0)
        self.assertIsNone(item["previous_item_id"])
        self.assertFalse(item["merged"])

    def test_duplicate_report_merges_into_first_ticket(self):
        first = self.service.create_item(payload("SP-2", 1.0, "R-1"), "a", "inspector")
        again = self.service.create_item(payload("SP-2", 1.3, "R-2"), "b", "inspector")
        self.assertTrue(again["merged"])
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["report_count"], 1)
        self.assertEqual(again["reading"], 1.3)
        third = self.service.create_item(payload("SP-2", 1.4), "c", "inspector")
        self.assertEqual(third["report_count"], 2)
        items = self.service.list_items("viewer")
        self.assertEqual(len([i for i in items if i["measure_point"] == "SP-2"]), 1)

    def test_recurrence_links_previous_closed_and_conclusion(self):
        first = self.service.create_item(payload("SP-3", 1.0), "a", "inspector")
        closed = self.close_item(first, conclusion="已灌浆处理，复检合格")
        second = self.service.create_item(payload("SP-3", 1.1), "a", "inspector")
        self.assertFalse(second["merged"])
        self.assertEqual(second["previous_item_id"], closed["id"])
        self.assertEqual(second["recurrence_count"], 1)
        self.assertEqual(second["last_conclusion"], "已灌浆处理，复检合格")
        detail = self.service.get_item(second["id"], "viewer")
        self.assertEqual(detail["last_conclusion"], "已灌浆处理，复检合格")
        listed = [i for i in self.service.list_items("viewer") if i["id"] == second["id"]][0]
        self.assertEqual(listed["recurrence_count"], 1)
        self.assertIn("blockers", listed)

    def test_escalation_after_third_recurrence_needs_manager(self):
        current = None
        for _ in range(4):
            item = self.service.create_item(payload("SP-4", 1.0), "a", "inspector")
            current = self.close_item(item, conclusion="已处理")
        fifth = self.service.create_item(payload("SP-4", 1.0), "a", "inspector")
        self.assertEqual(fifth["recurrence_count"], 4)
        self.assertEqual(fifth["severity"], "emergency")
        self.assertTrue(fifth["escalation_pending"])
        self.assertIn("应急升级待应急经理确认", fifth["blockers"])
        with self.assertRaises(ConflictError):
            self.service.transition(fifth["id"], STATES[1], fifth["version"], "a", "inspector")
        with self.assertRaises(PermissionDenied):
            self.service.confirm_escalation(fifth["id"], fifth["version"], "eng", "dam_engineer")
        confirmed = self.service.confirm_escalation(
            fifth["id"], fifth["version"], "mgr", "emergency_manager")
        self.assertFalse(confirmed["escalation_pending"])
        self.assertEqual(confirmed["blockers"], [])
        moved = self.service.transition(confirmed["id"], STATES[1], confirmed["version"],
                                        "a", "inspector")
        self.assertEqual(moved["status"], STATES[1])
        with self.assertRaises(ConflictError):
            self.service.confirm_escalation(fifth["id"], moved["version"], "mgr",
                                            "emergency_manager")

    def test_escalation_when_reading_doubles(self):
        first = self.service.create_item(payload("SP-5", 2.0), "a", "inspector")
        self.close_item(first, conclusion="已处理")
        second = self.service.create_item(payload("SP-5", 4.5), "a", "inspector")
        self.assertEqual(second["severity"], "emergency")
        self.assertTrue(second["escalation_pending"])

    def test_merged_report_can_escalate_on_doubling(self):
        first = self.service.create_item(payload("SP-6", 1.0), "a", "inspector")
        self.assertFalse(first["escalation_pending"])
        merged = self.service.create_item(payload("SP-6", 2.5), "b", "inspector")
        self.assertTrue(merged["merged"])
        self.assertEqual(merged["severity"], "emergency")
        self.assertTrue(merged["escalation_pending"])

    def test_no_escalation_below_limits(self):
        first = self.service.create_item(payload("SP-7", 2.0), "a", "inspector")
        self.close_item(first, conclusion="已处理")
        second = self.service.create_item(payload("SP-7", 3.9), "a", "inspector")
        self.assertEqual(second["severity"], "minor")
        self.assertFalse(second["escalation_pending"])


if __name__ == "__main__":
    unittest.main()
