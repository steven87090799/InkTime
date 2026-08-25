"""Fail-closed retention for historical ``photo_analysis`` rows."""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
import json
from typing import Any

from inktime.app.db import Database


DEFAULT_HISTORY_PER_PHOTO = 2
DEFAULT_DELETE_BATCH_SIZE = 200
MAX_DELETE_BATCH_SIZE = 500
MAX_CURRENT_IDENTITIES = 8


class PhotoAnalysisRetentionConflictError(RuntimeError):
    """The candidate set changed after the operator's dry-run."""


class PhotoAnalysisRetentionRepository:
    """Inventory and prune only rows proven to have no current or audit role."""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _configuration_digest(connection) -> str:
        """Bind a preview to every database input used to build the current plan."""

        digest = hashlib.sha256()
        queries = (
            "SELECT key,value_json,updated_at FROM settings ORDER BY key",
            """
            SELECT id,name,kind,base_url,enabled,priority,supports_vision,supports_batch,
                   supports_json_schema,updated_at
            FROM providers ORDER BY id
            """,
            """
            SELECT id,rules,memory_weight,beauty_weight,technical_weight,emotion_weight,
                   favorite_bonus,is_active,created_at
            FROM scoring_rule_versions ORDER BY id
            """,
        )
        for query in queries:
            for row in connection.execute(query):
                digest.update(
                    json.dumps(tuple(row), ensure_ascii=False, separators=(",", ":")).encode(
                        "utf-8"
                    )
                )
                digest.update(b"\n")
            digest.update(b"\0")
        return digest.hexdigest()

    @staticmethod
    def _identities(
        current_fingerprints: Iterable[str],
        current_specs: Iterable[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        fingerprints = tuple(
            dict.fromkeys(str(value).strip() for value in current_fingerprints if str(value).strip())
        )
        specs = tuple(dict.fromkeys(str(value).strip() for value in current_specs if str(value).strip()))
        if not fingerprints and not specs:
            raise ValueError("RETENTION-001 current analysis identity is required")
        if len(fingerprints) + len(specs) > MAX_CURRENT_IDENTITIES:
            raise ValueError("RETENTION-001 too many current analysis identities")
        return fingerprints, specs

    @staticmethod
    def _classification_cte(
        fingerprints: tuple[str, ...],
        specs: tuple[str, ...],
    ) -> tuple[str, tuple[str, ...]]:
        identity_terms: list[str] = []
        parameters: list[str] = []
        if fingerprints:
            identity_terms.append(
                f"analysis_fingerprint IN ({','.join('?' for _ in fingerprints)})"
            )
            parameters.extend(fingerprints)
        if specs:
            identity_terms.append(f"analysis_spec_json IN ({','.join('?' for _ in specs)})")
            parameters.extend(specs)
        identity_match = " OR ".join(identity_terms)
        return (
            f"""
            WITH latest_rows AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY photo_id
                           ORDER BY created_at DESC,id DESC
                       ) AS retention_rank
                FROM photo_analysis
            ),
            current_rows AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY photo_id
                           ORDER BY created_at DESC,id DESC
                       ) AS retention_rank
                FROM photo_analysis
                WHERE {identity_match}
            ),
            invalid_semantic_photos AS (
                SELECT DISTINCT photo_id
                FROM photo_analysis
                WHERE semantic_json IS NOT NULL
                  AND TRIM(semantic_json)!=''
                  AND json_valid(semantic_json)=0
            ),
            audit_references AS (
                SELECT analysis_id
                FROM photo_reviews
                WHERE analysis_id IS NOT NULL
                UNION
                SELECT analysis_id
                FROM photo_review_events
                WHERE analysis_id IS NOT NULL
            ),
            inherited_references AS (
                SELECT DISTINCT CAST(CASE WHEN json_valid(semantic_json)=1 THEN
                    json_extract(semantic_json,'$.inherited_from.analysis_id')
                END AS INTEGER) AS analysis_id
                FROM photo_analysis
                WHERE CASE WHEN json_valid(semantic_json)=1 THEN
                    json_type(semantic_json,'$.inherited_from.analysis_id')
                END='integer'
            ),
            base AS (
                SELECT a.id,a.photo_id,a.created_at,a.analysis_fingerprint,
                       CASE WHEN latest.retention_rank=1 THEN 1 ELSE 0 END AS is_latest,
                       CASE WHEN current.retention_rank=1 THEN 1 ELSE 0 END AS is_current,
                       CASE WHEN invalid.photo_id IS NOT NULL THEN 1 ELSE 0 END AS has_invalid_semantic,
                       CASE WHEN audit.analysis_id IS NOT NULL THEN 1 ELSE 0 END
                           AS has_audit_reference,
                       CASE WHEN inherited.analysis_id IS NOT NULL THEN 1 ELSE 0 END
                           AS has_inherited_reference
                FROM photo_analysis a
                JOIN latest_rows latest ON latest.id=a.id
                LEFT JOIN current_rows current ON current.id=a.id
                LEFT JOIN invalid_semantic_photos invalid ON invalid.photo_id=a.photo_id
                LEFT JOIN audit_references audit ON audit.analysis_id=a.id
                LEFT JOIN inherited_references inherited ON inherited.analysis_id=a.id
            ),
            historical_rows AS (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY photo_id
                           ORDER BY created_at DESC,id DESC
                       ) AS retention_rank
                FROM base
                WHERE is_latest=0
                  AND is_current=0
                  AND has_invalid_semantic=0
                  AND has_audit_reference=0
                  AND has_inherited_reference=0
            ),
            classified AS (
                SELECT base.id,base.photo_id,base.created_at,base.analysis_fingerprint,
                       CASE
                           WHEN base.is_latest=1 THEN 'latest'
                           WHEN base.is_current=1 THEN 'current'
                           WHEN base.has_invalid_semantic=1 THEN 'invalid_semantic_json'
                           WHEN base.has_audit_reference=1 THEN 'review_or_event'
                           WHEN base.has_inherited_reference=1 THEN 'inherited_source'
                           WHEN history.retention_rank<={DEFAULT_HISTORY_PER_PHOTO}
                               THEN 'historical_buffer'
                           ELSE 'candidate'
                       END AS retention_class
                FROM base
                LEFT JOIN historical_rows history ON history.id=base.id
            )
            """,
            tuple(parameters),
        )

    def _inventory_with_connection(
        self,
        connection,
        fingerprints: tuple[str, ...],
        specs: tuple[str, ...],
    ) -> dict[str, Any]:
        cte, parameters = self._classification_cte(fingerprints, specs)
        summary = connection.execute(
            cte  # noqa: S608 -- only bounded placeholder count is generated dynamically.
            + """
            SELECT COUNT(*) AS total_rows,
                   COALESCE(SUM(retention_class='latest'),0) AS latest_rows,
                   COALESCE(SUM(retention_class='current'),0) AS current_rows,
                   COALESCE(SUM(retention_class='invalid_semantic_json'),0)
                       AS invalid_semantic_json_rows,
                   COALESCE(SUM(retention_class='review_or_event'),0) AS review_or_event_rows,
                   COALESCE(SUM(retention_class='inherited_source'),0) AS inherited_source_rows,
                   COALESCE(SUM(retention_class='historical_buffer'),0) AS historical_buffer_rows,
                   COALESCE(SUM(retention_class='candidate'),0) AS candidate_rows,
                   COUNT(DISTINCT CASE WHEN retention_class='candidate' THEN photo_id END)
                       AS candidate_photos,
                   MIN(CASE WHEN retention_class='candidate' THEN created_at END)
                       AS oldest_candidate_created_at,
                   MAX(CASE WHEN retention_class='candidate' THEN created_at END)
                       AS newest_candidate_created_at
            FROM classified
            """,
            parameters,
        ).fetchone()
        sample = connection.execute(
            cte  # noqa: S608 -- only bounded placeholder count is generated dynamically.
            + """
            SELECT id,photo_id,created_at,analysis_fingerprint
            FROM classified
            WHERE retention_class='candidate'
            ORDER BY created_at,id
            LIMIT 50
            """,
            parameters,
        ).fetchall()
        digest = hashlib.sha256()
        configuration_digest = self._configuration_digest(connection)
        digest.update(
            json.dumps(
                {
                    "fingerprints": fingerprints,
                    "spec_sha256": [
                        hashlib.sha256(spec.encode("utf-8")).hexdigest() for spec in specs
                    ],
                    "history_per_photo": DEFAULT_HISTORY_PER_PHOTO,
                    "configuration_digest": configuration_digest,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        candidate_cursor = connection.execute(
            cte  # noqa: S608 -- only bounded placeholder count is generated dynamically.
            + """
            SELECT id,photo_id,created_at,COALESCE(analysis_fingerprint,'')
            FROM classified
            WHERE retention_class='candidate'
            ORDER BY created_at,id
            """,
            parameters,
        )
        for row in candidate_cursor:
            digest.update(
                json.dumps(tuple(row), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
            digest.update(b"\n")
        result = dict(summary)
        result.update(
            {
                "dry_run": True,
                "policy": {
                    "latest_per_photo": 1,
                    "current_identity_per_photo": 1,
                    "unreferenced_history_per_photo": DEFAULT_HISTORY_PER_PHOTO,
                    "review_and_event_references": "all",
                    "inherited_sources": "all",
                    "invalid_semantic_json": "protect_entire_photo",
                    "age_grace_days": 0,
                },
                "candidate_sample": [dict(row) for row in sample],
                "inventory_digest": digest.hexdigest(),
                "configuration_digest": configuration_digest,
            }
        )
        return result

    def inventory(
        self,
        *,
        current_fingerprints: Iterable[str],
        current_specs: Iterable[str] = (),
    ) -> dict[str, Any]:
        fingerprints, specs = self._identities(current_fingerprints, current_specs)
        with self.database.session() as connection:
            return self._inventory_with_connection(connection, fingerprints, specs)

    def prune_batch(
        self,
        *,
        current_fingerprints: Iterable[str],
        current_specs: Iterable[str] = (),
        batch_size: int = DEFAULT_DELETE_BATCH_SIZE,
        expected_inventory_digest: str,
    ) -> dict[str, Any]:
        fingerprints, specs = self._identities(current_fingerprints, current_specs)
        if type(batch_size) is not int or not 1 <= batch_size <= MAX_DELETE_BATCH_SIZE:
            raise ValueError(
                f"RETENTION-001 batch_size must be between 1 and {MAX_DELETE_BATCH_SIZE}"
            )
        cte, parameters = self._classification_cte(fingerprints, specs)
        with self.database.transaction(operation="photo_analysis_retention") as connection:
            before = self._inventory_with_connection(connection, fingerprints, specs)
            if str(expected_inventory_digest) != str(before["inventory_digest"]):
                raise PhotoAnalysisRetentionConflictError(
                    "RETENTION-003 inventory changed; run a new dry-run before applying"
                )
            connection.execute(
                cte  # noqa: S608 -- candidates are rechecked by the fixed CTE in this transaction.
                + """
                , candidate_batch AS (
                    SELECT id
                    FROM classified
                    WHERE retention_class='candidate'
                    ORDER BY created_at,id
                    LIMIT ?
                )
                DELETE FROM photo_analysis
                WHERE id IN (SELECT id FROM candidate_batch)
                """,
                (*parameters, batch_size),
            )
            deleted_rows = int(connection.execute("SELECT changes()").fetchone()[0])
        after = self.inventory(current_fingerprints=fingerprints, current_specs=specs)
        return {
            "dry_run": False,
            "batch_size": batch_size,
            "deleted_rows": deleted_rows,
            "candidate_rows_before": int(before["candidate_rows"]),
            "remaining_candidate_rows": int(after["candidate_rows"]),
            "complete": int(after["candidate_rows"]) == 0,
            "inventory": after,
        }
