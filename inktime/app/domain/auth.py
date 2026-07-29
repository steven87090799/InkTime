from __future__ import annotations

import re
import unicodedata

from inktime.app.core.errors import ApplicationError


USERNAME_MIN_LENGTH = 3
USERNAME_MAX_LENGTH = 64
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128
_USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")


class AuthValidationError(ApplicationError):
    code = "invalid_auth_input"
    public_message = "帳號或密碼格式不正確。"
    http_status = 400


class SetupAlreadyCompleted(ApplicationError):
    code = "setup_already_completed"
    public_message = "初始管理員已建立，請改用登入頁面。"
    http_status = 409


class LastAdministratorRequired(ApplicationError):
    code = "last_administrator_required"
    public_message = "系統至少必須保留一位啟用中的管理員。"
    http_status = 409


def normalize_username(value: str) -> str:
    return unicodedata.normalize("NFKC", value.strip()).casefold()


def validate_username(value: object) -> tuple[str, str]:
    if not isinstance(value, str):
        raise AuthValidationError("帳號必須是文字。", code="username_invalid_type")
    display = unicodedata.normalize("NFKC", value.strip())
    if not display:
        raise AuthValidationError("帳號不可空白。", code="username_blank")
    if len(display) < USERNAME_MIN_LENGTH:
        raise AuthValidationError(
            f"帳號至少需要 {USERNAME_MIN_LENGTH} 個字元。",
            code="username_too_short",
        )
    if len(display) > USERNAME_MAX_LENGTH:
        raise AuthValidationError(
            f"帳號最多只能有 {USERNAME_MAX_LENGTH} 個字元。",
            code="username_too_long",
        )
    if any(unicodedata.category(character).startswith("C") for character in display):
        raise AuthValidationError("帳號不可包含控制字元。", code="username_control_character")
    if _USERNAME.fullmatch(display) is None:
        raise AuthValidationError(
            "新帳號只可使用英文字母、數字、句點、底線與連字號，且須以字母或數字開頭。",
            code="username_invalid_characters",
        )
    return display, normalize_username(display)


def validate_password(value: object) -> str:
    if not isinstance(value, str):
        raise AuthValidationError("密碼必須是文字。", code="password_invalid_type")
    if "\x00" in value:
        raise AuthValidationError("密碼不可包含 NUL 字元。", code="password_contains_nul")
    if len(value) < PASSWORD_MIN_LENGTH:
        raise AuthValidationError(
            f"密碼至少需要 {PASSWORD_MIN_LENGTH} 個字元。",
            code="password_too_short",
        )
    if len(value) > PASSWORD_MAX_LENGTH:
        raise AuthValidationError(
            f"密碼最多只能有 {PASSWORD_MAX_LENGTH} 個字元。",
            code="password_too_long",
        )
    return value


def validate_role(value: object) -> str:
    if not isinstance(value, str) or value not in {"administrator", "viewer"}:
        raise AuthValidationError("不支援的角色。", code="role_invalid")
    return value
