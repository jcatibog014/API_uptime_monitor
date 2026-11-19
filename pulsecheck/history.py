"""The demo's older past: 90 days of rollups and incidents, from the practice scripts.

The demo replays its last 3 hours check by check (seed.py). Before that, it
needs months of history for the uptime strip, the 30- and 90-day numbers and
the incident list, but replaying 90 days of checks would take minutes. So
the rollups before the replay are written straight from the same scripts the
practice services answer from (practice.py):

- the last 7 days: an hour row per hour: every slot's outage minute counted
  exactly, the errors and response times from 6 sampled answers an hour;
- the 83 days before: a day row per day, from 24 sampled answers (spread
  over the day) for the response times and the share of errors, and the
  exact minutes of the Payments API's outages;
- an Incident for every past Payments outage, like the live code would have
  opened (confirmed after the 3rd failure, closed after 2 good checks).

The numbers come from the same scripts as the live checks, so the 90-day
strip and the live checks tell one story. Nothing here is a real check row:
raw checks only exist from the replay on.
"""
import random
from datetime import timedelta

from . import practice
from .metrics import bucket_index, day_starts, floor_hour, local_midnight
from .models import BUCKETS, Incident, Rollup
from .scheduler import offset_of

HOUR_DAYS = 7
DAYS = 90
DAY_SAMPLES = 24


def _row(monitor_id, period, start, checks, failures, times):
    """A Rollup from a count of checks, how many failed, and good-check response times
    (all of them, or a sample that stands for the whole day)."""
    ok = max(0, checks - failures)
    row = Rollup(monitor_id=monitor_id, period=period, start=start, checks=checks, ok=ok, slow=0, our_side=0, missed=0,
                 resting=0)
    if times and ok:
        scale = ok / len(times)
        row.sum_ms = sum(times) * scale
        row.min_ms, row.max_ms = min(times), max(times)
        counts = [0] * BUCKETS
        for ms in times:
            counts[bucket_index(ms)] += 1
        # Scale a sample up to the day's good checks, keeping the total exact.
        scaled = [int(count * scale) for count in counts]
        scaled[max(range(BUCKETS), key=lambda n: counts[n])] += ok - sum(scaled)
        for n in range(BUCKETS):
            setattr(row, f"b{n}", scaled[n])
    else:
        row.sum_ms = 0.0
        for n in range(BUCKETS):
            setattr(row, f"b{n}", 0)
    return row


HOUR_SAMPLES = 6


