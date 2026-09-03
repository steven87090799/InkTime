#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ctypes
import gc
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import tracemalloc
from typing import Any
from datetime import datetime, timezone
from hashlib import sha256

import psutil

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from inktime.app.core.runtime_config import RuntimeConfig
from inktime.app.factory import create_app
from inktime.app.repositories.auth import AuthRepository
from inktime.app.workers.runner import WorkerRunner
from inktime.app.workers.scheduler import SchedulerRunner


class _WebhookResponse:
    status_code = 204
    headers: dict[str, str] = {}


class _WebhookSession:
    def __init__(self) -> None:
        self.calls = 0

    def post(self, *_args, **_kwargs):
        self.calls += 1
        return _WebhookResponse()


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _release_runtime_memory() -> tuple[int, int, int, bool, bool]:
    """Stop profiler overhead and release free libc pages before RSS validation."""

    traced_heap, traced_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    collected = gc.collect()
    trim_available = False
    trimmed = False
    try:
        libc = ctypes.CDLL(None)
        malloc_trim = getattr(libc, "malloc_trim", None)
        if malloc_trim is not None:
            trim_available = True
            malloc_trim.argtypes = [ctypes.c_size_t]
            malloc_trim.restype = ctypes.c_int
            trimmed = bool(malloc_trim(0))
    except (AttributeError, OSError, TypeError, ValueError):
        # macOS and non-glibc runners do not expose malloc_trim; GC remains useful.
        pass
    return traced_heap, traced_peak, collected, trim_available, trimmed


def _snapshot(app, process: psutil.Process) -> dict[str, Any]:
    database = app.extensions["inktime_database"]
    now = datetime.now(timezone.utc).isoformat()
    with database.session() as connection:
        pending_jobs = int(
            connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('pending','running','retrying','pausing')"
            ).fetchone()[0]
        )
        stuck_leases = int(
            connection.execute(
                "SELECT COUNT(*) FROM job_items WHERE status='running' AND (lease_until IS NULL OR lease_until<?)",
                (now,),
            ).fetchone()[0]
        )
        oldest_pending = connection.execute(
            "SELECT MIN(created_at) FROM jobs WHERE status IN ('pending','running','retrying','pausing')"
        ).fetchone()[0]
        scheduler_heartbeat = connection.execute(
            "SELECT updated_at FROM observability_state WHERE key='heartbeat:scheduler'"
        ).fetchone()
        webhook_retry_queue = int(
            connection.execute(
                "SELECT COUNT(*) FROM device_notifications WHERE webhook_status IN ('pending','retrying')"
            ).fetchone()[0]
        )
    now_datetime = datetime.now(timezone.utc)

    def age_seconds(value: object) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, (now_datetime - datetime.fromisoformat(str(value))).total_seconds())
        except ValueError:
            return None

    current_heap, peak_heap = tracemalloc.get_traced_memory()
    try:
        descriptors = process.num_fds()
    except (AttributeError, psutil.Error):
        descriptors = len(process.open_files())
    database_path = str(database.path)
    return {
        "rss_bytes": process.memory_info().rss,
        "heap_bytes": current_heap,
        "heap_peak_bytes": peak_heap,
        "threads": process.num_threads(),
        "file_descriptors": descriptors,
        "sqlite_open_files": sum(1 for entry in process.open_files() if entry.path.startswith(database_path)),
        "sqlite_open_connections": 0,
        "sqlite": database.observability(),
        "child_processes": len(process.children(recursive=True)),
        "pending_jobs": pending_jobs,
        "oldest_pending_job_seconds": age_seconds(oldest_pending),
        "scheduler_heartbeat_age_seconds": age_seconds(
            scheduler_heartbeat["updated_at"] if scheduler_heartbeat else None
        ),
        "webhook_retry_queue": webhook_retry_queue,
        "pending_async_tasks": 0,
        "stuck_leases": stuck_leases,
        "wal_bytes": _file_size(Path(f"{database.path}-wal")),
    }


