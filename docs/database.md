# Database Guide

This project uses two local storage layers:

- `feedback_system.db` for structured data and extracted text
- `chromadb/` for vector embeddings of unit materials

Both are intended to be local to each developer's machine.

## Local-Only Workflow

Feedback Lens is designed so that each user maintains their own:

- SQLite database
- Chroma vector store
- document files under `documents/`

Do not rely on another user's local rows, vectors, or uploaded files being present after cloning the repository.

## Initialising vs Resetting

### Initialise or migrate

Use:

```bash
python build.py
```

This creates the baseline schema when needed, then applies every pending
versioned migration in order. It does not clear existing rows or vectors.
Applied versions and migration checksums are recorded in `schema_migrations`.

Normal product connections only validate the schema version. They never create
tables, add columns, create demo accounts, or otherwise patch the database at
runtime. If a database is out of date, run `python build.py` before starting the
product.

### Full reset

Use:

```bash
python main.py
```

Then choose menu option `1`.

That reset:

- deletes `feedback_system.db`
- deletes SQLite sidecar files such as `-journal`, `-wal`, and `-shm`
- deletes the full `chromadb/` directory
- recreates the schema from the packaged schema in `feedback_lens/setup/schema.sql`
- recreates an empty `chromadb/` directory

The operator must type `YES` exactly to proceed.

## Frozen Domain Model

Database V2 adds a durable domain model around the original generation tables.
The original tables remain temporarily as a compatibility layer so the current
generation, review, import, and export features continue to work unchanged.

The final `feature_completion` migration does not introduce another domain
redesign. It only adds the persistence required by the frozen student-account
and administrative-upload workflows:

- roster CSV preview, mapped rows, import differences, and withdrawal review;
- roster-restricted activation and password-reset token state;
- durable processing jobs for the supported uploads and account email;
- per-folder Moodle batch matching and exception review;
- one explicit current summative attempt per student and immutable invalid
  history after replacement;
- student institutional name/email, login session invalidation, and scoping
  material deactivation.

Schema changes are now frozen for this delivery. Multi-file submissions, OCR,
Moodle API synchronisation, student summative upload, and provider-specific
email infrastructure are future scope rather than placeholders in this schema.

### Organisation, permissions, and enrolment

- `organizations`, `courses`, and `unit_offerings` separate the academic
  definition of a course from a specific teaching period.
- `organization_role_assignments` gives a Chief Admin organization-wide scope.
- `unit_role_assignments` gives Unit Admin and Staff roles within an offering.
- `students` stores the stable institution student identifier.
- `student_enrolments` records unit membership and leaves the enrolment source
  open for manual, Moodle, or later API imports.

The legacy global `users.role` column is retained for current screens, but new
authorization work should use the scoped role-assignment tables.

### Versioned assessment configuration

- `assessment_plans` represents one assessment in a unit offering.
- `assessment_plan_versions` freezes the specification, rubric, maximum mark,
  and configuration used for a period of grading.
- `assessment_activities` defines the formative and summative channels
  independently, including the formative attempt limit and disclaimer.

Activating a replacement plan version preserves the previous configuration
instead of rewriting history.

### Moodle imports, attempts, and participants

- `submission_batches` records an Admin-uploaded Moodle ZIP and import outcome.
- `submission_attempts` stores both Moodle summative submissions and private
  student formative attempts.
- `submission_files` supports multiple files per attempt and retains the
  original folder-relative path.
- `submission_participants` supports one student now while allowing future
  group submissions without redesigning the attempt table.

### Allocation and marking workflow

- `marker_assignments` retains assignment history and enforces one active
  primary marker per submission.
- `submission_workflow_states` stores allocation, AI generation, and human
  marking as separate state dimensions.
- `submission_workflow_events` provides an append-only history for assignment,
  return, regeneration, confirmation, and invalidation actions.
- `feedback_revisions`, `criterion_feedback_revisions`, and
  `overall_feedback_revisions` preserve every AI, Marker, and Admin revision.
- `assessment_results` points to the current revision and retains calculated
  versus final marks plus Marker/Admin confirmation metadata.

Moodle remains the authoritative release channel. Database V2 deliberately has
no student-visible "released" state for summative feedback.

### Reproducibility and retrieval provenance

- `index_builds` and `index_build_items` identify the exact vector index used.
- `generation_input_snapshots` freezes the submission, assessment version,
  rubric, specification, index build, model configuration, and code version
  associated with a generation.
- `model_usage_records` supports token, latency, and cost accounting.
- `retrieval_queries_v2` and `retrieval_hits_v2` preserve query-level ranking
  evidence and whether each hit was used in the prompt.

