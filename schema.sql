-- =====================================================
-- FEEDBACK GENERATION PIPELINE SCHEMA (SQLite)
-- Baseline version
-- =====================================================

PRAGMA foreign_keys = ON;

-- =========================
-- 1. UNITS
-- =========================
CREATE TABLE units (
    unit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_code TEXT NOT NULL,
    unit_name TEXT NOT NULL,
    semester TEXT,
    year INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TRIGGER trg_units_updated_at
AFTER UPDATE ON units
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE units
    SET updated_at = CURRENT_TIMESTAMP
    WHERE unit_id = NEW.unit_id;
END;

-- =========================
-- 2. TUTORS
-- =========================
CREATE TABLE tutors (
    tutor_id INTEGER PRIMARY KEY AUTOINCREMENT,
    institution_identifier TEXT NOT NULL UNIQUE,
    full_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TRIGGER trg_tutors_updated_at
AFTER UPDATE ON tutors
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE tutors
    SET updated_at = CURRENT_TIMESTAMP
    WHERE tutor_id = NEW.tutor_id;
END;

-- =========================
-- 3. UNIT_TUTORS
-- A tutor can belong to multiple units,
-- and a unit can have multiple tutors
-- =========================
CREATE TABLE unit_tutors (
    unit_tutor_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id INTEGER NOT NULL,
    tutor_id INTEGER NOT NULL,
    role TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (unit_id, tutor_id),
    CONSTRAINT fk_unit_tutors_unit
        FOREIGN KEY (unit_id) REFERENCES units(unit_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_unit_tutors_tutor
        FOREIGN KEY (tutor_id) REFERENCES tutors(tutor_id)
        ON DELETE CASCADE
);

-- =========================
-- 4. ASSIGNMENTS
-- =========================
CREATE TABLE assignments (
    assignment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id INTEGER NOT NULL,
    assignment_name TEXT NOT NULL,
    assignment_type TEXT,
    -- e.g. reflection, report, essay, short_answer
    description TEXT,
    due_date TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_assignments_unit
        FOREIGN KEY (unit_id) REFERENCES units(unit_id)
        ON DELETE CASCADE
);

CREATE TRIGGER trg_assignments_updated_at
AFTER UPDATE ON assignments
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE assignments
    SET updated_at = CURRENT_TIMESTAMP
    WHERE assignment_id = NEW.assignment_id;
END;

CREATE INDEX idx_assignments_unit_id ON assignments(unit_id);

-- =========================
-- 5. ASSIGNMENT SPECIFICATIONS
-- =========================
CREATE TABLE assignment_specs (
    spec_id INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    source_file_path TEXT,
    raw_text TEXT,
    cleaned_text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_assignment_specs_assignment
        FOREIGN KEY (assignment_id) REFERENCES assignments(assignment_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_assignment_specs_assignment_id ON assignment_specs(assignment_id);

-- =========================
-- 6. RUBRICS
-- =========================
CREATE TABLE rubrics (
    rubric_id INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    source_file_path TEXT,
    raw_text TEXT,
    cleaned_text TEXT,
    structured_rubric_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_rubrics_assignment
        FOREIGN KEY (assignment_id) REFERENCES assignments(assignment_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_rubrics_assignment_id ON rubrics(assignment_id);

-- =========================
-- 7. RUBRIC CRITERIA
-- =========================
CREATE TABLE rubric_criteria (
    criterion_id INTEGER PRIMARY KEY AUTOINCREMENT,
    rubric_id INTEGER NOT NULL,
    criterion_name TEXT NOT NULL,
    criterion_description TEXT,
    criterion_order INTEGER NOT NULL,
    performance_levels_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_rubric_criteria_rubric
        FOREIGN KEY (rubric_id) REFERENCES rubrics(rubric_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_rubric_criteria_rubric_id ON rubric_criteria(rubric_id);
CREATE INDEX idx_rubric_criteria_order ON rubric_criteria(rubric_id, criterion_order);

-- =========================
-- 8. UNIT MATERIALS
-- =========================
CREATE TABLE unit_materials (
    material_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id INTEGER NOT NULL,
    assignment_id INTEGER,
    material_type TEXT NOT NULL,
    -- e.g. lecture_slide, lecture_transcript, tutorial_sheet, reading, sample_solution
    title TEXT NOT NULL,
    week_number INTEGER,
    source_file_path TEXT,
    raw_text TEXT,
    cleaned_text TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_unit_materials_unit
        FOREIGN KEY (unit_id) REFERENCES units(unit_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_unit_materials_assignment
        FOREIGN KEY (assignment_id) REFERENCES assignments(assignment_id)
        ON DELETE SET NULL
);

CREATE TRIGGER trg_unit_materials_updated_at
AFTER UPDATE ON unit_materials
FOR EACH ROW
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE unit_materials
    SET updated_at = CURRENT_TIMESTAMP
    WHERE material_id = NEW.material_id;
END;

CREATE INDEX idx_unit_materials_unit_id ON unit_materials(unit_id);
CREATE INDEX idx_unit_materials_assignment_id ON unit_materials(assignment_id);
CREATE INDEX idx_unit_materials_type ON unit_materials(material_type);

-- =========================
-- 9. STUDENT SUBMISSIONS
-- =========================
CREATE TABLE student_submissions (
    submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id INTEGER NOT NULL,
    student_identifier TEXT NOT NULL,
    -- can be anonymised ID instead of real student ID
    original_file_path TEXT,
    raw_text TEXT,
    cleaned_text TEXT NOT NULL,
    submitted_at TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_student_submissions_assignment
        FOREIGN KEY (assignment_id) REFERENCES assignments(assignment_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_student_submissions_assignment_id ON student_submissions(assignment_id);
CREATE INDEX idx_student_submissions_student_identifier ON student_submissions(student_identifier);

-- =========================
-- 10. MATERIAL CHUNKS
-- =========================
CREATE TABLE material_chunks (
    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
    material_id INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    chunk_text TEXT NOT NULL,
    section_title TEXT,
    page_number_start INTEGER,
    page_number_end INTEGER,
    token_count INTEGER,
    chunking_strategy TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (material_id, chunk_index),
    CONSTRAINT fk_material_chunks_material
        FOREIGN KEY (material_id) REFERENCES unit_materials(material_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_material_chunks_material_id ON material_chunks(material_id);

-- =========================
-- 11. CHUNK EMBEDDING MAP
-- Maps SQL chunk records to vector DB records
-- =========================
CREATE TABLE chunk_embedding_map (
    embedding_map_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id INTEGER NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_version TEXT,
    vector_store_name TEXT NOT NULL,
    vector_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (chunk_id, embedding_model, embedding_version),
    CONSTRAINT fk_chunk_embedding_map_chunk
        FOREIGN KEY (chunk_id) REFERENCES material_chunks(chunk_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_chunk_embedding_map_chunk_id ON chunk_embedding_map(chunk_id);
CREATE INDEX idx_chunk_embedding_map_vector_id ON chunk_embedding_map(vector_id);

-- =========================
-- 12. GENERATION RUNS
-- One row per feedback generation attempt
-- =========================
CREATE TABLE generation_runs (
    generation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL,
    assignment_id INTEGER NOT NULL,
    rubric_id INTEGER NOT NULL,
    pipeline_version TEXT NOT NULL,
    llm_model TEXT NOT NULL,
    prompt_template_version TEXT NOT NULL,
    retrieval_strategy TEXT,
    temperature REAL,
    top_k INTEGER,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    error_message TEXT,
    CONSTRAINT fk_generation_runs_submission
        FOREIGN KEY (submission_id) REFERENCES student_submissions(submission_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_generation_runs_assignment
        FOREIGN KEY (assignment_id) REFERENCES assignments(assignment_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_generation_runs_rubric
        FOREIGN KEY (rubric_id) REFERENCES rubrics(rubric_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_generation_runs_submission_id ON generation_runs(submission_id);
CREATE INDEX idx_generation_runs_assignment_id ON generation_runs(assignment_id);
CREATE INDEX idx_generation_runs_status ON generation_runs(status);

-- =========================
-- 13. RETRIEVAL RECORDS
-- Stores which chunks were retrieved in a generation run
-- =========================
CREATE TABLE retrieval_records (
    retrieval_record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id INTEGER NOT NULL,
    criterion_id INTEGER,
    query_text TEXT NOT NULL,
    chunk_id INTEGER NOT NULL,
    rank_position INTEGER NOT NULL,
    similarity_score REAL,
    used_in_prompt INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_retrieval_records_generation
        FOREIGN KEY (generation_id) REFERENCES generation_runs(generation_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_retrieval_records_criterion
        FOREIGN KEY (criterion_id) REFERENCES rubric_criteria(criterion_id)
        ON DELETE SET NULL,
    CONSTRAINT fk_retrieval_records_chunk
        FOREIGN KEY (chunk_id) REFERENCES material_chunks(chunk_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_retrieval_records_generation_id ON retrieval_records(generation_id);
CREATE INDEX idx_retrieval_records_criterion_id ON retrieval_records(criterion_id);

-- =========================
-- 14. CRITERION FEEDBACK
-- One row per criterion for one generation run
-- =========================
CREATE TABLE criterion_feedback (
    criterion_feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id INTEGER NOT NULL,
    criterion_id INTEGER NOT NULL,
    strengths TEXT,
    areas_for_improvement TEXT,
    improvement_suggestion TEXT,
    suggested_level TEXT,
    evidence_summary TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (generation_id, criterion_id),
    CONSTRAINT fk_criterion_feedback_generation
        FOREIGN KEY (generation_id) REFERENCES generation_runs(generation_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_criterion_feedback_criterion
        FOREIGN KEY (criterion_id) REFERENCES rubric_criteria(criterion_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_criterion_feedback_generation_id ON criterion_feedback(generation_id);
CREATE INDEX idx_criterion_feedback_criterion_id ON criterion_feedback(criterion_id);

-- =========================
-- 15. OVERALL FEEDBACK
-- One overall comment per generation run
-- =========================
CREATE TABLE overall_feedback (
    overall_feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id INTEGER NOT NULL,
    overall_comment TEXT NOT NULL,
    key_strengths TEXT,
    priority_improvements TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (generation_id),
    CONSTRAINT fk_overall_feedback_generation
        FOREIGN KEY (generation_id) REFERENCES generation_runs(generation_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_overall_feedback_generation_id ON overall_feedback(generation_id);

-- =========================
-- 16. HUMAN REVIEWS
-- Post-generation tutor review / moderation
-- =========================
CREATE TABLE human_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id INTEGER NOT NULL,
    tutor_id INTEGER NOT NULL,
    review_type TEXT NOT NULL,
    -- e.g. tutor_review, calibration_review, moderation
    rating_accuracy INTEGER,
    rating_usefulness INTEGER,
    rating_tone INTEGER,
    approved INTEGER,
    comments TEXT,
    reviewed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_human_reviews_generation
        FOREIGN KEY (generation_id) REFERENCES generation_runs(generation_id)
        ON DELETE CASCADE,
    CONSTRAINT fk_human_reviews_tutor
        FOREIGN KEY (tutor_id) REFERENCES tutors(tutor_id)
        ON DELETE RESTRICT
);

CREATE INDEX idx_human_reviews_generation_id ON human_reviews(generation_id);
CREATE INDEX idx_human_reviews_tutor_id ON human_reviews(tutor_id);