from __future__ import annotations

import json
import sqlite3


def record_audit_event(
    conn: sqlite3.Connection,
    event_type: str,
    entity_type: str,
    entity_id: object | None,
    *,
    actor_user_id: int | None = None,
    metadata: dict | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO audit_events
            (actor_user_id, event_type, entity_type, entity_id,
             metadata_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            actor_user_id,
            event_type,
            entity_type,
            None if entity_id is None else str(entity_id),
            json.dumps(metadata or {}, ensure_ascii=False),
        ),
    )
    return int(cursor.lastrowid)
