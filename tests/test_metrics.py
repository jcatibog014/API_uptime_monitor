"""Metrics: rollups that are safe to run twice, percentiles from merged histograms, uptime that
leaves out what isn't the service's fault, days in the workspace's own time zone, and charts."""
from datetime import datetime, timedelta

from sqlalchemy import func, select

from pulsecheck import charts, checking, db, metrics, probe, rollups, seed, worker
from pulsecheck.models import Check, Incident, Monitor, Rollup

from .conftest import make_monitor, words

NINE = datetime(2026, 11, 18, 9, 0)


def add_check(s, monitor, moment, ms=None, ok=True, our_side=False):
    result = probe.Result(ok=ok, total_ms=ms, status_code=200 if ok else 503,
                          error_kind=None if ok else "http_status", message="test")
    check = checking.record(s, monitor, result, moment, moment)
    check.our_side = our_side
    return check


# ---------- Percentiles ----------

def test_a_percentile_is_read_off_the_histogram():
    buckets = [0] * 21
    buckets[5] = 100  # 100 good checks between 75 and 100 ms
    assert metrics.percentile_of(buckets, 50) == 87.5
    assert metrics.percentile_of(buckets, 100) == 100
    assert metrics.percentile_of([0] * 21, 50) is None


def test_exact_percentiles_from_raw_checks():
    stats = metrics.Stats(values=[float(n) for n in range(1, 101)])
    assert (stats.percentile(50), stats.percentile(95), stats.percentile(99)) == (50.0, 95.0, 99.0)


def test_adding_histograms_gives_the_percentile_of_both():
    """The average of two p95s is wrong; the p95 of two added histograms is right."""
    fast, slow = metrics.Stats(), metrics.Stats()
    for _ in range(90):
        fast.buckets[metrics.bucket_index(40)] += 1
    for _ in range(10):
        slow.buckets[metrics.bucket_index(1800)] += 1
    fast.ok, slow.ok = 90, 10
    both = metrics.Stats().add(fast).add(slow)
    assert both.percentile(50) < 50 and both.percentile(95) > 1500


def test_uptime_leaves_out_our_side():
    stats = metrics.Stats(checks=100, ok=97, our_side=2)
    assert stats.counted == 98 and round(stats.uptime, 4) == round(97 / 98, 4)


# ---------- Rollups ----------

def test_an_hour_is_rolled_up_and_running_it_twice_changes_nothing(app, owner):
    _, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id)
    with app.app_context():
        s = db.session()
        monitor = s.get(Monitor, monitor_id)
        for n in range(60):
            add_check(s, monitor, NINE + timedelta(minutes=n), ms=50 + n, ok=n not in (10, 11),
                      our_side=n == 11)
        s.commit()
        for _ in range(2):
            rollups.recompute_hour(s, NINE)
            s.commit()
        [row] = s.scalars(select(Rollup).where(Rollup.period == "hour")).all()
        assert (row.checks, row.ok, row.our_side) == (60, 58, 1)
        assert row.min_ms == 50 and row.max_ms == 109 and sum(row.histogram()) == 58
        # A late re-check for that hour is picked up by the next run.
        add_check(s, monitor, NINE + timedelta(minutes=10, seconds=20), ms=60)
        s.commit()
        rollups.recompute_hour(s, NINE)
        s.commit()
        [row] = s.scalars(select(Rollup).where(Rollup.period == "hour")).all()
        assert (row.checks, row.ok) == (61, 59)


def test_long_windows_agree_with_the_raw_checks(app, owner, site):
    _, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id, url=f"{site}/ok", next_check_at=NINE)
    end = NINE + timedelta(hours=5, minutes=30)
    with app.app_context():
        s = db.session()
        worker.simulate(s, NINE, end)
        rollups.run(s, end)
        done = rollups.done_until(s, end)
        raw = metrics.raw_stats(s, [monitor_id], NINE, end)[monitor_id]
        long = metrics.window(s, [monitor_id], end - timedelta(days=7), end, done, "Asia/Manila")[monitor_id]
        assert done == datetime(2026, 11, 18, 14, 0)
        assert (long.checks, long.ok) == (raw.checks, raw.ok) and raw.checks > 300
        assert not long.exact and raw.exact


def test_the_crew_rolls_up_every_five_minutes(app, owner, site):
    _, workspace_id = owner
    make_monitor(app, workspace_id, url=f"{site}/ok", next_check_at=NINE)
    with app.app_context():
        s = db.session()
        worker.simulate(s, NINE, NINE + timedelta(hours=2, minutes=6))
        assert s.scalar(select(func.count()).select_from(Rollup).where(Rollup.period == "hour")) == 2
        assert s.scalar(select(func.count()).select_from(Rollup).where(Rollup.period == "day")) >= 1
        assert rollups.rolled_until(s) == NINE + timedelta(hours=2)


def test_a_day_is_the_workspaces_own_day():
    # Manila is UTC+8: its day starts at 16:00 UTC the evening before.
    assert metrics.local_midnight(NINE, "Asia/Manila") == datetime(2026, 11, 17, 16, 0)
    # New York changes its clocks on Mar 8, 2026: that day has 23 hours.
    days = metrics.day_starts("America/New_York", datetime(2026, 3, 7, 12), datetime(2026, 3, 9, 12))
    lengths = [(later - earlier).total_seconds() / 3600 for earlier, later in zip(days, days[1:])]
    assert lengths == [24.0, 23.0]


