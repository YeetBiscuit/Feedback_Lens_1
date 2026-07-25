from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

from feedback_lens.web.common import record_audit_event
from feedback_lens.web.config import (
    get_secret_key,
    get_web_settings,
)
from feedback_lens.web.errors import ApiError
from feedback_lens.web.jobs import enqueue_job
from feedback_lens.web.mail import get_email_sender, mail_is_configured
from feedback_lens.web.security import (
    can_administer_unit,
    hash_request_key,
)


GENERIC_ACCOUNT_MESSAGE = (
    "If the details match an eligible account, an email will be sent."
)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _token_signature(
    public_id: str,
    token_type: str,
    expires_at: str | None,
) -> str:
    payload = f"{public_id}|{token_type}|{expires_at or ''}"
    digest = hmac.new(
        get_secret_key().encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_token_value(row: sqlite3.Row | dict) -> str:
    public_id = str(row["public_id"])
    signature = _token_signature(
        public_id,
        str(row["token_type"]),
        row["expires_at"],
    )
    return f"{public_id}.{signature}"


def _token_hash(token_value: str) -> str:
    return hashlib.sha256(token_value.encode("utf-8")).hexdigest()


def _insert_token(
    conn: sqlite3.Connection,
    token_type: str,
    *,
    unit_offering_id: int | None,
    student_id: int | None,
    issued_by_user_id: int | None,
    expires_at: str | None,
) -> sqlite3.Row:
    public_id = secrets.token_urlsafe(24)
    signature = _token_signature(public_id, token_type, expires_at)
    token_value = f"{public_id}.{signature}"
    token_id = int(
        conn.execute(
            """
            INSERT INTO account_tokens
                (public_id, token_hash, token_type, unit_offering_id,
                 student_id, issued_by_user_id, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                public_id,
                _token_hash(token_value),
                token_type,
                unit_offering_id,
                student_id,
                issued_by_user_id,
                expires_at,
            ),
        ).lastrowid
    )
    return conn.execute(
        "SELECT * FROM account_tokens WHERE account_token_id = ?",
        (token_id,),
    ).fetchone()


def verify_token(
    conn: sqlite3.Connection,
    token_value: str,
    expected_type: str,
) -> sqlite3.Row | None:
    if not token_value or "." not in token_value:
        return None
    public_id = token_value.split(".", 1)[0]
    row = conn.execute(
        """
        SELECT *
        FROM account_tokens
        WHERE public_id = ?
          AND token_type = ?
        """,
        (public_id, expected_type),
    ).fetchone()
    if row is None:
        return None
    expected_value = build_token_value(row)
    if not hmac.compare_digest(expected_value, token_value):
        return None
    if not hmac.compare_digest(row["token_hash"], _token_hash(token_value)):
        return None
    if row["revoked_at"] is not None or row["consumed_at"] is not None:
        return None
    if row["expires_at"] is not None:
        expires = datetime.strptime(
            str(row["expires_at"]),
            "%Y-%m-%d %H:%M:%S",
        ).replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            return None
    return row


def create_or_rotate_unit_entry(
    conn: sqlite3.Connection,
    actor_user_id: int,
    unit_offering_id: int,
) -> dict:
    if not can_administer_unit(conn, actor_user_id, unit_offering_id):
        raise ApiError("unit_forbidden", "Not authorised.", 403)
    if not mail_is_configured():
        raise ApiError(
            "mail_not_configured",
            "Student activation is unavailable until outbound email is configured.",
            409,
        )
    conn.execute(
        """
        UPDATE account_tokens
        SET revoked_at = CURRENT_TIMESTAMP
        WHERE token_type = 'unit_activation_entry'
          AND unit_offering_id = ?
          AND revoked_at IS NULL
        """,
        (unit_offering_id,),
    )
    row = _insert_token(
        conn,
        "unit_activation_entry",
        unit_offering_id=unit_offering_id,
        student_id=None,
        issued_by_user_id=actor_user_id,
        expires_at=None,
    )
    record_audit_event(
        conn,
        "account.unit_activation_entry_rotated",
        "unit_offering",
        unit_offering_id,
        actor_user_id=actor_user_id,
    )
    conn.commit()
    token_value = build_token_value(row)
    return {
        "activation_url": (
            f"{get_web_settings().public_base_url}/activate/{token_value}"
        ),
        "public_id": row["public_id"],
    }


def current_unit_entry(
    conn: sqlite3.Connection,
    actor_user_id: int,
    unit_offering_id: int,
) -> dict:
    if not can_administer_unit(conn, actor_user_id, unit_offering_id):
        raise ApiError("unit_forbidden", "Not authorised.", 403)
    row = conn.execute(
        """
        SELECT *
        FROM account_tokens
        WHERE token_type = 'unit_activation_entry'
          AND unit_offering_id = ?
          AND revoked_at IS NULL
        ORDER BY account_token_id DESC
        LIMIT 1
        """,
        (unit_offering_id,),
    ).fetchone()
    return {
        "mail_configured": mail_is_configured(),
        "activation_url": (
            f"{get_web_settings().public_base_url}/activate/"
            f"{build_token_value(row)}"
            if row is not None
            else None
        ),
    }


def _rate_limited(
    conn: sqlite3.Connection,
    event_type: str,
    identity_hash: str,
    source_hash: str,
) -> bool:
    settings = get_web_settings()
    identity_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM audit_events
        WHERE event_type IN (
            'account.activation_requested',
            'account.activation_resent',
            'account.password_reset_requested'
        )
          AND datetime(created_at) >= datetime('now', '-1 hour')
          AND json_extract(metadata_json, '$.identity_hash') = ?
        """,
        (identity_hash,),
    ).fetchone()[0]
    source_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM audit_events
        WHERE event_type IN (
            'account.activation_requested',
            'account.activation_resent',
            'account.password_reset_requested'
        )
          AND datetime(created_at) >= datetime('now', '-1 hour')
          AND json_extract(metadata_json, '$.source_hash') = ?
        """,
        (source_hash,),
    ).fetchone()[0]
    return (
        int(identity_count) >= settings.identity_rate_limit_per_hour
        or int(source_count) >= settings.source_rate_limit_per_hour
    )


def _request_hashes(identity: str, source_address: str) -> tuple[str, str]:
    secret = get_secret_key()
    return (
        hash_request_key(identity.casefold(), secret),
        hash_request_key(source_address or "unknown", secret),
    )


def request_activation(
    conn: sqlite3.Connection,
    entry_token: str,
    student_identifier: str,
    email: str,
    source_address: str,
    *,
    resend: bool = False,
) -> str:
    normalized_identifier = student_identifier.strip()
    normalized_email = email.strip().casefold()
    identity_hash, source_hash = _request_hashes(
        f"{normalized_identifier}|{normalized_email}",
        source_address,
    )
    event_type = (
        "account.activation_resent"
        if resend
        else "account.activation_requested"
    )
    entry = verify_token(
        conn,
        entry_token,
        "unit_activation_entry",
    )
    limited = _rate_limited(
        conn,
        event_type,
        identity_hash,
        source_hash,
    )
    student = None
    if entry is not None and not limited and mail_is_configured():
        student = conn.execute(
            """
            SELECT student.*
            FROM student_enrolments AS enrolment
            JOIN students AS student
              ON student.student_id = enrolment.student_id
            WHERE enrolment.unit_offering_id = ?
              AND enrolment.status = 'active'
              AND student.institution_student_identifier = ?
              AND lower(student.institution_email) = lower(?)
            """,
            (
                entry["unit_offering_id"],
                normalized_identifier,
                normalized_email,
            ),
        ).fetchone()
    outcome = "not_matched"
    if limited:
        outcome = "rate_limited"
    elif student is not None and student["user_id"] is None:
        conn.execute(
            """
            UPDATE account_tokens
            SET revoked_at = CURRENT_TIMESTAMP
            WHERE token_type = 'student_activation'
              AND student_id = ?
              AND consumed_at IS NULL
              AND revoked_at IS NULL
            """,
            (student["student_id"],),
        )
        expires = _utc_text(
            datetime.now(timezone.utc)
            + timedelta(hours=get_web_settings().activation_ttl_hours)
        )
        token = _insert_token(
            conn,
            "student_activation",
            unit_offering_id=int(entry["unit_offering_id"]),
            student_id=int(student["student_id"]),
            issued_by_user_id=None,
            expires_at=expires,
        )
        enqueue_job(
            conn,
            "account_email",
            unit_offering_id=int(entry["unit_offering_id"]),
            account_token_id=int(token["account_token_id"]),
            payload={"message_type": "student_activation"},
        )
        outcome = "queued"
    elif student is not None:
        outcome = "already_active"
    record_audit_event(
        conn,
        event_type,
        "student_account_request",
        student["student_id"] if student is not None else None,
        metadata={
            "identity_hash": identity_hash,
            "source_hash": source_hash,
            "outcome": outcome,
            "unit_offering_id": (
                int(entry["unit_offering_id"]) if entry is not None else None
            ),
        },
    )
    conn.commit()
    return GENERIC_ACCOUNT_MESSAGE


def complete_activation(
    conn: sqlite3.Connection,
    token_value: str,
    password: str,
) -> int:
    if len(password) < 12:
        raise ApiError(
            "password_too_short",
            "Password must contain at least 12 characters.",
            422,
        )
    token = verify_token(conn, token_value, "student_activation")
    if token is None:
        raise ApiError(
            "token_invalid",
            "This activation link is invalid or has expired.",
            409,
        )
    student = conn.execute(
        "SELECT * FROM students WHERE student_id = ?",
        (token["student_id"],),
    ).fetchone()
    if student is None or not student["institution_email"]:
        raise ApiError("student_not_found", "Student record is incomplete.", 409)
    if student["user_id"] is None:
        user_id = int(
            conn.execute(
                """
                INSERT INTO users
                    (email, password_hash, role, display_name,
                     student_identifier, session_version)
                VALUES (?, ?, 'student', ?, ?, 1)
                """,
                (
                    student["institution_email"],
                    generate_password_hash(password),
                    student["full_name"],
                    student["institution_student_identifier"],
                ),
            ).lastrowid
        )
        conn.execute(
            "UPDATE students SET user_id = ? WHERE student_id = ?",
            (user_id, student["student_id"]),
        )
    else:
        user_id = int(student["user_id"])
        conn.execute(
            """
            UPDATE users
            SET password_hash = ?,
                session_version = session_version + 1
            WHERE user_id = ?
            """,
            (generate_password_hash(password), user_id),
        )
    conn.execute(
        """
        UPDATE account_tokens
        SET consumed_at = CURRENT_TIMESTAMP
        WHERE account_token_id = ?
        """,
        (token["account_token_id"],),
    )
    record_audit_event(
        conn,
        "account.activated",
        "student",
        student["student_id"],
        actor_user_id=user_id,
    )
    conn.commit()
    return user_id


def request_password_reset(
    conn: sqlite3.Connection,
    email: str,
    source_address: str,
) -> str:
    normalized_email = email.strip().casefold()
    identity_hash, source_hash = _request_hashes(
        normalized_email,
        source_address,
    )
    event_type = "account.password_reset_requested"
    limited = _rate_limited(
        conn,
        event_type,
        identity_hash,
        source_hash,
    )
    student = None
    if not limited and mail_is_configured():
        student = conn.execute(
            """
            SELECT student.*
            FROM students AS student
            JOIN users AS user ON user.user_id = student.user_id
            WHERE lower(student.institution_email) = lower(?)
              AND user.role = 'student'
            """,
            (normalized_email,),
        ).fetchone()
    outcome = "rate_limited" if limited else "not_matched"
    if student is not None:
        conn.execute(
            """
            UPDATE account_tokens
            SET revoked_at = CURRENT_TIMESTAMP
            WHERE token_type = 'password_reset'
              AND student_id = ?
              AND consumed_at IS NULL
              AND revoked_at IS NULL
            """,
            (student["student_id"],),
        )
        expires = _utc_text(
            datetime.now(timezone.utc)
            + timedelta(
                minutes=get_web_settings().password_reset_ttl_minutes
            )
        )
        token = _insert_token(
            conn,
            "password_reset",
            unit_offering_id=None,
            student_id=int(student["student_id"]),
            issued_by_user_id=None,
            expires_at=expires,
        )
        enqueue_job(
            conn,
            "account_email",
            account_token_id=int(token["account_token_id"]),
            payload={"message_type": "password_reset"},
        )
        outcome = "queued"
    record_audit_event(
        conn,
        event_type,
        "student_account_request",
        student["student_id"] if student is not None else None,
        metadata={
            "identity_hash": identity_hash,
            "source_hash": source_hash,
            "outcome": outcome,
        },
    )
    conn.commit()
    return GENERIC_ACCOUNT_MESSAGE


def complete_password_reset(
    conn: sqlite3.Connection,
    token_value: str,
    password: str,
) -> int:
    if len(password) < 12:
        raise ApiError(
            "password_too_short",
            "Password must contain at least 12 characters.",
            422,
        )
    token = verify_token(conn, token_value, "password_reset")
    if token is None:
        raise ApiError(
            "token_invalid",
            "This password reset link is invalid or has expired.",
            409,
        )
    student = conn.execute(
        "SELECT * FROM students WHERE student_id = ?",
        (token["student_id"],),
    ).fetchone()
    if student is None or student["user_id"] is None:
        raise ApiError("account_not_found", "Account not found.", 409)
    user_id = int(student["user_id"])
    conn.execute(
        """
        UPDATE users
        SET password_hash = ?,
            session_version = session_version + 1
        WHERE user_id = ?
        """,
        (generate_password_hash(password), user_id),
    )
    conn.execute(
        """
        UPDATE account_tokens
        SET consumed_at = CURRENT_TIMESTAMP
        WHERE account_token_id = ?
        """,
        (token["account_token_id"],),
    )
    conn.execute(
        """
        UPDATE account_tokens
        SET revoked_at = CURRENT_TIMESTAMP
        WHERE student_id = ?
          AND account_token_id != ?
          AND consumed_at IS NULL
          AND revoked_at IS NULL
          AND token_type IN ('student_activation', 'password_reset')
        """,
        (student["student_id"], token["account_token_id"]),
    )
    record_audit_event(
        conn,
        "account.password_reset_completed",
        "student",
        student["student_id"],
        actor_user_id=user_id,
    )
    conn.commit()
    return user_id


def handle_account_email_job(
    conn: sqlite3.Connection,
    job: sqlite3.Row,
) -> dict:
    token = conn.execute(
        """
        SELECT
            token.*,
            student.institution_email,
            student.full_name
        FROM account_tokens AS token
        JOIN students AS student ON student.student_id = token.student_id
        WHERE token.account_token_id = ?
        """,
        (job["account_token_id"],),
    ).fetchone()
    if token is None or not token["institution_email"]:
        raise RuntimeError("The account email target no longer exists.")
    if token["revoked_at"] is not None or token["consumed_at"] is not None:
        return {"status": "cancelled", "reason": "token_inactive"}
    token_value = build_token_value(token)
    settings = get_web_settings()
    if token["token_type"] == "student_activation":
        subject = "Activate your Feedback Lens account"
        link = f"{settings.public_base_url}/account/activate/{token_value}"
        action = "activate your account"
    elif token["token_type"] == "password_reset":
        subject = "Reset your Feedback Lens password"
        link = f"{settings.public_base_url}/account/reset/{token_value}"
        action = "reset your password"
    else:
        raise RuntimeError("Unsupported account email token.")
    body = (
        f"Hello {token['full_name'] or 'student'},\n\n"
        f"Use the link below to {action}:\n\n{link}\n\n"
        "If you did not request this, you can ignore this email."
    )
    provider_message_id = get_email_sender().send(
        str(token["institution_email"]),
        subject,
        body,
    )
    record_audit_event(
        conn,
        "account.email_sent",
        "account_token",
        token["account_token_id"],
        metadata={
            "message_type": token["token_type"],
            "provider_message_id": provider_message_id,
        },
    )
    conn.commit()
    return {
        "status": "sent",
        "provider_message_id": provider_message_id,
    }