def hour_rows(monitor, hour):
    """One hour: every slot's outage minute counted exactly (cheap), the flaky errors and the
    response times from 6 sampled answers (an answer is the expensive part)."""
    key, params = practice.parse(monitor.url)
    service = practice.SERVICES[key]
    first = hour + timedelta(seconds=offset_of(monitor.id, monitor.interval_s) % monitor.interval_s)
    slots = [first + timedelta(seconds=n * monitor.interval_s) for n in range(3600 // monitor.interval_s)]
    slots = [slot for slot in slots if slot < hour + timedelta(hours=1)]
    down = sum(practice.in_outage(key, service, params, slot) for slot in slots)
    picked = slots[::max(1, len(slots) // HOUR_SAMPLES)][:HOUR_SAMPLES]
    answers = [practice.answer(monitor.url, slot, monitor.id, monitor.timeout_s, monitor.expected_statuses)
               for slot in picked]
    good = [answer for answer in answers if answer.ok]
    flaky = round((len(slots) - down) * (len(answers) - len(good)) / len(answers)) if service.flaky_share else 0
    return _row(monitor.id, "hour", hour, len(slots), min(len(slots), down + flaky), [answer.total_ms for answer in good])


def day_row(monitor, day, following):
    """One day, from samples, plus the exact outage minutes of a service that has outages."""
    key, params = practice.parse(monitor.url)
    service = practice.SERVICES[key]
    checks = int((following - day).total_seconds() // monitor.interval_s)
    dice = random.Random(f"history:{monitor.id}:{day.isoformat()}")
    samples = [day + timedelta(seconds=dice.randrange(int((following - day).total_seconds()))) for _ in range(DAY_SAMPLES)]
    answers = [practice.answer(monitor.url, moment, monitor.id, monitor.timeout_s, monitor.expected_statuses)
               for moment in samples]
    sampled_errors = sum(not answer.ok for answer in answers)
    failures = round(checks * sampled_errors / DAY_SAMPLES) if service.flaky_share else 0
    if service.outage_every or "down" in params:
        step = timedelta(seconds=monitor.interval_s)
        moment, outage_slots = day, 0
        while moment < following:
            outage_slots += practice.in_outage(key, service, params, moment)
            moment += step
        failures += outage_slots
    return _row(monitor.id, "day", day, checks, min(failures, checks), [a.total_ms for a in answers if a.ok])


def outages(monitor, start, end):
    """The script's outage windows that begin in [start, end): [(first minute, minutes)]."""
    key, params = practice.parse(monitor.url)
    service = practice.SERVICES[key]
    if not service.outage_every:
        return []
    found, moment, inside = [], start, None
    while moment < end:
        down = practice.in_outage(key, service, params, moment)
        if down and inside is None:
            inside = moment
        elif not down and inside is not None:
            found.append((inside, int((moment - inside).total_seconds() // 60)))
            inside = None
        moment += timedelta(minutes=1)
    return found


def build(s, workspace, by_service, start, now):
    """seed.extras: the demo's rollups and incidents before the replay starts at `start`."""
    zone_name = workspace.time_zone
    first_hour = floor_hour(start) - timedelta(days=HOUR_DAYS)
    hour_days_from = local_midnight(first_hour + timedelta(days=1), zone_name)  # whole days from hour rows
    for monitor in [monitor for monitor in by_service.values() if monitor.kind == "practice"]:
        rows = []
        hour = first_hour
        while hour < floor_hour(start):
            rows.append(hour_rows(monitor, hour))
            hour += timedelta(hours=1)
        by_hour = {row.start: row for row in rows}
        for day in day_starts(zone_name, start - timedelta(days=DAYS), start):
            following = local_midnight(day + timedelta(hours=26), zone_name)
            if following > floor_hour(start):
                break  # today: the rollup job adds it up from the hours (seed.after_replay)
            if day >= hour_days_from:
                rows.append(_sum_hours(monitor.id, day, [by_hour[h] for h in by_hour if day <= h < following]))
            else:
                rows.append(day_row(monitor, day, following))
        s.add_all(rows)
        for opened, minutes in outages(monitor, start - timedelta(days=DAYS), start):
            confirmed = opened + timedelta(seconds=60)  # the 1st failure, 20 s and 40 s re-checks, then the 3rd
            s.add(Incident(workspace_id=workspace.id, monitor_id=monitor.id, opened_at=opened, confirmed_at=confirmed,
                           resolved_at=opened + timedelta(minutes=minutes, seconds=20), cause="timeout",
                           message=f"No answer within {monitor.timeout_s} s.", failed_checks=minutes + 2))


def _sum_hours(monitor_id, day, rows):
    total = Rollup(monitor_id=monitor_id, period="day", start=day, checks=0, ok=0, slow=0, our_side=0, missed=0,
                   resting=0, sum_ms=0.0)
    for n in range(BUCKETS):
        setattr(total, f"b{n}", 0)
    for row in rows:
        for name in ("checks", "ok", "slow", "our_side", "missed", "resting", "sum_ms"):
            setattr(total, name, getattr(total, name) + (getattr(row, name) or 0))
        for n in range(BUCKETS):
            setattr(total, f"b{n}", getattr(total, f"b{n}") + getattr(row, f"b{n}"))
        if row.min_ms is not None:
            total.min_ms = row.min_ms if total.min_ms is None else min(total.min_ms, row.min_ms)
            total.max_ms = row.max_ms if total.max_ms is None else max(total.max_ms, row.max_ms)
    return total


def init_app(app):
    from . import seed
    if build not in seed.extras:
        seed.extras.append(build)
