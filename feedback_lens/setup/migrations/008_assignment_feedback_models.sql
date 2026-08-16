-- Assignment-level default feedback models and structured provider errors.
--
-- The selected model is a default for future generation requests. Existing
-- feedback keeps the provider/model snapshot already stored on generation_runs.

ALTER TABLE assessment_plans
ADD COLUMN default_llm_provider TEXT NOT NULL DEFAULT 'deepseek';

ALTER TABLE assessment_plans
ADD COLUMN default_llm_model TEXT NOT NULL DEFAULT 'deepseek-v4-pro';

ALTER TABLE assessment_plans
ADD COLUMN feedback_model_updated_by_user_id INTEGER
    REFERENCES users(user_id) ON DELETE SET NULL;

ALTER TABLE assessment_plans
ADD COLUMN feedback_model_updated_at TEXT;

ALTER TABLE generation_runs
ADD COLUMN provider_error_code TEXT;

ALTER TABLE generation_runs
ADD COLUMN provider_http_status INTEGER;

ALTER TABLE generation_runs
ADD COLUMN provider_request_id TEXT;

CREATE INDEX idx_generation_runs_assignment_started
    ON generation_runs(assignment_id, started_at, generation_id);
