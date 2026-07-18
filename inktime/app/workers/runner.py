from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
from pathlib import Path

from inktime.app.core.logging import log_event
from inktime.app.workers.job_worker import BoundedJobWorker
from inktime.app.workers.scanner import PhotoScanner
from inktime.app.domain.photos import PhotoPreprocessor


LOGGER = logging.getLogger("worker")


class WorkerRunner:
    def __init__(self, app) -> None:
        self.app = app
        self.stop = threading.Event()
        self.current: BoundedJobWorker | None = None

    def request_stop(self, *_args) -> None:
        self.stop.set()
        if self.current:
            self.current.request_stop()

    def run_once(self) -> int:
        repository = self.app.extensions["inktime_job_repository"]
        recovered = repository.recover_stale()
        processed_jobs = 0
        for job in repository.list(limit=100):
            if self.stop.is_set() or job["status"] not in {"running", "retrying"}:
                continue
            settings = json.loads(job["settings_json"])
            scoring_profile = self.app.extensions["inktime_scoring_repository"].current()
            provider = self.app.extensions["inktime_provider_service"].build_router()
            analysis = self.app.extensions["inktime_analysis_service"]

            def processor(
                item,
                *,
                job=job,
                settings=settings,
                provider=provider,
                analysis=analysis,
                scoring_profile=scoring_profile,
            ):
                if job["kind"] == "scan":
                    scanner = PhotoScanner(
                        self.app.extensions["inktime_photo_repository"],
                        PhotoPreprocessor(),
                        self.app.extensions["inktime_thumbnail_cache"],
                    )
                    return scanner.scan(
                        settings.get("library_name", "主要照片庫"),
                        Path(settings["root_path"]),
                        build_thumbnails=bool(settings.get("build_thumbnails", True)),
                    )
                if job["kind"] == "render":
                    return self.app.extensions["inktime_render_service"].publish(
                        [str(value) for value in settings.get("photo_ids", [])],
                        str(job["created_by"] or "system"),
                    )
                if job["kind"] == "backup":
                    path = self.app.extensions["inktime_backup_service"].create()
                    return {"backup": path.name}
                return analysis.analyze_photo(
                    photo_id=item["photo_id"],
                    job_id=job["id"],
                    provider=provider,
                    strategy=job["strategy"],
                    low_model=settings.get(
                        "low_model", self.app.extensions["inktime_settings_repository"].get("model.low_model")
                    ),
                    high_model=settings.get(
                        "high_model",
                        self.app.extensions["inktime_settings_repository"].get("model.high_model"),
                    ),
                    stage_two_threshold=float(
                        settings.get(
                            "stage_two_threshold",
                            self.app.extensions["inktime_settings_repository"].get(
                                "analysis.stage_two_threshold"
                            ),
                        )
                    ),
                    ranking_weights={
                        "memory": float(scoring_profile["memory_weight"]),
                        "beauty": float(scoring_profile["beauty_weight"]),
                        "technical_quality": float(scoring_profile["technical_weight"]),
                        "emotion": float(scoring_profile["emotion_weight"]),
                    },
                    favorite_bonus=float(scoring_profile["favorite_bonus"]),
                    scoring_version_id=str(scoring_profile["id"]),
                )

            self.current = BoundedJobWorker(
                repository,
                processor,
                concurrency=int(
                    settings.get(
                        "concurrency",
                        self.app.extensions["inktime_settings_repository"].get("analysis.concurrency"),
                    )
                ),
                queue_multiplier=2,
                max_attempts=int(
                    settings.get(
                        "max_retries",
                        self.app.extensions["inktime_settings_repository"].get("analysis.max_retries"),
                    )
                ),
            )
            log_event(
                LOGGER,
                logging.INFO,
                "開始處理工作",
                event="job_started",
                job_id=job["id"],
                details={"recovered_items": recovered},
            )
            self.current.run_job(job["id"])
            self.current = None
            processed_jobs += 1
        return processed_jobs

    def run_forever(self, poll_seconds: float = 2.0) -> None:
        while not self.stop.is_set():
            if self.run_once() == 0:
                self.stop.wait(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="InkTime 背景 Worker")
    parser.add_argument("--once", action="store_true", help="處理目前工作後結束")
    args = parser.parse_args()
    from server import app

    runner = WorkerRunner(app)
    signal.signal(signal.SIGTERM, runner.request_stop)
    signal.signal(signal.SIGINT, runner.request_stop)
    with app.app_context():
        if args.once:
            runner.run_once()
        else:
            runner.run_forever()


if __name__ == "__main__":
    main()
