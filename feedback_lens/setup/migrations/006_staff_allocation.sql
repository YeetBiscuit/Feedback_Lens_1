-- Staff allocation notifications.
--
-- Assignment history remains in marker_assignments, workflow events, and the
-- append-only audit log.  This table only stores the small, user-facing
-- dashboard notification needed by the staff workflow.

CREATE TABLE IF NOT EXISTS user_notifications (
    notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    unit_offering_id INTEGER,
    assessment_plan_id INTEGER,
    action_url TEXT,
    read_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_user_notifications_user
        FOREIGN KEY (user_id) REFERENCES users(user_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_user_notifications_offering
        FOREIGN KEY (unit_offering_id)
        REFERENCES unit_offerings(unit_offering_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_user_notifications_plan
        FOREIGN KEY (assessment_plan_id)
        REFERENCES assessment_plans(assessment_plan_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_user_notifications_unread
    ON user_notifications(user_id, read_at, created_at);

CREATE INDEX IF NOT EXISTS idx_user_notifications_assessment
    ON user_notifications(assessment_plan_id, created_at);
