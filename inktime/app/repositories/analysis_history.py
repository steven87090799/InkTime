"""Presentation-only access to preserved model history, never ranking input."""


def historical_model_sql(alias: str = "a") -> str:
    """Recognize old model records by explicit provider and analysis stage.

    Aliases are fixed by callers; no request values are interpolated. Local or
    ambiguous records must not become model results merely because they have a
    caption or an old schema version.
    """
    return (
        f"({alias}.schema_version IN (1,2,3) AND {alias}.score_kind='legacy' "
        f"AND lower(trim(COALESCE({alias}.provider,''))) NOT IN "
        "('','inherited','local','local-prefilter','local-quality-v3','virtual-display-local') "
        f"AND lower(COALESCE({alias}.stage,'')) IN "
        "('single','stage_one','stage_two','cache','inherited'))"
    )


def display_analysis_order_sql(alias: str = "a") -> str:
    """Show current model results, then model history, then local evidence."""
    return (
        f"CASE WHEN {alias}.schema_version=4 AND {alias}.score_kind='semantic' THEN 0 "
        f"WHEN {historical_model_sql(alias)} THEN 1 "
        f"WHEN {alias}.score_kind='local_quality' THEN 2 ELSE 3 END,"
        f"{alias}.created_at DESC,{alias}.id DESC"
    )
