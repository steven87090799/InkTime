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
        if not 1 <= int(depth) <= 14:
            raise ValueError("QUEUE-001 Queue 深度必須介於 1 到 14")
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
    ) -> dict[str, Any]:
        item_id, now = str(uuid4()), utc_now()
        with self.database.transaction() as connection:
            queue = connection.execute(
                "SELECT depth FROM device_content_queues WHERE device_id=?", (device_id,)
            ).fetchone()
            if not queue:
                raise ValueError("QUEUE-001 必須先建立裝置 Queue")
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
            active = int(
                connection.execute(
                    "SELECT COUNT(*) FROM device_content_queue_items WHERE device_id=? AND status IN ('PENDING','READY','AVAILABLE','DOWNLOADED','ACKNOWLEDGED')",
                    (device_id,),
                ).fetchone()[0]
            )
            if active >= int(queue["depth"]):
                connection.execute(
                    "UPDATE device_content_queue_items SET status='CANCELLED',updated_at=? WHERE id=(SELECT id FROM device_content_queue_items WHERE device_id=? AND status IN ('PENDING','READY','AVAILABLE') ORDER BY priority ASC,position DESC,id DESC LIMIT 1)",
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
                "INSERT INTO device_content_queue_items(id,device_id,release_id,position,priority,display_after,expires_at,status,idempotency_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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

    def manifest(self, device_id: str, *, release_root) -> dict[str, Any]:
        queue = self.queue(device_id)
        if not queue:
            self.ensure_queue(device_id)
            queue = self.queue(device_id)
        assert queue is not None
        now = utc_now()
        items = []
        for row in queue["items"]:
            if row["status"] not in {"READY", "AVAILABLE", "DOWNLOADED", "ACKNOWLEDGED"} or (
                row["expires_at"] and str(row["expires_at"]) < now
            ):
                continue
            manifest_path = release_root / str(row["release_id"]) / "manifest.json"
            try:
                release = json.loads(manifest_path.read_text(encoding="utf-8"))
                file_item = next(
                    item for item in release.get("files", []) if str(item.get("name", "")).endswith(".bin")
                )
            except (OSError, ValueError, StopIteration, json.JSONDecodeError):
                continue
            items.append(
                {
                    "queue_item_id": row["id"],
                    "release_id": row["release_id"],
                    "display_after": row["display_after"],
                    "expires_at": row["expires_at"],
                    "priority": row["priority"],
                    "sha256": file_item.get("sha256"),
                    "size": file_item.get("size"),
                    "download_url": f"/api/device/v1/queue/items/{row['id']}/files/{file_item.get('name')}",
                }
            )
        return {
            "schema_version": 1,
            "queue_version": queue["queue"]["queue_version"],
            "device_id": device_id,
            "generated_at": now,
            "items": items,
            "last_known_good_release_id": queue["queue"]["last_known_good_release_id"],
        }

    def queue_ack(self, *, device_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        item_id, event = str(payload.get("queue_item_id", "")), str(payload.get("event", ""))
        key = str(payload.get("idempotency_key", "")).strip()
        if event not in QUEUE_EVENTS or not item_id or not key:
            raise ValueError("QUEUE-001 ACK 缺少 queue_item_id、event 或 idempotency_key")
        try:
            queue_version = int(str(payload.get("queue_version")))
        except (TypeError, ValueError) as exc:
            raise ValueError("QUEUE-001 ACK 缺少有效 queue_version") from exc
        now = utc_now()
        with self.database.transaction() as connection:
            item = connection.execute(
                "SELECT * FROM device_content_queue_items WHERE id=? AND device_id=?", (item_id, device_id)
            ).fetchone()
            if not item:
                raise PermissionError("QUEUE-002 Queue Item 不屬於此裝置")
            queue = connection.execute(
                "SELECT queue_version FROM device_content_queues WHERE device_id=?", (device_id,)
            ).fetchone()
            if queue is None or int(queue["queue_version"]) != queue_version:
                raise ValueError("QUEUE-003 ACK Queue 版本已過期")
            existing_event = connection.execute(
                "SELECT 1 FROM device_content_queue_events WHERE queue_item_id=? AND event_type=? AND idempotency_key=?",
                (item_id, event, key),
            ).fetchone()
            if existing_event:
                return {"status": "ok", "queue_item_id": item_id, "event": event, "idempotent": True}
            if event not in QUEUE_ALLOWED_EVENTS.get(str(item["status"]), set()):
                raise ValueError("QUEUE-004 ACK 狀態轉移不合法")
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
            status = QUEUE_STATUS_FOR_EVENT[event]
            displayed_at = now if event == "DISPLAY_COMPLETED" else None
            connection.execute(
                "UPDATE device_content_queue_items SET status=?,downloaded_at=CASE WHEN ? IN ('DOWNLOAD_COMPLETED','HASH_VERIFIED') THEN ? ELSE downloaded_at END,displayed_at=COALESCE(?,displayed_at),retry_count=retry_count+CASE WHEN ?='DISPLAY_FAILED' THEN 1 ELSE 0 END,last_error_code=?,updated_at=? WHERE id=?",
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
                connection.execute(
                    "UPDATE device_content_queues SET current_release_id=?,last_known_good_release_id=?,updated_at=? WHERE device_id=?",
                    (item["release_id"], item["release_id"], now, device_id),
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
                                now[:10],
                                item["release_id"],
                                now,
                                _json({"device_id": device_id, "queue_item_id": item_id}),
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
                            "SELECT COUNT(*) FROM rollout_targets WHERE rollout_id=? AND status<>'succeeded'",
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
        return {"status": "ok", "queue_item_id": item_id, "event": event}

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
        run_id, now, summary = str(uuid4()), utc_now(), {}
        mapping = {
            "decision_trace": ("selection_decision_traces", "created_at", "id"),
            "decision_candidate": ("selection_decision_candidates", "id", "id"),
            "queue_event": ("device_content_queue_events", "created_at", "id"),
            "device_event": ("device_events", "created_at", "id"),
            "job_log": ("job_events", "created_at", "id"),
        }
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO data_cleanup_runs(id,started_at,dry_run,status) VALUES (?,?,?,?)",
                (run_id, now, int(dry_run), "running"),
            )
            for policy in connection.execute(
                "SELECT * FROM data_retention_policies WHERE enabled=1"
            ).fetchall():
                spec = mapping.get(policy["data_type"])
                if not spec:
                    continue
                table, time_column, id_column = spec
                cutoff = (
                    datetime.now(timezone.utc) - timedelta(days=int(policy["retention_days"]))
                ).isoformat()
                if table == "selection_decision_candidates":
                    # 候選明細僅在其 Trace 已過保留期後才刪除。
                    ids = connection.execute(
                        "SELECT c.id FROM selection_decision_candidates c JOIN selection_decision_traces t ON t.trace_id=c.trace_id WHERE t.created_at<? LIMIT ?",
                        (cutoff, int(policy["cleanup_batch_size"])),
                    ).fetchall()
                elif table == "selection_decision_traces":
                    ids = connection.execute(
                        "SELECT id FROM selection_decision_traces WHERE created_at<? AND release_id IS NULL LIMIT ?",
                        (cutoff, int(policy["cleanup_batch_size"])),
                    ).fetchall()
                else:
                    ids = connection.execute(
                        f"SELECT {id_column} FROM {table} WHERE {time_column}<? LIMIT ?",  # noqa: S608 -- mapping is fixed above
                        (cutoff, int(policy["cleanup_batch_size"])),
                    ).fetchall()
                summary[policy["data_type"]] = len(ids)
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
            connection.execute(
                "UPDATE data_cleanup_runs SET completed_at=?,status='completed',summary_json=? WHERE id=?",
                (utc_now(), _json(summary), run_id),
            )
        return {"id": run_id, "dry_run": dry_run, "summary": summary}

    def expire_operational_data(self) -> dict[str, int]:
        """供 Scheduler 使用的小型、冪等維護；不掃描檔案系統。"""
        now = utc_now()
        with self.database.transaction() as connection:
            feedback = connection.execute(
                "DELETE FROM photo_feedback WHERE feedback_type='SKIP_TEMPORARILY' AND expires_at IS NOT NULL AND expires_at<=?",
                (now,),
            ).rowcount
            queue = connection.execute(
                "UPDATE device_content_queue_items SET status='EXPIRED',updated_at=? WHERE status NOT IN ('DISPLAYED','EXPIRED','CANCELLED') AND expires_at IS NOT NULL AND expires_at<=?",
                (now, now),
            ).rowcount
        return {"expired_feedback": int(feedback), "expired_queue_items": int(queue)}

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
            devices = connection.execute(
                "SELECT d.id FROM devices d JOIN releases r ON r.id=? AND r.render_profile=d.panel_profile WHERE d.enabled=1 ORDER BY d.id",
                (campaign["release_id"],),
            ).fetchall()
            if not devices:
                raise ValueError("ROLLOUT-001 沒有可用裝置")
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
