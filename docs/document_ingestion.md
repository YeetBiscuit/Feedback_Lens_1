# Document Ingestion Guide

This guide covers how files are organised, how they are imported, and how unit materials are automatically chunked and embedded.

## Canonical Document Layout

Keep local files under:

- `documents/resources/`
- `documents/specifications/`
- `documents/rubrics/`
- `documents/submissions/`

These directories are local working data and are not intended to be shared through git.

## Supported File Types

- unit materials: `.pdf` and `.txt`
- assignment specifications: `.pdf` and `.txt`
- rubric imports: `.pdf`
- student submissions: `.pdf` and `.txt`

## Path Storage

If a file lives inside the repository, its stored path is normalised to a project-relative path. That makes metadata more portable than absolute machine-specific paths.

The extracted text is the operational source of truth. The stored file path is metadata only.

## Unit Materials

Unit materials are the only documents currently embedded into ChromaDB for retrieval.

Common examples:

- lecture transcripts
- lecture slides
- tutorial sheets
- readings
- sample solutions

### Command

```bash
python ingest.py <file_path> <unit_id> <material_type> <title> [week_number]
```

Example:

```bash
python ingest.py "documents/resources/week3_transcript.txt" 1 lecture_transcript "Week 3 Transcript" 3
```

### What The Pipeline Does

When you run `ingest.py`, the system:

1. extracts pages from the source file
2. stores the document in `unit_materials`
3. chunks the extracted text into `material_chunks`
4. embeds the chunks with `all-MiniLM-L6-v2`
5. stores vectors in the local `chromadb/` directory
6. stores vector-to-chunk links in `chunk_embedding_map`

### Chunking Behavior

The current chunker is a naive sliding-window chunker:

- default chunk size: `500` words
- default overlap: `100` words
- page ranges are preserved per chunk

Chunking happens in `feedback_lens/file_management/indexing/chunking.py` through `chunk_pages()`, which currently delegates to `naive_chunking()`.

### Embedding Behavior

Embedding is handled in `feedback_lens/file_management/indexing/embedding.py`.

Current defaults:

- embedding model: `all-MiniLM-L6-v2`
- vector store: local persistent ChromaDB in `chromadb/`
- collection name: derived from `unit_code`, `year`, and `semester`

Materials for the same unit accumulate in the same Chroma collection.

## Assignment Specifications

Assignment specifications are stored in SQLite and versioned by assignment.

### Command

```bash
python import_documents.py assignment-spec <assignment_id> <file_path>
```

Example:

```bash
python import_documents.py assignment-spec 1 "documents/specifications/assignment1.pdf"
```

### What Gets Stored

- `source_file_path`
- `raw_text`
- `cleaned_text`
- `retrieval_cues_json`
- `version`

During spec import, the system also prepares a list of retrieval-ready cues from the assignment brief. These cues are stored in `retrieval_cues_json` and later used to retrieve relevant unit materials from ChromaDB.

If you import a new spec for the same assignment, the version increases and the generation pipeline uses the latest version and its associated cue list.

## Rubrics

Rubrics are imported from PDF and stored in SQLite. They are versioned by assignment.

### Command

```bash
python import_documents.py rubric <assignment_id> <file_path>
```

Example:

```bash
python import_documents.py rubric 1 "documents/rubrics/assignment1_rubric.pdf"
```

### What The Rubric Import Does

1. extracts raw text from the PDF
2. extracts table-like structures with PyMuPDF
3. stores the extracted structure in `rubrics.structured_rubric_json`
4. parses criterion rows into `rubric_criteria`

The parser preserves the rubric's original performance column headers rather than remapping them to internal grade-band labels.

If no rubric criteria can be extracted from the tables, the import fails.

### What Gets Stored

- `source_file_path`
- `raw_text`
- `cleaned_text`
- `structured_rubric_json`
- parsed rows in `rubric_criteria`

If you import a later rubric for the same assignment, the newer version becomes the one used during feedback generation.

## Student Submissions

Student submissions are stored in SQLite and versioned by assignment plus student identifier.

### Command

```bash
python import_documents.py submission <assignment_id> <student_identifier> <file_path> [--submitted-at YYYY-MM-DDTHH:MM:SS]
```

Example:

```bash
python import_documents.py submission 1 student_001 "documents/submissions/student_001.pdf"
```

Example with submission timestamp:

```bash
python import_documents.py submission 1 student_001 "documents/submissions/student_001.pdf" --submitted-at 2026-04-12T18:30:00
```

### What Gets Stored

- `original_file_path`
- `raw_text`
- `cleaned_text`
- `submitted_at`
- `version`

The generation pipeline uses the specific row referenced by `submission_id`.

## Re-Import Expectations

- re-importing a spec creates a newer spec version
- re-importing a rubric creates a newer rubric version
- re-importing a submission for the same student and assignment creates a newer submission version
- re-ingesting a unit material creates a new `unit_materials` row and new chunk/vector records

There is no deduplication layer yet, so repeated ingestion of the same resource will create additional records.
