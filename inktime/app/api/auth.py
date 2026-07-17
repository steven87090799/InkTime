from __future__ import annotations

from flask import Blueprint, current_app, flash, g, redirect, render_template, request, session, url_for

from inktime.app.repositories.auth import AuthRepository
from inktime.app.web.access import administrator_required, login_required


bp = Blueprint("auth", __name__)


def _repository() -> AuthRepository:
    return current_app.extensions["inktime_auth_repository"]


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    repository = _repository()
    if repository.count_users() > 0:
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirmation = request.form.get("password_confirmation", "")
        if not username:
            flash("請輸入管理員帳號。", "error")
        elif password != confirmation:
            flash("兩次輸入的密碼不同。", "error")
        else:
            try:
                user_id = repository.create_user(username, password)
            except Exception as exc:
                flash(str(exc), "error")
            else:
                session.clear()
                session["user_id"] = user_id
                session.permanent = True
                flash("管理員建立完成。", "success")
                return redirect(url_for("dashboard.dashboard"))
    return render_template("setup.html")


@bp.route("/login", methods=["GET", "POST"])
def login():
    repository = _repository()
    if repository.count_users() == 0:
        return redirect(url_for("auth.setup"))
    if request.method == "POST":
        ip_address = request.remote_addr or "unknown"
        username = request.form.get("username", "")
        if repository.ip_blocked(ip_address):
            flash("登入失敗次數過多，請 15 分鐘後再試。", "error")
            return render_template("login.html"), 429
        user = repository.authenticate(username, request.form.get("password", ""))
        repository.record_login(username, ip_address, user is not None, user["id"] if user else None)
        if user is None:
            flash("帳號或密碼錯誤。", "error")
        else:
            session.clear()
            session["user_id"] = user["id"]
            session.permanent = True
            next_path = request.args.get("next", "")
            if not next_path.startswith("/") or next_path.startswith("//"):
                next_path = url_for("dashboard.dashboard")
            return redirect(next_path)
    return render_template("login.html")


@bp.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        new_password = request.form.get("new_password", "")
        if new_password != request.form.get("confirmation", ""):
            flash("兩次輸入的新密碼不同。", "error")
        else:
            try:
                _repository().change_password(
                    g.user["id"], request.form.get("current_password", ""), new_password
                )
            except ValueError as exc:
                flash(str(exc), "error")
            else:
                session.clear()
                flash("密碼已變更，請重新登入。", "success")
                return redirect(url_for("auth.login"))
    return render_template("change_password.html")


@bp.post("/api/v1/users")
@administrator_required
def create_user():
    payload = request.get_json(silent=True) or {}
    user_id = _repository().create_user(
        str(payload.get("username", "")),
        str(payload.get("password", "")),
        str(payload.get("role", "viewer")),
    )
    return {"id": user_id}, 201