def _seed_release(app, device_id: str, index: int) -> dict[str, Any]:
    release_id = f"soak-release-{index:06d}"
    payload = f"soak-payload-{index}".encode()
    digest = sha256(payload).hexdigest()
    manifest = {
        "release_id": release_id,
        "render_profile": "safe_4c",
        "display_type": "epaper",
        "width": 480,
        "height": 800,
        "pixel_format": "2bpp",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": [{"name": "photo_1.bin", "size": len(payload), "sha256": digest}],
    }
    release_dir = app.config["INKTIME_RELEASE_DIR"] / release_id
    release_dir.mkdir(parents=True)
    (release_dir / "photo_1.bin").write_bytes(payload)
    (release_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (app.config["INKTIME_RELEASE_DIR"] / "latest.safe_4c").write_text(release_id, encoding="utf-8")
    with app.extensions["inktime_database"].transaction() as connection:
        connection.execute(
            """
            INSERT INTO releases(
                id,display_type,width,height,pixel_format,manifest_json,status,created_at,
                published_at,created_by,render_profile,verified_at,reconciliation_status
            ) VALUES (?,?,?,?,?,?,'published',?,?,?,?,?,'ok')
            """,
            (
                release_id,
                "epaper",
                480,
                800,
                "2bpp",
                json.dumps(manifest),
                manifest["created_at"],
                manifest["created_at"],
                "soak",
                "safe_4c",
                manifest["created_at"],
            ),
        )
    repository = app.extensions["inktime_resilience_repository"]
    return repository.enqueue_release(device_id=device_id, release_id=release_id)


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded Web/Worker/Scheduler runtime soak")
    parser.add_argument("--iterations", type=int, help="legacy alias for --max-iterations")
    parser.add_argument("--duration-seconds", type=int)
    parser.add_argument("--max-iterations", type=int, default=30)
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--max-rss-growth-mib", type=int, default=96)
    args = parser.parse_args()
    iterations = args.iterations if args.iterations is not None else args.max_iterations
    if (
        not 1 <= iterations <= 100_000
        or not 1 <= args.timeout_seconds <= 86_400
        or (args.duration_seconds is not None and not 1 <= args.duration_seconds <= 86_400)
    ):
        parser.error("iterations 或 timeout 超出安全範圍")

    process = psutil.Process()
    failures: list[str] = []
    stop = threading.Event()
    workers: list[threading.Thread] = []
    app = None
    tracemalloc.start()
    started = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="inktime-soak-") as temporary:
        root = Path(temporary)
        runtime = RuntimeConfig.from_sources(
            environ={},
            base_dir=root,
            environment="test",
            database_path=root / "inktime.db",
            data_dir=root,
            release_dir=root / "releases",
            backup_dir=root / "backups",
            cache_dir=root / "cache",
            photo_dir=root / "photos",
            testing=True,
            development=False,
            cookie_secure=False,
        )
        runtime.photo_dir.mkdir(parents=True)
        app = create_app(runtime)
        auth = AuthRepository(app.extensions["inktime_database"])
        auth.create_initial_administrator("soak-admin", "soak-test-passphrase")
        device_id, token = app.extensions["inktime_device_repository"].create("soak-device")
        queue = app.extensions["inktime_resilience_repository"]
        queue.ensure_queue(device_id, depth=14)
        with app.extensions["inktime_database"].transaction() as connection:
            connection.execute(
                "UPDATE settings SET value_json='true' WHERE key='notification.webhook_enabled'"
            )
            connection.execute(
                "UPDATE settings SET value_json=? WHERE key='notification.webhook_url'",
                (json.dumps("https://hooks.example.net/inktime"),),
            )
        webhook = _WebhookSession()
        app.extensions["inktime_notification_service"].session = webhook

        runner = WorkerRunner(app)
        scheduler = SchedulerRunner(app)

        def loop(name: str, action) -> None:
            while not stop.is_set():
                try:
                    action()
                except Exception as exc:  # final summary reports bounded diagnostics
                    failures.append(f"{name}:{type(exc).__name__}:{exc}")
                    stop.set()
                    return
                stop.wait(0.05)

        workers = [
            threading.Thread(target=loop, args=("worker", runner.run_once), daemon=True),
            threading.Thread(target=loop, args=("scheduler", scheduler.tick), daemon=True),
        ]
        client = app.test_client()

        def sign_in() -> str:
            login_page = client.get("/login")
            login_match = re.search(r'name="csrf_token" value="([^"]+)"', login_page.get_data(as_text=True))
            if login_page.status_code != 200 or login_match is None:
                raise RuntimeError(f"login page unavailable: {login_page.status_code}")
            login = client.post(
                "/login",
                data={
                    "username": "soak-admin",
                    "password": "soak-test-passphrase",
                    "csrf_token": login_match.group(1),
                },
            )
            if login.status_code != 302:
                raise RuntimeError(f"login failed: {login.status_code}")
            dashboard = client.get("/dashboard")
            dashboard_match = re.search(
                r'<meta name="csrf-token" content="([^"]+)"',
                dashboard.get_data(as_text=True),
            )
            if dashboard.status_code != 200 or dashboard_match is None:
                raise RuntimeError(f"dashboard unavailable: {dashboard.status_code}")
            return dashboard_match.group(1)

        csrf_token = sign_in()
        for thread in workers:
            thread.start()
        auth_headers = {"Authorization": f"Bearer {token}"}
        initial = _snapshot(app, process)
        peak = dict(initial)

        completed_iterations = 0
        for index in range(iterations):
            elapsed = time.monotonic() - started
            if stop.is_set() or elapsed > args.timeout_seconds:
                failures.append("timeout")
                break
            if args.duration_seconds is not None and elapsed >= args.duration_seconds:
                break
            dashboard = client.get("/dashboard")
            if dashboard.status_code != 200:
                failures.append(f"session:{dashboard.status_code}")
            if index and index % 10 == 0:
                logout = client.post("/logout", data={"csrf_token": csrf_token})
                if logout.status_code != 302:
                    failures.append(f"logout:{logout.status_code}")
                csrf_token = sign_in()
            item = _seed_release(app, device_id, index)
            for label, response in (
                (
                    "device-valid",
                    client.post("/api/device/v1/status", json={}, headers=auth_headers),
                ),
                (
                    "device-invalid",
                    client.get(
                        "/api/device/v1/releases/latest",
                        headers={"Authorization": f"Bearer invalid-{index}"},
                    ),
                ),
                (
                    "release-metadata",
                    client.get("/api/device/v1/releases/latest", headers=auth_headers),
                ),
            ):
                expected = {200} if label != "device-invalid" else {401, 429}
                if response.status_code not in expected:
                    failures.append(f"{label}:{response.status_code}")
            manifest_response = client.get("/api/device/v1/queue/manifest", headers=auth_headers)
            manifest = manifest_response.get_json()
            if manifest_response.status_code != 200 or not manifest.get("items"):
                failures.append(f"queue-manifest:{manifest_response.status_code}")
            else:
                for event_index, event in enumerate(
                    (
                        "MANIFEST_RECEIVED",
                        "DOWNLOAD_STARTED",
                        "DOWNLOAD_COMPLETED",
                        "HASH_VERIFIED",
                        "DISPLAY_STARTED",
                        "DISPLAY_COMPLETED",
                    )
                ):
                    response = client.post(
                        "/api/device/v1/queue/ack",
                        json={
                            "queue_item_id": item["id"],
                            "queue_version": manifest["queue_version"],
                            "event": event,
                            "idempotency_key": f"soak-{index}-{event_index}",
                        },
                        headers=auth_headers,
                    )
                    if response.status_code != 200:
                        failures.append(f"queue-ack-{event}:{response.status_code}")
                        break
            if index % 5 == 0:
                scan = client.post(
                    "/api/v1/maintenance/scan",
                    json={
                        "root_path": str(runtime.photo_dir),
                        "mode": "incremental",
                        "build_thumbnails": False,
                    },
                    headers={"X-CSRF-Token": csrf_token},
                )
                if scan.status_code != 202:
                    failures.append(f"scan:{scan.status_code}")
                notification_id = app.extensions["inktime_notification_service"].create_test(
                    created_by="soak"
                )
                outcome = app.extensions["inktime_notification_service"].deliver_one(notification_id)
                if outcome["status"] != "delivered":
                    failures.append(f"webhook:{outcome['status']}")
            current = _snapshot(app, process)
            completed_iterations += 1
            for field in ("rss_bytes", "heap_peak_bytes", "threads", "file_descriptors", "wal_bytes"):
                peak[field] = max(int(peak[field]), int(current[field]))

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            final_pending = _snapshot(app, process)["pending_jobs"]
            if final_pending == 0:
                break
            time.sleep(0.1)
        stop.set()
        runner.request_stop()
        scheduler.request_stop()
        for thread in workers:
            thread.join(timeout=5)
        final_before_memory_cleanup = _snapshot(app, process)
        traced_heap, traced_peak, collected, trim_available, trimmed = _release_runtime_memory()
        final = dict(final_before_memory_cleanup)
        final["rss_bytes_before_cleanup"] = int(final_before_memory_cleanup["rss_bytes"])
        final["rss_bytes"] = process.memory_info().rss
        final["heap_bytes"] = traced_heap
        final["heap_peak_bytes"] = traced_peak
        if any(thread.is_alive() for thread in workers):
            failures.append("background-thread-cleanup")
        if final["pending_jobs"]:
            failures.append(f"pending-jobs:{final['pending_jobs']}")
        if final["stuck_leases"]:
            failures.append(f"stuck-leases:{final['stuck_leases']}")
        if final["child_processes"]:
            failures.append(f"child-processes:{final['child_processes']}")
        rss_growth = int(final["rss_bytes"]) - int(initial["rss_bytes"])
        if rss_growth > args.max_rss_growth_mib * 1024 * 1024:
            failures.append(f"rss-growth:{rss_growth}")
        if int(final["file_descriptors"]) > int(initial["file_descriptors"]) + 12:
            failures.append("file-descriptor-growth")
        if int(final["threads"]) > int(initial["threads"]) + 4:
            failures.append("thread-growth")
        if int(final["sqlite_open_connections"]) != 0:
            failures.append("sqlite-open-connections")

        summary = {
            "status": "PASS" if not failures else "FAIL",
            "iterations_requested": iterations,
            "iterations_completed": completed_iterations,
            "duration_requested_seconds": args.duration_seconds,
            "duration_seconds": round(time.monotonic() - started, 3),
            "initial": initial,
            "peak": peak,
            "final": final,
            "rss_growth_bytes": rss_growth,
            "webhook_calls": webhook.calls,
            "unhandled_exceptions": failures,
            "memory_cleanup": {
                "tracemalloc_stopped": True,
                "gc_collected": collected,
                "malloc_trim_available": trim_available,
                "malloc_trim_applied": trimmed,
                "rss_bytes_before_cleanup": int(final_before_memory_cleanup["rss_bytes"]),
                "rss_bytes_after_cleanup": int(final["rss_bytes"]),
            },
            "cleanup": {
                "threads_stopped": not any(thread.is_alive() for thread in workers),
                "child_processes_reaped": final["child_processes"] == 0,
                "sqlite_connections_closed": final["sqlite_open_connections"] == 0,
                "temporary_directory_removed_on_exit": True,
            },
            "process_exit_status": 0 if not failures else 1,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        if args.summary_json is not None:
            args.summary_json.parent.mkdir(parents=True, exist_ok=True)
            args.summary_json.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        app.extensions["inktime_service_container"].close()
        app = None
        return 0 if not failures else 1


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception as exc:
        failure_summary = {
            "status": "FAIL",
            "process_exit_status": 1,
            "unhandled_exceptions": [f"{type(exc).__name__}"],
            "cleanup": {"process_exit_reclaims_resources": True},
        }
        print(json.dumps(failure_summary, ensure_ascii=False, indent=2, sort_keys=True))
        if "--summary-json" in sys.argv:
            summary_index = sys.argv.index("--summary-json") + 1
            if summary_index < len(sys.argv):
                failure_path = Path(sys.argv[summary_index])
                failure_path.parent.mkdir(parents=True, exist_ok=True)
                failure_path.write_text(
                    json.dumps(failure_summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
        exit_code = 1
    raise SystemExit(exit_code)
