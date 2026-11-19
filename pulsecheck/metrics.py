"""Metrics: uptime, response-time percentiles and the numbers behind the charts.

Where the numbers come from:
- the last two days: the raw checks themselves (exact percentiles);
- longer: the hourly and daily rollups (rollups.py), added up, plus the raw
  checks of the hours not rolled up yet. Percentiles then come from the
  merged histograms, so they are estimates within one bucket (shown with ≈).

Uptime = good checks / counted checks. "Counted" leaves out checks that
failed on OUR side (canary.py) and, of course, slots nobody checked (gaps):
neither says anything about the service. So 99.9% means "of the times we
really looked, it answered right 999 times in 1,000".

Every function takes the time window as arguments (never the clock), so tests
and the demo can ask about any moment.
"""
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import case, func, select

from .dates import from_zone, to_zone
from .models import BUCKET_EDGES, BUCKETS, Check, Gap, Rollup

RAW_WINDOW = timedelta(days=2)  # shorter windows read raw checks
PERIODS = {"24h": ("24 Hours", timedelta(hours=24)), "7d": ("7 Days", timedelta(days=7)),
           "30d": ("30 Days", timedelta(days=30)), "90d": ("90 Days", timedelta(days=90))}
STRIP_DAYS = 90
# The uptime strip's levels: (lowest uptime for this level, level). Below the last: "down".
LEVELS = ((0.999, "up"), (0.99, "some"))


@dataclass
class Stats:
    checks: int = 0
    ok: int = 0
    slow: int = 0
    our_side: int = 0
    missed: int = 0
    sum_ms: float = 0.0
    min_ms: float | None = None
    max_ms: float | None = None
    buckets: list = field(default_factory=lambda: [0] * BUCKETS)
    values: list | None = None  # the exact response times, when they came from raw checks

    @property
    def counted(self):
        return self.checks - self.our_side

    @property
    def uptime(self):
        """A fraction (0.9993), or None when nothing was counted."""
        return self.ok / self.counted if self.counted else None

    @property
    def average(self):
        return self.sum_ms / self.ok if self.ok else None

    @property
    def exact(self):
        return self.values is not None

    def add(self, other):
        self.checks += other.checks
        self.ok += other.ok
        self.slow += other.slow
        self.our_side += other.our_side
        self.missed += other.missed
        self.sum_ms += other.sum_ms
        mins = [value for value in (self.min_ms, other.min_ms) if value is not None]
        maxes = [value for value in (self.max_ms, other.max_ms) if value is not None]
        self.min_ms, self.max_ms = (min(mins) if mins else None), (max(maxes) if maxes else None)
        self.buckets = [mine + theirs for mine, theirs in zip(self.buckets, other.buckets)]
        if self.values is not None and other.values is not None:
            self.values = self.values + other.values
        else:
            self.values = None
        return self

    def percentile(self, p):
        """The response time below which p% of good checks fell (None with no good checks)."""
        if self.values:
            ordered = sorted(self.values)
            rank = max(0, math.ceil(p / 100 * len(ordered)) - 1)  # nearest rank
            return ordered[rank]
        return percentile_of(self.buckets, p, self.min_ms, self.max_ms)


def percentile_of(buckets, p, low=None, high=None):
    """Read a percentile off a histogram: find the bucket that holds it, and assume the
    times inside a bucket are spread evenly (so 30% into the 100-150 ms bucket is 115 ms)."""
    total = sum(buckets)
    if not total:
        return None
    target = p / 100 * total
    seen = 0
    for n, count in enumerate(buckets):
        if count and seen + count >= target:
            bottom = BUCKET_EDGES[n - 1] if n else 0
            top = BUCKET_EDGES[n] if n < len(BUCKET_EDGES) else (high or bottom * 2)
            if low is not None:
                bottom = max(bottom, min(low, top))
            if high is not None:
                top = min(top, max(high, bottom))
            return bottom + (top - bottom) * (target - seen) / count
        seen += count
    return high


def bucket_of(column):
    """SQL: which bucket a response time falls in (0 .. BUCKETS-1)."""
    return case(*[(column <= edge, n) for n, edge in enumerate(BUCKET_EDGES)], else_=len(BUCKET_EDGES))


def bucket_index(ms):
    for n, edge in enumerate(BUCKET_EDGES):
        if ms <= edge:
            return n
    return len(BUCKET_EDGES)


# ---------- Raw checks ----------

