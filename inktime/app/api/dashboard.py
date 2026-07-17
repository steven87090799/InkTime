from __future__ import annotations

from flask import Blueprint, current_app, render_template

from inktime.app.web.access import login_required


bp = Blueprint("dashboard", __name__)


@bp.get("/dashboard")
@login_required
def dashboard():
    database = current_app.extensions["inktime_database"]
    with database.session() as connection:
        counts = {
            "photos": connection.execute("SELECT COUNT(*) FROM photos").fetchone()[0],
            "analyzed": connection.execute(
                "SELECT COUNT(*) FROM photos WHERE status='analyzed'"
            ).fetchone()[0],
            "failed": connection.execute(
                "SELECT COUNT(*) FROM photos WHERE status='failed'"
            ).fetchone()[0],
            "duplicates": connection.execute(
                "SELECT COUNT(*) FROM photos WHERE duplicate_group_id IS NOT NULL"
            ).fetchone()[0],
            "running_jobs": connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('preparing','running','pausing','retrying')"
            ).fetchone()[0],
            "today_tokens": connection.execute(
                "SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM api_usage WHERE date(started_at)=date('now')"
            ).fetchone()[0],
            "month_cost": connection.execute(
                "SELECT COALESCE(SUM(COALESCE(actual_cost, estimated_cost)),0) FROM api_usage WHERE strftime('%Y-%m',started_at)=strftime('%Y-%m','now')"
            ).fetchone()[0],
        }
        recent_errors = connection.execute(
            "SELECT error_code, message, last_seen_at, occurrences FROM job_errors WHERE resolved_at IS NULL ORDER BY last_seen_at DESC LIMIT 5"
        ).fetchall()
    return render_template("dashboard.html", counts=counts, recent_errors=recent_errors)
