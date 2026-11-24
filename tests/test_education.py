from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import education  # noqa: E402
import setu  # noqa: E402


def test_plan_respects_daily_budget_and_excludes_exam_day():
    result = education.study_plan(
        ["algebra", "graphs", "word problems"], "2026-06-12", 60,
        start_date="2026-06-01", session_minutes=45, review_minutes=15,
    )

    assert result["topics_scheduled"] == ["algebra", "graphs", "word problems"]
    assert result["unscheduled_topics"] == []
    assert all(item["total_minutes"] <= 60 for item in result["schedule"])
    assert all(item["date"] != "2026-06-12" for item in result["schedule"])


def test_reviews_observe_spaced_intervals_and_are_reported_when_unfit():
    result = education.study_plan(
        ["biology"], "2026-06-10", 45, start_date="2026-06-01",
        session_minutes=45, review_minutes=15,
    )
    reviews = [task for day in result["schedule"] for task in day["tasks"]
               if task["type"] == "review"]

    assert [task["interval_days"] for task in reviews] == [1, 3, 7]
    assert all(task["delayed_days"] == 0 for task in reviews)
    assert all(day["total_minutes"] <= 45 for day in result["schedule"])


def test_weekdays_only_skips_weekends_and_bounded_schedule_reports_overflow():
    result = education.study_plan(
        ["a", "b", "c", "d", "e", "f"], "2026-06-06", 45,
        start_date="2026-06-01", session_minutes=45, include_weekends=False,
    )

    assert all(date.fromisoformat(day["date"]).weekday() < 5 for day in result["schedule"])
    assert result["unscheduled_topics"]
    assert result["warnings"]


def test_invalid_dates_budgets_and_duplicate_topics_are_rejected():
    with pytest.raises(ValueError, match="after start_date"):
        education.study_plan(["math"], "2026-06-01", 60, start_date="2026-06-01")
    with pytest.raises(ValueError, match="positive integer"):
        education.study_plan(["math"], "2026-06-02", 0, start_date="2026-06-01")
    with pytest.raises(ValueError, match="duplicate topic"):
        education.study_plan(["Math", "math"], "2026-06-02", 60,
                             start_date="2026-06-01")


def test_cli_outputs_json():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "education.py"), "plan",
         "--topics", "fractions,decimals", "--start-date", "2026-06-01",
         "--exam-date", "2026-06-08", "--minutes-per-day", "60"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["check"] == "education-study-plan"


def test_router_selects_education_for_study_planning_but_not_other_schools():
    routed = setu.route("help me make a study plan for my biology exam")
    assert routed["route"] == "education"
    assert setu.route("teach me photosynthesis") ["route"] == "education"
    assert setu.route("quiz me on geometry") ["route"] == "education"
    assert setu.route("write my school complaint letter")["route"] == "admin"


def test_education_context_uses_shared_memory_and_pack_validation():
    context = setu.build_context("education", "study plan")
    assert context["pack"] == "education"
    with pytest.raises(ValueError, match="unknown pack"):
        setu.remember("not-a-real-pack", "input", "output", True)
