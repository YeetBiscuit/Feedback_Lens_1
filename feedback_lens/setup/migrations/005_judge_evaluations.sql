CREATE TABLE IF NOT EXISTS judge_evaluations (
    evaluation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id INTEGER NOT NULL,
    judge_provider TEXT NOT NULL,
    judge_model TEXT NOT NULL,
    dimension TEXT NOT NULL
        CHECK (dimension IN ('grounding', 'specificity', 'actionability')),
    score INTEGER
        CHECK (score IS NULL OR score BETWEEN 1 AND 5),
    reason TEXT,
    evidence TEXT,
    defects_json TEXT,
    missing_evidence_json TEXT,
    attempt_number INTEGER NOT NULL DEFAULT 1,
    accepted INTEGER NOT NULL DEFAULT 0
        CHECK (accepted IN (0, 1)),
    gate_source TEXT NOT NULL DEFAULT 'pipeline',
    evaluated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_judge_evaluations_generation
        FOREIGN KEY (generation_id) REFERENCES generation_runs(generation_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_judge_evaluations_generation
    ON judge_evaluations(generation_id);

CREATE INDEX IF NOT EXISTS idx_judge_evaluations_provider
    ON judge_evaluations(judge_provider);

CREATE INDEX IF NOT EXISTS idx_judge_evaluations_accepted
    ON judge_evaluations(generation_id, accepted);