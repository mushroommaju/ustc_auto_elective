"""Fail-closed selection decisions independent of any particular course plan."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any


SLOT_RE = re.compile(r"[^:;]+:\s*([1-7])\(([^)]+)\)\s*")
WEEK_RE = re.compile(r"第?\d+(?:\s*(?:~|-|至)\s*第?\d+)?(?:周)?(?:\s*[,，]\s*第?\d+(?:\s*(?:~|-|至)\s*第?\d+)?(?:周)?)*\s*")


def _code(lesson: dict[str, Any]) -> str:
    return str(lesson.get("code") or "").strip()


def _base(code: str) -> str:
    return code.rsplit(".", 1)[0] if "." in code else code


def _id(lesson: dict[str, Any]) -> int | None:
    value = lesson.get("id")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _credits(lesson: dict[str, Any]) -> float:
    course = lesson.get("course")
    value = course.get("credits") if isinstance(course, dict) else lesson.get("credits")
    if isinstance(value, bool) or value is None:
        raise ValueError("课程学分无效")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("课程学分无效") from error
    if result < 0 or not math.isfinite(result):
        raise ValueError("课程学分无效")
    return result


def _text(lesson: dict[str, Any], field: str) -> str:
    value = lesson.get(field) or {}
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("textZh") or value.get("text") or "").strip()
    return ""


def _schedule(lesson: dict[str, Any], *, selected: bool, unscheduled: set[str]) -> tuple[set[tuple[int, int]], tuple[int, int]] | None:
    timetable, weeks = _text(lesson, "dateTimePlace"), _text(lesson, "weekText")
    if not timetable and not weeks and selected and _code(lesson) in unscheduled:
        return None
    if not timetable or not weeks or not WEEK_RE.fullmatch(weeks):
        raise ValueError("课程上课时间或周次无法确认")
    slots: set[tuple[int, int]] = set()
    for piece in (part.strip() for part in re.split(r"[;\n]", timetable) if part.strip()):
        match = SLOT_RE.fullmatch(piece)
        if not match:
            raise ValueError("课程上课时间格式无法确认")
        day, units = match.groups()
        for unit in units.split(","):
            if not unit.strip().isdigit():
                raise ValueError("课程上课时间格式无法确认")
            slots.add((int(day), int(unit.strip())))
    if not slots:
        raise ValueError("课程上课时间格式无法确认")
    numbers = [int(value) for value in re.findall(r"\d+", weeks)]
    return slots, (min(numbers), max(numbers))


class Engine:
    """One-action synchronous state machine; its caller owns transport and cadence."""
    def __init__(self, client: Any, *, targets: list[str], mode: str, max_credits: float,
                 swaps: dict[str, str], after_select: dict[str, str], journal_path: Path,
                 logger: Any, prerequisites: dict[str, dict[str, tuple[str, ...]]] | None = None,
                 groups: dict[str, str] | None = None,
                 unscheduled_selected_codes: set[str] | None = None) -> None:
        self.client = client
        self.targets = list(dict.fromkeys(targets))
        self.mode = mode
        self.max_credits = float(max_credits)
        self.swaps = dict(swaps)
        self.after_select = dict(after_select)
        self.prerequisites = dict(prerequisites or {})
        self.groups = dict(groups or {})
        self.unscheduled = set(unscheduled_selected_codes or set())
        self.journal_path = Path(journal_path)
        self.logger = logger
        self._cooldowns: dict[str, int] = {}
        self._warned_unscheduled: set[str] = set()
        self._journal = self._read_journal()

    def _read_journal(self) -> dict[str, Any] | None:
        if not self.journal_path.exists():
            return None
        try:
            value = json.loads(self.journal_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"stage": "unknown"}
        except (OSError, json.JSONDecodeError):
            return {"stage": "unknown"}

    def _save_journal(self, value: dict[str, Any]) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".transaction-", suffix=".tmp", dir=self.journal_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(value, output, ensure_ascii=False, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.journal_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self._journal = value

    def _clear_journal(self) -> None:
        if self.journal_path.exists():
            self.journal_path.unlink()
        self._journal = None

    def _selected(self) -> list[dict[str, Any]] | None:
        try:
            value = self.client.selected()
            return value if isinstance(value, list) else None
        except Exception:
            return None

    @staticmethod
    def _matches(items: list[dict[str, Any]], code: str) -> list[dict[str, Any]]:
        return [item for item in items if _code(item) == code]

    def _gate(self, code: str, selected: list[dict[str, Any]]) -> str | None:
        codes = {_code(item) for item in selected}
        rule = self.prerequisites.get(code, {})
        need_selected = tuple(rule.get("selected", ()))
        need_absent = tuple(rule.get("absent", ()))
        if not all(item in codes for item in need_selected):
            return "等待前置课程已选"
        if any(item in codes for item in need_absent):
            return "等待前置课程退掉"
        group = self.groups.get(code)
        group_bases = {_base(member) for member, name in self.groups.items() if group and name == group}
        if any(other != code and (_base(other) in group_bases or _base(other) == _base(code)) for other in codes):
            return "同组选项已有课程，禁止重复选择"
        return None

    def _conflicts(self, target: dict[str, Any], selected: list[dict[str, Any]]) -> tuple[str | None, list[str]]:
        try:
            target_schedule = _schedule(target, selected=False, unscheduled=self.unscheduled)
        except ValueError as error:
            return str(error), []
        conflicts: list[str] = []
        for item in selected:
            try:
                current = _schedule(item, selected=True, unscheduled=self.unscheduled)
            except ValueError:
                return "已选课程时间信息无法确认", []
            if target_schedule is None or current is None:
                continue
            slots_a, (start_a, end_a) = target_schedule
            slots_b, (start_b, end_b) = current
            if slots_a & slots_b and start_a <= end_b and start_b <= end_a:
                conflicts.append(_code(item))
        return None, conflicts

    def _can_add(self, target: dict[str, Any], selected: list[dict[str, Any]]) -> tuple[str | None, list[str]]:
        if _id(target) is None:
            return "课堂编号无效", []
        if len(target.get("scheduleGroups") or []) > 1:
            return "课堂有多个可选教学班，已停止", []
        return self._conflicts(target, selected)

    def _credit_ok(self, selected: list[dict[str, Any]], target: dict[str, Any]) -> bool:
        return sum(_credits(item) for item in selected) + _credits(target) <= self.max_credits + 1e-9

    def _status(self, selected: list[dict[str, Any]], lessons: list[dict[str, Any]], counts: dict[Any, int]) -> dict[str, str]:
        codes = {_code(item) for item in selected}
        result: dict[str, str] = {}
        for code in self.targets:
            if code in codes:
                result[code] = "已选"
                continue
            found = self._matches(lessons, code)
            if len(found) != 1:
                result[code] = "课堂代码未唯一匹配"
                continue
            target = found[0]
            identifier = _id(target)
            used = counts.get(identifier, counts.get(str(identifier)))
            limit = target.get("limitCount")
            if (not isinstance(used, int) or isinstance(used, bool) or used < 0 or
                    not isinstance(limit, int) or isinstance(limit, bool) or limit < 0):
                result[code] = "人数未知"
                continue
            notes = ["已满"] if used >= limit else []
            gate = self._gate(code, selected)
            reason, conflicts = self._can_add(target, selected)
            if gate: notes.append(gate)
            if reason: notes.append(reason)
            elif conflicts: notes.append("与课表冲突")
            elif not self._credit_ok(selected, target): notes.append("加课将超过学分上限")
            result[code] = f"人数 {used}/{limit}" + (f"（{'；'.join(notes)}）" if notes else "（可尝试）")
        return result

    @staticmethod
    def _pending(outcome: Any) -> bool:
        return not isinstance(outcome, dict) or outcome.get("code") == "PENDING" or not isinstance(outcome.get("success"), bool)

    def _submit(self, action: str, identifier: int, journal: dict[str, Any]) -> bool | None:
        self._save_journal(journal)
        self.logger.info("操作意图：%s %s", action, journal.get("target", "课程"))
        try:
            request_id = self.client.submit(action, identifier)
        except Exception:
            self.logger.critical("操作结果未知：%s", journal.get("target", "课程"))
            return None
        self._save_journal({**journal, "request_id": str(request_id), "stage": f"{journal['stage']}_submitted"})
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                outcome = self.client.outcome(str(request_id))
            except Exception:
                self.logger.critical("操作结果未知：%s", journal.get("target", "课程"))
                return None
            if not self._pending(outcome):
                self.logger.info("教务明确%s：%s", "成功" if outcome["success"] else "拒绝", journal.get("target", "课程"))
                return bool(outcome["success"])
            time.sleep(1)
        self.logger.critical("操作结果未知：%s", journal.get("target", "课程"))
        return None

    def _after_success(self, target_code: str, selected: list[dict[str, Any]]) -> str | None:
        drop_code = self.after_select.get(target_code)
        if not drop_code:
            self._clear_journal()
            return None
        fresh = self._selected()
        if fresh is None or not self._matches(fresh, target_code):
            return "目标课程未在官方课表中确认，已停止自动操作"
        selected = fresh
        if not drop_code or not self._matches(selected, drop_code):
            self._clear_journal()
            return None
        old = self._matches(selected, drop_code)
        if len(old) != 1 or _id(old[0]) is None:
            return "后续退课课堂状态不唯一，保留事务"
        outcome = self._submit("drop", _id(old[0]), {"stage": "post_success_drop", "target": target_code, "drop": drop_code})
        if outcome is None:
            return "后续退课结果未知，已停止自动操作"
        current = self._selected()
        if outcome and current is not None and not self._matches(current, drop_code):
            self._clear_journal()
            self.logger.info("课表已确认：已退 %s", drop_code)
            return None
        return "后续退课未确认，已停止自动操作"

    def _direct_add(self, code: str, target: dict[str, Any], selected: list[dict[str, Any]], count: int) -> str | None:
        if not self._credit_ok(selected, target): return "加课将超过学分上限"
        identifier = _id(target)
        assert identifier is not None
        outcome = self._submit("add", identifier, {"stage": "add", "target": code, "lesson_id": identifier})
        if outcome is None: return "加课结果未知，已停止自动操作"
        current = self._selected()
        present = bool(current and self._matches(current, code))
        if current is None or outcome != present: return "加课结果未确认，已停止自动操作"
        if not outcome:
            self._clear_journal()
            self._cooldowns[code] = count
            return "教务未确认选上，等待人数变化后再试"
        self.logger.info("课表已确认：已选 %s", code)
        return self._after_success(code, current)

    def _swap_add(self, code: str, target: dict[str, Any], selected: list[dict[str, Any]], count: int) -> str | None:
        old_code = self.swaps.get(code, "")
        old = self._matches(selected, old_code)
        if len(old) != 1 or _id(old[0]) is None: return "替换课程状态不唯一"
        old_id = _id(old[0])
        assert old_id is not None
        if not self._credit_ok([item for item in selected if _id(item) != old_id], target): return "替换后加课将超过学分上限"
        outcome = self._submit("drop", old_id, {"stage": "swap_drop", "target": code, "drop": old_code})
        current = self._selected()
        if outcome is None: return "替换退课结果未知，已停止自动操作"
        still_old = bool(current and self._matches(current, old_code))
        if current is None or outcome == still_old: return "替换退课结果未确认，已停止自动操作"
        if not outcome:
            self._clear_journal()
            self._cooldowns[code] = count
            return "替换退课被拒绝，等待人数变化后再试"
        gate = self._gate(code, current)
        reason, conflicts = self._can_add(target, current)
        if gate or reason or conflicts or not self._credit_ok(current, target):
            return "退课后加课条件变化，已停止自动操作"
        identifier = _id(target)
        assert identifier is not None
        outcome = self._submit("add", identifier, {"stage": "swap_add", "target": code, "drop": old_code})
        current = self._selected()
        present = bool(current and self._matches(current, code))
        if outcome is None or current is None or outcome != present: return "替换加课结果未确认，已停止自动操作"
        if outcome:
            self.logger.info("课表已确认：已选 %s", code)
            return self._after_success(code, current)
        # The only rollback is a fresh, definitive failed add after a confirmed drop.
        try: offered = self.client.lessons()
        except Exception: offered = []
        old_options = self._matches(offered if isinstance(offered, list) else [], old_code)
        restore = old_options[0] if len(old_options) == 1 else None
        if restore is None or not self._credit_ok(current, restore): return "加课未成功且无法安全恢复原课程，已停止自动操作"
        reason, conflicts = self._can_add(restore, current)
        if reason or conflicts: return "加课未成功且原课程恢复条件不满足，已停止自动操作"
        restored = self._submit("add", _id(restore), {"stage": "swap_restore", "target": code, "restore": old_code})
        confirmed = self._selected()
        if restored and confirmed is not None and self._matches(confirmed, old_code):
            self._clear_journal()
            self._cooldowns[code] = count
            return "加课未成功，已恢复原课程；等待人数变化后再试"
        return "原课程恢复未确认，已停止自动操作"

    def tick(self, lessons: list[dict[str, Any]], counts: dict[Any, int], *, observed_at: float | None = None) -> dict[str, Any]:
        observed_at = time.monotonic() if observed_at is None else observed_at
        selected = self.client.selected()
        if not isinstance(selected, list): raise ValueError("已选课程格式异常")
        try: credits = sum(_credits(item) for item in selected)
        except ValueError: return {"selected_credits": None, "targets": {}, "blocked": "已选课程学分无效，未执行自动操作"}
        result = {"selected_credits": credits, "targets": self._status(selected, lessons, counts), "blocked": None}
        if self._journal is not None:
            result["blocked"] = "检测到未完成选退课事务，已停止自动操作；请先人工核对"
            return result
        if self.mode != "auto":
            return result
        for item in selected:
            if _code(item) in self.unscheduled and not _text(item, "dateTimePlace") and not _text(item, "weekText") and _code(item) not in self._warned_unscheduled:
                self.logger.warning("%s 无排课信息；按明确配置跳过其冲突判断", _code(item))
                self._warned_unscheduled.add(_code(item))
        if time.monotonic() - observed_at > 5:
            result["blocked"] = "人数数据已过期，等待下一批官网人数后再执行自动操作"
            return result
        for code in self.targets:
            if self._matches(selected, code):
                # A configured post-success cleanup is one transition, not a chance to add another target.
                if self.after_select.get(code) and self._matches(selected, self.after_select[code]):
                    result["blocked"] = self._after_success(code, selected)
                    return result
                continue
            gate = self._gate(code, selected)
            if gate:
                result["blocked"] = gate
                continue
            found = self._matches(lessons, code)
            if len(found) != 1: continue
            target = found[0]
            identifier = _id(target)
            used = counts.get(identifier, counts.get(str(identifier)))
            limit = target.get("limitCount")
            if (not isinstance(used, int) or isinstance(used, bool) or used < 0 or
                    not isinstance(limit, int) or isinstance(limit, bool) or limit < 0):
                result["blocked"] = "人数数据无效，未执行自动操作"
                continue
            if used >= limit or self._cooldowns.get(code) == used:
                continue
            reason, conflicts = self._can_add(target, selected)
            if reason:
                result["blocked"] = reason
                continue
            if not conflicts:
                result["blocked"] = self._direct_add(code, target, selected, used)
                return result
            if conflicts == [self.swaps.get(code)] and self.swaps.get(code):
                result["blocked"] = self._swap_add(code, target, selected, used)
                return result
            result["blocked"] = "目标课程与当前课表冲突，未授权退课"
        return result
