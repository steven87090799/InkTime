"""Flask session expiry reads the shared authority for every cookie operation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Flask, Request
from flask.sessions import SecureCookieSessionInterface, SessionMixin
from itsdangerous import BadSignature


class SettingsSessionInterface(SecureCookieSessionInterface):
    @staticmethod
    def _lifetime(app: Flask) -> timedelta:
        repository = app.extensions["inktime_settings_repository"]
        values = repository.get_many(["security.session_minutes"],
                                     defaults={"security.session_minutes": 30}, fresh=True)
        return timedelta(minutes=int(values["security.session_minutes"]))

    def open_session(self, app: Flask, request: Request):
        serializer = self.get_signing_serializer(app)
        if serializer is None:
            return None
        cookie = request.cookies.get(self.get_cookie_name(app))
        if not cookie:
            return self.session_class()
        try:
            data = serializer.loads(cookie, max_age=int(self._lifetime(app).total_seconds()))
            return self.session_class(data)
        except BadSignature:
            return self.session_class()

    def get_expiration_time(self, app: Flask, session: SessionMixin) -> datetime | None:
        return datetime.now(timezone.utc) + self._lifetime(app) if session.permanent else None
