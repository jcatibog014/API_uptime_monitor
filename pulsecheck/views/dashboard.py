"""The Dashboard: every monitor at a glance, like a ward's wall of heart monitors."""
from flask import Blueprint, g, render_template

from .. import checking, db, monitors
from ..auth import login_required

bp = Blueprint("dashboard", __name__)
# Parts of the page that later stages fill: function(s, workspace) -> dict.
panels = []


@bp.get("/dashboard")
@login_required
def home():
    s = db.session()
    rows = [(monitor, checking.last(s, monitor.id)) for monitor in monitors.all_of(s, g.workspace.id)]
    checked = [last for _monitor, last in rows if last is not None]
    data = {"rows": rows, "up": sum(1 for last in checked if last.ok),
            "down": sum(1 for last in checked if not last.ok)}
    for panel in panels:
        data.update(panel(s, g.workspace))
    return render_template("dashboard.html", **data)
