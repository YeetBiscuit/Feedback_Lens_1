# Usage Guide

This guide shows the normal operator workflow from database setup through feedback generation.

## Typical Workflow

There are two supported ways to prepare data before feedback generation.

Manual workflow:

1. Initialise your local database
2. Create a unit
3. Create an assignment linked to that unit
4. Import the assignment specification
5. Import the rubric PDF
6. Ingest unit materials
7. Import a student submission
8. Generate feedback
9. Review the saved results

Whole-unit workflow:

1. Initialise your local database
2. Prepare a unit folder under `documents/units/{COURSE_CODE}/`
3. Import the reviewed unit package with `ingest_unit.py`
4. Generate feedback for the imported `submission_id` values
5. Review the saved results

## 1. Initialise Your Local Environment

```powershell
.venv\Scripts\Activate.ps1
python build.py
```

If you want to fully clear your local data first:

```powershell
python main.py
```

Then choose option `1` and type `YES`.

## 2. Create A Unit And Assignment

Run:

```powershell
python main.py
```

Use:

- option `2` to add a unit through guided prompts
- option `3` to add an assignment and link it to an existing unit
- option `9` only if you still want raw SQL

The unit flow prompts for:

- unit code
- unit name
- semester
- year

The assignment flow prompts for:

- unit selection
- assignment name
- assignment type
- description
- due date

## 3. Option A: Manual Import

Use this path when you want to add one assignment, rubric, material file, or submission at a time.

### Import Assignment Documents

Import the assignment specification:

```powershell
python import_documents.py assignment-spec 1 "documents/specifications/assignment1.pdf"
```

Import the rubric:

```powershell
python import_documents.py rubric 1 "documents/rubrics/assignment1_rubric.pdf"
```

The `1` in both commands is the `assignment_id`.

### Ingest Unit Materials

Example:

```powershell
python ingest.py "documents/resources/week3_transcript.txt" 1 lecture_transcript "Week 3 Transcript" 3
python ingest.py "documents/resources/week4_transcript.txt" 1 lecture_transcript "Week 4 Transcript" 4
```

The `1` here is the `unit_id`.

### Import A Student Submission

Example:

```powershell
python import_documents.py submission 1 student_001 "documents/submissions/student_001.pdf"
```

The command will print the new `submission_id`. That is the ID you use for feedback generation.

## 4. Option B: Whole-Unit Ingestion

Use this path when you already have a complete unit folder, such as one produced by `generate_unit.py` or prepared manually.

Expected folder shape:

```text
documents/units/{COURSE_CODE}/
  schema.json
  unit_manifest.json
  lectures/
  tutorials/
  assignments/
    {assignment_slug}/
      spec.pdf
      rubric.pdf
      submissions/
        student_001.pdf
  resources/
```

Import the reviewed package:

```powershell
python ingest_unit.py documents/units/{COURSE_CODE}
```

What `ingest_unit.py` imports:

- unit metadata from `schema.json` and `unit_manifest.json`
- assignments
- assignment specifications
- rubrics and rubric criteria
- student submissions
- unit materials
- ChromaDB embeddings for imported unit materials

By default, unchanged files are skipped using content hashes. Use `--force` only when you intentionally want to import recognized files as new versions:

```powershell
python ingest_unit.py documents/units/{COURSE_CODE} --force
```

The command prints imported `submission_id` values. Use those IDs for feedback generation.

If you are unsure whether the folder layout or manifest is correct, run an optional dry run first. This scans and reports planned imports without writing to SQLite or ChromaDB:

```powershell
python ingest_unit.py documents/units/{COURSE_CODE} --dry-run
```

For generated unit packages, see [unit_generation.md](unit_generation.md).

## 5. Configure The LLM Provider

For Qwen:

```powershell
$env:QWEN_API_KEY="your_key_here"
```

For Gemini:

```powershell
$env:GEMINI_API_KEY="your_key_here"
```

For NVIDIA DeepSeek:

```powershell
$env:NVIDIA_API_KEY="your_key_here"
```

If you need more detail about providers and model selection, see [configuration.md](configuration.md).

## 6. Generate Feedback

Run:

```powershell
python generate_feedback.py <submission_id> --provider qwen --per-cue-top-k 5 --max-final-chunks 10
```

Or with Gemini:

```powershell
python generate_feedback.py <submission_id> --provider gemini --per-cue-top-k 5 --max-final-chunks 10
```

Or with NVIDIA DeepSeek:

```powershell
python generate_feedback.py <submission_id> --provider nvidia_deepseek --per-cue-top-k 5 --max-final-chunks 10
```

Example:

```powershell
python generate_feedback.py 1 --provider qwen --per-cue-top-k 5 --max-final-chunks 10
```

The default generation mode is `retrieval`, which uses assignment-spec cues to retrieve relevant unit-material chunks before prompting the LLM.

You can also run the optional planned retrieval strategy:

```powershell
python generate_feedback.py 1 --provider qwen --mode retrieval --strategy planned --per-cue-top-k 5 --max-final-chunks 10
```

