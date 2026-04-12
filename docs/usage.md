# Usage Guide

This guide shows the normal operator workflow from database setup through feedback generation.

## Typical Workflow

1. Initialise your local database
2. Create a unit
3. Create an assignment linked to that unit
4. Import the assignment specification
5. Import the rubric PDF
6. Ingest unit materials
7. Import a student submission
8. Generate feedback
9. Inspect the saved results

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

## 3. Import Assignment Documents

Import the assignment specification:

```powershell
python import_documents.py assignment-spec 1 "documents/specifications/assignment1.pdf"
```

Import the rubric:

```powershell
python import_documents.py rubric 1 "documents/rubrics/assignment1_rubric.pdf"
```

The `1` in both commands is the `assignment_id`.

## 4. Ingest Unit Materials

Example:

```powershell
python ingest.py "documents/resources/week3_transcript.txt" 1 lecture_transcript "Week 3 Transcript" 3
python ingest.py "documents/resources/week4_transcript.txt" 1 lecture_transcript "Week 4 Transcript" 4
```

The `1` here is the `unit_id`.

## 5. Import A Student Submission

Example:

```powershell
python import_documents.py submission 1 student_001 "documents/submissions/student_001.pdf"
```

The command will print the new `submission_id`. That is the ID you use for feedback generation.

## 6. Configure The LLM Provider

For Qwen:

```powershell
$env:QWEN_API_KEY="your_key_here"
```

If you need more detail about providers and model selection, see [configuration.md](configuration.md).

## 7. Generate Feedback

Run:

```powershell
python generate_feedback.py <submission_id> --provider qwen --top-k 5
```

Example:

```powershell
python generate_feedback.py 1 --provider qwen --top-k 5
```

What happens:

1. the system loads the specified submission
2. it loads the latest spec and latest rubric for that submission's assignment
3. it loads the retrieval cues prepared during spec import, queries ChromaDB once per cue, and deduplicates the matched unit-material chunks
4. it sends a strict JSON prompt to the selected provider
5. it saves the result into SQLite

Expected output shape:

```text
Completed generation_run=1 using qwen:qwen3.5-plus. retrieval_cues=5, deduplicated_chunks=5, criterion_count=4, overall_grade_band=D.
```

## 8. Inspect Results

Run:

```powershell
python main.py
```

Use option `9` and try:

```sql
SELECT * FROM generation_runs;
```

```sql
SELECT * FROM overall_feedback;
```

```sql
SELECT rc.criterion_name, cf.strengths, cf.areas_for_improvement,
       cf.improvement_suggestion, cf.suggested_level
FROM criterion_feedback AS cf
JOIN rubric_criteria AS rc ON rc.criterion_id = cf.criterion_id
WHERE cf.generation_id = 1
ORDER BY rc.criterion_order;
```

```sql
SELECT rank_position, chunk_id, similarity_score
FROM retrieval_records
WHERE generation_id = 1
ORDER BY rank_position;
```

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
python generate_feedback.py 1 --provider qwen --top-k 5
```
