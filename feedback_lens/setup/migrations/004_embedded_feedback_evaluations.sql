CREATE TABLE IF NOT EXISTS embedded_feedback_evaluations (
    evaluation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id INTEGER NOT NULL,
    participant_role TEXT NOT NULL
        CHECK (participant_role IN ('student', 'educator')),
    rater_key_hash TEXT NOT NULL,
    rating_usefulness INTEGER NOT NULL
        CHECK (rating_usefulness BETWEEN 1 AND 5),
    comment TEXT,
    consent_confirmed INTEGER NOT NULL DEFAULT 1
        CHECK (consent_confirmed = 1),
    consented_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_embedded_feedback_evaluations_generation
        FOREIGN KEY (generation_id) REFERENCES generation_runs(generation_id)
        ON DELETE CASCADE,
    CONSTRAINT uq_embedded_feedback_evaluation_role
        UNIQUE (generation_id, participant_role, rater_key_hash)
);

CREATE INDEX IF NOT EXISTS idx_embedded_feedback_evaluations_generation
    ON embedded_feedback_evaluations(generation_id);

CREATE INDEX IF NOT EXISTS idx_embedded_feedback_evaluations_role
    ON embedded_feedback_evaluations(participant_role);

CREATE INDEX IF NOT EXISTS idx_embedded_feedback_evaluations_rater
    ON embedded_feedback_evaluations(rater_key_hash);
