"""Shared thumbnail/preview HTTP boundary, including the Review workbench."""

from pathlib import Path

from flask import current_app, send_file

from inktime.app.core.paths import safe_join, UnsafePathError
from inktime.app.domain.photos.formats import DERIVATIVE_MEDIA_TYPE, ImageSourceError, log_image_error


def photo_derivative_response(photo_id: str, size: int, *, invalid_path_status: int = 404):
    stage = "preview" if size == 1600 else "thumbnail"
    photo = current_app.extensions["inktime_photo_repository"].get_with_path(photo_id)
    suffix = ""
    try:
        if photo is None or not str(photo["sha256"] or ""):
            raise ImageSourceError("IMG-MISSING", "找不到照片", 404)
        suffix = Path(photo["relative_path"]).suffix.lower()
        path = safe_join(Path(photo["root_path"]), photo["relative_path"])
        cache = current_app.extensions["inktime_thumbnail_cache"]
        if not path.is_file():
            with cache.acquire_existing(str(photo["sha256"]), size) as derivative:
                if derivative is not None:
                    response = send_file(derivative, mimetype=DERIVATIVE_MEDIA_TYPE, conditional=True, max_age=0)
                    response.headers["X-InkTime-Photo-Source"] = "retained-preview"
                    return response
            raise ImageSourceError("IMG-MISSING", "找不到來源原檔，也沒有可用的保留縮圖；請確認來源掛載或還原原檔", 404)
        # send_file opens the file while the shard is held, so cleanup cannot
        # remove it between generation and open. Streaming owns that open fd.
        with cache.acquire_for_use(path, str(photo["sha256"]), size) as derivative:
            return send_file(derivative, mimetype=DERIVATIVE_MEDIA_TYPE, conditional=True, max_age=300)
    except UnsafePathError:
        error = ImageSourceError("IMG-IO", "照片路徑不可用", invalid_path_status)
    except ImageSourceError as exc:
        error = exc
    except PermissionError:
        error = ImageSourceError("IMG-IO", "無法讀取照片或快取", 403)
    except FileNotFoundError:
        error = ImageSourceError("IMG-MISSING", "原始照片不存在", 404)
    except (OSError, ValueError):
        error = ImageSourceError("THUMB-001", "縮圖無法產生", 503)
    log_image_error(error, photo_id=photo_id, stage=stage, suffix=suffix)
    return {"error_code": error.code, "message": "縮圖無法產生：" + str(error), "stage": stage}, error.status