def test_old_checks_and_rollups_are_tidied(app, owner):
    _, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id)
    with app.app_context():
        s = db.session()
        monitor = s.get(Monitor, monitor_id)
        add_check(s, monitor, NINE - timedelta(days=20), ms=50)
        add_check(s, monitor, NINE - timedelta(days=2), ms=50)
        s.commit()
        rollups.tidy(s, NINE)
        s.commit()
        assert s.scalar(select(func.count()).select_from(Check)) == 1


# ---------- Charts ----------

def test_the_strip_is_shorter_on_worse_days():
    days = [metrics.Day(NINE, metrics.Stats(checks=1000, ok=ok), metrics.level_of(metrics.Stats(checks=1000, ok=ok)))
            for ok in (1000, 995, 900)] + [metrics.Day(NINE, metrics.Stats(), "none")]
    bars = charts.strip(days)["bars"]
    assert [bar["day"].level for bar in bars] == ["up", "some", "down", "none"]
    heights = [bar["height"] for bar in bars]
    assert heights[0] > heights[1] > heights[2] > heights[3]


def test_a_chart_with_no_good_checks_draws_nothing():
    assert charts.response_chart([metrics.Point(NINE, NINE, None, None, metrics.Stats())], "24h") is None
    assert charts.nice_top(730) == 1000 and charts.nice_top(180) == 200


def test_a_failed_check_drops_the_sparkline():
    spark = charts.sparkline([100.0, 120.0, None])
    assert spark["failed"] and spark["end_y"] == charts.SPARK_HEIGHT - 2


# ---------- Pages ----------

def test_the_monitor_page_shows_its_numbers(app, owner, site):
    from pulsecheck.dates import utcnow
    client, workspace_id = owner
    now = utcnow()
    monitor_id = make_monitor(app, workspace_id, url=f"{site}/ok", next_check_at=now - timedelta(hours=2))
    with app.app_context():
        worker.simulate(db.session(), now - timedelta(hours=2), now)
    for period in ("24h", "7d", "30d", "90d"):
        page = words(client.get(f"/monitors/{monitor_id}?period={period}"))
        assert "Numbers" in page and "Uptime" in page and "Slowest 5%" in page and "Last 90 Days" in page, period
        assert "View data table" in page and "100%" in page, period
    page = client.get(f"/monitors/{monitor_id}").get_data(as_text=True)
    assert 'class="c-line"' in page and 'class="s-up"' in page and "Where the Time Goes" in page
    assert 'class="spark' in client.get("/dashboard").get_data(as_text=True)
    assert "Uptime, 24 Hours" in words(client.get("/dashboard"))


# ---------- The demo's older past ----------

def test_the_demo_has_ninety_days_of_history(app):
    from pulsecheck.dates import utcnow
    now = utcnow()
    with app.app_context():
        s = db.session()
        seed.build(s, now)
        s.commit()
        worker.run_until_idle(s)
        payments = s.scalar(select(Monitor).where(Monitor.name == "Payments API"))
        days = s.scalar(select(func.count()).select_from(Rollup).where(Rollup.monitor_id == payments.id,
                                                                      Rollup.period == "day"))
        assert 88 <= days <= 91
        past = s.scalars(select(Incident).where(Incident.monitor_id == payments.id,
                                                Incident.opened_at < now - seed.REPLAY)).all()
        assert 25 <= len(past) <= 32 and all(not incident.is_open for incident in past)
        strip = metrics.strip(s, [payments.id], now, rollups.done_until(s, now), "Asia/Manila")[payments.id]
        # A 7-minute outage in a day of 1,440 checks is 99.5%: "some downtime".
        assert 25 <= sum(day.level == "some" for day in strip) <= 32
        assert sum(day.level == "up" for day in strip) > 50
        assert strip[-1].level != "none"  # today comes from the replayed checks


def test_hours_without_raw_checks_come_from_their_rollups(app, owner):
    """Raw checks are deleted after 14 days, and a demo's older past has none: a short window
    falls back to the hourly rollups before the first raw check, instead of "no data"."""
    _, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id)
    with app.app_context():
        s = db.session()
        s.add(Rollup(monitor_id=monitor_id, period="hour", start=NINE, checks=60, ok=59, slow=0, our_side=0,
                     missed=0, sum_ms=5900.0, **{f"b{n}": (59 if n == 5 else 0) for n in range(21)}))
        monitor = s.get(Monitor, monitor_id)
        for n in range(30):
            add_check(s, monitor, NINE + timedelta(hours=1, minutes=n), ms=80)
        s.commit()
        stats = metrics.window(s, [monitor_id], NINE - timedelta(hours=5), NINE + timedelta(hours=2), NINE,
                               "Asia/Manila")[monitor_id]
        assert (stats.checks, stats.ok) == (90, 89) and not stats.exact


def test_rolling_up_an_hour_keeps_rows_that_have_no_checks(app, owner):
    """A demo's older past is rollups without checks: recomputing that hour must not erase it."""
    _, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id)
    with app.app_context():
        s = db.session()
        s.add(Rollup(monitor_id=monitor_id, period="hour", start=NINE, checks=60, ok=60, slow=0, our_side=0,
                     missed=0, sum_ms=6000.0, **{f"b{n}": (60 if n == 5 else 0) for n in range(21)}))
        s.commit()
        rollups.recompute_hour(s, NINE)
        s.commit()
        assert s.scalar(select(Rollup.checks).where(Rollup.monitor_id == monitor_id)) == 60
