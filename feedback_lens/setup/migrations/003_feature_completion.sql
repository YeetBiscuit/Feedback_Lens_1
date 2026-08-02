-- Feedback Lens final bounded database supplement.
--
-- V2 remains the domain model.  This migration only persists the states
-- required by the frozen student-account and administrative-upload workflows.

-- =========================
-- CASE-INSENSITIVE IDENTITY
-- =========================
CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email_nocase
    ON users(lower(trim(email)));

CREATE UNIQUE INDEX IF NOT EXISTS uq_students_institution_email
    ON students(institution_email COLLATE NOCASE)
    WHERE institution_email IS NOT NULL
      AND trim(institution_email) != '';

CREATE INDEX IF NOT EXISTS idx_students_user
    ON students(user_id);

CREATE TRIGGER IF NOT EXISTS trg_students_linked_email_insert
BEFORE INSERT ON students
FOR EACH ROW
WHEN NEW.user_id IS NOT NULL
 AND (
     NEW.institution_email IS NULL
     OR NOT EXISTS (
         SELECT 1
         FROM users
         WHERE user_id = NEW.user_id
           AND lower(trim(email)) =
               lower(trim(NEW.institution_email))
     )
 )
BEGIN
    SELECT RAISE(
        ABORT,
        'linked student and user must have the same email'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_students_linked_email_update
BEFORE UPDATE OF user_id, institution_email ON students
FOR EACH ROW
WHEN NEW.user_id IS NOT NULL
 AND (
     NEW.institution_email IS NULL
     OR NOT EXISTS (
         SELECT 1
         FROM users
         WHERE user_id = NEW.user_id
           AND lower(trim(email)) =
               lower(trim(NEW.institution_email))
     )
 )
BEGIN
    SELECT RAISE(
        ABORT,
        'linked student and user must have the same email'
    );
END;

-- =========================
-- ROSTER PREVIEW AND COMMIT
-- =========================
CREATE TABLE IF NOT EXISTS roster_imports (
    roster_import_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_offering_id INTEGER NOT NULL,
    uploaded_by_user_id INTEGER NOT NULL,
    source_file_name TEXT NOT NULL,
    source_file_path TEXT NOT NULL,
    source_content_hash TEXT NOT NULL,
    column_mapping_json TEXT,
    status TEXT NOT NULL DEFAULT 'uploaded'
        CHECK (
            status IN (
                'uploaded', 'previewed', 'committing',
                'imported', 'partially_imported', 'failed'
            )
        ),
    total_row_count INTEGER NOT NULL DEFAULT 0
        CHECK (total_row_count >= 0),
    valid_row_count INTEGER NOT NULL DEFAULT 0
        CHECK (valid_row_count >= 0),
    invalid_row_count INTEGER NOT NULL DEFAULT 0
        CHECK (invalid_row_count >= 0),
    new_student_count INTEGER NOT NULL DEFAULT 0
        CHECK (new_student_count >= 0),
    updated_student_count INTEGER NOT NULL DEFAULT 0
        CHECK (updated_student_count >= 0),
    withdrawal_candidate_count INTEGER NOT NULL DEFAULT 0
        CHECK (withdrawal_candidate_count >= 0),
    withdrawn_student_count INTEGER NOT NULL DEFAULT 0
        CHECK (withdrawn_student_count >= 0),
    error_message TEXT,
    uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    previewed_at TEXT,
    committed_at TEXT,
    CHECK (
        column_mapping_json IS NULL
        OR json_valid(column_mapping_json)
    ),
    CONSTRAINT fk_roster_imports_offering
        FOREIGN KEY (unit_offering_id)
        REFERENCES unit_offerings(unit_offering_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_roster_imports_uploaded_by
        FOREIGN KEY (uploaded_by_user_id) REFERENCES users(user_id)
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_roster_import_source
    ON roster_imports(unit_offering_id, source_content_hash);
CREATE INDEX IF NOT EXISTS idx_roster_imports_offering
    ON roster_imports(unit_offering_id, uploaded_at);
CREATE INDEX IF NOT EXISTS idx_roster_imports_status
    ON roster_imports(status);

CREATE TABLE IF NOT EXISTS roster_import_rows (
    roster_import_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    roster_import_id INTEGER NOT NULL,
    source_row_number INTEGER,
    raw_data_json TEXT,
    institution_student_identifier TEXT COLLATE NOCASE,
    full_name TEXT,
    institution_email TEXT COLLATE NOCASE,
    student_id INTEGER,
    action TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            action IN (
                'pending', 'create', 'update', 'unchanged',
                'invalid', 'withdrawal_candidate', 'withdrawn',
                'skipped'
            )
        ),
    validation_error TEXT,
    applied_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        source_row_number IS NULL
        OR source_row_number > 0
    ),
    CHECK (
        raw_data_json IS NULL
        OR json_valid(raw_data_json)
    ),
    CONSTRAINT fk_roster_rows_import
        FOREIGN KEY (roster_import_id)
        REFERENCES roster_imports(roster_import_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_roster_rows_student
        FOREIGN KEY (student_id) REFERENCES students(student_id)
        ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_roster_import_source_row
    ON roster_import_rows(roster_import_id, source_row_number)
    WHERE source_row_number IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_roster_import_rows_action
    ON roster_import_rows(roster_import_id, action);
CREATE INDEX IF NOT EXISTS idx_roster_import_rows_identifier
    ON roster_import_rows(
        roster_import_id,
        institution_student_identifier
    );

-- =========================
-- ACCOUNT ENTRY AND ONE-TIME TOKENS
-- =========================
CREATE TABLE IF NOT EXISTS account_tokens (
    account_token_id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL UNIQUE,
    token_type TEXT NOT NULL
        CHECK (
            token_type IN (
                'unit_activation_entry',
                'student_activation',
                'password_reset'
            )
        ),
    unit_offering_id INTEGER,
    student_id INTEGER,
    issued_by_user_id INTEGER,
    expires_at TEXT,
    consumed_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (
            token_type = 'unit_activation_entry'
            AND unit_offering_id IS NOT NULL
            AND student_id IS NULL
            AND expires_at IS NULL
            AND consumed_at IS NULL
        )
        OR
        (
            token_type = 'student_activation'
            AND unit_offering_id IS NOT NULL
            AND student_id IS NOT NULL
            AND expires_at IS NOT NULL
        )
        OR
        (
            token_type = 'password_reset'
            AND unit_offering_id IS NULL
            AND student_id IS NOT NULL
            AND expires_at IS NOT NULL
        )
    ),
    CHECK (consumed_at IS NULL OR revoked_at IS NULL),
    CONSTRAINT fk_account_tokens_offering
        FOREIGN KEY (unit_offering_id)
        REFERENCES unit_offerings(unit_offering_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_account_tokens_student
        FOREIGN KEY (student_id) REFERENCES students(student_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_account_tokens_issuer
        FOREIGN KEY (issued_by_user_id) REFERENCES users(user_id)
        ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_unit_activation_entry
    ON account_tokens(unit_offering_id)
    WHERE token_type = 'unit_activation_entry'
      AND revoked_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_student_account_token
    ON account_tokens(student_id, token_type)
    WHERE token_type IN ('student_activation', 'password_reset')
      AND consumed_at IS NULL
      AND revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_account_tokens_lookup
    ON account_tokens(public_id, token_type);
CREATE INDEX IF NOT EXISTS idx_account_tokens_expiry
    ON account_tokens(token_type, expires_at);

-- Account request rate limiting uses privacy-safe hashes in the existing
-- append-only audit table instead of adding another account-event table.
CREATE INDEX IF NOT EXISTS idx_audit_account_request_key
    ON audit_events(
        event_type,
        json_extract(metadata_json, '$.request_key_hash'),
        created_at
    )
    WHERE event_type IN (
        'account.activation_requested',
        'account.activation_resent',
        'account.password_reset_requested'
    );

-- =========================
-- DURABLE, BOUNDED PROCESSING JOBS
-- =========================
CREATE TABLE IF NOT EXISTS processing_jobs (
    processing_job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL
        CHECK (
            job_type IN (
                'roster_import',
                'scoping_note_ingest',
                'assignment_spec_ingest',
                'rubric_ingest',
                'submission_batch_ingest',
                'account_email'
            )
        ),
    unit_offering_id INTEGER,
    assessment_plan_id INTEGER,
    roster_import_id INTEGER,
    submission_batch_id INTEGER,
    account_token_id INTEGER,
    created_by_user_id INTEGER,
    source_file_path TEXT,
    source_content_hash TEXT,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (
            status IN (
                'queued', 'running', 'succeeded', 'failed', 'cancelled'
            )
        ),
    priority INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0
        CHECK (attempt_count >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3
        CHECK (max_attempts > 0),
    progress_current INTEGER NOT NULL DEFAULT 0
        CHECK (progress_current >= 0),
    progress_total INTEGER
        CHECK (progress_total IS NULL OR progress_total >= 0),
    payload_json TEXT,
    result_json TEXT,
    available_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    locked_by TEXT,
    locked_at TEXT,
    heartbeat_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    completed_at TEXT,
    CHECK (
        progress_total IS NULL
        OR progress_current <= progress_total
    ),
    CHECK (payload_json IS NULL OR json_valid(payload_json)),
    CHECK (result_json IS NULL OR json_valid(result_json)),
    CHECK (
        (job_type = 'roster_import' AND roster_import_id IS NOT NULL)
        OR
        (
            job_type = 'scoping_note_ingest'
            AND unit_offering_id IS NOT NULL
        )
        OR
        (
            job_type IN ('assignment_spec_ingest', 'rubric_ingest')
            AND assessment_plan_id IS NOT NULL
        )
        OR
        (
            job_type = 'submission_batch_ingest'
            AND submission_batch_id IS NOT NULL
        )
        OR
        (
            job_type = 'account_email'
            AND account_token_id IS NOT NULL
        )
    ),
    CONSTRAINT fk_processing_jobs_offering
        FOREIGN KEY (unit_offering_id)
        REFERENCES unit_offerings(unit_offering_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_processing_jobs_plan
        FOREIGN KEY (assessment_plan_id)
        REFERENCES assessment_plans(assessment_plan_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_processing_jobs_roster
        FOREIGN KEY (roster_import_id)
        REFERENCES roster_imports(roster_import_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_processing_jobs_batch
        FOREIGN KEY (submission_batch_id)
        REFERENCES submission_batches(submission_batch_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_processing_jobs_token
        FOREIGN KEY (account_token_id)
        REFERENCES account_tokens(account_token_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_processing_jobs_created_by
        FOREIGN KEY (created_by_user_id) REFERENCES users(user_id)
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_processing_jobs_queue
    ON processing_jobs(status, available_at, priority);
CREATE INDEX IF NOT EXISTS idx_processing_jobs_heartbeat
    ON processing_jobs(status, heartbeat_at);
CREATE INDEX IF NOT EXISTS idx_processing_jobs_roster
    ON processing_jobs(roster_import_id);
CREATE INDEX IF NOT EXISTS idx_processing_jobs_batch
    ON processing_jobs(submission_batch_id);
CREATE INDEX IF NOT EXISTS idx_processing_jobs_token
    ON processing_jobs(account_token_id);

-- =========================
-- MOODLE BATCH REVIEW QUEUE
-- =========================
CREATE UNIQUE INDEX IF NOT EXISTS uq_submission_batch_source
    ON submission_batches(
        assessment_activity_id,
        source_content_hash
    )
    WHERE source_content_hash IS NOT NULL
      AND trim(source_content_hash) != '';

CREATE INDEX IF NOT EXISTS idx_submission_files_content_hash
    ON submission_files(content_hash);

CREATE TABLE IF NOT EXISTS submission_batch_items (
    submission_batch_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_batch_id INTEGER NOT NULL,
    item_number INTEGER NOT NULL CHECK (item_number > 0),
    source_folder_name TEXT NOT NULL,
    source_relative_path TEXT NOT NULL,
    detected_student_identifier TEXT COLLATE NOCASE,
    student_id INTEGER,
    candidate_student_ids_json TEXT,
    detected_files_json TEXT,
    accepted_file_name TEXT,
    accepted_file_path TEXT,
    accepted_content_hash TEXT,
    accepted_mime_type TEXT,
    accepted_size_bytes INTEGER
        CHECK (
            accepted_size_bytes IS NULL
            OR accepted_size_bytes >= 0
        ),
    item_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (
            item_status IN (
                'pending', 'matched', 'unmatched', 'ambiguous',
                'format_error', 'ready', 'imported', 'duplicate',
                'ignored', 'failed'
            )
        ),
    submission_attempt_id INTEGER UNIQUE,
    resolved_by_user_id INTEGER,
    resolved_at TEXT,
    resolution_note TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (submission_batch_id, item_number),
    UNIQUE (submission_batch_id, source_relative_path),
    CHECK (
        candidate_student_ids_json IS NULL
        OR json_valid(candidate_student_ids_json)
    ),
    CHECK (
        detected_files_json IS NULL
        OR json_valid(detected_files_json)
    ),
    CONSTRAINT fk_batch_items_batch
        FOREIGN KEY (submission_batch_id)
        REFERENCES submission_batches(submission_batch_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_batch_items_student
        FOREIGN KEY (student_id) REFERENCES students(student_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_batch_items_attempt
        FOREIGN KEY (submission_attempt_id)
        REFERENCES submission_attempts(submission_attempt_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_batch_items_resolved_by
        FOREIGN KEY (resolved_by_user_id) REFERENCES users(user_id)
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_batch_items_review
    ON submission_batch_items(submission_batch_id, item_status);
CREATE INDEX IF NOT EXISTS idx_batch_items_student
    ON submission_batch_items(student_id, item_status);
CREATE INDEX IF NOT EXISTS idx_batch_items_content_hash
    ON submission_batch_items(accepted_content_hash);

CREATE TRIGGER IF NOT EXISTS trg_batch_items_updated_at
AFTER UPDATE ON submission_batch_items
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE submission_batch_items
    SET updated_at = CURRENT_TIMESTAMP
    WHERE submission_batch_item_id = NEW.submission_batch_item_id;
END;

-- =========================
-- CURRENT AND INVALID ATTEMPTS
-- =========================
CREATE TABLE IF NOT EXISTS current_summative_attempts (
    assessment_activity_id INTEGER NOT NULL,
    student_id INTEGER NOT NULL,
    submission_attempt_id INTEGER NOT NULL UNIQUE,
    set_by_user_id INTEGER,
    set_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (assessment_activity_id, student_id),
    CONSTRAINT fk_current_attempt_activity
        FOREIGN KEY (assessment_activity_id)
        REFERENCES assessment_activities(assessment_activity_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_current_attempt_student
        FOREIGN KEY (student_id) REFERENCES students(student_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_current_attempt_attempt
        FOREIGN KEY (submission_attempt_id)
        REFERENCES submission_attempts(submission_attempt_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_current_attempt_set_by
        FOREIGN KEY (set_by_user_id) REFERENCES users(user_id)
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_current_summative_student
    ON current_summative_attempts(student_id);

CREATE TRIGGER IF NOT EXISTS trg_current_summative_attempt_insert
BEFORE INSERT ON current_summative_attempts
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM submission_attempts AS attempt
    JOIN submission_participants AS participant
      ON participant.submission_attempt_id =
         attempt.submission_attempt_id
    WHERE attempt.submission_attempt_id =
          NEW.submission_attempt_id
      AND attempt.assessment_activity_id =
          NEW.assessment_activity_id
      AND attempt.purpose = 'summative'
      AND attempt.validity_status = 'valid'
      AND participant.student_id = NEW.student_id
      AND participant.participant_role = 'primary'
)
BEGIN
    SELECT RAISE(
        ABORT,
        'current summative attempt must be valid and match its student'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_current_summative_attempt_update
BEFORE UPDATE OF
    assessment_activity_id,
    student_id,
    submission_attempt_id
ON current_summative_attempts
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM submission_attempts AS attempt
    JOIN submission_participants AS participant
      ON participant.submission_attempt_id =
         attempt.submission_attempt_id
    WHERE attempt.submission_attempt_id =
          NEW.submission_attempt_id
      AND attempt.assessment_activity_id =
          NEW.assessment_activity_id
      AND attempt.purpose = 'summative'
      AND attempt.validity_status = 'valid'
      AND participant.student_id = NEW.student_id
      AND participant.participant_role = 'primary'
)
BEGIN
    SELECT RAISE(
        ABORT,
        'current summative attempt must be valid and match its student'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_attempt_invalidation_requires_replacement
BEFORE UPDATE OF
    validity_status,
    superseded_by_attempt_id,
    invalidated_by_user_id,
    invalidated_at,
    invalidation_reason
ON submission_attempts
FOR EACH ROW
WHEN OLD.validity_status = 'valid'
 AND NEW.validity_status != 'valid'
 AND (
     NEW.invalidated_by_user_id IS NULL
     OR NEW.invalidated_at IS NULL
     OR NEW.invalidation_reason IS NULL
     OR trim(NEW.invalidation_reason) = ''
     OR (
         NEW.validity_status = 'superseded'
         AND (
             NEW.superseded_by_attempt_id IS NULL
             OR NEW.superseded_by_attempt_id =
                OLD.submission_attempt_id
             OR NOT EXISTS (
                 SELECT 1
                 FROM submission_attempts AS replacement
                 WHERE replacement.submission_attempt_id =
                       NEW.superseded_by_attempt_id
                   AND replacement.assessment_activity_id =
                       OLD.assessment_activity_id
                   AND replacement.purpose = OLD.purpose
                   AND replacement.validity_status = 'valid'
                   AND EXISTS (
                       SELECT 1
                       FROM submission_participants AS old_participant
                       JOIN submission_participants AS new_participant
                         ON new_participant.student_id =
                            old_participant.student_id
                        AND new_participant.participant_role = 'primary'
                       WHERE old_participant.submission_attempt_id =
                             OLD.submission_attempt_id
                         AND old_participant.participant_role = 'primary'
                         AND new_participant.submission_attempt_id =
                             replacement.submission_attempt_id
                   )
             )
         )
     )
     OR (
         NEW.validity_status = 'void'
         AND NEW.superseded_by_attempt_id IS NOT NULL
     )
 )
BEGIN
    SELECT RAISE(
        ABORT,
        'invalid attempt requires actor, time, reason, and valid replacement'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_valid_attempt_has_no_invalidation
BEFORE UPDATE OF
    validity_status,
    superseded_by_attempt_id,
    invalidated_by_user_id,
    invalidated_at,
    invalidation_reason
ON submission_attempts
FOR EACH ROW
WHEN NEW.validity_status = 'valid'
 AND (
     NEW.superseded_by_attempt_id IS NOT NULL
     OR NEW.invalidated_by_user_id IS NOT NULL
     OR NEW.invalidated_at IS NOT NULL
     OR NEW.invalidation_reason IS NOT NULL
 )
BEGIN
    SELECT RAISE(
        ABORT,
        'valid submission attempts cannot carry invalidation metadata'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_attempt_invalidation_not_current
BEFORE UPDATE OF validity_status ON submission_attempts
FOR EACH ROW
WHEN OLD.validity_status = 'valid'
 AND NEW.validity_status != 'valid'
 AND EXISTS (
     SELECT 1
     FROM current_summative_attempts
     WHERE submission_attempt_id = OLD.submission_attempt_id
 )
BEGIN
    SELECT RAISE(
        ABORT,
        'move the current attempt pointer before invalidating this attempt'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_invalid_attempt_immutable
BEFORE UPDATE ON submission_attempts
FOR EACH ROW
WHEN OLD.validity_status != 'valid'
BEGIN
    SELECT RAISE(
        ABORT,
        'superseded or void submission attempts are immutable'
    );
END;

-- Direct writes to outputs of an invalid attempt are blocked.  Workflow events
-- remain append-only so the invalidation itself can still be recorded.
CREATE TRIGGER IF NOT EXISTS trg_invalid_attempt_files_insert
BEFORE INSERT ON submission_files
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM submission_attempts
    WHERE submission_attempt_id = NEW.submission_attempt_id
      AND validity_status != 'valid'
)
BEGIN
    SELECT RAISE(ABORT, 'invalid submission outputs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_invalid_attempt_files_immutable
BEFORE UPDATE ON submission_files
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM submission_attempts
    WHERE submission_attempt_id = OLD.submission_attempt_id
      AND validity_status != 'valid'
)
BEGIN
    SELECT RAISE(ABORT, 'invalid submission outputs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_invalid_attempt_marker_insert
BEFORE INSERT ON marker_assignments
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM submission_attempts
    WHERE submission_attempt_id = NEW.submission_attempt_id
      AND validity_status != 'valid'
)
BEGIN
    SELECT RAISE(ABORT, 'invalid submission outputs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_invalid_attempt_marker_update
BEFORE UPDATE ON marker_assignments
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM submission_attempts
    WHERE submission_attempt_id = OLD.submission_attempt_id
      AND validity_status != 'valid'
)
BEGIN
    SELECT RAISE(ABORT, 'invalid submission outputs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_invalid_attempt_workflow_insert
BEFORE INSERT ON submission_workflow_states
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM submission_attempts
    WHERE submission_attempt_id = NEW.submission_attempt_id
      AND validity_status != 'valid'
)
BEGIN
    SELECT RAISE(ABORT, 'invalid submission outputs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_invalid_attempt_workflow_update
BEFORE UPDATE ON submission_workflow_states
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM submission_attempts
    WHERE submission_attempt_id = OLD.submission_attempt_id
      AND validity_status != 'valid'
)
BEGIN
    SELECT RAISE(ABORT, 'invalid submission outputs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_invalid_attempt_generation_insert
BEFORE INSERT ON generation_runs
FOR EACH ROW
WHEN NEW.submission_attempt_id IS NOT NULL
 AND EXISTS (
     SELECT 1
     FROM submission_attempts
     WHERE submission_attempt_id = NEW.submission_attempt_id
       AND validity_status != 'valid'
 )
BEGIN
    SELECT RAISE(ABORT, 'invalid submission outputs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_invalid_attempt_generation_update
BEFORE UPDATE ON generation_runs
FOR EACH ROW
WHEN OLD.submission_attempt_id IS NOT NULL
 AND EXISTS (
     SELECT 1
     FROM submission_attempts
     WHERE submission_attempt_id = OLD.submission_attempt_id
       AND validity_status != 'valid'
 )
BEGIN
    SELECT RAISE(ABORT, 'invalid submission outputs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_invalid_attempt_revision_insert
BEFORE INSERT ON feedback_revisions
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM submission_attempts
    WHERE submission_attempt_id = NEW.submission_attempt_id
      AND validity_status != 'valid'
)
BEGIN
    SELECT RAISE(ABORT, 'invalid submission outputs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_invalid_attempt_revision_update
BEFORE UPDATE ON feedback_revisions
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM submission_attempts
    WHERE submission_attempt_id = OLD.submission_attempt_id
      AND validity_status != 'valid'
)
BEGIN
    SELECT RAISE(ABORT, 'invalid submission outputs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_invalid_attempt_criterion_insert
BEFORE INSERT ON criterion_feedback_revisions
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM feedback_revisions AS revision
    JOIN submission_attempts AS attempt
      ON attempt.submission_attempt_id =
         revision.submission_attempt_id
    WHERE revision.feedback_revision_id = NEW.feedback_revision_id
      AND attempt.validity_status != 'valid'
)
BEGIN
    SELECT RAISE(ABORT, 'invalid submission outputs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_invalid_attempt_criterion_update
BEFORE UPDATE ON criterion_feedback_revisions
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM feedback_revisions AS revision
    JOIN submission_attempts AS attempt
      ON attempt.submission_attempt_id =
         revision.submission_attempt_id
    WHERE revision.feedback_revision_id = OLD.feedback_revision_id
      AND attempt.validity_status != 'valid'
)
BEGIN
    SELECT RAISE(ABORT, 'invalid submission outputs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_invalid_attempt_overall_insert
BEFORE INSERT ON overall_feedback_revisions
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM feedback_revisions AS revision
    JOIN submission_attempts AS attempt
      ON attempt.submission_attempt_id =
         revision.submission_attempt_id
    WHERE revision.feedback_revision_id = NEW.feedback_revision_id
      AND attempt.validity_status != 'valid'
)
BEGIN
    SELECT RAISE(ABORT, 'invalid submission outputs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_invalid_attempt_overall_update
BEFORE UPDATE ON overall_feedback_revisions
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM feedback_revisions AS revision
    JOIN submission_attempts AS attempt
      ON attempt.submission_attempt_id =
         revision.submission_attempt_id
    WHERE revision.feedback_revision_id = OLD.feedback_revision_id
      AND attempt.validity_status != 'valid'
)
BEGIN
    SELECT RAISE(ABORT, 'invalid submission outputs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_invalid_attempt_result_insert
BEFORE INSERT ON assessment_results
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM submission_attempts
    WHERE submission_attempt_id = NEW.submission_attempt_id
      AND validity_status != 'valid'
)
BEGIN
    SELECT RAISE(ABORT, 'invalid submission outputs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_invalid_attempt_result_update
BEFORE UPDATE ON assessment_results
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
    FROM submission_attempts
    WHERE submission_attempt_id = OLD.submission_attempt_id
      AND validity_status != 'valid'
)
BEGIN
    SELECT RAISE(ABORT, 'invalid submission outputs are immutable');
END;

-- Unit-level scoping material is additive.  Deactivation is explicit and
-- historical rows remain available for provenance.
CREATE INDEX IF NOT EXISTS idx_unit_materials_active
    ON unit_materials(unit_id, material_type, is_active);

CREATE TRIGGER IF NOT EXISTS trg_unit_material_deactivation
BEFORE UPDATE OF
    is_active,
    deactivated_by_user_id,
    deactivated_at,
    deactivation_reason
ON unit_materials
FOR EACH ROW
WHEN OLD.is_active = 1
 AND NEW.is_active = 0
 AND (
     NEW.deactivated_by_user_id IS NULL
     OR NEW.deactivated_at IS NULL
     OR NEW.deactivation_reason IS NULL
     OR trim(NEW.deactivation_reason) = ''
 )
BEGIN
    SELECT RAISE(
        ABORT,
        'material deactivation requires actor, time, and reason'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_unit_material_no_reactivation
BEFORE UPDATE OF is_active ON unit_materials
FOR EACH ROW
WHEN OLD.is_active = 0
 AND NEW.is_active != OLD.is_active
BEGIN
    SELECT RAISE(
        ABORT,
        'deactivated unit material cannot be reactivated'
    );
END;
