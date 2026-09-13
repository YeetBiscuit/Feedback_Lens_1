"""Aggregates for the admin judge quality view.

Reads from the tables the quality gate writes to. Nothing here calls a model
or recomputes a verdict - the stored decisions are the record.
"""

import sqlite3


DIMENSION_ORDER = ["grounding", "specificity", "actionability"]
SCORE_RANGE = [1, 2, 3, 4, 5]


def _score_distribution(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """
        SELECT judge_model, dimension, score, COUNT(*) AS n
        FROM judge_evaluations
        WHERE score IS NOT NULL
        GROUP BY judge_model, dimension, score
        """
    ).fetchall()

    distribution = {}
    for row in rows:
        model = row["judge_model"]
        dimension = row["dimension"]
        distribution.setdefault(model, {})
        distribution[model].setdefault(dimension, {s: 0 for s in SCORE_RANGE})
        distribution[model][dimension][row["score"]] = row["n"]

    for model, dimensions in distribution.items():
        for dimension in DIMENSION_ORDER:
            dimensions.setdefault(dimension, {s: 0 for s in SCORE_RANGE})

    return distribution


def _judge_summary(conn: sqlite3.Connection) -> list:
    """One row per model.

    Grouped by model, not by (model, provider). The same model logged under
    two provider strings is still one judge; splitting it would double-count
    judgements and leave both rows reading the same distribution.
    """
    rows = conn.execute(
        """
        SELECT judge_model,
               GROUP_CONCAT(DISTINCT judge_provider) AS providers,
               COUNT(*) AS judgements,
               MIN(score) AS lowest,
               MAX(score) AS highest,
               ROUND(AVG(score), 2) AS mean_score,
               SUM(CASE WHEN score = 5 THEN 1 ELSE 0 END) AS top_scores,
               MIN(date(evaluated_at)) AS first_used,
               MAX(date(evaluated_at)) AS last_used
        FROM judge_evaluations
        WHERE score IS NOT NULL
        GROUP BY judge_model
        ORDER BY last_used DESC, judge_model
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _dimension_breakdown(conn: sqlite3.Connection) -> list:
    rows = conn.execute(
        """
        SELECT judge_model,
               dimension,
               COUNT(*) AS judgements,
               ROUND(AVG(score), 2) AS mean_score,
               MIN(score) AS lowest,
               MAX(score) AS highest
        FROM judge_evaluations
        WHERE score IS NOT NULL
        GROUP BY judge_model, dimension
        ORDER BY judge_model, dimension
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _verdict_counts(conn: sqlite3.Connection) -> list:
    rows = conn.execute(
        """
        SELECT verdict, COUNT(*) AS n, ROUND(AVG(attempts), 2) AS mean_attempts
        FROM generation_quality_verdicts
        GROUP BY verdict
        ORDER BY n DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def get_judge_quality_overview(conn: sqlite3.Connection) -> dict:
    """Everything the admin AI Quality page needs, in one payload."""
    summary = _judge_summary(conn)

    return {
        "dimensions": DIMENSION_ORDER,
        "score_range": SCORE_RANGE,
        "judges": summary,
        "distribution": _score_distribution(conn),
        "by_dimension": _dimension_breakdown(conn),
        "verdicts": _verdict_counts(conn),
        "total_judgements": sum(j["judgements"] for j in summary),
    }
