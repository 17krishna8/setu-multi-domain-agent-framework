#!/usr/bin/env python3
"""Setu Education — deterministic study-plan and spaced-review scheduling.

The planner organizes user-provided topics and dates. It does not judge mastery,
produce answers for graded work, or invent curriculum facts.

Example:
    python3 education.py plan --topics "algebra,linear equations,graphs" \
        --exam-date 2026-07-15 --minutes-per-day 90
"""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta

REVIEW_INTERVALS_DAYS = (1, 3, 7)


def study_plan(topics: list[str], exam_date: str, minutes_per_day: int, *,
               start_date: str | None = None, session_minutes: int = 45,
               review_minutes: int = 15, include_weekends: bool = True) -> dict:
    """Build a predictable study schedule without exceeding the daily time budget.

    Learning sessions are placed first in topic order. Reviews are then scheduled
    no earlier than 1, 3, and 7 days after learning, moved forward when a day is
    full. Unscheduled work is reported explicitly rather than silently dropped.
    The exam date is exclusive; no study tasks are scheduled on exam day.
    """
    if not isinstance(topics, list) or not topics:
        raise ValueError("at least one topic is required")
    clean_topics = [str(topic).strip() for topic in topics]
    if any(not topic for topic in clean_topics):
        raise ValueError("topics cannot be empty")
    seen: set[str] = set()
    for topic in clean_topics:
        key = topic.casefold()
        if key in seen:
            raise ValueError(f"duplicate topic: {topic}")
        seen.add(key)

    try:
        exam = date.fromisoformat(exam_date)
        start = date.fromisoformat(start_date) if start_date else date.today()
    except (TypeError, ValueError) as exc:
        raise ValueError("dates must use YYYY-MM-DD format") from exc
    if exam <= start:
        raise ValueError("exam_date must be after start_date")
    if not isinstance(minutes_per_day, int) or isinstance(minutes_per_day, bool) or minutes_per_day <= 0:
        raise ValueError("minutes_per_day must be a positive integer")
    if not isinstance(session_minutes, int) or isinstance(session_minutes, bool) or session_minutes <= 0:
        raise ValueError("session_minutes must be a positive integer")
    if session_minutes > minutes_per_day:
        raise ValueError("session_minutes cannot exceed minutes_per_day")
    if not isinstance(review_minutes, int) or isinstance(review_minutes, bool) or review_minutes <= 0:
        raise ValueError("review_minutes must be a positive integer")
    if review_minutes > minutes_per_day:
        raise ValueError("review_minutes cannot exceed minutes_per_day")

    days: list[date] = []
    current = start
    while current < exam:
        if include_weekends or current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    if not days:
        raise ValueError("no study days available before the exam")

    plan: dict[date, list[dict]] = {day: [] for day in days}
    used: dict[date, int] = {day: 0 for day in days}
    learning_dates: dict[str, date] = {}
    unscheduled_topics: list[str] = []

    # Give every topic one focused learning session before allocating reviews.
    day_index = 0
    for topic in clean_topics:
        while day_index < len(days) and used[days[day_index]] + session_minutes > minutes_per_day:
            day_index += 1
        if day_index >= len(days):
            unscheduled_topics.append(topic)
            continue
        day = days[day_index]
        plan[day].append({"type": "learn", "topic": topic, "minutes": session_minutes})
        used[day] += session_minutes
        learning_dates[topic] = day

    unscheduled_reviews: list[dict] = []
    review_queue: list[tuple[date, int, str]] = []
    for topic in clean_topics:
        learned_on = learning_dates.get(topic)
        if learned_on is None:
            continue
        for interval in REVIEW_INTERVALS_DAYS:
            due = learned_on + timedelta(days=interval)
            if due < exam:
                review_queue.append((due, interval, topic))

    review_queue.sort(key=lambda item: (item[0], item[1], item[2].casefold()))
    for due, interval, topic in review_queue:
        first_index = next((i for i, day in enumerate(days) if day >= due), len(days))
        scheduled = False
        for day in days[first_index:]:
            if used[day] + review_minutes <= minutes_per_day:
                plan[day].append({
                    "type": "review", "topic": topic, "minutes": review_minutes,
                    "interval_days": interval, "due_date": due.isoformat(),
                    "delayed_days": (day - due).days,
                })
                used[day] += review_minutes
                scheduled = True
                break
        if not scheduled:
            unscheduled_reviews.append({"topic": topic, "interval_days": interval,
                                        "due_date": due.isoformat(),
                                        "minutes": review_minutes})

    schedule = []
    for day in days:
        tasks = plan[day]
        schedule.append({"date": day.isoformat(), "tasks": tasks,
                         "total_minutes": used[day],
                         "remaining_minutes": minutes_per_day - used[day]})

    warnings = []
    if unscheduled_topics:
        warnings.append("Some topics could not fit before the exam; add study days or time.")
    if unscheduled_reviews:
        warnings.append("Some spaced reviews could not fit within the daily time budget.")
    delayed_count = sum(task.get("delayed_days", 0) > 0
                        for day in schedule for task in day["tasks"])
    if delayed_count:
        warnings.append(f"{delayed_count} review(s) were moved later because earlier days were full.")

    return {
        "check": "education-study-plan",
        "method": "deterministic calendar scheduling; no model-generated dates or durations",
        "start_date": start.isoformat(),
        "exam_date": exam.isoformat(),
        "exam_day_included": False,
        "minutes_per_day": minutes_per_day,
        "session_minutes": session_minutes,
        "review_minutes": review_minutes,
        "review_intervals_days": list(REVIEW_INTERVALS_DAYS),
        "include_weekends": include_weekends,
        "topics_requested": clean_topics,
        "topics_scheduled": list(learning_dates),
        "unscheduled_topics": unscheduled_topics,
        "unscheduled_reviews": unscheduled_reviews,
        "schedule": schedule,
        "warnings": warnings,
        "note": "A scheduled session is a planning aid, not evidence of mastery. "
                "Use active recall and ask a teacher for help when needed.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="education.py",
                                     description="Create a deterministic study plan")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="schedule learning sessions and spaced reviews")
    plan.add_argument("--topics", required=True,
                      help="comma-separated topics in the order you want to study")
    plan.add_argument("--exam-date", required=True, help="YYYY-MM-DD; study day is exclusive")
    plan.add_argument("--start-date", default=None, help="YYYY-MM-DD; defaults to today")
    plan.add_argument("--minutes-per-day", type=int, required=True)
    plan.add_argument("--session-minutes", type=int, default=45)
    plan.add_argument("--review-minutes", type=int, default=15)
    plan.add_argument("--weekdays-only", action="store_true",
                      help="leave Saturday and Sunday unscheduled")
    args = parser.parse_args(argv)
    topics = [item.strip() for item in args.topics.split(",")]
    try:
        result = study_plan(topics, args.exam_date, args.minutes_per_day,
                            start_date=args.start_date,
                            session_minutes=args.session_minutes,
                            review_minutes=args.review_minutes,
                            include_weekends=not args.weekdays_only)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
