"""Shared ownership predicate for queued realtime and remote Batch work."""

from inktime.app.repositories.analysis_batches import ACTIVE_BATCH_STATUSES


class AnalysisReservationConflict(ValueError):
    code = "ANALYSIS-RESERVATION-CONFLICT"
    not_sent = True


def held_batch_sql(alias: str = "reserved_batch") -> str:
    """An unresolved external side effect outlives a local terminal status."""
    statuses = ",".join(f"'{status}'" for status in sorted(ACTIVE_BATCH_STATUSES))
    return f"""({alias}.status IN ({statuses}) OR (
        {alias}.abandon_confirmed_at IS NULL AND (
            COALESCE({alias}.last_error_code,'') IN ('BATCH-SUBMISSION-UNKNOWN','BATCH-UPLOAD-UNKNOWN',
                                       'submission_unknown','upload_unknown','submission_persist_unknown')
            OR ({alias}.remote_batch_id IS NOT NULL AND
                COALESCE({alias}.remote_status,'') NOT IN ('completed','failed','expired','cancelled'))
        )
    ))"""  # noqa: S608 -- fixed repository-owned status values and alias.


def paid_batch_item_sql(item: str, batch: str) -> str:
    return f"""({batch}.remote_batch_id IS NOT NULL OR {batch}.submitted_at IS NOT NULL
        OR {item}.raw_response_json IS NOT NULL OR {item}.request_id IS NOT NULL
        OR {item}.status IN ('success','imported'))"""


def valid_batch_retry_sql(item: str, batch: str) -> str:
    return f"""({item}.status IN ('failed','missing','retry_pending','schema_invalid',
                                  'duplicate_custom_id','unexpected_custom_id')
        AND {batch}.status IN ('completed','completed_with_errors','failed','expired','cancelled')
        AND COALESCE({batch}.remote_status,'') IN ('completed','failed','expired','cancelled')
        AND NOT {held_batch_sql(batch)})"""


def assert_no_batch_content_owner(connection, content_sha256: str) -> None:
    """Run inside the single-call writer transaction before creating its operation.

    Queued Batch jobs own bytes before their JSONL/items have been prepared;
    Batch items retain that ownership after a parent job stops or a file moves.
    """
    if connection.execute(
        f"""SELECT 1 FROM analysis_batch_items reserved_batch_item
            JOIN analysis_batches reserved_batch ON reserved_batch.id=reserved_batch_item.batch_id
            WHERE reserved_batch_item.content_sha256=? AND
                  ({held_batch_sql()} OR {paid_batch_item_sql('reserved_batch_item', 'reserved_batch')})
            UNION ALL
            SELECT 1 FROM job_items reserved_item
            JOIN jobs reserved_job ON reserved_job.id=reserved_item.job_id
            JOIN photos reserved_photo ON reserved_photo.id=reserved_item.photo_id
            WHERE reserved_photo.sha256=? AND reserved_job.kind='analysis_batch'
              AND reserved_item.status IN ('pending','running','retrying')
              AND reserved_job.status IN ('pending','preparing','running','pausing','retrying','paused','budget_exceeded')
            LIMIT 1""",  # noqa: S608 -- repository-owned SQL and aliases only.
        (content_sha256, content_sha256),
    ).fetchone():
        raise AnalysisReservationConflict("相同照片已有 Batch 付費證據或未完成操作；請從原批次／完整備份恢復結果，禁止再次付費送圖")


def reserved_analysis_sql() -> str:
    """Return a fixed SQL predicate referencing the outer photo alias ``p``.

    Paused jobs can resume, and an unknown remote submission may already be
    billable. Both retain ownership until explicitly resolved.
    """
    return f"""(
        EXISTS (
            SELECT 1 FROM job_items reserved_item
            JOIN jobs reserved_job ON reserved_job.id=reserved_item.job_id
            WHERE reserved_item.photo_id IN (
                SELECT reserved_photo.id FROM photos reserved_photo
                WHERE reserved_photo.id=p.id OR
                      (COALESCE(p.sha256,'')<>'' AND reserved_photo.sha256=p.sha256)
            )
              AND reserved_item.status IN ('pending','running','retrying')
              AND reserved_job.kind IN ('analysis','analysis_batch')
              AND (reserved_item.status='running' OR reserved_job.status IN (
                  'pending','preparing','running','pausing','retrying','paused','budget_exceeded'
              ))
        ) OR EXISTS (
            SELECT 1 FROM analysis_batch_items reserved_batch_item
            JOIN analysis_batches reserved_batch ON reserved_batch.id=reserved_batch_item.batch_id
            WHERE (reserved_batch_item.photo_id=p.id OR
                   (COALESCE(p.sha256,'')<>'' AND reserved_batch_item.content_sha256=p.sha256))
              AND {held_batch_sql()}
        )
    )"""  # noqa: S608 -- statuses are repository-owned constants.
