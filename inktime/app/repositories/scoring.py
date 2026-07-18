from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import uuid4

from inktime.app.db import Database
from inktime.app.domain.analysis.scoring import validate_ranking_weights
from inktime.app.repositories.settings import SettingsRepository


SETTING_KEYS = {
    "rules": "analysis.scoring_rules",
    "memory": "analysis.ranking_memory_weight",
    "beauty": "analysis.ranking_beauty_weight",
    "technical_quality": "analysis.ranking_technical_weight",
    "emotion": "analysis.ranking_emotion_weight",
    "favorite_bonus": "analysis.ranking_favorite_bonus",
}


class ScoringProfileRepository:
    def __init__(self, database: Database, settings: SettingsRepository) -> None:
        self.database = database
        self.settings = settings

    def _settings_snapshot(self) -> dict:
        return {
            "rules": str(self.settings.get(SETTING_KEYS["rules"], "")),
            "memory_weight": float(self.settings.get(SETTING_KEYS["memory"], 50)),
            "beauty_weight": float(self.settings.get(SETTING_KEYS["beauty"], 20)),
            "technical_weight": float(
                self.settings.get(SETTING_KEYS["technical_quality"], 10)
            ),
            "emotion_weight": float(self.settings.get(SETTING_KEYS["emotion"], 20)),
            "favorite_bonus": float(self.settings.get(SETTING_KEYS["favorite_bonus"], 5)),
        }

    def ensure_initial(self) -> str:
        snapshot = self._settings_snapshot()
        version_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                active = connection.execute(
                    "SELECT id FROM scoring_rule_versions WHERE is_active=1"
                ).fetchone()
                if active:
                    connection.execute("COMMIT")
                    return str(active["id"])
                connection.execute(
                    """
                    INSERT INTO scoring_rule_versions(
                        id,name,rules,memory_weight,beauty_weight,technical_weight,emotion_weight,
                        favorite_bonus,is_active,created_by,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,1,NULL,?)
                    """,
                    (
                        version_id,
                        "預設評分規則",
                        snapshot["rules"],
                        snapshot["memory_weight"],
                        snapshot["beauty_weight"],
                        snapshot["technical_weight"],
                        snapshot["emotion_weight"],
                        snapshot["favorite_bonus"],
                        now,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return version_id

    def current(self) -> dict:
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT * FROM scoring_rule_versions WHERE is_active=1"
            ).fetchone()
        if row is None:
            self.ensure_initial()
            return self.current()
        return dict(row)

    def list(self, limit: int = 50) -> list[dict]:
        with self.database.session() as connection:
            rows = connection.execute(
                "SELECT * FROM scoring_rule_versions ORDER BY created_at DESC,id DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def create(
        self,
        *,
        name: str,
        rules: str,
        weights: dict[str, float],
        favorite_bonus: float,
        created_by: str,
        source_ip: str,
    ) -> dict:
        clean_name = name.strip()
        clean_rules = rules.strip()
        if not clean_name or len(clean_name) > 80:
            raise ValueError("版本名稱必須為 1 到 80 個字元")
        if len(clean_rules) < 100 or len(clean_rules) > 12000:
            raise ValueError("照片評分規則必須為 100 到 12000 個字元")
        values = validate_ranking_weights(weights)
        bonus = float(favorite_bonus)
        if bonus < 0 or bonus > 30:
            raise ValueError("最愛加分必須介於 0 到 30")

        version_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        setting_values = {
            SETTING_KEYS["rules"]: clean_rules,
            SETTING_KEYS["memory"]: values["memory"],
            SETTING_KEYS["beauty"]: values["beauty"],
            SETTING_KEYS["technical_quality"]: values["technical_quality"],
            SETTING_KEYS["emotion"]: values["emotion"],
            SETTING_KEYS["favorite_bonus"]: bonus,
        }
        with self.database.session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for key, value in setting_values.items():
                    previous = connection.execute(
                        "SELECT value_json FROM settings WHERE key=?", (key,)
                    ).fetchone()
                    if previous is None:
                        raise KeyError(key)
                    encoded = json.dumps(value, ensure_ascii=False)
                    connection.execute(
                        "UPDATE settings SET value_json=?,updated_by=?,updated_at=? WHERE key=?",
                        (encoded, created_by, now, key),
                    )
                    connection.execute(
                        """
                        INSERT INTO setting_history(
                            key,changed_at,changed_by,old_value_summary,new_value_summary,source_ip,
                            requires_restart
                        ) VALUES (?,?,?,?,?,?,0)
                        """,
                        (
                            key,
                            now,
                            created_by,
                            str(previous["value_json"])[:500],
                            encoded[:500],
                            source_ip[:64],
                        ),
                    )
                connection.execute("UPDATE scoring_rule_versions SET is_active=0 WHERE is_active=1")
                connection.execute(
                    """
                    INSERT INTO scoring_rule_versions(
                        id,name,rules,memory_weight,beauty_weight,technical_weight,emotion_weight,
                        favorite_bonus,is_active,created_by,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,1,?,?)
                    """,
                    (
                        version_id,
                        clean_name,
                        clean_rules,
                        values["memory"],
                        values["beauty"],
                        values["technical_quality"],
                        values["emotion"],
                        bonus,
                        created_by,
                        now,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.get(version_id)

    def get(self, version_id: str) -> dict:
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT * FROM scoring_rule_versions WHERE id=?", (version_id,)
            ).fetchone()
        if row is None:
            raise KeyError(version_id)
        return dict(row)

    def restore(self, version_id: str, *, created_by: str, source_ip: str) -> dict:
        version = self.get(version_id)
        return self.create(
            name=f"還原：{version['name']}",
            rules=str(version["rules"]),
            weights={
                "memory": float(version["memory_weight"]),
                "beauty": float(version["beauty_weight"]),
                "technical_quality": float(version["technical_weight"]),
                "emotion": float(version["emotion_weight"]),
            },
            favorite_bonus=float(version["favorite_bonus"]),
            created_by=created_by,
            source_ip=source_ip,
        )
