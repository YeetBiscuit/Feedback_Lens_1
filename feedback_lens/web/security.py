from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from functools import wraps

from flask import jsonify, request, session

from feedback_lens.db.connection import connect_db


def fetch_authenticated_user(
    conn: sqlite3.Connection,
) -> sqlite3.Row | None:
    user_id = session.get("user_id")
    session_version = session.get("session_version")
    if user_id is None or session_version is None:
        return None
    row = conn.execute(
        """
        SELECT
            user_id,
            email,
            role,
            display_name,
            tutor_id,
            session_version,
            account_status
        FROM users
        WHERE user_id = ?
        """,
        (user_id,),
    ).fetchone()
    if (
        row is None
        or row["account_status"] != "active"
        or int(row["session_version"]) != int(session_version)
    ):
        session.clear()
        return None
    return row


def is_chief_admin(conn: sqlite3.Connection, user_id: int) -> bool:
    return (
        conn.execute(
            """
            SELECT 1
            FROM organization_role_assignments
            WHERE user_id = ?
              AND role = 'chief_admin'
              AND active = 1
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        is not None
    )


def can_access_admin_workspace(
    conn: sqlite3.Connection,
    user_id: int,
) -> bool:
    if is_chief_admin(conn, user_id):
        return True
    return (
        conn.execute(
            """
            SELECT 1
            FROM unit_role_assignments
            WHERE user_id = ?
              AND role = 'unit_admin'
              AND active = 1
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        is not None
    )


def has_unit_role(
    conn: sqlite3.Connection,
    user_id: int,
    unit_offering_id: int,
    roles: tuple[str, ...],
) -> bool:
    placeholders = ", ".join("?" for _ in roles)
    return (
        conn.execute(
            f"""
            SELECT 1
            FROM unit_role_assignments
            WHERE user_id = ?
              AND unit_offering_id = ?
              AND role IN ({placeholders})
              AND active = 1
            LIMIT 1
            """,
            (user_id, unit_offering_id, *roles),
        ).fetchone()
        is not None
    )


def can_administer_unit(
    conn: sqlite3.Connection,
    user_id: int,
    unit_offering_id: int,
) -> bool:
    chief_for_organization = (
        conn.execute(
            """
            SELECT 1
            FROM unit_offerings AS offering
            JOIN courses AS course ON course.course_id = offering.course_id
            JOIN organization_role_assignments AS role
              ON role.organization_id = course.organization_id
            WHERE offering.unit_offering_id = ?
              AND role.user_id = ?
              AND role.role = 'chief_admin'
              AND role.active = 1
            LIMIT 1
            """,
            (unit_offering_id, user_id),
        ).fetchone()
        is not None
    )
    return chief_for_organization or has_unit_role(
        conn,
        user_id,
        unit_offering_id,
        ("unit_admin",),
    )


def csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def require_csrf(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        expected = session.get("_csrf_token")
        provided = (
            request.headers.get("X-CSRF-Token")
            or request.form.get("_csrf_token")
        )
        if (
            not expected
            or not provided
            or not hmac.compare_digest(str(expected), str(provided))
        ):
            return (
                jsonify(
                    {
                        "error": {
                            "code": "csrf_failed",
                            "message": "The form expired. Refresh and try again.",
                        }
                    }
                ),
                403,
            )
        return view(*args, **kwargs)

    return wrapped


def require_json_user(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        with connect_db() as conn:
            user = fetch_authenticated_user(conn)
        if user is None:
            return (
                jsonify(
                    {
                        "error": {
                            "code": "authentication_required",
                            "message": "Please log in.",
                        }
                    }
                ),
                401,
            )
        return view(*args, **kwargs)

    return wrapped


def hash_request_key(value: str, secret_key: str) -> str:
    return hmac.new(
        secret_key.encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
