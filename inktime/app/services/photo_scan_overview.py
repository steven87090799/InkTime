from __future__ import annotations

from pathlib import Path


def scan_schedule_display(task: dict | None) -> dict:
    """Describe saved settings without rewriting custom schedules on page load."""
    result = {"preset": "custom", "time": "03:00", "label": "尚未設定自動掃描"}
    if task is None:
        return result
    fields = str(task["cron"]).split()
    if len(fields) == 5 and fields[0].isdigit() and fields[1].isdigit():
        result["time"] = f"{int(fields[1]):02d}:{int(fields[0]):02d}"
        if not task.get("weekdays"):
            tail = " ".join(fields[2:])
            presets = {"* * *": ("daily", "每天"), "* * 1": ("weekly", "每週一"), "1 * *": ("monthly", "每月 1 日")}
            if tail in presets:
                result["preset"], frequency = presets[tail]
                result["label"] = f"{frequency} {result['time']}"
    if result["preset"] == "custom":
        result["label"] = f"自訂排程（{task['cron']}）"
    if not task["enabled"]:
        result["preset"] = "manual"
        result["label"] = "僅手動掃描（自動掃描已停用）"
    return result


def photo_scan_overview(*, photos, jobs, schedules, photo_dir, timezone: str) -> dict:
    task = schedules.get("incremental_scan")
    config = task["config"] if task else {}
    root = Path(config.get("root_path") or photo_dir).expanduser().resolve()
    job = jobs.latest_photo_scan(str(root))
    scan = photos.latest_scan(root)
    labels = {
        "pending": "等待掃描", "preparing": "準備掃描", "running": "掃描工作執行中",
        "pausing": "正在暫停", "paused": "已暫停", "retrying": "等待重試",
        "completed": "掃描工作已完成", "completed_with_errors": "工作完成，但有失敗項目",
        "failed": "掃描工作失敗", "cancelled": "掃描工作已取消", "budget_exceeded": "工作已停止",
    }
    return {
        "root": str(root), "library_name": config.get("library_name", "主要照片庫"),
        "timezone": timezone, "schedule": task, "frequency": scan_schedule_display(task),
        "job": job, "run": scan,
        "job_label": labels.get(job["status"], "查看工作狀態") if job else "尚未建立掃描工作",
        "active": bool(job and job["status"] in {"pending", "preparing", "running", "pausing", "retrying"}),
    }
