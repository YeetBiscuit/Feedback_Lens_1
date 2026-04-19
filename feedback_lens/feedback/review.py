import json
import sqlite3


def list_generation_runs(
    conn: sqlite3.Connection,
    limit: int = 10,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            gr.generation_id,
            gr.status,
            gr.started_at,
            gr.completed_at,
            gr.llm_provider,
            gr.llm_model,
            gr.top_k,
            gr.temperature,
            ss.student_identifier,
            a.assignment_name,
            u.unit_code,
            of.overall_grade_band
        FROM generation_runs AS gr
        JOIN student_submissions AS ss ON ss.submission_id = gr.submission_id
        JOIN assignments AS a ON a.assignment_id = gr.assignment_id
        JOIN units AS u ON u.unit_id = a.unit_id
        LEFT JOIN overall_feedback AS of ON of.generation_id = gr.generation_id
        ORDER BY gr.generation_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def fetch_generation_review(
    conn: sqlite3.Connection,
    generation_id: int,
) -> dict:
    run = conn.execute(
        """
        SELECT
            gr.*,
            ss.student_identifier,
            ss.original_file_path,
            a.assignment_name,
            a.assignment_type,
            a.description AS assignment_description,
            a.due_date,
            u.unit_code,
            u.unit_name,
            u.semester,
            u.year,
            r.version AS rubric_version,
            s.version AS spec_version
        FROM generation_runs AS gr
        JOIN student_submissions AS ss ON ss.submission_id = gr.submission_id
        JOIN assignments AS a ON a.assignment_id = gr.assignment_id
        JOIN units AS u ON u.unit_id = a.unit_id
        LEFT JOIN rubrics AS r ON r.rubric_id = gr.rubric_id
        LEFT JOIN assignment_specs AS s
            ON s.assignment_id = gr.assignment_id
           AND s.version = (
                SELECT MAX(version)
                FROM assignment_specs
                WHERE assignment_id = gr.assignment_id
           )
        WHERE gr.generation_id = ?
        """,
        (generation_id,),
    ).fetchone()
    if run is None:
        raise ValueError(f"No generation run found with generation_id={generation_id}")

    overall_feedback = conn.execute(
        """
        SELECT *
        FROM overall_feedback
        WHERE generation_id = ?
        """,
        (generation_id,),
    ).fetchone()

    criterion_feedback = conn.execute(
        """
        SELECT
            rc.criterion_id,
            rc.criterion_name,
            rc.criterion_description,
            rc.criterion_order,
            cf.strengths,
            cf.areas_for_improvement,
            cf.improvement_suggestion,
            cf.suggested_level,
            cf.evidence_summary
        FROM criterion_feedback AS cf
        JOIN rubric_criteria AS rc ON rc.criterion_id = cf.criterion_id
        WHERE cf.generation_id = ?
        ORDER BY rc.criterion_order, rc.criterion_id
        """,
        (generation_id,),
    ).fetchall()

    retrieval_records = conn.execute(
        """
        SELECT
            rr.retrieval_record_id,
            rr.query_text,
            rr.chunk_id,
            rr.rank_position,
            rr.similarity_score,
            rr.used_in_prompt,
            mc.chunk_index,
            mc.chunk_text,
            mc.page_number_start,
            mc.page_number_end,
            um.material_id,
            um.title AS material_title,
            um.material_type,
            um.week_number,
            um.source_file_path
        FROM retrieval_records AS rr
        JOIN material_chunks AS mc ON mc.chunk_id = rr.chunk_id
        JOIN unit_materials AS um ON um.material_id = mc.material_id
        WHERE rr.generation_id = ?
        ORDER BY rr.rank_position, rr.retrieval_record_id
        """,
        (generation_id,),
    ).fetchall()

    return {
        "run": run,
        "overall_feedback": overall_feedback,
        "criterion_feedback": criterion_feedback,
        "retrieval_records": retrieval_records,
    }


def parse_json_text_list(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []

    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return [raw_value]

    if not isinstance(parsed, list):
        return [str(parsed)]

    return [str(item) for item in parsed]
