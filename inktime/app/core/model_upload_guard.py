"""Recheck mutable upload consent immediately before handing a request to transport."""
from pathlib import Path
from inktime.app.db import Database


class UploadNotAllowedError(ValueError):
    code = "CONFIG_INVALID"
    not_sent = True


def assert_upload_allowed(database_path, photo_id, job_id=None):
    database = Database(Path(database_path))
    with database.session() as connection:
        photo = connection.execute("SELECT never_upload,lifecycle_status FROM photos WHERE id=?", (photo_id,)).fetchone()
        if photo is None or photo["never_upload"] or photo["lifecycle_status"] != "active":
            raise UploadNotAllowedError("照片目前禁止上傳或已移除；請求未送出")
        if job_id:
            job = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None or job["status"] in {"cancelled", "paused", "pausing", "failed", "budget_exceeded"}:
                raise UploadNotAllowedError("工作目前已停止或取消；請求未送出")