def raw_stats(s, monitor_ids, start, end, keep_values=True):
    """{monitor_id: Stats} from the raw checks with start <= scheduled_at < end."""
    found = {monitor_id: Stats(values=[] if keep_values else None) for monitor_id in monitor_ids}
    if not monitor_ids:
        return found
    rows = s.execute(select(Check.monitor_id, Check.ok, Check.slow, Check.our_side, Check.total_ms)
                     .where(Check.monitor_id.in_(monitor_ids), Check.scheduled_at >= start,
                            Check.scheduled_at < end)).all()
    for monitor_id, ok, slow, our_side, total_ms in rows:
        stats = found[monitor_id]
        stats.checks += 1
        stats.our_side += bool(our_side)
        if ok:
            stats.ok += 1
            stats.slow += bool(slow)
            if total_ms is not None:
                stats.sum_ms += total_ms
                stats.min_ms = total_ms if stats.min_ms is None else min(stats.min_ms, total_ms)
                stats.max_ms = total_ms if stats.max_ms is None else max(stats.max_ms, total_ms)
                stats.buckets[bucket_index(total_ms)] += 1
                if stats.values is not None:
                    stats.values.append(total_ms)
    gaps = s.execute(select(Gap.monitor_id, func.sum(Gap.slots)).where(
        Gap.monitor_id.in_(monitor_ids), Gap.start >= start, Gap.start < end).group_by(Gap.monitor_id)).all()
    for monitor_id, slots in gaps:
        found[monitor_id].missed += int(slots or 0)
    return found


# ---------- Rollups ----------

def from_row(row):
    return Stats(checks=row.checks, ok=row.ok, slow=row.slow, our_side=row.our_side, missed=row.missed,
                 sum_ms=row.sum_ms or 0.0, min_ms=row.min_ms, max_ms=row.max_ms, buckets=row.histogram())


def rollup_stats(s, monitor_ids, period, start, end):
    """{monitor_id: Stats} adding up the rollups of `period` with start <= start < end (in SQL)."""
    found = {monitor_id: Stats() for monitor_id in monitor_ids}
    if not monitor_ids:
        return found
    columns = [func.sum(getattr(Rollup, name)) for name in ("checks", "ok", "slow", "our_side", "missed", "sum_ms")]
    columns += [func.min(Rollup.min_ms), func.max(Rollup.max_ms)]
    columns += [func.sum(getattr(Rollup, f"b{n}")) for n in range(BUCKETS)]
    rows = s.execute(select(Rollup.monitor_id, *columns).where(
        Rollup.monitor_id.in_(monitor_ids), Rollup.period == period, Rollup.start >= start, Rollup.start < end)
        .group_by(Rollup.monitor_id)).all()
    for row in rows:
        values = list(row[1:])
        checks, ok, slow, our_side, missed, sum_ms, min_ms, max_ms = values[:8]
        found[row[0]] = Stats(checks=int(checks or 0), ok=int(ok or 0), slow=int(slow or 0),
                              our_side=int(our_side or 0), missed=int(missed or 0), sum_ms=float(sum_ms or 0),
                              min_ms=min_ms, max_ms=max_ms, buckets=[int(value or 0) for value in values[8:]])
    return found


def local_midnight(moment, zone_name):
    """The UTC moment the workspace's day containing `moment` began."""
    local = to_zone(moment, zone_name).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    return from_zone(local, zone_name)


def day_starts(zone_name, first, last):
    """UTC moments of the local midnights from the day of `first` to the day of `last`."""
    starts, moment = [], local_midnight(first, zone_name)
    while moment <= last:
        starts.append(moment)
        moment = local_midnight(moment + timedelta(hours=26), zone_name)  # +26 h lands in the next day, DST or not
    return starts


def floor_hour(moment):
    return moment.replace(minute=0, second=0, microsecond=0)


def first_raw(s, monitor_ids, start, end):
    """{monitor_id: the first raw check in [start, end)}: before it, only rollups know."""
    if not monitor_ids:
        return {}
    return dict(s.execute(select(Check.monitor_id, func.min(Check.scheduled_at)).where(
        Check.monitor_id.in_(monitor_ids), Check.scheduled_at >= start, Check.scheduled_at < end)
        .group_by(Check.monitor_id)).all())


def short_window(s, monitor_ids, start, end, keep_values=True):
    """A window of up to two days: the raw checks, exactly. Hours before a monitor's first raw
    check in the window (raw checks deleted after 14 days, or a demo's older past) come
    from its hourly rollups instead, so a long-ago gap in the raw checks isn't "no data"."""
    found = raw_stats(s, monitor_ids, start, end, keep_values)
    firsts = first_raw(s, monitor_ids, start, end)
    for monitor_id in monitor_ids:
        until = floor_hour(firsts.get(monitor_id, end))
        if until > start:
            older = rollup_stats(s, [monitor_id], "hour", floor_hour(start), until)[monitor_id]
            if older.checks:
                found[monitor_id].add(older)
    return found


