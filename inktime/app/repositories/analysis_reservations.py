"""Shared ownership predicate for queued realtime and remote Batch work."""

from inktime.app.repositories.analysis_batches import ACTIVE_BATCH_STATUSES


class AnalysisReservationConflict(ValueError):
    code = "ANALYSIS-RESERVATION-CONFLICT"


def reserved_analysis_sql() -> str:
    """Return a fixed SQL predicate referencing the outer photo alias ``p``.

    Paused jobs can resume, and an unknown remote submission may already be
    billable. Both retain ownership until explicitly resolved.
    """
    batch_statuses = ",".join(f"'{status}'" for status in sorted(ACTIVE_BATCH_STATUSES))
    return f"""(
        EXISTS (
            SELECT 1 FROM job_items reserved_item
            JOIN jobs reserved_job ON reserved_job.id=reserved_item.job_id
            WHERE reserved_item.photo_id=p.id
              AND reserved_item.status IN ('pending','running','retrying')
              AND reserved_job.kind IN ('analysis','analysis_batch')
              AND (reserved_item.status='running' OR reserved_job.status IN (
                  'pending','preparing','running','pausing','retrying','paused','budget_exceeded'
              ))
        ) OR EXISTS (
            SELECT 1 FROM analysis_batch_items reserved_batch_item
            JOIN analysis_batches reserved_batch ON reserved_batch.id=reserved_batch_item.batch_id
            WHERE reserved_batch_item.photo_id=p.id
              AND reserved_batch.status IN ({batch_statuses})
        )
    )"""  # noqa: S608 -- statuses are repository-owned constants.
