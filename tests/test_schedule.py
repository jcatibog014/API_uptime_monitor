"""Scheduled jobs: the timetable (no drift, spread, gaps), the queue (claims, retries, dead
letters, leases), the crew, the timer, and the Crew page."""
import json
import random
import threading
from datetime import datetime, timedelta

from sqlalchemy import func, select

from pulsecheck import db, queue, scheduler, worker
from pulsecheck.dates import utcnow
from pulsecheck.models import Check, Gap, Job, Monitor, Worker

from .conftest import make_monitor, post, text, words

NINE = datetime(2026, 11, 18, 9, 0)


def rows(app, model, *where):
    with app.app_context():
        return db.session().scalars(select(model).where(*where)).all()


def set_next(app, monitor_id, moment):
    with app.app_context():
        s = db.session()
        s.get(Monitor, monitor_id).next_check_at = moment
        s.commit()


# ---------- The timetable ----------

def test_monitors_are_spread_over_the_minute():
    offsets = {scheduler.offset_of(monitor_id, 60) for monitor_id in range(1, 26)}
    assert len(offsets) >= 20  # 25 monitors, nearly all at a different second


def test_slots_follow_the_timetable():
    slot = scheduler.slot_after(1, 60, NINE)
    assert slot > NINE and (slot - NINE).total_seconds() <= 60
    assert scheduler.slot_after(1, 60, slot) == slot + timedelta(seconds=60)


def test_an_hour_of_checks_never_drifts(app, owner, site):
    _, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id, url=f"{site}/ok")
    set_next(app, monitor_id, NINE)
    with app.app_context():
        worker.simulate(db.session(), NINE, NINE + timedelta(hours=1))
    checks = sorted(rows(app, Check, Check.monitor_id == monitor_id), key=lambda check: check.scheduled_at)
    assert 60 <= len(checks) <= 61
    gaps = {(later.scheduled_at - earlier.scheduled_at).total_seconds() for earlier, later in zip(checks[1:], checks[2:])}
    assert gaps == {60.0}  # every later check exactly a minute after the one before
    offset = scheduler.offset_of(monitor_id, 60)
    assert all((check.scheduled_at - scheduler.EPOCH).total_seconds() % 60 == offset for check in checks[1:])
    assert all(check.ok and not check.manual for check in checks)


def test_a_sleeping_site_leaves_a_gap_not_downtime(app, owner, site):
    _, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id, url=f"{site}/ok")
    set_next(app, monitor_id, NINE)
    later = NINE + timedelta(minutes=10, seconds=20)  # the site woke up 10 minutes late
    with app.app_context():
        s = db.session()
        assert scheduler.run_due(s, later) == 1
        worker.run_until_idle(s, now_fn=lambda: later)
    [gap] = rows(app, Gap, Gap.monitor_id == monitor_id)
    assert (gap.start, gap.end, gap.slots) == (NINE, NINE + timedelta(minutes=10), 10)
    [check] = rows(app, Check, Check.monitor_id == monitor_id)
    assert check.scheduled_at == NINE + timedelta(minutes=10)  # only the latest slot, no burst of ten
    [monitor] = rows(app, Monitor, Monitor.id == monitor_id)
    assert monitor.next_check_at > later


def test_two_schedulers_start_a_slot_once(app, owner):
    _, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id)
    set_next(app, monitor_id, NINE)
    with app.app_context():
        first, second = db.new_session(), db.new_session()
        try:
            # Both read the monitor as due before either claims it.
            assert scheduler.run_due(first, NINE) + scheduler.run_due(second, NINE) == 1
        finally:
            first.close()
            second.close()
    assert len(rows(app, Job, Job.kind == "check")) == 1


def test_a_late_check_shows_its_lag_but_keeps_the_timetable(app, owner, site):
    _, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id, url=f"{site}/ok")
    set_next(app, monitor_id, NINE)
    late = NINE + timedelta(seconds=25)
    with app.app_context():
        worker.run_until_idle(db.session(), now_fn=lambda: late)
    [check] = rows(app, Check, Check.monitor_id == monitor_id)
    assert (check.started_at - check.scheduled_at).total_seconds() == 25
    [monitor] = rows(app, Monitor, Monitor.id == monitor_id)
    assert monitor.next_check_at == scheduler.slot_after(monitor_id, 60, NINE)


