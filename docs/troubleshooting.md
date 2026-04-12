# Troubleshooting Guide

This guide covers the most common issues when setting up data, importing files, ingesting resources, and generating feedback.

## Missing API Key

Symptom:

```text
Missing API key. Please set environment variable QWEN_API_KEY.
```

Fix:

```powershell
$env:QWEN_API_KEY="your_key_here"
```

Then rerun `python generate_feedback.py ...`.

## Unsupported Provider

Symptom:

```text
Unsupported LLM provider '...'
```

Fix:

- use a registered provider name such as `qwen`
- check `feedback_lens/feedback/llm/providers.py` if you added a custom provider

## No Assignment Or Submission Found

Symptom examples:

- `No assignment found with assignment_id=...`
- `No student submission found with submission_id=...`

Fix:

- confirm the relevant row exists in SQLite
- use `python main.py` and inspect the relevant tables
- make sure you are using `assignment_id` for imports and `submission_id` for generation

## No Assignment Specification Found

Symptom:

```text
No assignment specification found for assignment_id=...
```

Fix:

- import the spec first with `python import_documents.py assignment-spec ...`
- confirm a row exists in `assignment_specs`

## No Rubric Or No Rubric Criteria Found

Symptom examples:

- `No rubric found for assignment_id=...`
- `No rubric criteria found for rubric_id=...`
- `No rubric criteria could be extracted from the rubric PDF tables.`

Fix:

- confirm the rubric file is a PDF
- confirm the rubric is mostly table-based
- re-import the rubric and inspect `rubrics.structured_rubric_json`
- inspect `rubric_criteria` to see whether criteria were parsed

If the PDF layout is unusual, the current rubric parser may need adjustment.

## No Retrieved Course Material Chunks

Symptom:

```text
No course material chunks were retrieved from collection '...'. Ingest unit materials before generating feedback.
```

Fix:

- ingest at least one unit material with `python ingest.py ...`
- make sure the material was ingested for the same unit as the assignment
- confirm rows exist in `unit_materials`, `material_chunks`, and `chunk_embedding_map`
- confirm `chromadb/` still exists on your machine

## Chroma Collection Does Not Exist

Symptom:

```text
Collection '...' does not exist in './chromadb'.
```

Fix:

- no unit materials have been embedded yet for that unit, or
- your local `chromadb/` was deleted or reset

Re-ingest the unit materials for that unit.

## Repeated Ingestion Creates Extra Records

Symptom:

- duplicate-looking unit materials or multiple versions of documents

Explanation:

- specs, rubrics, and submissions are versioned
- unit-material ingestion currently does not deduplicate identical files

Fix:

- inspect the relevant tables and delete unwanted rows manually if needed
- if local data is messy and disposable, use the full reset in `python main.py`

## Want A Clean Slate

Use:

```powershell
python main.py
```

Then choose option `1` and type `YES`.

This clears both:

- `feedback_system.db`
- `chromadb/`

## Path Portability Confusion

If a file was imported successfully, later generation does not require reopening that original file path. The extracted text is already stored in SQLite.

However:

- re-importing or re-ingesting does require the source file to still exist at the path you provide
- each user must maintain their own local `documents/` files

## Useful Diagnostics

Check imported specs:

```sql
SELECT spec_id, assignment_id, version, source_file_path
FROM assignment_specs
ORDER BY spec_id DESC;
```

Check imported rubrics:

```sql
SELECT rubric_id, assignment_id, version, source_file_path
FROM rubrics
ORDER BY rubric_id DESC;
```

Check parsed criteria:

```sql
SELECT rubric_id, criterion_id, criterion_name, criterion_order
FROM rubric_criteria
ORDER BY rubric_id DESC, criterion_order;
```

Check ingested materials:

```sql
SELECT material_id, unit_id, material_type, title, source_file_path
FROM unit_materials
ORDER BY material_id DESC;
```

Check generation runs:

```sql
SELECT generation_id, submission_id, llm_provider, llm_model, status, error_message
FROM generation_runs
ORDER BY generation_id DESC;
```
