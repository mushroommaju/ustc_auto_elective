import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from autoelective.engine import Engine


PRIMARY = "DEMO1001P.01"
OLD = "OLD1001P.01"
BACK = "BACK1001P.01"
ALT1, ALT2 = "DEMO2001P.01", "DEMO2001P.02"
OTHER = "DEMO3001P.01"


def lesson(code, ident, credits, when="A: 1(1,2)", weeks="2~18", limit=10, groups=None):
    return {"id": ident, "code": code, "limitCount": limit, "course": {"credits": credits},
            "dateTimePlace": {"textZh": when}, "weekText": {"textZh": weeks},
            "scheduleGroups": [] if groups is None else groups}


class Client:
    def __init__(self, selected, outcomes=None):
        self.current, self.outcomes, self.calls, self.available = list(selected), list(outcomes or [{"success": True}]), [], []
    def selected(self): return list(self.current)
    def lessons(self): return list(self.available)
    def submit(self, action, ident): self.calls.append((action, ident)); return str(len(self.calls))
    def outcome(self, request_id): return self.outcomes.pop(0) if self.outcomes else {"success": True}


class Log:
    def info(self, *args): pass
    def warning(self, *args): pass
    def critical(self, *args): pass


class EngineTests(unittest.TestCase):
    def test_pending_false_is_not_a_definitive_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Client([], outcomes=[{'success': False, 'code': 'PENDING'}, {'success': True}])
            engine = self.engine(client, Path(folder) / 'journal')
            with patch('autoelective.engine.time.sleep') as sleep:
                self.assertTrue(engine._submit('add', 10, {'stage': 'add', 'target': PRIMARY}))
                sleep.assert_called_once_with(1)
            self.assertEqual(client.calls, [('add', 10)])

    def test_other_course_unlisted_section_in_exclusive_group_blocks(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Client([lesson('DEMO3001P.99', 99, 2)])
            engine = self.engine(client, Path(folder) / 'journal', targets=(ALT1, OTHER),
                                 groups={ALT1: 'one', OTHER: 'one'})
            self.assertIsNotNone(engine._gate(ALT1, client.current))
            self.assertEqual(client.calls, [])

    def engine(self, client, journal, *, targets=(PRIMARY,), **kwargs):
        max_credits = kwargs.pop("max_credits", 12)
        return Engine(client, targets=list(targets), mode="auto", max_credits=max_credits, swaps={PRIMARY: OLD},
                      after_select={PRIMARY: BACK}, journal_path=journal, logger=Log(), **kwargs)

    def test_direct_credit_boundary_allows_equal_and_blocks_over(self):
        target = lesson(PRIMARY, 10, 4, "A: 3(1,2)")
        with tempfile.TemporaryDirectory() as folder:
            client = Client([lesson(OTHER, 1, 8, "A: 5(1,2)")])
            def add(action, ident):
                client.calls.append((action, ident))
                if action == "add": client.current.append(target)
                return str(len(client.calls))
            client.submit = add
            self.assertIsNone(self.engine(client, Path(folder) / "a").tick([target], {10: 0})["blocked"])
            client = Client([lesson(OTHER, 1, 9, "A: 5(1,2)")])
            result = self.engine(client, Path(folder) / "b").tick([target], {10: 0})
            self.assertIn("上限", result["blocked"]); self.assertEqual(client.calls, [])

    def test_unknown_journal_and_stale_counts_never_submit(self):
        target = lesson(PRIMARY, 10, 4, "A: 3(1,2)")
        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder) / "journal"; journal.write_text('{"stage":"unknown"}', encoding="utf-8")
            client = Client([])
            self.assertIn("未完成", self.engine(client, journal).tick([target], {10: 0})["blocked"])
            self.assertEqual(client.calls, [])
            result = self.engine(client, Path(folder) / "fresh").tick([target], {10: 0}, observed_at=time.monotonic() - 6)
            self.assertIn("过期", result["blocked"]); self.assertEqual(client.calls, [])

    def test_outcome_exception_is_unknown_once_and_blocks_restart(self):
        target = lesson(PRIMARY, 10, 4, "A: 3(1,2)")
        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder) / "journal"
            client = Client([])
            client.outcome = lambda _: (_ for _ in ()).throw(RuntimeError("network"))
            result = self.engine(client, journal).tick([target], {10: 0})
            self.assertIn("未知", result["blocked"])
            self.assertEqual(client.calls, [("add", 10)])
            self.assertTrue(journal.exists())
            restarted = Client([])
            result = self.engine(restarted, journal).tick([target], {10: 0})
            self.assertIn("未完成", result["blocked"])
            self.assertEqual(restarted.calls, [])

    def test_full_duplicate_multigroup_and_invalid_counts_never_submit(self):
        target = lesson(PRIMARY, 10, 4, "A: 3(1,2)")
        duplicate = lesson(PRIMARY, 11, 4, "A: 4(1,2)")
        grouped = lesson(PRIMARY, 10, 4, "A: 3(1,2)", groups=[{"id": 1}, {"id": 2}])
        with tempfile.TemporaryDirectory() as folder:
            for offered, counts in (([target], {10: 10}), ([target, duplicate], {10: 0, 11: 0}),
                                    ([grouped], {10: 0}), ([target], {10: True}), ([target], {10: -1})):
                client = Client([])
                self.engine(client, Path(folder) / str(len(client.calls))).tick(offered, counts)
                self.assertEqual(client.calls, [])

    def test_monitor_mode_never_submits(self):
        target = lesson(PRIMARY, 10, 4, "A: 3(1,2)")
        with tempfile.TemporaryDirectory() as folder:
            client = Client([])
            engine = Engine(client, targets=[PRIMARY], mode="monitor", max_credits=12, swaps={}, after_select={},
                            journal_path=Path(folder) / "journal", logger=Log())
            self.assertIsNone(engine.tick([target], {10: 0})["blocked"])
            self.assertEqual(client.calls, [])

    def test_generic_prerequisites_and_group_alternatives(self):
        alt1, alt2 = lesson(ALT1, 20, 2, "A: 3(1,2)"), lesson(ALT2, 21, 2, "A: 4(1,2)")
        prerequisite = {ALT1: {"selected": (PRIMARY,), "absent": (OLD,)}, ALT2: {"selected": (PRIMARY,), "absent": (OLD,)}}
        with tempfile.TemporaryDirectory() as folder:
            for selected in ([], [lesson(PRIMARY, 10, 4), lesson(OLD, 11, 4, "A: 5(1,2)")]):
                client = Client(selected)
                result = self.engine(client, Path(folder) / str(len(selected)), targets=(ALT1, ALT2), prerequisites=prerequisite,
                                     groups={ALT1: "alternative", ALT2: "alternative"}).tick([alt1, alt2], {20: 0, 21: 0})
                self.assertIn("等待前置", result["blocked"]); self.assertEqual(client.calls, [])
            client = Client([lesson(PRIMARY, 10, 4), lesson("DEMO2001P.99", 99, 2, "A: 6(1,2)")])
            result = self.engine(client, Path(folder) / "chosen", targets=(ALT1, ALT2), prerequisites=prerequisite,
                                 groups={ALT1: "alternative", ALT2: "alternative"}).tick([alt1, alt2], {20: 0, 21: 0})
            self.assertIn("同组", result["blocked"]); self.assertEqual(client.calls, [])

    def test_priority_then_conflicting_alternative_falls_through_without_drop(self):
        alt1, alt2 = lesson(ALT1, 20, 2, "A: 3(1,2)"), lesson(ALT2, 21, 2, "A: 4(1,2)")
        required = {ALT1: {"selected": (PRIMARY,), "absent": ()}, ALT2: {"selected": (PRIMARY,), "absent": ()}}
        with tempfile.TemporaryDirectory() as folder:
            client = Client([lesson(PRIMARY, 10, 4, "A: 3(1,2)"), lesson(OTHER, 1, 12, "A: 5(1,2)")])
            def add(action, ident):
                client.calls.append((action, ident))
                if action == "add": client.current.append(alt2)
                return str(len(client.calls))
            client.submit = add
            result = self.engine(client, Path(folder) / "fallthrough", targets=(ALT1, ALT2), prerequisites=required,
                                 groups={ALT1: "alternative", ALT2: "alternative"}, max_credits=20).tick([alt1, alt2], {20: 0, 21: 0})
            self.assertIsNone(result["blocked"]); self.assertEqual(client.calls, [("add", 21)])

    def test_swap_post_drop_and_then_next_tick_prerequisite(self):
        target, old, back, alternative = (lesson(PRIMARY, 10, 4, "A: 1(1,2)"), lesson(OLD, 11, 4, "A: 1(1,2)"),
                                          lesson(BACK, 12, 4, "A: 5(1,2)"), lesson(ALT2, 21, 2, "A: 4(1,2)"))
        rules = {ALT2: {"selected": (PRIMARY,), "absent": (OLD, BACK)}}
        with tempfile.TemporaryDirectory() as folder:
            client = Client([old, back, lesson(OTHER, 1, 11, "A: 6(1,2)")])
            client.available = [old]
            def submit(action, ident):
                client.calls.append((action, ident))
                if action == "drop": client.current = [item for item in client.current if item["id"] != ident]
                elif ident == 10: client.current.append(target)
                elif ident == 21: client.current.append(alternative)
                return str(len(client.calls))
            client.submit = submit
            engine = self.engine(client, Path(folder) / "flow", targets=(PRIMARY, ALT2), prerequisites=rules, max_credits=20,
                                 groups={ALT2: "alternative"})
            engine.tick([target, alternative], {10: 0, 21: 0})
            self.assertEqual(client.calls, [("drop", 11), ("add", 10), ("drop", 12)])
            self.assertIsNone(engine.tick([target, alternative], {10: 0, 21: 0})["blocked"])
            self.assertEqual(client.calls, [("drop", 11), ("add", 10), ("drop", 12), ("add", 21)])

    def test_definitive_failed_swap_add_restores_original_only(self):
        target, old = lesson(PRIMARY, 10, 4, "A: 1(1,2)"), lesson(OLD, 11, 4, "A: 1(1,2)")
        with tempfile.TemporaryDirectory() as folder:
            client = Client([old, lesson(OTHER, 1, 15, "A: 5(1,2)")], outcomes=[{"success": True}, {"success": False}, {"success": True}])
            client.available = [old]
            def submit(action, ident):
                client.calls.append((action, ident))
                if action == "drop": client.current = [item for item in client.current if item["id"] != ident]
                elif ident == 11: client.current.append(old)
                return str(len(client.calls))
            client.submit = submit
            result = self.engine(client, Path(folder) / "restore", max_credits=20).tick([target], {10: 0})
            self.assertIn("已恢复", result["blocked"]); self.assertEqual(client.calls, [("drop", 11), ("add", 10), ("add", 11)])

    def test_unknown_swap_add_never_rolls_back(self):
        target = lesson(PRIMARY, 10, 4, "A: 1(1,2)")
        old = lesson(OLD, 11, 4, "A: 1(1,2)")
        with tempfile.TemporaryDirectory() as folder:
            client = Client([old, lesson(OTHER, 1, 8, "A: 5(1,2)")])
            def submit(action, ident):
                client.calls.append((action, ident))
                if action == "drop":
                    client.current = [item for item in client.current if item["id"] != ident]
                return str(len(client.calls))
            client.submit = submit
            client.outcome = lambda request_id: ({"success": True} if request_id == "1" else (_ for _ in ()).throw(RuntimeError("network")))
            journal = Path(folder) / "unknown"
            result = self.engine(client, journal).tick([target], {10: 0})
            self.assertIn("未确认", result["blocked"])
            self.assertEqual(client.calls, [("drop", 11), ("add", 10)])
            self.assertTrue(journal.exists())

    def test_unconfigured_blank_schedule_blocks_and_explicit_selected_exemption_allows(self):
        target = lesson(PRIMARY, 10, 4, "A: 3(1,2)")
        blank = lesson(OTHER, 1, 2, "", "")
        with tempfile.TemporaryDirectory() as folder:
            client = Client([blank])
            self.assertIn("无法确认", self.engine(client, Path(folder) / "no").tick([target], {10: 0})["blocked"])
            client = Client([blank])
            def add(action, ident): client.calls.append((action, ident)); client.current.append(target); return str(len(client.calls))
            client.submit = add
            self.assertIsNone(self.engine(client, Path(folder) / "yes", unscheduled_selected_codes={OTHER}).tick([target], {10: 0})["blocked"])


if __name__ == "__main__": unittest.main()