def test_a_paused_monitor_is_not_checked(app, owner, site):
    client, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id, url=f"{site}/ok")
    post(client, f"/monitors/{monitor_id}/pause")
    [monitor] = rows(app, Monitor, Monitor.id == monitor_id)
    assert monitor.paused and monitor.next_check_at is None
    with app.app_context():
        worker.simulate(db.session(), NINE, NINE + timedelta(minutes=10))
    assert rows(app, Check, Check.monitor_id == monitor_id) == []
    response = post(client, f"/monitors/{monitor_id}/pause", follow_redirects=True)
    assert "is checked again, starting now" in words(response)
    [monitor] = rows(app, Monitor, Monitor.id == monitor_id)
    assert not monitor.paused and monitor.next_check_at is not None


def test_a_new_monitor_is_checked_straight_away(app, owner):
    client, _ = owner
    post(client, "/monitors/new", {"name": "Website", "url": "https://example.com/", "method": "GET",
                                   "expected_statuses": "200-299", "timeout_s": "10", "interval_s": "300"})
    [monitor] = rows(app, Monitor)
    assert abs((monitor.next_check_at - utcnow()).total_seconds()) < 5


def test_a_queued_check_for_a_paused_monitor_does_nothing(app, owner, site):
    _, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id, url=f"{site}/ok")
    set_next(app, monitor_id, NINE)
    with app.app_context():
        s = db.session()
        scheduler.run_due(s, NINE)
        s.get(Monitor, monitor_id).paused = True
        s.commit()
        worker.run_until_idle(s, now_fn=lambda: NINE)
    assert rows(app, Check, Check.monitor_id == monitor_id) == []
    assert [job.status for job in rows(app, Job, Job.kind == "check")] == ["done"]


# ---------- The queue ----------

TRIES = []


@queue.handler("test_flaky")
def flaky(s, job, now):
    TRIES.append(now)
    if job.payload.get("fail_until", 0) >= job.attempts:
        raise queue.Retry("The service said no.", after=job.payload.get("after"))


def test_the_same_work_is_queued_once(ctx):
    first = queue.enqueue(ctx, "test_flaky", run_at=NINE, dedupe_key="same")
    second = queue.enqueue(ctx, "test_flaky", run_at=NINE, dedupe_key="same")
    ctx.commit()
    assert first.id == second.id and ctx.scalar(select(func.count()).select_from(Job)) == 1


