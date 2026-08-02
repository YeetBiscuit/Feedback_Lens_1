-- Feedback Lens database V2
--
-- This migration adds the durable academic, permission, assessment, workflow,
-- provenance, revision, and audit model. Existing V1 tables remain in place as
-- a compatibility layer for the current application.

-- =========================
-- ORGANISATION AND ACADEMIC STRUCTURE
-- =========================
CREATE TABLE IF NOT EXISTS organizations (
    organization_id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_code TEXT NOT NULL COLLATE NOCASE UNIQUE,
    organization_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS courses (
    course_id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    course_code TEXT NOT NULL COLLATE NOCASE,
    course_name TEXT NOT NULL,
    faculty TEXT,
    academic_level TEXT,
    discipline TEXT,
    credit_points REAL,
    learning_outcomes_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (organization_id, course_code),
    CHECK (
        learning_outcomes_json IS NULL
        OR json_valid(learning_outcomes_json)
    ),
    CONSTRAINT fk_courses_organization
        FOREIGN KEY (organization_id) REFERENCES organizations(organization_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS unit_offerings (
    unit_offering_id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id INTEGER NOT NULL,
    legacy_unit_id INTEGER UNIQUE,
    academic_year INTEGER,
    teaching_period TEXT,
    offering_name TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('draft', 'active', 'archived')),
    archived_at TEXT,
    archived_by_user_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (course_id, academic_year, teaching_period),
    CONSTRAINT fk_unit_offerings_course
        FOREIGN KEY (course_id) REFERENCES courses(course_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_unit_offerings_legacy_unit
        FOREIGN KEY (legacy_unit_id) REFERENCES units(unit_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_unit_offerings_archived_by
        FOREIGN KEY (archived_by_user_id) REFERENCES users(user_id)
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_unit_offerings_course
    ON unit_offerings(course_id);
CREATE INDEX IF NOT EXISTS idx_unit_offerings_status
    ON unit_offerings(status);

-- =========================
-- SCOPED ROLES AND ENROLMENTS
-- =========================
CREATE TABLE IF NOT EXISTS organization_role_assignments (
    organization_role_assignment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role = 'chief_admin'),
    assigned_by_user_id INTEGER,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    assigned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TEXT,
    UNIQUE (organization_id, user_id, role),
    CONSTRAINT fk_organization_roles_organization
        FOREIGN KEY (organization_id) REFERENCES organizations(organization_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_organization_roles_user
        FOREIGN KEY (user_id) REFERENCES users(user_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_organization_roles_assigned_by
        FOREIGN KEY (assigned_by_user_id) REFERENCES users(user_id)
        ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS unit_role_assignments (
    unit_role_assignment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_offering_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('unit_admin', 'staff')),
    assigned_by_user_id INTEGER,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    assigned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TEXT,
    UNIQUE (unit_offering_id, user_id, role),
    CONSTRAINT fk_unit_roles_offering
        FOREIGN KEY (unit_offering_id) REFERENCES unit_offerings(unit_offering_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_unit_roles_user
        FOREIGN KEY (user_id) REFERENCES users(user_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_unit_roles_assigned_by
        FOREIGN KEY (assigned_by_user_id) REFERENCES users(user_id)
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_unit_roles_user
    ON unit_role_assignments(user_id, active);
CREATE INDEX IF NOT EXISTS idx_unit_roles_offering
    ON unit_role_assignments(unit_offering_id, role, active);

CREATE TABLE IF NOT EXISTS students (
    student_id INTEGER PRIMARY KEY AUTOINCREMENT,
    institution_student_identifier TEXT NOT NULL COLLATE NOCASE UNIQUE,
    user_id INTEGER UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_students_user
        FOREIGN KEY (user_id) REFERENCES users(user_id)
        ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS student_enrolments (
    student_enrolment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_offering_id INTEGER NOT NULL,
    student_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('pending', 'active', 'withdrawn', 'completed')),
    source TEXT NOT NULL DEFAULT 'manual'
        CHECK (source IN ('manual', 'moodle', 'legacy_import', 'api')),
    source_reference TEXT,
    enrolled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TEXT,
    UNIQUE (unit_offering_id, student_id),
    CONSTRAINT fk_student_enrolments_offering
        FOREIGN KEY (unit_offering_id) REFERENCES unit_offerings(unit_offering_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_student_enrolments_student
        FOREIGN KEY (student_id) REFERENCES students(student_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_student_enrolments_student
    ON student_enrolments(student_id, status);

-- =========================
-- ASSESSMENT PLANS AND VERSIONED CONFIGURATION
-- =========================
CREATE TABLE IF NOT EXISTS assessment_plans (
    assessment_plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_offering_id INTEGER NOT NULL,
    legacy_assignment_id INTEGER UNIQUE,
    assessment_code TEXT,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'active', 'closed', 'archived')),
    supports_formative INTEGER NOT NULL DEFAULT 1
        CHECK (supports_formative IN (0, 1)),
    supports_summative INTEGER NOT NULL DEFAULT 1
        CHECK (supports_summative IN (0, 1)),
    created_by_user_id INTEGER,
    archived_at TEXT,
    archived_by_user_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (unit_offering_id, assessment_code),
    CONSTRAINT fk_assessment_plans_offering
        FOREIGN KEY (unit_offering_id) REFERENCES unit_offerings(unit_offering_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_assessment_plans_legacy_assignment
        FOREIGN KEY (legacy_assignment_id) REFERENCES assignments(assignment_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_assessment_plans_created_by
        FOREIGN KEY (created_by_user_id) REFERENCES users(user_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_assessment_plans_archived_by
        FOREIGN KEY (archived_by_user_id) REFERENCES users(user_id)
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_assessment_plans_offering
    ON assessment_plans(unit_offering_id, status);

CREATE TABLE IF NOT EXISTS assessment_plan_versions (
    assessment_plan_version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_plan_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    spec_id INTEGER,
    rubric_id INTEGER,
    maximum_mark REAL,
    configuration_json TEXT,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'active', 'superseded')),
    created_by_user_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    activated_at TEXT,
    superseded_at TEXT,
    UNIQUE (assessment_plan_id, version),
    CHECK (
        configuration_json IS NULL
        OR json_valid(configuration_json)
    ),
    CONSTRAINT fk_assessment_versions_plan
        FOREIGN KEY (assessment_plan_id) REFERENCES assessment_plans(assessment_plan_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_assessment_versions_spec
        FOREIGN KEY (spec_id) REFERENCES assignment_specs(spec_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_assessment_versions_rubric
        FOREIGN KEY (rubric_id) REFERENCES rubrics(rubric_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_assessment_versions_created_by
        FOREIGN KEY (created_by_user_id) REFERENCES users(user_id)
        ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_assessment_plan_active_version
    ON assessment_plan_versions(assessment_plan_id)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS assessment_activities (
    assessment_activity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_plan_version_id INTEGER NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('formative', 'summative')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    maximum_attempts INTEGER CHECK (maximum_attempts IS NULL OR maximum_attempts > 0),
    auto_release_feedback INTEGER NOT NULL DEFAULT 0
        CHECK (auto_release_feedback IN (0, 1)),
    staff_review_required INTEGER NOT NULL DEFAULT 1
        CHECK (staff_review_required IN (0, 1)),
    admin_confirmation_required INTEGER NOT NULL DEFAULT 1
        CHECK (admin_confirmation_required IN (0, 1)),
    disclaimer_text TEXT,
    opens_at TEXT,
    closes_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (assessment_plan_version_id, purpose),
    CONSTRAINT fk_assessment_activities_version
        FOREIGN KEY (assessment_plan_version_id)
        REFERENCES assessment_plan_versions(assessment_plan_version_id)
        ON DELETE CASCADE
);

-- =========================
-- MOODLE BATCHES AND SUBMISSION ATTEMPTS
-- =========================
CREATE TABLE IF NOT EXISTS submission_batches (
    submission_batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_activity_id INTEGER NOT NULL,
    uploaded_by_user_id INTEGER NOT NULL,
    source_system TEXT NOT NULL DEFAULT 'moodle',
    source_file_name TEXT,
    source_file_path TEXT,
    source_content_hash TEXT,
    status TEXT NOT NULL DEFAULT 'uploaded'
        CHECK (
            status IN (
                'uploaded', 'validating', 'ready', 'imported',
                'partially_imported', 'failed'
            )
        ),
    detected_submission_count INTEGER,
    imported_submission_count INTEGER,
    error_message TEXT,
    uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    CONSTRAINT fk_submission_batches_activity
        FOREIGN KEY (assessment_activity_id)
        REFERENCES assessment_activities(assessment_activity_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_submission_batches_uploaded_by
        FOREIGN KEY (uploaded_by_user_id) REFERENCES users(user_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_submission_batches_activity
    ON submission_batches(assessment_activity_id, uploaded_at);

CREATE TRIGGER IF NOT EXISTS trg_submission_batches_summative_only
BEFORE INSERT ON submission_batches
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM assessment_activities
    WHERE assessment_activity_id = NEW.assessment_activity_id
      AND purpose = 'summative'
)
BEGIN
    SELECT RAISE(
        ABORT,
        'Moodle batches can only target summative activities'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_submission_batches_summative_only_update
BEFORE UPDATE OF assessment_activity_id ON submission_batches
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM assessment_activities
    WHERE assessment_activity_id = NEW.assessment_activity_id
      AND purpose = 'summative'
)
BEGIN
    SELECT RAISE(
        ABORT,
        'Moodle batches can only target summative activities'
    );
END;

CREATE TABLE IF NOT EXISTS submission_attempts (
    submission_attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_activity_id INTEGER NOT NULL,
    submission_batch_id INTEGER,
    legacy_submission_id INTEGER UNIQUE,
    purpose TEXT NOT NULL CHECK (purpose IN ('formative', 'summative')),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    source_version INTEGER,
    source_system TEXT NOT NULL
        CHECK (
            source_system IN (
                'student_portal', 'moodle', 'staff_upload',
                'legacy_import', 'api'
            )
        ),
    source_reference TEXT,
    visibility TEXT NOT NULL
        CHECK (visibility IN ('student_private', 'assigned_staff')),
    status TEXT NOT NULL DEFAULT 'imported'
        CHECK (
            status IN (
                'uploaded', 'imported', 'ready', 'processing',
                'completed', 'failed'
            )
        ),
    submitted_by_user_id INTEGER,
    submitted_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (purpose = 'formative' AND visibility = 'student_private')
        OR
        (purpose = 'summative' AND visibility = 'assigned_staff')
    ),
    CONSTRAINT fk_submission_attempts_activity
        FOREIGN KEY (assessment_activity_id)
        REFERENCES assessment_activities(assessment_activity_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_submission_attempts_batch
        FOREIGN KEY (submission_batch_id) REFERENCES submission_batches(submission_batch_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_submission_attempts_legacy
        FOREIGN KEY (legacy_submission_id)
        REFERENCES student_submissions(submission_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_submission_attempts_submitted_by
        FOREIGN KEY (submitted_by_user_id) REFERENCES users(user_id)
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_submission_attempts_activity
    ON submission_attempts(assessment_activity_id, purpose, status);
CREATE INDEX IF NOT EXISTS idx_submission_attempts_batch
    ON submission_attempts(submission_batch_id);

CREATE TRIGGER IF NOT EXISTS trg_submission_attempts_match_activity
BEFORE INSERT ON submission_attempts
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM assessment_activities
    WHERE assessment_activity_id = NEW.assessment_activity_id
      AND purpose = NEW.purpose
)
BEGIN
    SELECT RAISE(
        ABORT,
        'submission purpose must match its assessment activity'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_submission_attempts_match_activity_update
BEFORE UPDATE OF assessment_activity_id, purpose ON submission_attempts
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM assessment_activities
    WHERE assessment_activity_id = NEW.assessment_activity_id
      AND purpose = NEW.purpose
)
BEGIN
    SELECT RAISE(
        ABORT,
        'submission purpose must match its assessment activity'
    );
END;

-- The many-to-many participant table keeps V2 ready for future group work,
-- while the first product release creates exactly one primary participant.
CREATE TABLE IF NOT EXISTS submission_participants (
    submission_attempt_id INTEGER NOT NULL,
    student_id INTEGER NOT NULL,
    participant_role TEXT NOT NULL DEFAULT 'primary'
        CHECK (participant_role IN ('primary', 'member')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (submission_attempt_id, student_id),
    CONSTRAINT fk_submission_participants_attempt
        FOREIGN KEY (submission_attempt_id)
        REFERENCES submission_attempts(submission_attempt_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_submission_participants_student
        FOREIGN KEY (student_id) REFERENCES students(student_id)
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_submission_primary_participant
    ON submission_participants(submission_attempt_id)
    WHERE participant_role = 'primary';

CREATE TABLE IF NOT EXISTS submission_files (
    submission_file_id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_attempt_id INTEGER NOT NULL,
    original_file_name TEXT NOT NULL,
    relative_path TEXT,
    storage_path TEXT,
    content_hash TEXT,
    mime_type TEXT,
    size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (submission_attempt_id, relative_path, original_file_name),
    CONSTRAINT fk_submission_files_attempt
        FOREIGN KEY (submission_attempt_id)
        REFERENCES submission_attempts(submission_attempt_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_submission_files_attempt
    ON submission_files(submission_attempt_id);

-- =========================
-- ALLOCATION AND HUMAN WORKFLOW
-- =========================
CREATE TABLE IF NOT EXISTS marker_assignments (
    marker_assignment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_attempt_id INTEGER NOT NULL,
    marker_user_id INTEGER NOT NULL,
    assigned_by_user_id INTEGER NOT NULL,
    assignment_reason TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    assigned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TEXT,
    CONSTRAINT fk_marker_assignments_attempt
        FOREIGN KEY (submission_attempt_id)
        REFERENCES submission_attempts(submission_attempt_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_marker_assignments_marker
        FOREIGN KEY (marker_user_id) REFERENCES users(user_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_marker_assignments_assigned_by
        FOREIGN KEY (assigned_by_user_id) REFERENCES users(user_id)
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_marker_per_submission
    ON marker_assignments(submission_attempt_id)
    WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_marker_assignments_marker
    ON marker_assignments(marker_user_id, active);

CREATE TRIGGER IF NOT EXISTS trg_marker_assignments_summative_only
BEFORE INSERT ON marker_assignments
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM submission_attempts
    WHERE submission_attempt_id = NEW.submission_attempt_id
      AND purpose = 'summative'
)
BEGIN
    SELECT RAISE(
        ABORT,
        'markers can only be assigned to summative submissions'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_marker_assignments_summative_only_update
BEFORE UPDATE OF submission_attempt_id ON marker_assignments
FOR EACH ROW
WHEN NOT EXISTS (
    SELECT 1
    FROM submission_attempts
    WHERE submission_attempt_id = NEW.submission_attempt_id
      AND purpose = 'summative'
)
BEGIN
    SELECT RAISE(
        ABORT,
        'markers can only be assigned to summative submissions'
    );
END;

CREATE TABLE IF NOT EXISTS submission_workflow_states (
    submission_attempt_id INTEGER PRIMARY KEY,
    allocation_status TEXT NOT NULL DEFAULT 'unassigned'
        CHECK (allocation_status IN ('unassigned', 'assigned')),
    ai_generation_status TEXT NOT NULL DEFAULT 'not_started'
        CHECK (
            ai_generation_status IN (
                'not_started', 'queued', 'running', 'generated', 'failed'
            )
        ),
    marking_status TEXT NOT NULL DEFAULT 'not_started'
        CHECK (
            marking_status IN (
                'not_started', 'in_progress',
                'marker_confirmed', 'admin_confirmed'
            )
        ),
    current_generation_id INTEGER,
    current_feedback_revision_id INTEGER,
    marker_confirmed_by_user_id INTEGER,
    marker_confirmed_at TEXT,
    admin_confirmed_by_user_id INTEGER,
    admin_confirmed_at TEXT,
    state_version INTEGER NOT NULL DEFAULT 1 CHECK (state_version > 0),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_workflow_state_attempt
        FOREIGN KEY (submission_attempt_id)
        REFERENCES submission_attempts(submission_attempt_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_workflow_state_generation
        FOREIGN KEY (current_generation_id) REFERENCES generation_runs(generation_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_workflow_state_marker
        FOREIGN KEY (marker_confirmed_by_user_id) REFERENCES users(user_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_workflow_state_admin
        FOREIGN KEY (admin_confirmed_by_user_id) REFERENCES users(user_id)
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_workflow_state_queue
    ON submission_workflow_states(
        allocation_status,
        ai_generation_status,
        marking_status
    );

CREATE TABLE IF NOT EXISTS submission_workflow_events (
    workflow_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_attempt_id INTEGER NOT NULL,
    actor_user_id INTEGER,
    event_type TEXT NOT NULL,
    from_state_json TEXT,
    to_state_json TEXT,
    comment TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (from_state_json IS NULL OR json_valid(from_state_json)),
    CHECK (to_state_json IS NULL OR json_valid(to_state_json)),
    CONSTRAINT fk_workflow_events_attempt
        FOREIGN KEY (submission_attempt_id)
        REFERENCES submission_attempts(submission_attempt_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_workflow_events_actor
        FOREIGN KEY (actor_user_id) REFERENCES users(user_id)
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_workflow_events_attempt
    ON submission_workflow_events(submission_attempt_id, created_at);

-- =========================
-- RETRIEVAL INDEX BUILDS AND GENERATION PROVENANCE
-- =========================
CREATE TABLE IF NOT EXISTS index_builds (
    index_build_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_offering_id INTEGER NOT NULL,
    vector_store_name TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_version TEXT,
    chunking_strategy TEXT,
    status TEXT NOT NULL DEFAULT 'building'
        CHECK (status IN ('building', 'active', 'superseded', 'failed')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    UNIQUE (
        unit_offering_id,
        vector_store_name,
        embedding_model,
        embedding_version
    ),
    CONSTRAINT fk_index_builds_offering
        FOREIGN KEY (unit_offering_id) REFERENCES unit_offerings(unit_offering_id)
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_index_build_per_store
    ON index_builds(unit_offering_id, vector_store_name)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS index_build_items (
    index_build_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    index_build_id INTEGER NOT NULL,
    chunk_id INTEGER NOT NULL,
    vector_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (index_build_id, chunk_id),
    UNIQUE (index_build_id, vector_id),
    CONSTRAINT fk_index_build_items_build
        FOREIGN KEY (index_build_id) REFERENCES index_builds(index_build_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_index_build_items_chunk
        FOREIGN KEY (chunk_id) REFERENCES material_chunks(chunk_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS generation_input_snapshots (
    generation_input_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id INTEGER NOT NULL UNIQUE,
    submission_attempt_id INTEGER,
    assessment_plan_version_id INTEGER,
    spec_id INTEGER,
    rubric_id INTEGER,
    index_build_id INTEGER,
    code_version TEXT,
    input_snapshot_hash TEXT,
    generation_configuration_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        generation_configuration_json IS NULL
        OR json_valid(generation_configuration_json)
    ),
    CONSTRAINT fk_generation_snapshots_generation
        FOREIGN KEY (generation_id) REFERENCES generation_runs(generation_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_generation_snapshots_attempt
        FOREIGN KEY (submission_attempt_id)
        REFERENCES submission_attempts(submission_attempt_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_generation_snapshots_plan_version
        FOREIGN KEY (assessment_plan_version_id)
        REFERENCES assessment_plan_versions(assessment_plan_version_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_generation_snapshots_spec
        FOREIGN KEY (spec_id) REFERENCES assignment_specs(spec_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_generation_snapshots_rubric
        FOREIGN KEY (rubric_id) REFERENCES rubrics(rubric_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_generation_snapshots_index_build
        FOREIGN KEY (index_build_id) REFERENCES index_builds(index_build_id)
        ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS model_usage_records (
    model_usage_record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id INTEGER NOT NULL,
    operation TEXT NOT NULL
        CHECK (operation IN ('planning', 'generation', 'regeneration')),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    provider_request_id TEXT,
    input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    latency_ms INTEGER CHECK (latency_ms IS NULL OR latency_ms >= 0),
    estimated_cost REAL CHECK (estimated_cost IS NULL OR estimated_cost >= 0),
    currency TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_model_usage_generation
        FOREIGN KEY (generation_id) REFERENCES generation_runs(generation_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_model_usage_generation
    ON model_usage_records(generation_id, operation);

CREATE TABLE IF NOT EXISTS retrieval_queries_v2 (
    retrieval_query_id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id INTEGER NOT NULL,
    query_sequence INTEGER NOT NULL,
    source TEXT NOT NULL
        CHECK (source IN ('assignment_spec', 'planner', 'criterion', 'legacy')),
    criterion_id INTEGER,
    cue_text TEXT,
    query_text TEXT NOT NULL,
    requested_top_k INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (generation_id, query_sequence),
    CONSTRAINT fk_retrieval_queries_generation
        FOREIGN KEY (generation_id) REFERENCES generation_runs(generation_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_retrieval_queries_criterion
        FOREIGN KEY (criterion_id) REFERENCES rubric_criteria(criterion_id)
        ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS retrieval_hits_v2 (
    retrieval_hit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    retrieval_query_id INTEGER NOT NULL,
    chunk_id INTEGER NOT NULL,
    rank_position INTEGER NOT NULL CHECK (rank_position > 0),
    score REAL,
    score_metric TEXT,
    used_in_prompt INTEGER NOT NULL DEFAULT 1 CHECK (used_in_prompt IN (0, 1)),
    chunk_content_hash TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (retrieval_query_id, rank_position),
    UNIQUE (retrieval_query_id, chunk_id),
    CONSTRAINT fk_retrieval_hits_query
        FOREIGN KEY (retrieval_query_id)
        REFERENCES retrieval_queries_v2(retrieval_query_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_retrieval_hits_chunk
        FOREIGN KEY (chunk_id) REFERENCES material_chunks(chunk_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_retrieval_hits_chunk
    ON retrieval_hits_v2(chunk_id);

-- =========================
-- IMMUTABLE FEEDBACK REVISIONS AND CURRENT RESULT
-- =========================
CREATE TABLE IF NOT EXISTS feedback_revisions (
    feedback_revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_attempt_id INTEGER NOT NULL,
    generation_id INTEGER,
    revision_number INTEGER NOT NULL CHECK (revision_number > 0),
    source TEXT NOT NULL CHECK (source IN ('ai', 'marker', 'admin', 'migration')),
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (
            status IN (
                'draft', 'marker_confirmed',
                'admin_confirmed', 'superseded'
            )
        ),
    based_on_revision_id INTEGER,
    created_by_user_id INTEGER,
    calculated_total_mark REAL,
    final_total_mark REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (submission_attempt_id, revision_number),
    CONSTRAINT fk_feedback_revisions_attempt
        FOREIGN KEY (submission_attempt_id)
        REFERENCES submission_attempts(submission_attempt_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_feedback_revisions_generation
        FOREIGN KEY (generation_id) REFERENCES generation_runs(generation_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_feedback_revisions_based_on
        FOREIGN KEY (based_on_revision_id)
        REFERENCES feedback_revisions(feedback_revision_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_feedback_revisions_created_by
        FOREIGN KEY (created_by_user_id) REFERENCES users(user_id)
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_feedback_revisions_attempt
    ON feedback_revisions(submission_attempt_id, revision_number);

CREATE TABLE IF NOT EXISTS criterion_feedback_revisions (
    criterion_feedback_revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    feedback_revision_id INTEGER NOT NULL,
    criterion_id INTEGER NOT NULL,
    strengths TEXT,
    areas_for_improvement TEXT,
    improvement_suggestion TEXT,
    suggested_level TEXT,
    evidence_summary TEXT,
    mark REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (feedback_revision_id, criterion_id),
    CONSTRAINT fk_criterion_feedback_revisions_revision
        FOREIGN KEY (feedback_revision_id)
        REFERENCES feedback_revisions(feedback_revision_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_criterion_feedback_revisions_criterion
        FOREIGN KEY (criterion_id) REFERENCES rubric_criteria(criterion_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS overall_feedback_revisions (
    overall_feedback_revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    feedback_revision_id INTEGER NOT NULL UNIQUE,
    overall_comment TEXT NOT NULL,
    key_strengths TEXT,
    priority_improvements TEXT,
    overall_grade_band TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_overall_feedback_revisions_revision
        FOREIGN KEY (feedback_revision_id)
        REFERENCES feedback_revisions(feedback_revision_id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS assessment_results (
    submission_attempt_id INTEGER PRIMARY KEY,
    current_feedback_revision_id INTEGER NOT NULL,
    calculated_total_mark REAL,
    final_total_mark REAL,
    marker_confirmed_by_user_id INTEGER,
    marker_confirmed_at TEXT,
    admin_confirmed_by_user_id INTEGER,
    admin_confirmed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_assessment_results_attempt
        FOREIGN KEY (submission_attempt_id)
        REFERENCES submission_attempts(submission_attempt_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_assessment_results_revision
        FOREIGN KEY (current_feedback_revision_id)
        REFERENCES feedback_revisions(feedback_revision_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_assessment_results_marker
        FOREIGN KEY (marker_confirmed_by_user_id) REFERENCES users(user_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_assessment_results_admin
        FOREIGN KEY (admin_confirmed_by_user_id) REFERENCES users(user_id)
        ON DELETE SET NULL
);

-- The workflow state references the revision table declared above. SQLite
-- cannot add this cross-reference to an existing table without rebuilding it,
-- so revision integrity is enforced from assessment_results and application
-- transactions for migrated databases.

-- =========================
-- PRIVACY-SAFE METRICS, AUDIT, AND DATA LIFECYCLE
-- =========================
CREATE TABLE IF NOT EXISTS anonymized_usage_metrics (
    anonymized_usage_metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_offering_id INTEGER,
    assessment_activity_id INTEGER,
    metric_date TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value REAL NOT NULL,
    dimensions_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (
        unit_offering_id,
        assessment_activity_id,
        metric_date,
        metric_name,
        dimensions_json
    ),
    CHECK (dimensions_json IS NULL OR json_valid(dimensions_json)),
    CONSTRAINT fk_anonymous_metrics_offering
        FOREIGN KEY (unit_offering_id) REFERENCES unit_offerings(unit_offering_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_anonymous_metrics_activity
        FOREIGN KEY (assessment_activity_id)
        REFERENCES assessment_activities(assessment_activity_id)
        ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    audit_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (metadata_json IS NULL OR json_valid(metadata_json)),
    CONSTRAINT fk_audit_events_actor
        FOREIGN KEY (actor_user_id) REFERENCES users(user_id)
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_events_entity
    ON audit_events(entity_type, entity_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_events_actor
    ON audit_events(actor_user_id, created_at);

-- This intentionally has no foreign key to an assessment plan: a Chief Admin
-- permanent deletion may remove the entity while preserving the deletion fact.
CREATE TABLE IF NOT EXISTS data_lifecycle_events (
    data_lifecycle_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER,
    action TEXT NOT NULL CHECK (action IN ('archive', 'restore', 'permanent_delete')),
    entity_type TEXT NOT NULL,
    entity_identifier TEXT NOT NULL,
    details_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (details_json IS NULL OR json_valid(details_json)),
    CONSTRAINT fk_data_lifecycle_actor
        FOREIGN KEY (actor_user_id) REFERENCES users(user_id)
        ON DELETE SET NULL
);
