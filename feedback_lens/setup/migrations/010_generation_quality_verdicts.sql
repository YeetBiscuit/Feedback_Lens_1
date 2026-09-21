-- One row per generation recording the quality gate's decision at the time it
-- was made. The verdict is persisted rather than recomputed so that changing
-- the threshold or the judge configuration later does not silently rewrite
-- past decisions.
CREATE TABLE IF NOT EXISTS generation_quality_verdicts (
    verdict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id INTEGER NOT NULL,
    verdict TEXT NOT NULL
        CHECK (verdict IN ('passed', 'passed_after_revision', 'needs_review', 'judge_failed')),
    threshold REAL NOT NULL,
    min_dimension_score REAL,
    attempts INTEGER NOT NULL DEFAULT 1,
    judge_config TEXT,
    note TEXT,
    decided_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_generation_quality_verdicts_generation
        FOREIGN KEY (generation_id) REFERENCES generation_runs(generation_id)
        ON DELETE CASCADE,
    CONSTRAINT uq_generation_quality_verdict
        UNIQUE (generation_id)
);

CREATE INDEX IF NOT EXISTS idx_generation_quality_verdicts_verdict
    ON generation_quality_verdicts(verdict);