def test_two_workers_never_claim_the_same_job(app):
    with app.app_context():
        s = db.session()
        for n in range(40):
            queue.enqueue(s, "test_flaky", run_at=NINE, dedupe_key=f"job-{n}")
        s.commit()
        claimed, lock = [], threading.Lock()

        def take(name):
            with app.app_context():
                own = db.new_session()
                try:
                    for _ in range(20):
                        ids = queue.claim(own, name, NINE, limit=3)
                        with lock:
                            claimed.extend(ids)
                finally:
                    own.close()

        threads = [threading.Thread(target=take, args=(f"w{n}",)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert sorted(claimed) == sorted(set(claimed)) and len(claimed) == 40


def test_backoff_has_full_jitter_and_a_cap():
    rng = random.Random(1)
    waits = [queue.backoff(1, rng) for _ in range(200)]
    assert all(1 <= wait <= 30 for wait in waits) and len({round(wait) for wait in waits}) > 10
    assert all(queue.backoff(20, rng) <= queue.BACKOFF_CAP for _ in range(50))


def test_a_failing_job_is_retried_then_becomes_a_dead_letter(app):
    TRIES.clear()
    with app.app_context():
        s = db.session()
        job = queue.enqueue(s, "test_flaky", {"fail_until": 99}, run_at=NINE, max_attempts=3)
        s.commit()
        worker.simulate(s, NINE, NINE + timedelta(hours=2))
        s.expire_all()
        job = s.get(Job, job.id)
        assert job.status == queue.DEAD and job.attempts == 3
        assert "Gave up after 3 tries" in job.last_error and "The service said no." in job.last_error
    assert len(TRIES) == 3 and TRIES[0] < TRIES[1] < TRIES[2]


def test_retry_after_wins_over_the_backoff(app):
    TRIES.clear()
    with app.app_context():
        s = db.session()
        queue.enqueue(s, "test_flaky", {"fail_until": 1, "after": 600}, run_at=NINE)
        s.commit()
        worker.simulate(s, NINE, NINE + timedelta(hours=1))
    assert (TRIES[1] - TRIES[0]).total_seconds() == 600


def test_a_dead_workers_job_is_put_back(app):
    with app.app_context():
        s = db.session()
        job = queue.enqueue(s, "test_flaky", run_at=NINE)
        s.commit()
        queue.claim(s, "gone-worker", NINE)
        assert queue.reap(s, NINE + timedelta(seconds=30)) == 0  # its lease still holds
        assert queue.reap(s, NINE + queue.LEASE + timedelta(seconds=1)) == 1
        s.expire_all()
        job = s.get(Job, job.id)
        assert job.status == queue.QUEUED and "stopped during the job" in job.last_error


# ---------- The crew, the timer and the Crew page ----------

def test_a_round_writes_a_heartbeat(app):
    with app.app_context():
        s = db.session()
        worker.register(s, "w-1", "in the web app", NINE)
        worker.run_round(s, "w-1", NINE + timedelta(seconds=5))
        assert s.get(Worker, "w-1").last_seen == NINE + timedelta(seconds=5)


def test_the_crew_sleeps_until_the_next_check(app, owner):
    _, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id)
    set_next(app, monitor_id, NINE + timedelta(seconds=40))
    with app.app_context():
        assert worker.seconds_to_sleep(db.session(), NINE) == 40


def test_the_timer_needs_its_key(app, client, owner, site):
    app.config["TICK_KEY"] = "tick-secret"
    _, workspace_id = owner
    make_monitor(app, workspace_id, url=f"{site}/ok", next_check_at=datetime(2020, 1, 1))
    assert client.post("/tasks/tick").status_code == 404
    assert client.post("/tasks/tick", headers={"X-Tick-Key": "wrong"}).status_code == 404
    response = client.post("/tasks/tick", headers={"X-Tick-Key": "tick-secret"})
    assert response.status_code == 200
    assert json.loads(response.data)["jobs_run"] >= 1


def test_the_crew_page_shows_workers_lag_and_dead_letters(app, owner, site):
    client, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id, url=f"{site}/ok")
    now = utcnow().replace(microsecond=0)
    set_next(app, monitor_id, now - timedelta(seconds=3))
    with app.app_context():
        s = db.session()
        worker.register(s, "web-1-ab", "in the web app", now)
        worker.run_until_idle(s, worker_id="web-1-ab")
        dead = queue.enqueue(s, "test_flaky", run_at=now, workspace_id=workspace_id)
        queue.die(s, dead, now, "Gave up after 6 tries. Last error: no")
        s.commit()
        dead_id = dead.id
    page = words(client.get("/crew"))
    assert "Web app worker 1" in page and "On Duty" in page
    assert "Typical, last hour" in page
    assert "Gave up after 6 tries" in page
    response = post(client, f"/crew/jobs/{dead_id}/retry", follow_redirects=True)
    assert "back in the queue" in text(response)
    [job] = rows(app, Job, Job.id == dead_id)
    assert job.status == queue.QUEUED and job.attempts == 0


def test_the_crew_page_lists_gaps(app, owner, site):
    client, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id, url=f"{site}/ok")
    set_next(app, monitor_id, NINE)
    with app.app_context():
        scheduler.run_due(db.session(), NINE + timedelta(minutes=30))
    page = words(client.get("/crew"))
    assert "Payments API" in page and "Checks Skipped" in page and "uptime leaves these minutes out" in page


def test_a_worker_is_quiet_then_stopped_and_an_empty_crew_raises_the_alarm(app, owner):
    from pulsecheck.views.crew import worker_state
    from pulsecheck.models import Worker
    now = utcnow()
    def state(minutes_ago):
        return worker_state(Worker(id="w", kind="in the web app", started_at=now,
                                   last_seen=now - timedelta(minutes=minutes_ago)), now)
    # Idle workers write a heartbeat at least every 5 minutes, so 5 minutes is still on duty.
    assert (state(1), state(5), state(7), state(20)) == ("on", "on", "quiet", "off")
    from pulsecheck import pulse_words
    assert pulse_words.WORKER["quiet"].tone == "alarm"  # a solid pill: it needs you
    client, _ = owner
    with app.app_context():
        s = db.session()
        worker.register(s, "web-1-ab", "in the web app", now - timedelta(minutes=20))
        s.commit()
    page = words(client.get("/crew"))
    assert "No worker is on duty" in page and "1 worker from earlier" in page and "Web app worker 1" not in page
    with app.app_context():
        s = db.session()
        worker.register(s, "web-2-cd", "in the web app", now)
        s.commit()
    assert "No worker is on duty" not in words(client.get("/crew"))
