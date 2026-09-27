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


def _generator_summary(conn: sqlite3.Connection) -> list:
    rows = conn.execute(
        """
        SELECT COALESCE(gr.llm_model, 'unknown') AS llm_model,
               COUNT(*) AS runs,
               SUM(CASE WHEN gr.status = 'completed' THEN 1 ELSE 0 END) AS completed,
               COUNT(v.generation_id) AS gated,
               SUM(CASE WHEN v.verdict LIKE 'passed%' THEN 1 ELSE 0 END) AS passed,
               MIN(date(gr.started_at)) AS first_used,
               MAX(date(gr.started_at)) AS last_used
        FROM generation_runs gr
        LEFT JOIN generation_quality_verdicts v ON v.generation_id = gr.generation_id
        GROUP BY COALESCE(gr.llm_model, 'unknown')
        ORDER BY last_used DESC
        """
    ).fetchall()
    summary = [dict(row) for row in rows]

    score_rows = conn.execute(
        """
        SELECT COALESCE(gr.llm_model, 'unknown') AS llm_model,
               je.dimension,
               ROUND(AVG(je.score), 2) AS mean_score
        FROM judge_evaluations je
        JOIN generation_runs gr ON gr.generation_id = je.generation_id
        WHERE je.score IS NOT NULL
        GROUP BY COALESCE(gr.llm_model, 'unknown'), je.dimension
        """
    ).fetchall()
    scores = {}
    for row in score_rows:
        scores.setdefault(row["llm_model"], {})[row["dimension"]] = row["mean_score"]

    for item in summary:
        item["mean_scores"] = scores.get(item["llm_model"], {})
    return summary


def _judge_generator_matrix(conn: sqlite3.Connection) -> list:
    rows = conn.execute(
        """
        SELECT je.judge_model,
               COALESCE(gr.llm_model, 'unknown') AS generator_model,
               COUNT(*) AS n,
               ROUND(AVG(je.score), 2) AS mean_score
        FROM judge_evaluations je
        JOIN generation_runs gr ON gr.generation_id = je.generation_id
        WHERE je.score IS NOT NULL
        GROUP BY je.judge_model, COALESCE(gr.llm_model, 'unknown')
        ORDER BY MIN(gr.started_at), je.judge_model
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
        "generators": _generator_summary(conn),
        "judge_generator": _judge_generator_matrix(conn),
        "total_judgements": sum(j["judgements"] for j in summary),
    }