### Privacy, audit, and lifecycle

- `anonymized_usage_metrics` stores aggregate formative usage only.
- `audit_events` records security- and workflow-relevant actions.
- `data_lifecycle_events` preserves archive and permanent-deletion facts even
  after the deleted entity is gone.

Student-specific formative data is private to that student. Staff and Admin
reporting must use anonymous aggregates; formative content must never become a
summative generation input.

## Legacy Compatibility Tables

### Teaching and assignment structure

- `units` - unit metadata such as code, name, semester, and year
- `assignments` - assignments linked to a unit
- `tutors` and `unit_tutors` - tutor records and unit-tutor mapping

### Imported assignment documents

- `assignment_specs` - extracted assignment specification text and `retrieval_cues_json`, versioned per assignment
- `rubrics` - extracted rubric text and `structured_rubric_json`, versioned per assignment
- `rubric_criteria` - parsed criterion rows linked to one rubric version

### Unit materials and retrieval index metadata

- `unit_materials` - extracted course resources such as transcripts, slides, readings, or sample solutions
- `material_chunks` - chunked text segments created from unit materials
- `chunk_embedding_map` - links each chunk to a vector ID and vector-store collection

### Student work and generated feedback

- `student_submissions` - extracted submission text, versioned per assignment and student
- `generation_runs` - one row per generation attempt, including provider, model, retrieval limits, strategy, and status
- `retrieval_planning_records` - planner prompts, raw responses, and LLM-generated retrieval cues for planned retrieval runs
- `retrieval_records` - retrieved chunks associated with a generation run
- `criterion_feedback` - criterion-level generated comments
- `overall_feedback` - overall summary, strengths, improvements, and one overall grade band

### Human review

- `human_reviews` - manual review and adjudication layer for generated outputs
- `embedded_feedback_evaluations` - optional student and educator usefulness ratings, comments, research-use consent, response lifecycle timestamps, and a keyed pseudonymous rater value used to keep authorised users' responses separate; direct user IDs, names, and email addresses are not stored in this table

## Versioning Rules

- `assignment_specs` are versioned by `assignment_id`
- `rubrics` are versioned by `assignment_id`
- `student_submissions` are versioned by `assignment_id` plus `student_identifier`

The feedback pipeline always uses:

- the latest assignment specification for the assignment
- the latest rubric for the assignment
- the specific submission identified by `submission_id`

## What Is Stored In SQLite

For imported documents, SQLite stores:

- original or normalised source path metadata
- raw extracted text
- cleaned text used by the pipeline
- rubric table JSON for rubric imports
- parsed rubric criteria

This means feedback generation works from database records even if the original file path later changes.

## What Is Stored In ChromaDB

Only unit materials are embedded into ChromaDB.

The vector store contains:

- chunk text
- chunk metadata such as page range
- vector IDs linked back to `material_chunks` through `chunk_embedding_map`

Assignment specs, rubrics, and student submissions are stored in SQLite but are not currently embedded into ChromaDB.

## Schema Migrations

The baseline schema is `feedback_lens/setup/schema.sql`. Ordered migration SQL
lives under `feedback_lens/setup/migrations/`, and migration orchestration and
data backfill live in `feedback_lens/db/migrations.py`.

The current schema version is 3:

1. V1 stabilization converts former runtime patches into a one-time migration
   and normalizes legacy indexes and columns.
2. V2 creates the scoped role, assessment, submission workflow, revision,
   provenance, audit, and privacy tables, then backfills existing data.
3. `feature_completion` supplies the bounded account and upload state listed
   above without replacing the V2 domain model.

Migration execution is transactional, checksum-verified, idempotent, and ends
with a foreign-key check. Application code should call `connect_db()`; setup and
deployment code should call `initialise_database()` explicitly.

## Useful Inspection Queries

Check the latest imported documents:

```sql
SELECT assignment_id, version, source_file_path
FROM assignment_specs
ORDER BY assignment_id, version DESC;
```

```sql
SELECT assignment_id, version, source_file_path
FROM rubrics
ORDER BY assignment_id, version DESC;
```

Check student submissions:

```sql
SELECT submission_id, assignment_id, student_identifier, version, submitted_at
FROM student_submissions
ORDER BY submission_id DESC;
```

Check generation output:

```sql
SELECT generation_id, submission_id, llm_provider, llm_model, status, completed_at
FROM generation_runs
ORDER BY generation_id DESC;
```

```sql
SELECT *
FROM overall_feedback
ORDER BY generation_id DESC;
```
