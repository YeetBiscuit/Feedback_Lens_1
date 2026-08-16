-- Tutorial-group scoped staff allocation.
--
-- Tutorial membership is a hard eligibility boundary for automated marking.
-- Existing marker assignments remain untouched when a student changes group.

ALTER TABLE users
ADD COLUMN account_status TEXT NOT NULL DEFAULT 'active'
    CHECK (account_status IN ('pending', 'active', 'disabled'));

CREATE TABLE tutorial_groups (
    tutorial_group_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_offering_id INTEGER NOT NULL,
    group_code TEXT NOT NULL COLLATE NOCASE,
    group_name TEXT,
    source TEXT NOT NULL DEFAULT 'manual'
        CHECK (source IN ('manual', 'csv', 'api')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_by_user_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (trim(group_code) != ''),
    UNIQUE (unit_offering_id, group_code),
    CONSTRAINT fk_tutorial_groups_offering
        FOREIGN KEY (unit_offering_id) REFERENCES unit_offerings(unit_offering_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_tutorial_groups_creator
        FOREIGN KEY (created_by_user_id) REFERENCES users(user_id)
        ON DELETE SET NULL
);

CREATE TABLE tutorial_group_imports (
    tutorial_group_import_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_offering_id INTEGER NOT NULL,
    uploaded_by_user_id INTEGER NOT NULL,
    source_file_name TEXT NOT NULL,
    source_file_path TEXT NOT NULL,
    source_content_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'previewed'
        CHECK (status IN ('previewed', 'applied', 'partially_applied', 'failed')),
    total_row_count INTEGER NOT NULL DEFAULT 0 CHECK (total_row_count >= 0),
    valid_row_count INTEGER NOT NULL DEFAULT 0 CHECK (valid_row_count >= 0),
    invalid_row_count INTEGER NOT NULL DEFAULT 0 CHECK (invalid_row_count >= 0),
    assigned_count INTEGER NOT NULL DEFAULT 0 CHECK (assigned_count >= 0),
    moved_count INTEGER NOT NULL DEFAULT 0 CHECK (moved_count >= 0),
    unchanged_count INTEGER NOT NULL DEFAULT 0 CHECK (unchanged_count >= 0),
    error_message TEXT,
    previewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    applied_at TEXT,
    UNIQUE (unit_offering_id, source_content_hash),
    CONSTRAINT fk_tutorial_imports_offering
        FOREIGN KEY (unit_offering_id) REFERENCES unit_offerings(unit_offering_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_tutorial_imports_uploader
        FOREIGN KEY (uploaded_by_user_id) REFERENCES users(user_id)
        ON DELETE RESTRICT
);

CREATE TABLE student_tutorial_memberships (
    student_tutorial_membership_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_offering_id INTEGER NOT NULL,
    student_id INTEGER NOT NULL,
    tutorial_group_id INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual'
        CHECK (source IN ('manual', 'csv', 'api')),
    tutorial_group_import_id INTEGER,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_tutorial_memberships_offering
        FOREIGN KEY (unit_offering_id) REFERENCES unit_offerings(unit_offering_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_tutorial_memberships_student
        FOREIGN KEY (student_id) REFERENCES students(student_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_tutorial_memberships_group
        FOREIGN KEY (tutorial_group_id) REFERENCES tutorial_groups(tutorial_group_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_tutorial_memberships_import
        FOREIGN KEY (tutorial_group_import_id)
        REFERENCES tutorial_group_imports(tutorial_group_import_id)
        ON DELETE SET NULL
);

CREATE UNIQUE INDEX uq_active_student_tutorial_membership
    ON student_tutorial_memberships(unit_offering_id, student_id)
    WHERE active = 1;

CREATE TABLE tutorial_group_staff (
    tutorial_group_staff_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tutorial_group_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    allocation_weight INTEGER NOT NULL DEFAULT 1 CHECK (allocation_weight > 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    assigned_by_user_id INTEGER,
    assigned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TEXT,
    UNIQUE (tutorial_group_id, user_id),
    CONSTRAINT fk_tutorial_group_staff_group
        FOREIGN KEY (tutorial_group_id) REFERENCES tutorial_groups(tutorial_group_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_tutorial_group_staff_user
        FOREIGN KEY (user_id) REFERENCES users(user_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_tutorial_group_staff_assigner
        FOREIGN KEY (assigned_by_user_id) REFERENCES users(user_id)
        ON DELETE SET NULL
);

CREATE TABLE tutorial_group_import_rows (
    tutorial_group_import_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tutorial_group_import_id INTEGER NOT NULL,
    source_row_number INTEGER NOT NULL CHECK (source_row_number > 1),
    raw_data_json TEXT,
    institution_student_identifier TEXT COLLATE NOCASE,
    group_code TEXT COLLATE NOCASE,
    student_id INTEGER,
    previous_tutorial_group_id INTEGER,
    tutorial_group_id INTEGER,
    action TEXT NOT NULL
        CHECK (action IN ('assign', 'move', 'unchanged', 'invalid')),
    validation_error TEXT,
    applied_at TEXT,
    CHECK (raw_data_json IS NULL OR json_valid(raw_data_json)),
    CONSTRAINT fk_tutorial_import_rows_import
        FOREIGN KEY (tutorial_group_import_id)
        REFERENCES tutorial_group_imports(tutorial_group_import_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_tutorial_import_rows_student
        FOREIGN KEY (student_id) REFERENCES students(student_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_tutorial_import_rows_previous_group
        FOREIGN KEY (previous_tutorial_group_id)
        REFERENCES tutorial_groups(tutorial_group_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_tutorial_import_rows_group
        FOREIGN KEY (tutorial_group_id) REFERENCES tutorial_groups(tutorial_group_id)
        ON DELETE SET NULL
);

CREATE TABLE assessment_allocation_policies (
    assessment_plan_id INTEGER PRIMARY KEY,
    strategy TEXT NOT NULL CHECK (strategy = 'tutorial_groups'),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    enabled_by_user_id INTEGER NOT NULL,
    enabled_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_allocation_policies_plan
        FOREIGN KEY (assessment_plan_id) REFERENCES assessment_plans(assessment_plan_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_allocation_policies_enabler
        FOREIGN KEY (enabled_by_user_id) REFERENCES users(user_id)
        ON DELETE RESTRICT
);

CREATE TABLE staff_activation_invites (
    staff_activation_invite_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    unit_offering_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'revoked', 'expired')),
    issued_by_user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_staff_invites_user
        FOREIGN KEY (user_id) REFERENCES users(user_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_staff_invites_offering
        FOREIGN KEY (unit_offering_id) REFERENCES unit_offerings(unit_offering_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_staff_invites_issuer
        FOREIGN KEY (issued_by_user_id) REFERENCES users(user_id)
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX uq_pending_staff_activation_invite
    ON staff_activation_invites(user_id)
    WHERE status = 'pending';

ALTER TABLE marker_assignments
ADD COLUMN allocation_source TEXT NOT NULL DEFAULT 'manual'
    CHECK (allocation_source IN ('manual', 'equal', 'automatic', 'tutorial_groups'));

ALTER TABLE marker_assignments
ADD COLUMN tutorial_group_id INTEGER
    REFERENCES tutorial_groups(tutorial_group_id) ON DELETE SET NULL;

CREATE INDEX idx_tutorial_groups_offering
    ON tutorial_groups(unit_offering_id, active, group_code);
CREATE INDEX idx_tutorial_memberships_group
    ON student_tutorial_memberships(tutorial_group_id, active, student_id);
CREATE INDEX idx_tutorial_group_staff_group
    ON tutorial_group_staff(tutorial_group_id, active, user_id);
CREATE INDEX idx_tutorial_import_rows_import
    ON tutorial_group_import_rows(tutorial_group_import_id, action);
CREATE INDEX idx_marker_assignments_tutorial_group
    ON marker_assignments(tutorial_group_id, active, marker_user_id);
