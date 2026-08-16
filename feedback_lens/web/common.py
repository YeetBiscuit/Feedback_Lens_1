from __future__ import annotations

import json
import sqlite3


def student_import_is_ready(
    conn: sqlite3.Connection,
    unit_offering_id: int,
) -> bool:
    return (
        conn.execute(
            """
            SELECT 1
            FROM roster_imports
            WHERE unit_offering_id = ?
              AND status IN ('imported', 'partially_imported')
            UNION ALL
            SELECT 1
            FROM tutorial_group_imports AS import_record
            JOIN tutorial_group_import_rows AS import_row
              ON import_row.tutorial_group_import_id =
                 import_record.tutorial_group_import_id
            WHERE import_record.unit_offering_id = ?
              AND import_record.status = 'applied'
              AND json_extract(
                    import_row.raw_data_json,
                    '$._registration.available'
                  ) = 1
            LIMIT 1
            """,
            (unit_offering_id, unit_offering_id),
        ).fetchone()
        is not None
    )


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
