"""The Insights page: uptime goals and error budgets, this period against the one before, and a CSV."""
from flask import Blueprint, Response, g, render_template, request

from .. import db, insights
from ..auth import login_required
from ..dates import utcnow

bp = Blueprint("insights", __name__)


def chosen_period():
    period = request.args.get("period", "30d")
    return period if period in insights.PERIODS else "30d"


@bp.get("/insights")
@login_required
def index():
    s = db.session()
    period = chosen_period()
    found = insights.rows(s, g.workspace, period, utcnow())
    now, before = insights.totals(found)
    return render_template("insights.html", rows=found, period=period, periods=insights.PERIODS, total_now=now,
                           total_before=before, change=insights.change(now, before),
                           incidents=sum(row.incidents for row in found),
                           incidents_before=sum(row.incidents_before for row in found),
                           down_s=sum(row.down_s for row in found), budget_days=insights.BUDGET_DAYS)


@bp.get("/insights.csv")
@login_required
def export():
    period = chosen_period()
    found = insights.rows(db.session(), g.workspace, period, utcnow())
    return Response(insights.as_csv(found, period), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="pulse-check-{period}.csv"'})


def init_app(app):
    app.jinja_env.globals.update(goal_words=insights.goal_words, uptime_change=insights.change)
