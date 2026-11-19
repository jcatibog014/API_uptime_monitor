"""Insights: uptime goals, error budgets, burn rate, comparing periods, the CSV and the weekly report."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from pulsecheck import db, insights, metrics, worker
from pulsecheck.models import Alert, Channel, Incident, Monitor, Workspace

from .conftest import CSRF, make_monitor, post, text, words

NINE = datetime(2026, 11, 18, 9, 0)


def test_an_error_budget_is_what_the_goal_allows():
    budget = insights.Budget(allowed_s=(1 - 0.999) * 30 * 86400, used_s=7 * 60)
    assert round(budget.allowed_s / 60, 1) == 43.2 and round(budget.left_s / 60, 1) == 36.2
    assert budget.level == "good"
    assert insights.Budget(budget.allowed_s, 30 * 60).level == "warning"
    assert insights.Budget(budget.allowed_s, 50 * 60).level == "critical"
    assert insights.Budget(budget.allowed_s, 50 * 60).left_s == 0


def test_burn_rate_is_how_fast_the_budget_goes():
    assert insights.burn_rate(metrics.Stats(checks=1000, ok=990), 99.9) == pytest.approx(10)
    assert insights.burn_rate(metrics.Stats(checks=1000, ok=1000), 99.9) == 0
    assert insights.burn_rate(metrics.Stats(), 99.9) is None


def test_periods_are_compared_in_points():
    assert insights.change(metrics.Stats(checks=1000, ok=999), metrics.Stats(checks=1000, ok=990)) == 0.9
    assert insights.change(metrics.Stats(checks=10, ok=10), metrics.Stats()) is None


def test_downtime_counts_only_the_time_inside_the_window(app, owner):
    _, workspace_id = owner
    monitor_id = make_monitor(app, workspace_id)
    with app.app_context():
        s = db.session()
        s.add(Incident(workspace_id=workspace_id, monitor_id=monitor_id, opened_at=NINE - timedelta(minutes=10),
                       confirmed_at=NINE, resolved_at=NINE + timedelta(minutes=20), cause="timeout"))
        s.commit()
        assert insights.downtime(s, [monitor_id], NINE, NINE + timedelta(hours=1)) == {monitor_id: (1, 1200.0)}


def test_the_insights_page_and_its_csv(app, owner, site):
    from pulsecheck.dates import utcnow
    client, workspace_id = owner
    now = utcnow()
    make_monitor(app, workspace_id, url=f"{site}/ok", next_check_at=now - timedelta(hours=1))
    with app.app_context():
        worker.simulate(db.session(), now - timedelta(hours=1), now)
    for period in ("7d", "30d", "90d"):
        page = words(client.get(f"/insights?period={period}"))
        assert "Each Monitor" in page and "Payments API" in page and "Goal 99.9%" in page and "left" in page, period
    response = client.get("/insights.csv?period=7d")
    assert response.content_type.startswith("text/csv") and "attachment" in response.headers["Content-Disposition"]
    lines = response.get_data(as_text=True).lstrip("﻿").splitlines()
    assert lines[0].startswith('Monitor,"Uptime, last 7 days",')
    assert lines[1].startswith("Payments API,100%")


def test_the_goal_is_chosen_on_the_monitor(app, owner):
    client, _ = owner
    form = {"kind": "http", "name": "Health", "url": "https://api.example.com/", "method": "GET",
            "expected_statuses": "200", "timeout_s": "10", "interval_s": "60", "slo_target": "99.95"}
    assert post(client, "/monitors/new", form).status_code == 302
    with app.app_context():
        assert db.session().scalar(select(Monitor.slo_target)) == 99.95
    response = post(client, "/monitors/new", {**form, "slo_target": "120"})
    assert response.status_code == 400 and "Choose a goal from the list." in text(response)


# ---------- The weekly report ----------

def test_the_report_is_due_on_monday_at_eight_its_own_time():
    workspace = Workspace(time_zone="Asia/Manila")
    monday_8_manila = datetime(2026, 11, 23, 0, 0)  # 8 AM in Manila is midnight UTC
    assert insights.report_due(workspace, monday_8_manila - timedelta(minutes=1)) == monday_8_manila - timedelta(days=7)
    assert insights.report_due(workspace, monday_8_manila + timedelta(hours=3)) == monday_8_manila
    workspace.last_report_at = monday_8_manila
    assert insights.report_due(workspace, monday_8_manila + timedelta(days=2)) is None


def test_the_report_goes_to_every_channel_once(app, owner):
    client, workspace_id = owner
    make_monitor(app, workspace_id)
    post(client, "/settings", {"action": "report", "weekly_report": "1"})
    with app.app_context():
        s = db.session()
        s.add(Channel(workspace_id=workspace_id, kind="practice", name="Inbox", settings={"looks_like": "email"}))
        workspace = s.get(Workspace, workspace_id)
        assert workspace.weekly_report and insights.report_due(workspace, workspace.last_report_at) is None
        workspace.last_report_at = None
        s.commit()
        monday = datetime(2026, 11, 23, 0, 30)
        worker.simulate(s, monday, monday + timedelta(hours=3))
        titles = [alert.title for alert in s.scalars(select(Alert))]
        assert len(titles) == 1 and titles[0].startswith("Pulse Check weekly report")
    assert "The next one goes out on Monday at 8 AM." in words(client.get("/settings"))


def test_the_demo_has_insights_and_a_report(app):
    client = app.test_client()
    with client.session_transaction() as session:
        session["csrf"] = CSRF
    post(client, "/demo")
    with app.app_context():
        worker.run_until_idle(db.session())
        reports = db.session().scalars(select(Alert).where(Alert.event == "report")).all()
    assert len(reports) == 4  # one per practice channel (one may still be waiting for its retry)
    assert reports[0].title.startswith("Pulse Check weekly report")
    assert "Payments API:" in reports[0].body and "incident" in reports[0].body
    page = words(client.get("/insights"))
    assert "Goal 99.95%" in page and "Goal 99.5%" in page and "Incidents" in page


def test_the_budget_agrees_with_uptime():
    assert insights.spent(metrics.Stats(checks=10_000, ok=9_995)) == pytest.approx(0.0005 * 30 * 86400)
    assert insights.spent(metrics.Stats()) == 0
