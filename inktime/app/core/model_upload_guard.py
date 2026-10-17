"""Recheck mutable upload consent immediately before handing a request to transport."""
from pathlib import Path
from inktime.app.db import Database
from inktime.app.domain.photos.quality_policy import is_confirmed_screenshot


class UploadNotAllowedError(ValueError):
    code = "CONFIG_INVALID"
    not_sent = True


def assert_upload_allowed(database_path, photo_id, job_id=None, operation_id=None, expected_content_sha256=None):
    database = Database(Path(database_path))
    with database.session() as connection:
        photo = connection.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
        if (
            photo is None
            or photo["never_upload"]
            or photo["lifecycle_status"] != "active"
            or not photo["eligible"]
            or photo["exclusion_status"] in {"auto_excluded", "manually_excluded"}
            or is_confirmed_screenshot(dict(photo))
            or (expected_content_sha256 is not None and photo["sha256"] != expected_content_sha256)
        ):
            raise UploadNotAllowedError("照片目前已排除、禁止上傳或已移除；請先檢查並明確恢復，請求未送出")
        if job_id:
            job = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None or job["status"] in {"cancelled", "paused", "pausing", "failed", "budget_exceeded"}:
                raise UploadNotAllowedError("工作目前已停止或取消；請求未送出")
