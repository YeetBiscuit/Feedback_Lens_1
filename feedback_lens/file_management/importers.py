import json
import sqlite3
from pathlib import Path

from feedback_lens.db.connection import get_next_version
from feedback_lens.file_management.document_io import extract_document
from feedback_lens.file_management.parsers.rubric_parser import (
    extract_rubric_criteria,
    extract_rubric_tables,
)
from feedback_lens.file_management.parsers.spec_cues import build_assignment_spec_cues


def _ensure_assignment_exists(conn: sqlite3.Connection, assignment_id: int) -> None:
    row = conn.execute(
        "SELECT assignment_id FROM assignments WHERE assignment_id = ?",
        (assignment_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"No assignment found with assignment_id={assignment_id}")


def import_assignment_spec(
    conn: sqlite3.Connection,
    assignment_id: int,
    file_path: str | Path,
) -> dict:
    _ensure_assignment_exists(conn, assignment_id)
    document = extract_document(file_path)
    retrieval_cues = build_assignment_spec_cues(
        document["pages"],
        document["cleaned_text"],
    )
    version = get_next_version(conn, "assignment_specs", "assignment_id", assignment_id)

    cur = conn.execute(
        """
        INSERT INTO assignment_specs
            (assignment_id, version, source_file_path, raw_text, cleaned_text, retrieval_cues_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            assignment_id,
            version,
            document["file_path"],
            document["raw_text"],
            document["cleaned_text"],
            json.dumps(retrieval_cues, ensure_ascii=False, indent=2),
        ),
    )
    conn.commit()

    return {
        "spec_id": cur.lastrowid,
        "assignment_id": assignment_id,
        "version": version,
        "file_name": document["file_name"],
        "cue_count": len(retrieval_cues),
    }


def import_rubric(
    conn: sqlite3.Connection,
    assignment_id: int,
    file_path: str | Path,
) -> dict:
    _ensure_assignment_exists(conn, assignment_id)
    document = extract_document(file_path)
    tables = extract_rubric_tables(file_path)
    criteria = extract_rubric_criteria(tables)
    if not criteria:
        raise ValueError(
            "No rubric criteria could be extracted from the rubric PDF tables."
        )

    version = get_next_version(conn, "rubrics", "assignment_id", assignment_id)
    structured_rubric_json = json.dumps(
        {
            "file_name": document["file_name"],
            "page_count": document["page_count"],
            "tables": tables,
            "criteria_preview": criteria,
        },
        ensure_ascii=False,
        indent=2,
    )

    cur = conn.execute(
        """
        INSERT INTO rubrics
            (assignment_id, version, source_file_path, raw_text, cleaned_text, structured_rubric_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            assignment_id,
            version,
            document["file_path"],
            document["raw_text"],
            document["cleaned_text"],
            structured_rubric_json,
        ),
    )
    rubric_id = cur.lastrowid

    for criterion in criteria:
        conn.execute(
            """
            INSERT INTO rubric_criteria
                (rubric_id, criterion_name, criterion_description, criterion_order, performance_levels_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                rubric_id,
                criterion["criterion_name"],
                criterion["criterion_description"],
                criterion["criterion_order"],
                json.dumps(criterion["performance_levels"], ensure_ascii=False)
                if criterion["performance_levels"] is not None
                else None,
            ),
        )

    conn.commit()
    return {
        "rubric_id": rubric_id,
        "assignment_id": assignment_id,
        "version": version,
        "file_name": document["file_name"],
        "table_count": len(tables),
        "criteria_count": len(criteria),
    }


def import_student_submission(
    conn: sqlite3.Connection,
    assignment_id: int,
    student_identifier: str,
    file_path: str | Path,
    submitted_at: str | None = None,
) -> dict:
    _ensure_assignment_exists(conn, assignment_id)
    document = extract_document(file_path)
    version = get_next_version(
        conn,
        "student_submissions",
        "assignment_id",
        assignment_id,
        partition_column="student_identifier",
        partition_value=student_identifier,
    )

    cur = conn.execute(
        """
        INSERT INTO student_submissions
            (assignment_id, student_identifier, original_file_path,
             raw_text, cleaned_text, submitted_at, version)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            assignment_id,
            student_identifier,
            document["file_path"],
            document["raw_text"],
            document["cleaned_text"],
            submitted_at,
            version,
        ),
    )
    conn.commit()

    return {
        "submission_id": cur.lastrowid,
        "assignment_id": assignment_id,
        "student_identifier": student_identifier,
        "version": version,
        "file_name": document["file_name"],
    }
