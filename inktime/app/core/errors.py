from __future__ import annotations


class ApplicationError(Exception):
    """Safe, stable error contract shared by HTML and JSON entrypoints."""

    code = "APPLICATION-ERROR"
    public_message = "操作無法完成。"
    http_status = 400

    def __init__(
        self,
        public_message: str | None = None,
        *,
        code: str | None = None,
        http_status: int | None = None,
    ) -> None:
        if public_message is not None:
            self.public_message = public_message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        super().__init__(self.public_message)

    def response_body(self) -> dict[str, object]:
        return {
            "error": {
                "code": self.code,
                "message": self.public_message,
            }
        }
