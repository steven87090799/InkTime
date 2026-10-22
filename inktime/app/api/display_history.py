from __future__ import annotations

from flask import Blueprint, abort, current_app, render_template, request

from inktime.app.repositories.display_history import DisplayHistoryRepository
from inktime.app.web.access import login_required


bp = Blueprint("display_history", __name__)


def _repository():
    return DisplayHistoryRepository(current_app.extensions["inktime_database"])


def _listing():
    filters = {
        "target_date": request.args.get("date", "").strip()[:10],
        "photo_id": request.args.get("photo_id", "").strip()[:160],
        "tab": "displays" if request.args.get("tab") == "displays" else "selections",
    }
    try:
        result = _repository().list(**filters, page=request.args.get("page"))
    except ValueError as exc:
        abort(400, description=str(exc))
    return filters, result


@bp.get("/display-history")
@login_required
def history_page():
    filters, result = _listing()
    return render_template("display_history.html", filters=filters, result=result, detail=None)


@bp.get("/display-history/<run_id>")
@login_required
def history_detail(run_id: str):
    try:
        detail = _repository().detail(run_id, page=request.args.get("page"))
    except ValueError as exc:
        abort(400, description=str(exc))
    if detail is None:
        abort(404)
    return render_template("display_history.html", detail=detail)


@bp.get("/api/v1/display-history")
@login_required
def history_api():
    filters, result = _listing()
    return {**result, "filters": filters}


@bp.get("/api/v1/display-history/<run_id>")
@login_required
def history_detail_api(run_id: str):
    try:
        result = _repository().detail(run_id, page=request.args.get("page"))
    except ValueError as exc:
        abort(400, description=str(exc))
    if result is None:
        abort(404)
    return result