def window(s, monitor_ids, start, end, rolled_until, zone_name):
    """{monitor_id: Stats} for start <= moment < end, from the cheapest exact-enough source.

    rolled_until: hours before it have hourly rollups (rollups.py). Short windows read raw
    checks; long ones add up daily rollups for whole past days, hourly rollups for the
    rest, and raw checks for the hours not rolled up yet."""
    if end - start <= RAW_WINDOW:
        return short_window(s, monitor_ids, start, end)
    total = {monitor_id: Stats() for monitor_id in monitor_ids}

    def add(part):
        for monitor_id, stats in part.items():
            total[monitor_id].add(stats)

    today = local_midnight(min(end, rolled_until), zone_name)
    first_day = local_midnight(start, zone_name)
    add(rollup_stats(s, monitor_ids, "day", first_day, today))  # whole past days
    hours_end = max(today, floor_hour(min(end, rolled_until)))
    add(rollup_stats(s, monitor_ids, "hour", today, hours_end))  # today's rolled hours
    add(raw_stats(s, monitor_ids, hours_end, end, keep_values=False))  # and since then
    return total


# ---------- Chart series ----------

@dataclass
class Point:
    start: datetime
    end: datetime
    p50: float | None
    p95: float | None
    stats: Stats


def series(s, monitor_id, period, now, rolled_until, zone_name):
    """Points for a response-time chart: [Point] with p50 and p95 per bin."""
    _label, length = PERIODS[period]
    start = now - length
    points = []
    if period == "24h":
        step = timedelta(minutes=30)
        bin_start = start.replace(minute=start.minute // 30 * 30, second=0, microsecond=0)
        hours = {row.start: row for row in s.scalars(select(Rollup).where(
            Rollup.monitor_id == monitor_id, Rollup.period == "hour", Rollup.start >= floor_hour(start)))}
        while bin_start < now:
            stats = raw_stats(s, [monitor_id], bin_start, bin_start + step)[monitor_id]
            if not stats.checks and floor_hour(bin_start) in hours:
                stats = from_row(hours[floor_hour(bin_start)])  # no raw checks: the hour's rollup
            points.append(Point(bin_start, bin_start + step, stats.percentile(50), stats.percentile(95), stats))
            bin_start += step
        return points
    if period == "7d":
        rows = {row.start: row for row in s.scalars(select(Rollup).where(
            Rollup.monitor_id == monitor_id, Rollup.period == "hour", Rollup.start >= floor_hour(start)))}
        hour = floor_hour(start)
        while hour < now:
            if hour in rows:
                stats = from_row(rows[hour])
            elif hour >= floor_hour(rolled_until):
                stats = raw_stats(s, [monitor_id], hour, hour + timedelta(hours=1))[monitor_id]
            else:
                stats = Stats()
            points.append(Point(hour, hour + timedelta(hours=1), stats.percentile(50), stats.percentile(95), stats))
            hour += timedelta(hours=1)
        return points
    for day in day_starts(zone_name, start, now):
        following = local_midnight(day + timedelta(hours=26), zone_name)
        stats = window(s, [monitor_id], day, following, rolled_until, zone_name)[monitor_id] \
            if following > now else rollup_stats(s, [monitor_id], "day", day, following)[monitor_id]
        points.append(Point(day, following, stats.percentile(50), stats.percentile(95), stats))
    return points


@dataclass
class Day:
    start: datetime
    stats: Stats
    level: str  # "up", "some", "down" or "none"


def level_of(stats):
    if stats.uptime is None:
        return "none"
    for lowest, level in LEVELS:
        if stats.uptime >= lowest:
            return level
    return "down"


def strip(s, monitor_ids, now, rolled_until, zone_name, days=STRIP_DAYS):
    """{monitor_id: [Day]}: one per local day for the last `days` days, oldest first."""
    first = local_midnight(now - timedelta(days=days - 1), zone_name)
    starts = day_starts(zone_name, first, now)
    rows = s.scalars(select(Rollup).where(Rollup.monitor_id.in_(monitor_ids), Rollup.period == "day",
                                          Rollup.start >= first)).all() if monitor_ids else []
    by_key = {(row.monitor_id, row.start): row for row in rows}
    today = starts[-1]
    today_stats = window(s, monitor_ids, today, now, rolled_until, zone_name) if monitor_ids else {}
    found = {}
    for monitor_id in monitor_ids:
        line = []
        for start in starts:
            if start == today:
                stats = today_stats[monitor_id]
            else:
                row = by_key.get((monitor_id, start))
                stats = from_row(row) if row else Stats()
            line.append(Day(start, stats, level_of(stats)))
        found[monitor_id] = line
    return found
