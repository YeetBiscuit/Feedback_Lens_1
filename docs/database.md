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

This creates the schema if needed and applies lightweight schema updates. It does not clear existing rows or vectors.

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

## Table Groups

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
- `generation_runs` - one row per generation attempt, including provider, model, strategy, and status
- `retrieval_records` - retrieved chunks associated with a generation run
- `criterion_feedback` - criterion-level generated comments
- `overall_feedback` - overall summary, strengths, improvements, and one overall grade band

### Human review

- `human_reviews` - manual review and adjudication layer for generated outputs

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

## Schema Updates

Database connections go through `feedback_lens.db.connection.connect_db()`, which applies lightweight schema updates automatically. At the moment that includes:

- `generation_runs.llm_provider`
- `overall_feedback.overall_grade_band`

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
