from __future__ import annotations

from math import ceil
from pathlib import Path

from flask import Blueprint, Flask, abort, current_app, jsonify, render_template, request, send_file

from inktime.app.legacy.repository import LegacyPhotoRepositoryAdapter
from inktime.app.web.access import administrator_required
from inktime.app.web.templating import assert_no_asset_collisions


bp = Blueprint(
    "legacy",
    __name__,
    url_prefix="/legacy",
    template_folder="templates",
    static_folder="static",
    static_url_path="/static",
)


def _adapter() -> LegacyPhotoRepositoryAdapter:
    return current_app.extensions["inktime_legacy_photo_adapter"]


@bp.get("/review")
@administrator_required
def review():
    try:
        page_number = int(request.args.get("page", "1"))
    except ValueError:
        page_number = 1
    page = _adapter().page(
        page=page_number,
        month_day=str(request.args.get("md", "")),
        sort=str(request.args.get("sort", "memory")),
    )
    return render_template(
        "legacy/review.html",
        page=page,
        total_pages=max(1, ceil(page.total / page.page_size)),
    )


@bp.get("/api/md-list")
@administrator_required
def month_days():
    return jsonify({"md_list": _adapter().month_days()})


@bp.get("/sim")
@administrator_required
def simulator():
    photo_id = str(request.args.get("photo_id", ""))
    return render_template("legacy/simulator.html", photo_id=photo_id)


@bp.get("/sim-render")
@administrator_required
def simulator_render():
    path = _adapter().photo_path(str(request.args.get("photo_id", "")))
    if path is None or not path.is_file():
        abort(404)
    return send_file(path)


def register_legacy(app: Flask) -> None:
    app_root = Path(__file__).resolve().parents[1]
    legacy_root = Path(__file__).resolve().parent
    assert_no_asset_collisions(app_root / "web" / "templates", legacy_root / "templates", kind="Template")
    assert_no_asset_collisions(app_root / "web" / "static", legacy_root / "static", kind="Static")
    app.extensions["inktime_legacy_photo_adapter"] = LegacyPhotoRepositoryAdapter(
        app.extensions["inktime_photo_repository"]
    )
    app.register_blueprint(bp)
