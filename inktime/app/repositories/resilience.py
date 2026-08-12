"""決策、裝置韌性與發布安全的集中資料契約。

所有寫入透過 Database.transaction() 完成；此模組刻意只保存有界候選摘要，
避免把完整照片庫或敏感照片路徑帶進追蹤／管理 API。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import ceil
from hashlib import sha256
import json
from typing import Any, Iterable
from uuid import uuid4

from inktime.app.core.json_values import json_bool, json_int, nullable_json_int
from inktime.app.db import Database


FEEDBACK_TYPES = {
    "LIKE",
    "DISLIKE",
    "SKIP_TEMPORARILY",
    "NEVER_SHOW",
    "RESTORE",
    "PAIR_LIKE",
    "PAIR_UNRELATED",
    "LAYOUT_LIKE",
    "LAYOUT_DISLIKE",
    "CAPTION_LIKE",
    "CAPTION_DISLIKE",
    "PREFER_PRODUCTION",
    "PREFER_SHADOW",
}
QUEUE_EVENTS = {
    "MANIFEST_RECEIVED",
    "DOWNLOAD_STARTED",
    "DOWNLOAD_COMPLETED",
    "HASH_VERIFIED",
    "DISPLAY_STARTED",
    "DISPLAY_COMPLETED",
    "DISPLAY_FAILED",
}
QUEUE_STATUS_FOR_EVENT = {
    "MANIFEST_RECEIVED": "AVAILABLE",
    "DOWNLOAD_STARTED": "AVAILABLE",
    "DOWNLOAD_COMPLETED": "DOWNLOADED",
    "HASH_VERIFIED": "ACKNOWLEDGED",
    "DISPLAY_STARTED": "ACKNOWLEDGED",
    "DISPLAY_COMPLETED": "DISPLAYED",
    "DISPLAY_FAILED": "FAILED",
}
QUEUE_ALLOWED_EVENTS = {
    "READY": {"MANIFEST_RECEIVED", "DOWNLOAD_STARTED", "DISPLAY_FAILED"},
    "AVAILABLE": {"MANIFEST_RECEIVED", "DOWNLOAD_STARTED", "DOWNLOAD_COMPLETED", "DISPLAY_FAILED"},
    "DOWNLOADED": {"DOWNLOAD_COMPLETED", "HASH_VERIFIED", "DISPLAY_FAILED"},
    "ACKNOWLEDGED": {"HASH_VERIFIED", "DISPLAY_STARTED", "DISPLAY_COMPLETED", "DISPLAY_FAILED"},
    "DISPLAYED": {"DISPLAY_COMPLETED"},
    "FAILED": set(),
    "EXPIRED": set(),
    "CANCELLED": set(),
    "PENDING": set(),
}
ROLL_OUT_STATES = {
    "DRAFT": {"VALIDATING", "CANCELLED"},
    "VALIDATING": {"CANARY", "FAILED", "CANCELLED"},
    "CANARY": {"OBSERVING", "PAUSED", "ROLLING_BACK", "FAILED", "CANCELLED"},
    "OBSERVING": {"EXPANDING", "PAUSED", "ROLLING_BACK", "COMPLETED", "FAILED"},
    "EXPANDING": {"OBSERVING", "PAUSED", "ROLLING_BACK", "COMPLETED", "FAILED"},
    "PAUSED": {"CANARY", "OBSERVING", "EXPANDING", "ROLLING_BACK", "CANCELLED"},
    "ROLLING_BACK": {"ROLLED_BACK", "FAILED"},
    "COMPLETED": set(),
    "ROLLED_BACK": set(),
    "FAILED": set(),
    "CANCELLED": set(),
}
CLEANUP_AUDIT_RETENTION_DAYS = 90
CLEANUP_AUDIT_BATCH_SIZE = 10


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any, *, sort_keys: bool = False) -> str:
    return json.dumps(
        value if value is not None else {}, ensure_ascii=False, separators=(",", ":"), sort_keys=sort_keys
    )


class ResilienceRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def pagination(page: Any, page_size: Any, *, maximum: int = 100) -> tuple[int, int]:
        try:
            number, size = int(page or 1), int(page_size or 30)
        except (TypeError, ValueError) as exc:
            raise ValueError("DECISION-001 分頁格式錯誤") from exc
        if number < 1 or not 1 <= size <= maximum:
            raise ValueError("DECISION-001 page 必須大於 0，page_size 必須介於 1 到 100")
        return number, size

    def algorithm_version(
        self,
        *,
        name: str,
        version: str,
        configuration: dict[str, Any],
        renderer: str,
        layout: str,
        pairing: str,
        scoring: str,
    ) -> str:
        snapshot = _json(configuration, sort_keys=True)
        digest = sha256(snapshot.encode("utf-8")).hexdigest()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT id FROM algorithm_versions WHERE algorithm_name=? AND algorithm_version=? AND configuration_hash=?",
                (name[:100], version[:100], digest),
            ).fetchone()
            if row:
                return str(row["id"])
            identifier = str(uuid4())
            connection.execute(
                """INSERT INTO algorithm_versions(id,algorithm_name,algorithm_version,configuration_hash,
                   configuration_snapshot_json,renderer_version,layout_strategy_version,pairing_strategy_version,
                   scoring_strategy_version,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    identifier,
                    name[:100],
                    version[:100],
                    digest,
                    snapshot
                    if len(snapshot) <= 20_000
                    else _json(
                        {"truncated": True, "sha256": digest, "keys": sorted(configuration)[:100]},
                        sort_keys=True,
                    ),
                    renderer[:100],
                    layout[:100],
                    pairing[:100],
                    scoring[:100],
                    utc_now(),
                ),
            )
        return identifier

    def create_trace(
        self,
        *,
        execution_mode: str,
        algorithm_version_id: str | None,
        primary_photo_id: str | None,
        secondary_photo_id: str | None = None,
        device_id: str | None = None,
        layout_mode: str | None = None,
        fit_mode: str | None = None,
        candidates: Iterable[dict[str, Any]] = (),
        candidate_count: int = 0,
        eligible_count: int = 0,
        reasons: list[str] | None = None,
        rejections: dict[str, int] | None = None,
        context: dict[str, Any] | None = None,
        duration_ms: int = 0,
        release_id: str | None = None,
        correlation_key: str | None = None,
    ) -> str:
        if execution_mode not in {"production", "shadow", "manual_preview", "test"}:
            raise ValueError("DECISION-001 execution_mode 不合法")
        trace_id, now = str(uuid4()), utc_now()
        # 排名資料只保留前 50；呼叫端不可能意外將十萬張照片寫入資料庫。
        capped = list(candidates)[:50]
        rejection_summary = dict(rejections or {})
        with self.database.transaction() as connection:
            selected_score = next(
                (item.get("adjusted_score") for item in capped if item.get("selected")), None
            )
            connection.execute(
                """INSERT INTO selection_decision_traces(trace_id,device_id,execution_mode,algorithm_version_id,release_id,correlation_key,
                   primary_photo_id,secondary_photo_id,layout_mode,fit_mode,candidate_count,eligible_count,selected_score,
                   decision_reasons_json,rejection_summary_json,context_snapshot_json,duration_ms,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trace_id,
                    device_id,
                    execution_mode,
                    algorithm_version_id,
                    release_id,
                    (correlation_key or trace_id)[:100],
                    primary_photo_id,
                    secondary_photo_id,
                    layout_mode,
                    fit_mode,
                    max(candidate_count, len(capped)),
                    max(eligible_count, 0),
                    selected_score,
                    _json(reasons or []),
                    _json(rejection_summary),
                    _json(context or {}),
                    max(0, int(duration_ms)),
                    now,
                ),
            )
            for rank, item in enumerate(capped, 1):
                connection.execute(
                    """INSERT OR IGNORE INTO selection_decision_candidates(trace_id,photo_id,rank,base_score,adjusted_score,
                       selected,rejection_code,score_components_json) VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        trace_id,
                        str(item.get("photo_id") or item.get("id") or "") or None,
                        rank,
                        item.get("base_score", item.get("combined_score")),
                        item.get("adjusted_score", item.get("combined_score")),
                        int(bool(item.get("selected", False))),
                        str(item.get("rejection_code") or "") or None,
                        _json(item.get("score_components") or self.score_components(item)),
                    ),
                )
        return trace_id

    @staticmethod
    def score_components(item: dict[str, Any]) -> dict[str, float]:
        model = float(item.get("ranking_score") or item.get("final_ranking_score") or 0)
        quality = float(item.get("local_candidate_score") or 0)
        return {
            "model_score": model,
            "quality_score": quality,
            "user_preference_adjustment": 0.0,
            "novelty_adjustment": 0.0,
            "diversity_adjustment": 0.0,
            "contextual_adjustment": 0.0,
            "final_score": float(item.get("combined_score") or model),
        }

    def attach_release(self, trace_id: str, release_id: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE selection_decision_traces SET release_id=? WHERE trace_id=?", (release_id, trace_id)
            )

    def list_traces(
        self,
        *,
        page: Any = 1,
        page_size: Any = 30,
        device_id: str | None = None,
        mode: str | None = None,
        algorithm_version_id: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        number, size = self.pagination(page, page_size)
        clauses, params = ["1=1"], []
        for column, value in (
            ("t.device_id", device_id),
            ("t.execution_mode", mode),
            ("t.algorithm_version_id", algorithm_version_id),
        ):
            if value:
                clauses.append(f"{column}=?")
                params.append(str(value))
        if start:
            clauses.append("t.created_at>=?")
            params.append(str(start))
        if end:
            clauses.append("t.created_at<=?")
            params.append(str(end))
        where = " AND ".join(clauses)
        with self.database.session() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM selection_decision_traces t WHERE {where}",  # noqa: S608 -- clauses are fixed literals
                    params,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""SELECT t.*,a.algorithm_name,a.algorithm_version,d.name AS device_name
                    FROM selection_decision_traces t LEFT JOIN algorithm_versions a ON a.id=t.algorithm_version_id
                    LEFT JOIN devices d ON d.id=t.device_id WHERE {where}
                    ORDER BY t.created_at DESC,t.id DESC LIMIT ? OFFSET ?""",  # noqa: S608 -- clauses are fixed literals
                params + [size, (number - 1) * size],  # noqa: S608 -- clauses are fixed literals
            ).fetchall()
        return {
            "items": [self._trace_row(row) for row in rows],
            "page": number,
            "page_size": size,
            "total": total,
        }

    def trace(self, trace_id: str) -> dict[str, Any] | None:
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT * FROM selection_decision_traces WHERE trace_id=?", (trace_id,)
            ).fetchone()
            if not row:
                return None
            result = self._trace_row(row)
            candidates = connection.execute(
                "SELECT photo_id,rank,base_score,adjusted_score,selected,rejection_code,score_components_json FROM selection_decision_candidates WHERE trace_id=? ORDER BY rank",
                (trace_id,),
            ).fetchall()
        result["candidates"] = [
            {
                **dict(item),
                "selected": bool(item["selected"]),
                "score_components": json.loads(item["score_components_json"]),
            }
            for item in candidates
        ]
        return result

    @staticmethod
    def _trace_row(row: Any) -> dict[str, Any]:
        value = dict(row)
        for key in ("decision_reasons_json", "rejection_summary_json", "context_snapshot_json"):
            value[key.removesuffix("_json")] = json.loads(value.pop(key) or "{}")
        return value

    def submit_feedback(
        self, *, user_id: str, payload: dict[str, Any], trace_id: str | None = None
    ) -> dict[str, Any]:
        kind = str(payload.get("feedback_type", "")).upper()
        if kind not in FEEDBACK_TYPES:
            raise ValueError("FEEDBACK-001 不支援的回饋類型")
        photo_id = str(payload.get("photo_id") or "").strip()
        secondary = str(payload.get("secondary_photo_id") or "").strip() or None
        device_id = str(payload.get("device_id") or "").strip() or None
        linked_trace = trace_id or str(payload.get("decision_trace_id") or "").strip() or None
        if kind.startswith("PAIR_") and (not photo_id or not secondary):
            raise ValueError("FEEDBACK-001 配對回饋需要兩張照片")
        if kind in {"LIKE", "DISLIKE", "SKIP_TEMPORARILY", "NEVER_SHOW", "RESTORE"} and not photo_id:
            raise ValueError("FEEDBACK-001 照片回饋需要 photo_id")
        if kind.startswith("LAYOUT_") and not linked_trace:
            raise ValueError("FEEDBACK-001 版型回饋需要 decision_trace_id")
        if kind.startswith("CAPTION_") and not linked_trace:
            raise ValueError("FEEDBACK-001 文案回饋需要 decision_trace_id")
        expires_at = None
        if kind == "SKIP_TEMPORARILY":
            days = int(payload.get("days", 30))
            if not 1 <= days <= 3650:
                raise ValueError("FEEDBACK-001 暫時跳過天數必須介於 1 到 3650")
            expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        table = (
            "photo_pair_feedback"
            if kind.startswith("PAIR_")
            else "layout_feedback"
            if kind.startswith("LAYOUT_")
            else "caption_feedback"
            if kind.startswith("CAPTION_")
            else "photo_feedback"
        )
        now, metadata = (
            utc_now(),
            _json(payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}),
        )
        with self.database.transaction() as connection:
            if table == "photo_feedback":
                # SQLite UNIQUE treats NULL scopes as distinct.  Replace the precise
                # nullable scope explicitly so repeated no-device feedback is idempotent.
                connection.execute(
                    "DELETE FROM photo_feedback WHERE user_id=? AND device_id IS ? AND photo_id=? AND feedback_type=?",
                    (user_id, device_id, photo_id, kind),
                )
                connection.execute(
                    """INSERT INTO photo_feedback(user_id,device_id,photo_id,decision_trace_id,feedback_type,value,expires_at,metadata_json,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        user_id,
                        device_id,
                        photo_id,
                        linked_trace,
                        kind,
                        float(payload.get("value", 1)),
                        expires_at,
                        metadata,
                        now,
                        now,
                    ),
                )
                if kind == "NEVER_SHOW":
                    connection.execute(
                        "UPDATE photos SET eligible=0,exclusion_status='manually_excluded',reject_reason='USER_EXCLUDED',updated_at=? WHERE id=?",
                        (now, photo_id),
                    )
                elif kind == "RESTORE":
                    connection.execute(
                        "DELETE FROM photo_feedback WHERE photo_id=? AND feedback_type IN ('NEVER_SHOW','SKIP_TEMPORARILY')",
                        (photo_id,),
                    )
                    connection.execute(
                        "UPDATE photos SET eligible=1,exclusion_status='manually_restored',reject_reason=NULL,updated_at=? WHERE id=?",
                        (now, photo_id),
                    )
                connection.execute(
                    "INSERT INTO activity_events(source,source_id,severity,component,event,message,photo_id,details_json,created_at) VALUES ('feedback',?,'info','resilience','feedback_written','使用者回饋已寫入',?,?,?)",
                    (
                        f"{user_id}:{photo_id}:{kind}",
                        photo_id,
                        _json({"feedback_type": kind, "device_id": device_id}),
                        now,
                    ),
                )
            elif table == "photo_pair_feedback":
                connection.execute(
                    """INSERT INTO photo_pair_feedback(user_id,device_id,photo_id,secondary_photo_id,decision_trace_id,feedback_type,value,expires_at,metadata_json,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,device_id,photo_id,secondary_photo_id,feedback_type) DO UPDATE SET value=excluded.value,expires_at=excluded.expires_at,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",
                    (
                        user_id,
                        device_id,
                        photo_id,
                        secondary,
                        linked_trace,
                        kind,
                        float(payload.get("value", 1)),
                        expires_at,
                        metadata,
                        now,
                        now,
                    ),
                )
            else:
                connection.execute(
                    f"""INSERT INTO {table}(user_id,device_id,photo_id,secondary_photo_id,decision_trace_id,feedback_type,value,expires_at,metadata_json,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,device_id,decision_trace_id,feedback_type) DO UPDATE SET value=excluded.value,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at""",  # noqa: S608 -- table selected from fixed feedback-type mapping
                    (  # noqa: S608 -- table selected from fixed feedback-type mapping
                        user_id,
                        device_id,
                        photo_id or None,
                        secondary,
                        linked_trace,
                        kind,
                        float(payload.get("value", 1)),
                        expires_at,
                        metadata,
                        now,
                        now,
                    ),
                )
        return {"status": "ok", "feedback_type": kind, "expires_at": expires_at}

    def delete_feedback(self, feedback_id: int) -> bool:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT photo_id,feedback_type FROM photo_feedback WHERE id=?", (feedback_id,)
            ).fetchone()
            if row is None:
                return False
            if str(row["feedback_type"]) in {"NEVER_SHOW", "SKIP_TEMPORARILY"}:
                connection.execute(
                    "UPDATE photos SET eligible=1,exclusion_status='manually_restored',reject_reason=NULL,updated_at=? WHERE id=?",
                    (utc_now(), row["photo_id"]),
                )
            connection.execute("DELETE FROM photo_feedback WHERE id=?", (feedback_id,))
        return True

    def preference_adjustment(self, photo_id: str, *, user_id: str | None = None) -> float:
        clauses, params = ["photo_id=?", "(expires_at IS NULL OR expires_at>?)"], [photo_id, utc_now()]
        if user_id:
            clauses.append("user_id=?")
            params.append(user_id)
        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT feedback_type,value FROM photo_feedback WHERE " + " AND ".join(clauses),  # noqa: S608 -- clauses are fixed literals
                params,
            ).fetchall()
        weights = {"LIKE": 8, "DISLIKE": -8, "SKIP_TEMPORARILY": -30, "NEVER_SHOW": -1000, "RESTORE": 0}
        return sum(weights.get(str(row["feedback_type"]), 0) * float(row["value"]) for row in rows)

    def preference_adjustments(self, photo_ids: Iterable[str]) -> dict[str, float]:
        """以一個有索引的 bounded query 取得選片加權，不逐張開 SQLite 連線。"""
        identifiers = list(dict.fromkeys(str(value) for value in photo_ids if str(value)))[:500]
        if not identifiers:
            return {}
        placeholders = ",".join("?" for _ in identifiers)
        with self.database.session() as connection:
            rows = connection.execute(
                f"SELECT photo_id,feedback_type,value FROM photo_feedback WHERE photo_id IN ({placeholders}) AND (expires_at IS NULL OR expires_at>?)",  # noqa: S608
                identifiers + [utc_now()],
            ).fetchall()
        weights = {"LIKE": 8, "DISLIKE": -8, "SKIP_TEMPORARILY": -30, "NEVER_SHOW": -1000, "RESTORE": 0}
        result = {identifier: 0.0 for identifier in identifiers}
        for row in rows:
            result[str(row["photo_id"])] += weights.get(str(row["feedback_type"]), 0) * float(row["value"])
        return result

    def shadow_config(self) -> dict[str, Any]:
        with self.database.session() as connection:
            row = connection.execute("SELECT * FROM shadow_config WHERE id=1").fetchone()
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        result["generate_preview"] = bool(result["generate_preview"])
        result["device_ids"] = json.loads(result.pop("device_ids_json"))
        return result

    def update_shadow_config(self, payload: dict[str, Any], *, user_id: str) -> dict[str, Any]:
        current = self.shadow_config()
        percent = json_int(
            payload,
            "sample_percent",
            default=int(current["sample_percent"]),
            minimum=10,
            maximum=100,
            error_prefix="SHADOW-001",
        )
        if percent not in {10, 25, 50, 100}:
            raise ValueError("SHADOW-001 抽樣比例只支援 10、25、50、100")
        device_ids = payload.get("device_ids", current["device_ids"])
        if not isinstance(device_ids, list) or any(
            not isinstance(value, str) or not value for value in device_ids
        ):
            raise ValueError("SHADOW-001 device_ids 必須是裝置 ID 陣列")
        values = (
            int(
                json_bool(
                    payload,
                    "enabled",
                    default=bool(current["enabled"]),
                    error_prefix="SHADOW-001",
                )
            ),
            payload.get("algorithm_version_id", current["algorithm_version_id"]),
            _json(list(dict.fromkeys(device_ids))),
            percent,
            json_int(
                payload,
                "daily_max_runs",
                default=int(current["daily_max_runs"]),
                minimum=1,
                maximum=1000,
                error_prefix="SHADOW-001",
            ),
            int(
                json_bool(
                    payload,
                    "generate_preview",
                    default=bool(current["generate_preview"]),
                    error_prefix="SHADOW-001",
                )
            ),
            json_int(
                payload,
                "preview_retention_days",
                default=int(current["preview_retention_days"]),
                minimum=1,
                maximum=365,
                error_prefix="SHADOW-001",
            ),
            user_id,
            utc_now(),
        )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE shadow_config SET enabled=?,algorithm_version_id=?,device_ids_json=?,sample_percent=?,daily_max_runs=?,generate_preview=?,preview_retention_days=?,updated_by=?,updated_at=? WHERE id=1",
                values,
            )
        return self.shadow_config()

    def queue(self, device_id: str) -> dict[str, Any] | None:
        with self.database.session() as connection:
            head = connection.execute(
                "SELECT * FROM device_content_queues WHERE device_id=?", (device_id,)
            ).fetchone()
            if not head:
                return None
            items = connection.execute(
                "SELECT * FROM device_content_queue_items WHERE device_id=? ORDER BY position,id",
                (device_id,),
            ).fetchall()
        return {"queue": dict(head), "items": [dict(item) for item in items]}

    def ensure_queue(self, device_id: str, *, depth: int = 3) -> dict[str, Any]:
        if not 1 <= int(depth) <= 24:
            raise ValueError("QUEUE-001 Queue 深度必須介於 1 到 24")
        with self.database.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM devices WHERE id=? AND enabled=1", (device_id,)
            ).fetchone()
            if not exists:
                raise KeyError(device_id)
            connection.execute(
                "INSERT OR IGNORE INTO device_content_queues(device_id,depth,updated_at) VALUES (?,?,?)",
                (device_id, int(depth), utc_now()),
            )
            connection.execute(
                "UPDATE device_content_queues SET depth=?,updated_at=? WHERE device_id=?",
                (int(depth), utc_now(), device_id),
            )
        return self.queue(device_id) or {}

    def enqueue_release(
        self,
        *,
        device_id: str,
        release_id: str,
        position: int | None = None,
        priority: int = 100,
        display_after: str | None = None,
        expires_at: str | None = None,
        idempotency_key: str | None = None,
        delivery_mode: str = "online_queue",
        offline_prefetch_allowed: bool = False,
        offline_slot: str | None = None,
        ack_deadline: str | None = None,
        offline_schedule_id: str | None = None,
        terminal_ack_retention: str | None = None,
    ) -> dict[str, Any]:
        item_id, now = str(uuid4()), utc_now()
        if delivery_mode not in {"online_queue", "offline_schedule"}:
            raise ValueError("QUEUE-005 delivery_mode 不合法")
        if delivery_mode == "online_queue" and offline_prefetch_allowed:
            raise ValueError("QUEUE-005 一般 online Queue 不得標記 offline_prefetch_allowed")
        if delivery_mode == "offline_schedule" and not offline_prefetch_allowed:
            raise ValueError("QUEUE-005 offline Schedule Queue 必須標記 offline_prefetch_allowed")
        if delivery_mode == "offline_schedule" and not str(offline_schedule_id or "").strip():
            raise ValueError("QUEUE-005 offline Schedule Queue 必須綁定 offline_schedule_id")
        if delivery_mode == "online_queue" and offline_schedule_id is not None:
            raise ValueError("QUEUE-005 online Queue 不得綁定 offline_schedule_id")
        with self.database.transaction() as connection:
            queue = connection.execute(
                "SELECT depth FROM device_content_queues WHERE device_id=?", (device_id,)
            ).fetchone()
            if not queue:
                raise ValueError("QUEUE-001 必須先建立裝置 Queue")
            if delivery_mode == "offline_schedule":
                device = connection.execute(
                    "SELECT delivery_mode,offline_prefetch_allowed FROM devices WHERE id=?", (device_id,)
                ).fetchone()
                if not device or str(device["delivery_mode"]) != "inktime_offline_schedule" or not bool(device["offline_prefetch_allowed"]):
                    raise ValueError("QUEUE-005 裝置未啟用離線排程或 Prefetch")
                schedule_owner = connection.execute(
                    "SELECT 1 FROM device_offline_schedules WHERE id=? AND device_id=?",
                    (str(offline_schedule_id), device_id),
                ).fetchone()
                if schedule_owner is None:
                    raise ValueError("QUEUE-005 offline_schedule_id 不屬於此裝置")
            else:
                device = connection.execute(
                    "SELECT delivery_mode FROM devices WHERE id=?", (device_id,)
                ).fetchone()
                if device is None:
                    raise KeyError(device_id)
                if str(device["delivery_mode"]) == "inktime_offline_schedule":
                    raise ValueError("QUEUE-005 Enhanced offline 裝置不得接收 online Queue")
            compatible = connection.execute(
                """SELECT 1 FROM releases r JOIN devices d ON d.id=?
                WHERE r.id=? AND r.status='published' AND r.render_profile=d.panel_profile""",
                (device_id, release_id),
            ).fetchone()
            if not compatible:
                raise ValueError("QUEUE-002 Release 不存在或不是已發布狀態")
            duplicate = connection.execute(
                "SELECT * FROM device_content_queue_items WHERE device_id=? AND release_id=? AND status IN ('PENDING','READY','AVAILABLE','DOWNLOADED','ACKNOWLEDGED')",
                (device_id, release_id),
            ).fetchone()
            if duplicate:
                return dict(duplicate)
            if delivery_mode == "offline_schedule":
                raise ValueError("QUEUE-005 offline Slot 只能由 prepare_day() 建立")
            active = int(
                connection.execute(
                    "SELECT COUNT(*) FROM device_content_queue_items WHERE device_id=? AND delivery_mode='online_queue' AND status IN ('PENDING','READY','AVAILABLE','DOWNLOADED','ACKNOWLEDGED')",
                    (device_id,),
                ).fetchone()[0]
            ) if delivery_mode == "online_queue" else 0
            if active >= int(queue["depth"]):
                connection.execute(
                    "UPDATE device_content_queue_items SET status='CANCELLED',updated_at=? WHERE id=(SELECT id FROM device_content_queue_items WHERE device_id=? AND delivery_mode='online_queue' AND status IN ('PENDING','READY','AVAILABLE') ORDER BY priority ASC,position DESC,id DESC LIMIT 1)",
                    (now, device_id),
                )
                connection.execute(
                    "UPDATE device_content_queues SET queue_version=queue_version+1,updated_at=? WHERE device_id=?",
                    (now, device_id),
                )
            assigned_position = position or int(
                connection.execute(
                    "SELECT COALESCE(MAX(position),0)+1 FROM device_content_queue_items WHERE device_id=?",
                    (device_id,),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO device_content_queue_items(id,device_id,release_id,position,priority,display_after,expires_at,status,idempotency_key,delivery_mode,offline_prefetch_allowed,offline_slot,ack_deadline,terminal_ack_retention,offline_schedule_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item_id,
                    device_id,
                    release_id,
                    assigned_position,
                    max(1, min(int(priority), 1000)),
                    display_after,
                    expires_at,
                    "READY",
                    idempotency_key,
                    delivery_mode,
                    int(bool(offline_prefetch_allowed)),
                    offline_slot,
                    ack_deadline,
                    terminal_ack_retention,
                    offline_schedule_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE device_content_queues SET queue_version=queue_version+1,next_queued_release_id=?,updated_at=? WHERE device_id=?",
                (release_id, now, device_id),
            )
        return dict(self._queue_item(item_id))

    def _queue_item(self, item_id: str):
        with self.database.session() as connection:
            return connection.execute(
                "SELECT * FROM device_content_queue_items WHERE id=?", (item_id,)
            ).fetchone()

    def queue_ack(self, *, device_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        raw_item_id = payload.get("queue_item_id")
        raw_event = payload.get("event")
        raw_key = payload.get("idempotency_key")
        item_id = raw_item_id if type(raw_item_id) is str else ""
        event = raw_event if type(raw_event) is str else ""
        key = raw_key.strip() if type(raw_key) is str else ""
        if (
            event not in QUEUE_EVENTS
            or not item_id
            or len(item_id) > 128
            or not key
            or len(key) > 128
        ):
            raise ValueError("QUEUE-001 ACK 缺少 queue_item_id、event 或 idempotency_key")
        queue_version = json_int(
            payload,
            "queue_version",
            required=True,
            minimum=0,
            maximum=2_147_483_647,
            error_prefix="QUEUE-001",
        )
        now = utc_now()
        with self.database.transaction() as connection:
            item = connection.execute(
                "SELECT * FROM device_content_queue_items WHERE id=? AND device_id=?", (item_id, device_id)
            ).fetchone()
            if not item:
                raise PermissionError("QUEUE-002 Queue Item 不屬於此裝置")
            existing_event = connection.execute(
                "SELECT device_id,payload_json FROM device_content_queue_events "
                "WHERE queue_item_id=? AND event_type=? AND idempotency_key=?",
                (item_id, event, key),
            ).fetchone()
            if existing_event:
                try:
                    stored_payload = json.loads(str(existing_event["payload_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    stored_payload = {}
                incoming_release = str(payload.get("release_id", "")).strip()
                stored_release = str(stored_payload.get("release_id", "")).strip()
                if incoming_release and incoming_release != stored_release:
                    raise ValueError("QUEUE-005 ACK replay release_id 身分不一致")
                if str(existing_event["device_id"]) != device_id:
                    raise PermissionError("QUEUE-002 ACK replay 裝置身分不一致")
                return {"status": "ok", "queue_item_id": item_id, "event": event, "idempotent": True}
            queue = connection.execute(
                "SELECT queue_version,current_release_id,current_displayed_at,last_known_good_release_id,last_known_good_displayed_at FROM device_content_queues WHERE device_id=?",
                (device_id,),
            ).fetchone()
            prior_download_evidence = bool(item["downloaded_at"])
            if not prior_download_evidence:
                prior_download_evidence = bool(
                    connection.execute(
                        """
                        SELECT 1 FROM device_content_queue_events
                        WHERE queue_item_id=? AND event_type IN ('DOWNLOAD_COMPLETED','HASH_VERIFIED')
                        LIMIT 1
                        """,
                        (item_id,),
                    ).fetchone()
                )
            delayed_terminal = (
                event in {"DISPLAY_COMPLETED", "DISPLAY_FAILED"}
                and str(payload.get("ack_mode", "")) == "delayed_terminal"
                and str(item["delivery_mode"]) == "offline_schedule"
                and bool(item["offline_prefetch_allowed"])
                and bool(item["offline_schedule_id"])
                and (
                    str(item["status"]) in {"ACKNOWLEDGED", "DISPLAYED"}
                    or (str(item["status"]) == "CANCELLED" and prior_download_evidence)
                )
                and "release_id" in payload
                and str(payload.get("release_id")) == str(item["release_id"])
                and item["terminal_ack_retention"] is not None
                and str(item["terminal_ack_retention"]) >= now
            )
            stale_progress_ack = False
            if (
                queue is not None
                and queue_version < int(queue["queue_version"])
                and not delayed_terminal
                and str(payload.get("ack_mode", "")) != "delayed_terminal"
            ):
                stale_progress_ack = bool(
                    prior_download_evidence
                    and event
                    in {
                        "DOWNLOAD_COMPLETED",
                        "HASH_VERIFIED",
                        "DISPLAY_STARTED",
                        "DISPLAY_COMPLETED",
                        "DISPLAY_FAILED",
                    }
                )
            if queue is None or (
                int(queue["queue_version"]) != queue_version
                and not (delayed_terminal and queue_version <= int(queue["queue_version"]))
                and not stale_progress_ack
            ):
                raise ValueError("QUEUE-003 ACK Queue 版本已過期")
            if not delayed_terminal and event not in QUEUE_ALLOWED_EVENTS.get(str(item["status"]), set()):
                raise ValueError("QUEUE-004 ACK 狀態轉移不合法")
            if payload.get("event_epoch") is not None and not delayed_terminal:
                raise ValueError("QUEUE-005 event_epoch 僅可用於 delayed_terminal ACK")
            event_at = now
            timestamp_source = "server_fallback"
            history_date = now[:10]
            if delayed_terminal:
                slot = connection.execute(
                    """
                    SELECT s.show_at,os.target_date,os.timezone
                    FROM device_offline_schedule_slots s
                    JOIN device_offline_schedules os ON os.id=s.schedule_id
                    WHERE s.queue_item_id=? AND s.schedule_id=?
                    LIMIT 1
                    """,
                    (item_id, str(item["offline_schedule_id"])),
                ).fetchone()
                if slot is None:
                    raise ValueError("QUEUE-005 delayed_terminal 缺少離線 Slot 身分")
                history_date = str(slot["target_date"])
                if payload.get("event_epoch") is not None:
                    event_epoch = json_int(
                        payload,
                        "event_epoch",
                        required=True,
                        minimum=1,
                        maximum=4_102_444_800,
                        error_prefix="QUEUE-005",
                    )
                    try:
                        event_dt = datetime.fromtimestamp(event_epoch, timezone.utc)
                        show_at = datetime.fromisoformat(str(slot["show_at"]))
                        retention = datetime.fromisoformat(str(item["terminal_ack_retention"]))
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise ValueError("QUEUE-005 event_epoch 時間格式不合法") from exc
                    server_now = datetime.fromisoformat(now)
                    skew = timedelta(minutes=10)
                    if (
                        event_dt > server_now + skew
                        or event_dt < show_at - skew
                        or event_dt > retention
                    ):
                        raise ValueError("QUEUE-005 event_epoch 超出離線顯示時間範圍")
                    event_at = event_dt.isoformat()
                    timestamp_source = "device_event"
            connection.execute(
                "INSERT INTO device_content_queue_events(queue_item_id,device_id,event_type,idempotency_key,payload_json,created_at) VALUES (?,?,?,?,?,?)",
                (
                    item_id,
                    device_id,
                    event,
                    key,
                    _json({k: v for k, v in payload.items() if k not in {"token", "authorization"}}),
                    now,
                ),
            )
            status = str(item["status"]) if str(item["status"]) == "CANCELLED" and delayed_terminal else QUEUE_STATUS_FOR_EVENT[event]
            displayed_at = event_at if event == "DISPLAY_COMPLETED" else None
            connection.execute(
                "UPDATE device_content_queue_items SET status=?,downloaded_at=CASE WHEN ? IN ('DOWNLOAD_COMPLETED','HASH_VERIFIED') THEN ? ELSE downloaded_at END,displayed_at=COALESCE(displayed_at,?),retry_count=retry_count+CASE WHEN ?='DISPLAY_FAILED' THEN 1 ELSE 0 END,last_error_code=?,updated_at=? WHERE id=?",
                (
                    status,
                    event,
                    now,
                    displayed_at,
                    event,
                    str(payload.get("error_code", ""))[:64] or None,
                    now,
                    item_id,
                ),
            )
            if event == "DISPLAY_COMPLETED":
                # DISPLAY_COMPLETED is ordered by the event's actual display
                # time, not by ACK arrival order or release identity.  A
                # legacy pointer without a timestamp is advanced once so the
                # new ordering contract can take effect safely.
                incoming_displayed_at = datetime.fromisoformat(str(event_at))
                if incoming_displayed_at.tzinfo is None:
                    incoming_displayed_at = incoming_displayed_at.replace(tzinfo=timezone.utc)
                incoming_displayed_at = incoming_displayed_at.astimezone(timezone.utc)
                current_release_id = str(queue["current_release_id"] or "")
                current_displayed_at = str(queue["current_displayed_at"] or "")
                should_advance_pointer = not current_release_id or not current_displayed_at
                if current_displayed_at and current_release_id:
                    try:
                        current_displayed = datetime.fromisoformat(current_displayed_at)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("QUEUE-005 current pointer displayed_at 格式不合法") from exc
                    if current_displayed.tzinfo is None:
                        current_displayed = current_displayed.replace(tzinfo=timezone.utc)
                    current_displayed = current_displayed.astimezone(timezone.utc)
                    if incoming_displayed_at > current_displayed:
                        should_advance_pointer = True
                    elif incoming_displayed_at == current_displayed and current_release_id != str(item["release_id"]):
                        raise ValueError("QUEUE-005 相同 displayed_at 不可綁定不同 Release")
                if should_advance_pointer:
                    connection.execute(
                        "UPDATE device_content_queues SET current_release_id=?,current_displayed_at=?,last_known_good_release_id=?,last_known_good_displayed_at=?,updated_at=? WHERE device_id=?",
                        (item["release_id"], event_at, item["release_id"], event_at, now, device_id),
                    )
                release = connection.execute(
                    "SELECT manifest_json FROM releases WHERE id=?", (item["release_id"],)
                ).fetchone()
                try:
                    manifest = json.loads(str(release["manifest_json"])) if release else {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    manifest = {}
                for file_item in manifest.get("files", []):
                    photo_id = str(file_item.get("source_photo_id", ""))
                    if (
                        photo_id
                        and connection.execute("SELECT 1 FROM photos WHERE id=?", (photo_id,)).fetchone()
                    ):
                        connection.execute(
                            """INSERT INTO display_history(photo_id,history_date,selection_method,release_id,displayed_at,metadata_json)
                            SELECT ?,?,'device_queue_ack',?,?,? WHERE NOT EXISTS
                            (SELECT 1 FROM display_history WHERE photo_id=? AND release_id=? AND selection_method='device_queue_ack')""",
                            (
                                photo_id,
                                history_date,
                                item["release_id"],
                                event_at,
                                _json(
                                    {
                                        "device_id": device_id,
                                        "queue_item_id": item_id,
                                        "timestamp_source": timestamp_source,
                                    }
                                ),
                                photo_id,
                                item["release_id"],
                            ),
                        )
            target = connection.execute(
                "SELECT rollout_id FROM rollout_targets WHERE queue_item_id=?", (item_id,)
            ).fetchone()
            if target:
                rollout_id = str(target["rollout_id"])
                target_status = (
                    "succeeded"
                    if event == "DISPLAY_COMPLETED"
                    else "failed"
                    if event == "DISPLAY_FAILED"
                    else "in_progress"
                )
                connection.execute(
                    "UPDATE rollout_targets SET status=?,last_error_code=?,updated_at=? WHERE rollout_id=? AND device_id=?",
                    (
                        target_status,
                        str(payload.get("error_code", ""))[:64] or None,
                        now,
                        rollout_id,
                        device_id,
                    ),
                )
                connection.execute(
                    "INSERT INTO rollout_health_events(rollout_id,device_id,event_type,severity,error_code,details_json,created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        rollout_id,
                        device_id,
                        event,
                        "error" if event == "DISPLAY_FAILED" else "info",
                        str(payload.get("error_code", ""))[:64] or None,
                        _json({"queue_item_id": item_id}),
                        now,
                    ),
                )
                failures = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM rollout_targets WHERE rollout_id=? AND status='failed'",
                        (rollout_id,),
                    ).fetchone()[0]
                )
                if failures >= 2:
                    connection.execute(
                        "UPDATE device_content_queue_items SET status='CANCELLED',updated_at=? WHERE id IN (SELECT queue_item_id FROM rollout_targets WHERE rollout_id=? AND queue_item_id IS NOT NULL)",
                        (now, rollout_id),
                    )
                    targets = connection.execute(
                        "SELECT device_id FROM rollout_targets WHERE rollout_id=?", (rollout_id,)
                    ).fetchall()
                    rollback_ready = True
                    for target_row in targets:
                        rollback_device = str(target_row["device_id"])
                        rollback_device_mode = connection.execute(
                            "SELECT delivery_mode FROM devices WHERE id=?", (rollback_device,)
                        ).fetchone()
                        if rollback_device_mode and str(
                            rollback_device_mode["delivery_mode"] or "legacy_online"
                        ) == "inktime_offline_schedule":
                            connection.execute(
                                "UPDATE rollout_targets SET queue_item_id=NULL,status='rollback_skipped_incompatible_offline',last_error_code='ROLLBACK-005',updated_at=? WHERE rollout_id=? AND device_id=?",
                                (now, rollout_id, rollback_device),
                            )
                            connection.execute(
                                "INSERT INTO rollout_health_events(rollout_id,device_id,event_type,severity,error_code,details_json,created_at) VALUES (?,?,?,?,?,?,?)",
                                (
                                    rollout_id,
                                    rollback_device,
                                    "ROLLBACK_TARGET_SKIPPED",
                                    "warning",
                                    "ROLLBACK-005",
                                    _json({"reason": "enhanced_offline_requires_offline_schedule"}),
                                    now,
                                ),
                            )
                            continue
                        fallback = connection.execute(
                            "SELECT last_known_good_release_id FROM device_content_queues WHERE device_id=?",
                            (rollback_device,),
                        ).fetchone()
                        fallback_id = str(fallback["last_known_good_release_id"] or "") if fallback else ""
                        if not fallback_id:
                            rollback_ready = False
                            continue
                        existing = connection.execute(
                            "SELECT id FROM device_content_queue_items WHERE device_id=? AND release_id=?",
                            (rollback_device, fallback_id),
                        ).fetchone()
                        rollback_item = str(existing["id"]) if existing else str(uuid4())
                        if not existing:
                            position = int(
                                connection.execute(
                                    "SELECT COALESCE(MAX(position),0)+1 FROM device_content_queue_items WHERE device_id=?",
                                    (rollback_device,),
                                ).fetchone()[0]
                            )
                            connection.execute(
                                "INSERT INTO device_content_queue_items(id,device_id,release_id,position,priority,status,created_at,updated_at) VALUES (?,?,?,?,1000,'READY',?,?)",
                                (rollback_item, rollback_device, fallback_id, position, now, now),
                            )
                        connection.execute(
                            "UPDATE rollout_targets SET queue_item_id=?,status='rollback_pending',updated_at=? WHERE rollout_id=? AND device_id=?",
                            (rollback_item, now, rollout_id, rollback_device),
                        )
                    if rollback_ready:
                        connection.execute(
                            "UPDATE rollout_campaigns SET status='ROLLING_BACK',rollback_reason='ROLLBACK-001 連續兩台裝置顯示失敗',updated_at=? WHERE id=? AND status IN ('CANARY','OBSERVING','EXPANDING')",
                            (now, rollout_id),
                        )
                        self._action(
                            connection,
                            rollout_id,
                            None,
                            "automatic_rollback",
                            "ROLLBACK-001 連續兩台裝置顯示失敗",
                        )
                    else:
                        connection.execute(
                            "UPDATE rollout_campaigns SET status='FAILED',rollback_reason='ROLLBACK-002 找不到 Last Known Good',updated_at=? WHERE id=?",
                            (now, rollout_id),
                        )
                        self._action(
                            connection,
                            rollout_id,
                            None,
                            "rollback_failed",
                            "ROLLBACK-002 找不到 Last Known Good",
                        )
                elif event == "DISPLAY_COMPLETED":
                    pending = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM rollout_targets WHERE rollout_id=? AND status NOT IN ('succeeded','skipped_incompatible_offline','rollback_skipped_incompatible_offline')",
                            (rollout_id,),
                        ).fetchone()[0]
                    )
                    campaign = connection.execute(
                        "SELECT status FROM rollout_campaigns WHERE id=?", (rollout_id,)
                    ).fetchone()
                    if pending == 0 and campaign and str(campaign["status"]) == "ROLLING_BACK":
                        connection.execute(
                            "UPDATE rollout_campaigns SET status='ROLLED_BACK',updated_at=? WHERE id=?",
                            (now, rollout_id),
                        )
                        self._action(connection, rollout_id, None, "rollback_completed", None)
        return {"status": "ok", "queue_item_id": item_id, "event": event, "delayed_terminal": delayed_terminal}

    def retention_policies(self) -> list[dict[str, Any]]:
        with self.database.session() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM data_retention_policies ORDER BY data_type"
                ).fetchall()
            ]

    def update_retention(self, data_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {row["data_type"] for row in self.retention_policies()}
        if data_type not in allowed:
            raise KeyError(data_type)
        current = next(row for row in self.retention_policies() if row["data_type"] == data_type)
        days, batch = (
            json_int(
                payload,
                "retention_days",
                default=int(current["retention_days"]),
                minimum=1,
                maximum=36500,
                error_prefix="RETENTION-001",
            ),
            json_int(
                payload,
                "cleanup_batch_size",
                default=int(current["cleanup_batch_size"]),
                minimum=1,
                maximum=1000,
                error_prefix="RETENTION-001",
            ),
        )
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE data_retention_policies SET enabled=?,retention_days=?,maximum_items=?,maximum_bytes=?,minimum_items_to_keep=?,cleanup_batch_size=?,dry_run=?,updated_at=? WHERE data_type=?",
                (
                    int(
                        json_bool(
                            payload,
                            "enabled",
                            default=bool(current["enabled"]),
                            error_prefix="RETENTION-001",
                        )
                    ),
                    days,
                    nullable_json_int(
                        payload,
                        "maximum_items",
                        default=current["maximum_items"],
                        minimum=0,
                        maximum=9_223_372_036_854_775_807,
                        error_prefix="RETENTION-001",
                    ),
                    nullable_json_int(
                        payload,
                        "maximum_bytes",
                        default=current["maximum_bytes"],
                        minimum=0,
                        maximum=9_223_372_036_854_775_807,
                        error_prefix="RETENTION-001",
                    ),
                    json_int(
                        payload,
                        "minimum_items_to_keep",
                        default=int(current["minimum_items_to_keep"]),
                        minimum=0,
                        maximum=9_223_372_036_854_775_807,
                        error_prefix="RETENTION-001",
                    ),
                    batch,
                    int(
                        json_bool(
                            payload,
                            "dry_run",
                            default=bool(current["dry_run"]),
                            error_prefix="RETENTION-001",
                        )
                    ),
                    utc_now(),
                    data_type,
                ),
            )
        return next(row for row in self.retention_policies() if row["data_type"] == data_type)

    def cleanup(self, *, dry_run: bool = True) -> dict[str, Any]:
        run_id, now = str(uuid4()), utc_now()
        summary: dict[str, int] = {}
        outcomes: dict[str, str] = {}
        mapping = {
            "decision_trace": ("selection_decision_traces", "created_at", "id"),
            "decision_candidate": ("selection_decision_candidates", "id", "id"),
            "queue_event": ("device_content_queue_events", "created_at", "id"),
            "device_event": ("device_events", "created_at", "id"),
            "job_log": ("job_events", "created_at", "id"),
            "api_usage": ("api_usage", "started_at", "id"),
            "ai_trace": ("ai_trace_runs", "created_at", "trace_id"),
        }
        with self.database.transaction(operation="retention_cleanup_run") as connection:
            connection.execute(
                "INSERT INTO data_cleanup_runs(id,started_at,dry_run,status) VALUES (?,?,?,?)",
                (run_id, now, int(dry_run), "running"),
            )
        try:
            with self.database.session() as connection:
                policies = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM data_retention_policies WHERE enabled=1 ORDER BY data_type"
                    ).fetchall()
                ]
            for selected_policy in policies:
                spec = mapping.get(selected_policy["data_type"])
                if not spec:
                    continue
                table, time_column, id_column = spec
                with self.database.transaction(operation="retention_cleanup_policy") as connection:
                    policy = connection.execute(
                        "SELECT * FROM data_retention_policies WHERE data_type=? AND enabled=1",
                        (selected_policy["data_type"],),
                    ).fetchone()
                    if policy is None:
                        continue
                    if not dry_run and bool(policy["dry_run"]):
                        next_summary = {**summary, policy["data_type"]: 0}
                        next_outcomes = {**outcomes, policy["data_type"]: "skipped"}
                        # last_run_at means the policy was evaluated successfully;
                        # automatic observation-only skips are evaluations, not deletes.
                        connection.execute(
                            "UPDATE data_retention_policies SET last_run_at=? WHERE data_type=? AND enabled=1",
                            (now, policy["data_type"]),
                        )
                        connection.execute(
                            "UPDATE data_cleanup_runs SET summary_json=? WHERE id=? AND status='running'",
                            (_json({**next_summary, "_outcomes": next_outcomes}), run_id),
                        )
                        summary, outcomes = next_summary, next_outcomes
                        continue
                    cutoff = (
                        datetime.now(timezone.utc) - timedelta(days=int(policy["retention_days"]))
                    ).isoformat()
                    if table == "selection_decision_candidates":
                        # 候選明細僅在其 Trace 已過保留期後才刪除。
                        ids = connection.execute(
                            "SELECT c.id FROM selection_decision_candidates c JOIN selection_decision_traces t ON t.trace_id=c.trace_id WHERE t.created_at<? ORDER BY t.created_at,c.id LIMIT ?",
                            (cutoff, int(policy["cleanup_batch_size"])),
                        ).fetchall()
                    elif table == "selection_decision_traces":
                        ids = connection.execute(
                            "SELECT id FROM selection_decision_traces WHERE created_at<? AND release_id IS NULL ORDER BY created_at,id LIMIT ?",
                            (cutoff, int(policy["cleanup_batch_size"])),
                        ).fetchall()
                    elif table == "device_content_queue_events":
                        # A terminal offline acknowledgement remains auditable until
                        # its per-item retention fence.  Non-terminal event history
                        # still follows the ordinary bounded policy.
                        ids = connection.execute(
                            """
                            SELECT e.id
                            FROM device_content_queue_events e
                            JOIN device_content_queue_items qi ON qi.id=e.queue_item_id
                            WHERE e.created_at<?
                              AND (
                                  qi.status NOT IN ('DISPLAYED','CANCELLED','EXPIRED','FAILED')
                                  OR qi.terminal_ack_retention IS NULL
                                  OR qi.terminal_ack_retention<?
                              )
                            ORDER BY e.created_at,e.id
                            LIMIT ?
                            """,
                            (cutoff, now, int(policy["cleanup_batch_size"])),
                        ).fetchall()
                    elif table == "api_usage":
                        # BudgetService and AI-limit both use the current calendar
                        # month.  A shorter operator retention value must not erase
                        # that in-window evidence before the month closes.
                        ids = connection.execute(
                            "SELECT id FROM api_usage WHERE started_at<? AND date(started_at)<date('now','start of month') ORDER BY started_at,id LIMIT ?",
                            (cutoff, int(policy["cleanup_batch_size"])),
                        ).fetchall()
                    else:
                        ids = connection.execute(
                            f"SELECT {id_column} FROM {table} WHERE {time_column}<? ORDER BY {time_column},{id_column} LIMIT ?",  # noqa: S608 -- mapping is fixed above
                            (cutoff, int(policy["cleanup_batch_size"])),
                        ).fetchall()
                    for row in ids:
                        identifier = str(row[0])
                        connection.execute(
                            "INSERT INTO data_cleanup_items(cleanup_run_id,data_type,reference_id,action,result,created_at) VALUES (?,?,?,?,?,?)",
                            (
                                run_id,
                                policy["data_type"],
                                identifier,
                                "delete",
                                "planned" if dry_run else "deleted",
                                now,
                            ),
                        )
                        if not dry_run:
                            connection.execute(  # noqa: S608 -- mapping is fixed above
                                f"DELETE FROM {table} WHERE {id_column}=?",  # noqa: S608 -- mapping is fixed above
                                (identifier,),
                            )
                    next_summary = {**summary, policy["data_type"]: len(ids)}
                    next_outcomes = {
                        **outcomes,
                        policy["data_type"]: "planned" if dry_run else "deleted",
                    }
                    connection.execute(
                        "UPDATE data_retention_policies SET last_run_at=? WHERE data_type=? AND enabled=1",
                        (now, policy["data_type"]),
                    )
                    connection.execute(
                        "UPDATE data_cleanup_runs SET summary_json=? WHERE id=? AND status='running'",
                        (_json({**next_summary, "_outcomes": next_outcomes}), run_id),
                    )
                summary, outcomes = next_summary, next_outcomes
            with self.database.transaction(operation="retention_cleanup_summary") as connection:
                connection.execute(
                    "UPDATE data_cleanup_runs SET completed_at=?,status='completed',summary_json=? WHERE id=?",
                    (utc_now(), _json({**summary, "_outcomes": outcomes}), run_id),
                )
        except Exception:
            try:
                with self.database.transaction(operation="retention_cleanup_summary") as connection:
                    connection.execute(
                        "UPDATE data_cleanup_runs SET completed_at=?,status='failed',summary_json=?,error_code=? WHERE id=?",
                        (
                            utc_now(),
                            _json({**summary, "_outcomes": outcomes}),
                            "RETENTION-CLEANUP-FAILED",
                            run_id,
                        ),
                    )
            except Exception:  # noqa: S110 - preserve the original cleanup failure
                pass
            raise
        return {
            "id": run_id,
            "dry_run": dry_run,
            "summary": summary,
            "outcomes": outcomes,
        }

    def cleanup_audit_history(self) -> dict[str, int]:
        """Delete a bounded batch of terminal cleanup audits without auditing the GC itself."""

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=CLEANUP_AUDIT_RETENTION_DAYS)
        ).isoformat()
        with self.database.transaction(operation="cleanup_audit_gc") as connection:
            deleted = connection.execute(
                """
                DELETE FROM data_cleanup_runs
                WHERE id IN (
                    SELECT id
                    FROM data_cleanup_runs
                    WHERE completed_at<?
                      AND completed_at IS NOT NULL
                      AND status IN ('completed','failed')
                    ORDER BY completed_at,id
                    LIMIT ?
                )
                """,
                (cutoff, CLEANUP_AUDIT_BATCH_SIZE),
            ).rowcount
        return {
            "deleted_runs": max(0, int(deleted)),
            "retention_days": CLEANUP_AUDIT_RETENTION_DAYS,
            "batch_size": CLEANUP_AUDIT_BATCH_SIZE,
        }

    def expire_operational_data(self) -> dict[str, int]:
        """供 Scheduler 使用的小型、冪等維護；不掃描檔案系統。"""
        now = utc_now()
        queue_gc_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=90)
        ).isoformat()
        with self.database.transaction() as connection:
            feedback = connection.execute(
                "DELETE FROM photo_feedback WHERE feedback_type='SKIP_TEMPORARILY' AND expires_at IS NOT NULL AND expires_at<=?",
                (now,),
            ).rowcount
            queue = connection.execute(
                """
                UPDATE device_content_queue_items
                SET status='EXPIRED',updated_at=?
                WHERE status NOT IN ('DISPLAYED','EXPIRED','CANCELLED')
                  AND expires_at IS NOT NULL
                  AND expires_at<=?
                  AND NOT (
                      status='ACKNOWLEDGED'
                      AND delivery_mode='offline_schedule'
                      AND terminal_ack_retention IS NOT NULL
                      AND terminal_ack_retention>?
                  )
                """,
                (now, now, now),
            ).rowcount
            queue_gc = connection.execute(
                """
                DELETE FROM device_content_queue_items
                WHERE id IN (
                    SELECT qi.id
                    FROM device_content_queue_items qi
                    WHERE qi.status IN ('DISPLAYED','CANCELLED','EXPIRED','FAILED')
                      AND qi.delivery_mode='online_queue'
                      AND qi.offline_schedule_id IS NULL
                      AND qi.updated_at<?
                      AND (qi.terminal_ack_retention IS NULL OR qi.terminal_ack_retention<?)
                      AND NOT EXISTS (
                          SELECT 1 FROM device_content_queue_events e
                          WHERE e.queue_item_id=qi.id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM rollout_targets rt WHERE rt.queue_item_id=qi.id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM device_content_queues q
                          WHERE q.device_id=qi.device_id
                            AND qi.release_id IN (
                                q.current_release_id,
                                q.last_known_good_release_id,
                                q.next_queued_release_id,
                                q.emergency_fallback_release_id
                            )
                      )
                    ORDER BY qi.updated_at,qi.id
                    LIMIT 200
                )
                """,
                (queue_gc_cutoff, now),
            ).rowcount
        # SQLite's lightweight planner/statistics refresh is safe here because
        # this method runs on the low-frequency operational maintenance cadence.
        try:
            with self.database.session() as connection:
                connection.execute("PRAGMA optimize")
        except Exception:  # noqa: S110
            # Maintenance statistics must never turn a committed expiry into a
            # failed scheduler step.
            pass
        return {
            "expired_feedback": int(feedback),
            "expired_queue_items": int(queue),
            "gc_queue_items": int(queue_gc),
        }

    def create_rollout(
        self, *, release_id: str, name: str, user_id: str, stages: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        rollout_id, now = str(uuid4()), utc_now()
        defaults = stages or [
            {"target_percent": 1, "minimum_successful_devices": 1},
            {"target_percent": 10},
            {"target_percent": 50},
            {"target_percent": 100},
        ]
        with self.database.transaction() as connection:
            if not connection.execute(
                "SELECT 1 FROM releases WHERE id=? AND status='published'", (release_id,)
            ).fetchone():
                raise ValueError("ROLLOUT-001 Release 不存在或不是已發布狀態")
            connection.execute(
                "INSERT INTO rollout_campaigns(id,release_id,name,status,created_by,created_at,updated_at) VALUES (?,?,?,'DRAFT',?,?,?)",
                (rollout_id, release_id, name[:200], user_id, now, now),
            )
            for index, stage in enumerate(defaults, 1):
                connection.execute(
                    "INSERT INTO rollout_stages(rollout_id,stage_number,target_percent,minimum_observation_minutes,minimum_successful_devices,maximum_failure_rate,maximum_timeout_rate,minimum_ack_rate,manual_approval_required) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        rollout_id,
                        index,
                        max(1, min(int(stage.get("target_percent", 100)), 100)),
                        max(1, min(int(stage.get("minimum_observation_minutes", 30)), 1440)),
                        max(1, int(stage.get("minimum_successful_devices", 1))),
                        float(stage.get("maximum_failure_rate", 0.1)),
                        float(stage.get("maximum_timeout_rate", 0.1)),
                        float(stage.get("minimum_ack_rate", 0.9)),
                        int(bool(stage.get("manual_approval_required", False))),
                    ),
                )
            self._action(connection, rollout_id, user_id, "create", None)
        return self.rollout(rollout_id) or {}

    def start_rollout(self, rollout_id: str, *, actor_id: str) -> dict[str, Any]:
        """以第一個 Stage 的小樣本建立高優先佇列，正式指派不在這裡被覆寫。"""
        now = utc_now()
        with self.database.transaction() as connection:
            campaign = connection.execute(
                "SELECT * FROM rollout_campaigns WHERE id=?", (rollout_id,)
            ).fetchone()
            if not campaign:
                raise KeyError(rollout_id)
            if str(campaign["status"]) != "DRAFT":
                raise ValueError("ROLLOUT-001 只有 DRAFT 可開始 Canary")
            stage = connection.execute(
                "SELECT * FROM rollout_stages WHERE rollout_id=? ORDER BY stage_number LIMIT 1", (rollout_id,)
            ).fetchone()
            if not stage:
                raise ValueError("ROLLOUT-001 發布活動沒有 Stage")
            all_devices = connection.execute(
                "SELECT d.id,d.delivery_mode FROM devices d JOIN releases r ON r.id=? AND r.render_profile=d.panel_profile WHERE d.enabled=1 ORDER BY d.id",
                (campaign["release_id"],),
            ).fetchall()
            offline_devices = [
                row for row in all_devices
                if str(row["delivery_mode"] or "legacy_online") == "inktime_offline_schedule"
            ]
            devices = [
                row for row in all_devices
                if str(row["delivery_mode"] or "legacy_online") != "inktime_offline_schedule"
            ]
            if not devices:
                raise ValueError("ROLLOUT-005 Enhanced offline 裝置只能透過 offline schedule rollout")
            for device in offline_devices:
                device_id = str(device["id"])
                connection.execute(
                    "INSERT OR REPLACE INTO rollout_targets(rollout_id,device_id,status,queue_item_id,last_error_code,updated_at) VALUES (?,?,?,NULL,?,?)",
                    (rollout_id, device_id, "skipped_incompatible_offline", "ROLLOUT-005", now),
                )
                connection.execute(
                    "INSERT INTO rollout_health_events(rollout_id,device_id,event_type,severity,error_code,details_json,created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        rollout_id,
                        device_id,
                        "ROLLOUT_TARGET_SKIPPED",
                        "warning",
                        "ROLLOUT-005",
                        _json({"reason": "enhanced_offline_requires_offline_schedule"}),
                        now,
                    ),
                )
            target_count = min(
                len(devices),
                max(
                    int(stage["minimum_successful_devices"]),
                    ceil(len(devices) * int(stage["target_percent"]) / 100),
                ),
            )
            for device in devices[:target_count]:
                device_id = str(device["id"])
                connection.execute(
                    "INSERT OR IGNORE INTO device_content_queues(device_id,updated_at) VALUES (?,?)",
                    (device_id, now),
                )
                existing = connection.execute(
                    "SELECT id FROM device_content_queue_items WHERE device_id=? AND release_id=?",
                    (device_id, campaign["release_id"]),
                ).fetchone()
                item_id = str(existing["id"]) if existing else str(uuid4())
                if not existing:
                    position = int(
                        connection.execute(
                            "SELECT COALESCE(MAX(position),0)+1 FROM device_content_queue_items WHERE device_id=?",
                            (device_id,),
                        ).fetchone()[0]
                    )
                    connection.execute(
                        "INSERT INTO device_content_queue_items(id,device_id,release_id,position,priority,status,created_at,updated_at) VALUES (?,?,?,?,900,'READY',?,?)",
                        (item_id, device_id, campaign["release_id"], position, now, now),
                    )
                connection.execute(
                    "INSERT OR REPLACE INTO rollout_targets(rollout_id,device_id,status,queue_item_id,updated_at) VALUES (?,?, 'pending', ?, ?)",
                    (rollout_id, device_id, item_id, now),
                )
            connection.execute(
                "UPDATE rollout_stages SET status='active',started_at=? WHERE id=?", (now, stage["id"])
            )
            connection.execute(
                "UPDATE rollout_campaigns SET status='CANARY',updated_at=? WHERE id=?", (now, rollout_id)
            )
            self._action(connection, rollout_id, actor_id, "start_canary", None)
        return self.rollout(rollout_id) or {}

    def _action(
        self, connection, rollout_id: str, actor_id: str | None, action: str, reason: str | None
    ) -> None:
        connection.execute(
            "INSERT INTO rollout_actions(rollout_id,actor_id,action,reason,created_at) VALUES (?,?,?,?,?)",
            (rollout_id, actor_id, action, reason, utc_now()),
        )

    def rollout(self, rollout_id: str) -> dict[str, Any] | None:
        with self.database.session() as connection:
            campaign = connection.execute(
                "SELECT * FROM rollout_campaigns WHERE id=?", (rollout_id,)
            ).fetchone()
            if not campaign:
                return None
            stages = connection.execute(
                "SELECT * FROM rollout_stages WHERE rollout_id=? ORDER BY stage_number", (rollout_id,)
            ).fetchall()
            targets = connection.execute(
                "SELECT * FROM rollout_targets WHERE rollout_id=? ORDER BY id", (rollout_id,)
            ).fetchall()
        return {
            "campaign": dict(campaign),
            "stages": [dict(r) for r in stages],
            "targets": [dict(r) for r in targets],
        }

    def list_rollouts(self, *, page: Any = 1, page_size: Any = 30) -> dict[str, Any]:
        number, size = self.pagination(page, page_size)
        with self.database.session() as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM rollout_campaigns").fetchone()[0])
            rows = connection.execute(
                "SELECT * FROM rollout_campaigns ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (size, (number - 1) * size),
            ).fetchall()
        return {"items": [dict(r) for r in rows], "page": number, "page_size": size, "total": total}

    def transition_rollout(
        self, rollout_id: str, *, target: str, actor_id: str, reason: str | None = None
    ) -> dict[str, Any]:
        target = target.upper()
        with self.database.transaction() as connection:
            campaign = connection.execute(
                "SELECT * FROM rollout_campaigns WHERE id=?", (rollout_id,)
            ).fetchone()
            if not campaign:
                raise KeyError(rollout_id)
            current = str(campaign["status"])
            if target not in ROLL_OUT_STATES.get(current, set()):
                raise ValueError(f"ROLLOUT-001 {current} 不可轉為 {target}")
            connection.execute(
                "UPDATE rollout_campaigns SET status=?,rollback_reason=CASE WHEN ?='ROLLING_BACK' THEN ? ELSE rollback_reason END,updated_at=? WHERE id=?",
                (target, target, (reason or "")[:500], utc_now(), rollout_id),
            )
            self._action(connection, rollout_id, actor_id, target.lower(), reason)
            if target == "ROLLING_BACK":
                connection.execute(
                    "UPDATE device_content_queue_items SET status='CANCELLED',updated_at=? WHERE id IN (SELECT queue_item_id FROM rollout_targets WHERE rollout_id=? AND queue_item_id IS NOT NULL)",
                    (utc_now(), rollout_id),
                )
        return self.rollout(rollout_id) or {}
