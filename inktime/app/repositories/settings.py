from __future__ import annotations

from datetime import datetime, timezone
import base64
from hashlib import sha256
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from inktime.app.db import Database
from inktime.app.domain.analysis.scoring import DEFAULT_SCORING_RULES


SETTING_DEFINITIONS: dict[str, dict[str, Any]] = {
    "general.timezone": {
        "category": "一般設定",
        "default": "Asia/Taipei",
        "type": "string",
        "description": "歷史今日與排程使用的時區",
        "risk": "錯誤時區會造成跨日選片偏移",
        "restart": False,
    },
    "analysis.strategy": {
        "category": "分析設定",
        "default": "smart_two_stage",
        "type": "string",
        "description": "新工作的預設分析策略",
        "risk": "高品質策略成本較高",
        "restart": False,
    },
    "analysis.stage_two_threshold": {
        "category": "分析設定",
        "default": 65,
        "type": "number",
        "description": "進入第二階段的回憶分數門檻",
        "risk": "數值越低，模型成本越高",
        "min": 0,
        "max": 100,
        "restart": False,
    },
    "analysis.scoring_rules": {
        "category": "照片評分規則",
        "default": DEFAULT_SCORING_RULES,
        "type": "string",
        "description": "模型判斷哪些照片應取得較高或較低分的規則",
        "risk": "修改後會影響新分析結果；既有照片分數不會自動重算",
        "restart": False,
        "multiline": True,
        "rows": 26,
        "min_length": 100,
        "max_length": 12000,
        "full_width": True,
    },
    "analysis.concurrency": {
        "category": "分析設定",
        "default": 2,
        "type": "integer",
        "description": "Worker 最大並行數",
        "risk": "過高可能觸發 Rate Limit 或耗盡記憶體",
        "min": 1,
        "max": 32,
        "restart": True,
    },
    "analysis.max_retries": {
        "category": "分析設定",
        "default": 3,
        "type": "integer",
        "description": "單一項目最大嘗試次數",
        "risk": "重試會增加成本",
        "min": 0,
        "max": 10,
        "restart": False,
    },
    "model.low_model": {
        "category": "模型設定",
        "default": "gpt-4o-mini",
        "type": "string",
        "description": "第一階段低成本模型",
        "risk": "模型必須支援圖片與 JSON Schema",
        "restart": False,
    },
    "model.high_model": {
        "category": "模型設定",
        "default": "gpt-4o",
        "type": "string",
        "description": "第二階段高品質模型",
        "risk": "請確認 Provider 價格",
        "restart": False,
    },
    "budget.daily_warning": {
        "category": "成本設定",
        "default": 5.0,
        "type": "number",
        "description": "每日成本警告值（美元）",
        "risk": "只警告，不會停止",
        "min": 0,
        "max": 100000,
        "restart": False,
    },
    "budget.daily_stop": {
        "category": "成本設定",
        "default": 10.0,
        "type": "number",
        "description": "每日成本停止值（美元）",
        "risk": "達到後新模型請求會暫停",
        "min": 0,
        "max": 100000,
        "restart": False,
    },
    "budget.monthly_warning": {
        "category": "成本設定",
        "default": 50.0,
        "type": "number",
        "description": "每月成本警告值（美元）",
        "risk": "只警告，不會停止",
        "min": 0,
        "max": 1000000,
        "restart": False,
    },
    "budget.monthly_stop": {
        "category": "成本設定",
        "default": 100.0,
        "type": "number",
        "description": "每月成本停止值（美元）",
        "risk": "達到後新模型請求會暫停",
        "min": 0,
        "max": 1000000,
        "restart": False,
    },
    "budget.job_default": {
        "category": "成本設定",
        "default": 10.0,
        "type": "number",
        "description": "單一工作的預設預算",
        "risk": "工作達到後自動暫停",
        "min": 0,
        "max": 100000,
        "restart": False,
    },
    "budget.photo_max": {
        "category": "成本設定",
        "default": 0.25,
        "type": "number",
        "description": "單張照片成本上限",
        "risk": "過低可能使高品質分析無法執行",
        "min": 0,
        "max": 1000,
        "restart": False,
    },
    "budget.max_tokens": {
        "category": "成本設定",
        "default": 8000,
        "type": "integer",
        "description": "單次請求最大 Token 估算上限",
        "risk": "需與 Provider 模型能力相容",
        "min": 256,
        "max": 1000000,
        "restart": False,
    },
    "render.memory_threshold": {
        "category": "渲染設定",
        "default": 70,
        "type": "number",
        "description": "歷史今日選片最低回憶分數",
        "risk": "過高可能沒有候選照片",
        "min": 0,
        "max": 100,
        "restart": False,
    },
    "render.quantity": {
        "category": "渲染設定",
        "default": 5,
        "type": "integer",
        "description": "每次發布照片數",
        "risk": "增加數量會增加裝置下載量",
        "min": 1,
        "max": 50,
        "restart": False,
    },
    "render.font_path": {
        "category": "渲染設定",
        "default": "",
        "type": "string",
        "description": "繁體中文字型路徑",
        "risk": "缺少 CJK 字元時禁止正式發布",
        "restart": False,
    },
    "device.legacy_api_enabled": {
        "category": "裝置設定",
        "default": False,
        "type": "boolean",
        "description": "舊版 URL 金鑰下載模式",
        "risk": "不安全；Token 可能進入 URL 與 Log",
        "restart": True,
    },
    "system.log_format": {
        "category": "系統設定",
        "default": "human",
        "type": "string",
        "description": "Console Log 格式（human/json）",
        "risk": "JSON 適合集中式 Log",
        "restart": True,
    },
    "security.session_minutes": {
        "category": "安全設定",
        "default": 30,
        "type": "integer",
        "description": "管理介面閒置 Session 分鐘數",
        "risk": "過長會增加共用裝置風險",
        "min": 5,
        "max": 1440,
        "restart": True,
    },
    "backup.schedule_enabled": {
        "category": "備份設定",
        "default": True,
        "type": "boolean",
        "description": "每日自動建立資料庫備份",
        "risk": "需預留備份空間",
        "restart": False,
    },
    "backup.hour": {
        "category": "備份設定",
        "default": 3,
        "type": "integer",
        "description": "依系統時區執行備份的小時",
        "risk": "請避開大量分析時段",
        "min": 0,
        "max": 23,
        "restart": False,
    },
    "backup.retention": {
        "category": "備份設定",
        "default": 14,
        "type": "integer",
        "description": "保留的自動備份數量",
        "risk": "過低會縮短可回復時間",
        "min": 1,
        "max": 365,
        "restart": False,
    },
}


class SettingsRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def ensure_defaults(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO settings(key,category,value_json,value_type,requires_restart,updated_at) VALUES (?,?,?,?,?,?)",
                [
                    (
                        key,
                        definition["category"],
                        json.dumps(definition["default"], ensure_ascii=False),
                        definition["type"],
                        int(definition.get("restart", False)),
                        now,
                    )
                    for key, definition in SETTING_DEFINITIONS.items()
                ],
            )

    def all(self):
        with self.database.session() as connection:
            rows = connection.execute("SELECT * FROM settings ORDER BY category,key").fetchall()
        return [
            dict(row)
            | {"value": json.loads(row["value_json"]), "definition": SETTING_DEFINITIONS.get(row["key"], {})}
            for row in rows
        ]

    def get(self, key: str, default=None):
        with self.database.session() as connection:
            row = connection.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else default

    def update(self, key: str, value, *, changed_by: str, source_ip: str) -> None:
        definition = SETTING_DEFINITIONS.get(key)
        if definition is None:
            raise KeyError(key)
        value_type = definition["type"]
        if value_type == "integer":
            value = int(value)
        elif value_type == "number":
            value = float(value)
        elif value_type == "boolean":
            value = value is True or str(value).lower() in {"1", "true", "on", "yes"}
        else:
            value = str(value)
            if "min_length" in definition and len(value.strip()) < definition["min_length"]:
                raise ValueError(f"{key} 內容不可少於 {definition['min_length']} 個字元")
            if "max_length" in definition and len(value) > definition["max_length"]:
                raise ValueError(f"{key} 內容不可超過 {definition['max_length']} 個字元")
        if (
            "min" in definition
            and value < definition["min"]
            or "max" in definition
            and value > definition["max"]
        ):
            raise ValueError(f"{key} 超出合法範圍")
        now = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(value, ensure_ascii=False)
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                previous = connection.execute(
                    "SELECT value_json FROM settings WHERE key=?", (key,)
                ).fetchone()
                old_summary = previous["value_json"] if previous else "未設定"
                connection.execute(
                    "UPDATE settings SET value_json=?,updated_by=?,updated_at=? WHERE key=?",
                    (encoded, changed_by, now, key),
                )
                connection.execute(
                    """
                    INSERT INTO setting_history(key,changed_at,changed_by,old_value_summary,new_value_summary,source_ip,requires_restart)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        key,
                        now,
                        changed_by,
                        old_summary[:500],
                        encoded[:500],
                        source_ip[:64],
                        int(definition.get("restart", False)),
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise


class SecretStore:
    def __init__(self, database: Database, master_secret: str) -> None:
        self.database = database
        key = base64.urlsafe_b64encode(sha256(master_secret.encode("utf-8")).digest())
        self.cipher = Fernet(key)

    def set(self, key: str, value: str, updated_by: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        encrypted = self.cipher.encrypt(value.encode("utf-8"))
        with self.database.session() as connection:
            connection.execute(
                """
                INSERT INTO secrets(key,encrypted_value,updated_by,updated_at) VALUES (?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET encrypted_value=excluded.encrypted_value,updated_by=excluded.updated_by,updated_at=excluded.updated_at
                """,
                (key, encrypted, updated_by, now),
            )

    def get(self, key: str) -> str | None:
        with self.database.session() as connection:
            row = connection.execute("SELECT encrypted_value FROM secrets WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return self.cipher.decrypt(bytes(row["encrypted_value"])).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError("SEC-001 無法解密敏感設定；請確認主密鑰") from exc

    def delete(self, key: str) -> None:
        with self.database.session() as connection:
            connection.execute("DELETE FROM secrets WHERE key=?", (key,))