Planned retrieval adds an intermediate LLM planning step. The planner reads the assignment specification, rubric, and student submission, then generates targeted retrieval cues for the course-material search. The baseline retrieval strategy remains the default.

To generate a direct baseline without retrieved course context:

```powershell
python generate_feedback.py 1 --provider qwen --mode direct
```

Direct mode sends only the assignment metadata, structured rubric criteria, assignment specification text, and student submission text to the LLM. It records `pipeline_version=baseline_direct_v1`, `retrieval_strategy=none_direct_v1`, `per_cue_top_k=0`, and `max_final_chunks=0`.

What happens:

1. the system loads the specified submission
2. it loads the latest spec and latest rubric for that submission's assignment
3. in baseline `retrieval` mode, it loads the retrieval cues prepared during spec import
4. with `--strategy planned`, it asks the selected LLM to generate targeted retrieval cues from the spec, rubric, and submission
5. in either retrieval strategy, it queries ChromaDB once per cue, keeps `per_cue_top_k` hits for each cue, deduplicates the matches, and passes up to `max_final_chunks` chunks to the feedback generator
6. in `direct` mode, it skips both ChromaDB and retrieval planning, then uses only the submission, rubric, and spec inputs
7. it sends a strict JSON prompt to the selected provider
8. it saves the result into SQLite

Expected output shape:

```text
Completed generation_run=1 using qwen:qwen3.5-plus in retrieval mode. retrieval_strategy=assignment_spec_multi_cue_v1, retrieval_cues=5, per_cue_top_k=5, max_final_chunks=10, deduplicated_chunks=10, criterion_count=4, overall_grade_band=D.
```

## 7. Inspect Results

List recent generation runs:

```powershell
python review_generation.py list
```

Show the latest generation run in a readable report:

```powershell
python review_generation.py
```

Show a specific run:

```powershell
python review_generation.py show 1
```

Show all captured details for a specific run:

```powershell
python review_generation.py show 1 --full
```

The review command prints:

- generation metadata such as provider, model, prompt template, status, and timestamps
- overall feedback and grade band
- per-criterion feedback in rubric order
- by default, only the saved feedback and run metadata
- with `--full`, the retrieval planner prompt and result if the run used planned retrieval, retrieved chunks with full text, and the raw prompt and response for the feedback generation LLM

Export the latest generation run as JSON:

```powershell
python review_generation.py export --output exports/latest_generation_run.json
```

Export a specific run by typing the `generation_run` id after `export`:

```powershell
python review_generation.py export 1 --format markdown --output exports/generation_run_1.md
```

Export the same run as a self-contained HTML report with light, dark, and system theme controls:

```powershell
python review_generation.py export 1 --format html --output exports/generation_run_1.html
```

Full HTML exports keep the feedback results visible and fold the retrieval planner, retrieved chunks, and raw LLM prompt/response details behind expandable sections.

Export all runs as one JSON file:

```powershell
python review_generation.py export --all --output exports/generation_runs.json
```

Capture all details for a selected run:

```powershell
python review_generation.py export 1 --full --output exports/generation_run_1_full.json
```

By default, export produces a result-only file with generation metadata, overall feedback, and criterion feedback. `--full` additionally includes the retrieval planner prompt, raw planner response, normalized planned cues, retrieved chunks with full text, and the raw prompt and response for the feedback generation LLM. Full exports can be long and may contain sensitive student or assessment content.

If you still want raw SQL, `python main.py` option `9` remains available.

## End-To-End Example

Assume these files exist on your machine:

- `documents/specifications/assignment1.pdf`
- `documents/rubrics/assignment1_rubric.pdf`
- `documents/resources/week3_transcript.txt`
- `documents/resources/week4_transcript.txt`
- `documents/submissions/student_001.pdf`

Run:

```powershell
.venv\Scripts\Activate.ps1
python build.py
$env:QWEN_API_KEY="your_qwen_key_here"
```

For Gemini, set `$env:GEMINI_API_KEY="your_gemini_key_here"` instead and use `--provider gemini`. For NVIDIA DeepSeek, set `$env:NVIDIA_API_KEY="your_nvidia_key_here"` and use `--provider nvidia_deepseek`.

Create the unit and assignment:

1. Run `python main.py`
2. Choose option `2` and enter:
   `FIT2002`, `Data Structures and Algorithms`, `Semester 1`, `2026`
3. Choose option `3`, pick unit `1`, then enter:
   `Assignment 1`, `report`, `Graph traversal analysis report`, `2026-05-01`

Import documents:

```powershell
python import_documents.py assignment-spec 1 "documents/specifications/assignment1.pdf"
python import_documents.py rubric 1 "documents/rubrics/assignment1_rubric.pdf"
python ingest.py "documents/resources/week3_transcript.txt" 1 lecture_transcript "Week 3 Transcript" 3
python ingest.py "documents/resources/week4_transcript.txt" 1 lecture_transcript "Week 4 Transcript" 4
python import_documents.py submission 1 student_001 "documents/submissions/student_001.pdf"
python generate_feedback.py 1 --provider qwen --per-cue-top-k 5 --max-final-chunks 10
```
