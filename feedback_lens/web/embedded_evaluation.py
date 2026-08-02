from __future__ import annotations

import hashlib
import hmac
import sqlite3


EVALUATION_QUESTIONS = {
    "student": (
        "Overall, how useful was this feedback in helping you understand "
        "the strengths of your work and how to improve it?"
    ),
    "educator": (
        "Overall, how useful was the AI-generated feedback in supporting "
        "your review of this submission?"
    ),
}

RATING_ANCHORS = {
    "minimum": "Not at all useful",
    "maximum": "Extremely useful",
}

OPTIONAL_COMMENT_PROMPT = (
    "What was most useful, unclear, inaccurate, or missing?"
)

VOLUNTARY_NOTICES = {
    "student": (
        "This research evaluation is optional. Choosing not to respond "
        "will not affect your mark, feedback, or access to the system."
    ),
    "educator": (
        "This research evaluation is optional. Choosing not to respond "
        "will not affect your role or access to the system."
    ),
}

MAX_COMMENT_LENGTH = 1000


def pseudonymous_rater_key(user_id: int, secret_key: str | bytes) -> str:
    key = (
        secret_key
        if isinstance(secret_key, bytes)
        else str(secret_key).encode("utf-8")
    )
    return hmac.new(
        key,
        f"feedback-evaluation-user:{int(user_id)}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def validate_evaluation_payload(data: dict) -> tuple[int, str | None]:
    if data.get("consent_confirmed") is not True:
        raise ValueError(
            "Consent must be confirmed before the optional evaluation "
            "can be submitted."
        )

    try:
        rating = int(data.get("rating_usefulness"))
    except (TypeError, ValueError) as err:
        raise ValueError("A usefulness rating from 1 to 5 is required.") from err
    if rating < 1 or rating > 5:
        raise ValueError("The usefulness rating must be between 1 and 5.")

    raw_comment = data.get("comment")
    comment = str(raw_comment).strip() if raw_comment is not None else ""
    if len(comment) > MAX_COMMENT_LENGTH:
        raise ValueError(
            f"The optional comment must be {MAX_COMMENT_LENGTH} characters "
            "or fewer."
        )
    return rating, comment or None


def fetch_embedded_evaluation(
    conn: sqlite3.Connection,
    generation_id: int,
    participant_role: str,
    rater_key_hash: str,
) -> dict | None:
    row = conn.execute(
        """
        SELECT
            evaluation_id,
            generation_id,
            participant_role,
            rating_usefulness,
            comment,
            consent_confirmed,
            consented_at,
            created_at,
            updated_at
        FROM embedded_feedback_evaluations
        WHERE generation_id = ?
          AND participant_role = ?
          AND rater_key_hash = ?
        """,
        (generation_id, participant_role, rater_key_hash),
    ).fetchone()
    return dict(row) if row is not None else None


def save_embedded_evaluation(
    conn: sqlite3.Connection,
    generation_id: int,
    participant_role: str,
    rater_key_hash: str,
    *,
    rating_usefulness: int,
    comment: str | None,
) -> dict:
    if participant_role not in EVALUATION_QUESTIONS:
        raise ValueError("Unsupported participant role.")

    conn.execute(
        """
        INSERT INTO embedded_feedback_evaluations
            (
                generation_id,
                participant_role,
                rater_key_hash,
                rating_usefulness,
                comment,
                consent_confirmed
            )
        VALUES (?, ?, ?, ?, ?, 1)
        ON CONFLICT(
            generation_id,
            participant_role,
            rater_key_hash
        ) DO UPDATE SET
            rating_usefulness = excluded.rating_usefulness,
            comment = excluded.comment,
            consent_confirmed = 1,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            generation_id,
            participant_role,
            rater_key_hash,
            rating_usefulness,
            comment,
        ),
    )
    evaluation = fetch_embedded_evaluation(
        conn,
        generation_id,
        participant_role,
        rater_key_hash,
    )
    if evaluation is None:
        raise RuntimeError("The embedded evaluation could not be saved.")
    return evaluation


def delete_embedded_evaluation(
    conn: sqlite3.Connection,
    generation_id: int,
    participant_role: str,
    rater_key_hash: str,
) -> bool:
    cursor = conn.execute(
        """
        DELETE FROM embedded_feedback_evaluations
        WHERE generation_id = ?
          AND participant_role = ?
          AND rater_key_hash = ?
        """,
        (generation_id, participant_role, rater_key_hash),
    )
    return cursor.rowcount > 0
