# Unit Generation Guide

This guide covers `generate_unit.py`, which creates a reviewable synthetic unit package from one course description.

Unit generation is separate from ingestion. The generator writes files and audit metadata only. It does not import specs, rubrics, materials, or submissions into the feedback pipeline. Review the generated content first, then run `ingest_unit.py` when you are ready.

## What It Generates

`generate_unit.py` runs the structured curriculum prompt sequence and writes a unit-level folder:

```text
documents/units/{COURSE_CODE}/
  unit_manifest.json
  schema.json
  consistency_audit.txt
  lectures/
    week_01_{topic_slug}.txt
  tutorials/
    {assignment_slug}_worksheet.pdf
    {assignment_slug}_sample_answers.pdf
  assignments/
    {assignment_slug}/
      spec.pdf
      rubric.pdf
      submissions/
        submission_HD.pdf
        submission_D.pdf
        submission_C.pdf
        submission_P.pdf
  resources/
```

The generated package is local working data. It is ignored by git under `documents/units/`.

## Command

Generate from an inline description:

```powershell
python generate_unit.py --description "A third-year software engineering unit on secure web application design." --year 2026 --semester "Semester 1"
```

Generate from a text file:

```powershell
python generate_unit.py --description-file course_description.txt --year 2026 --semester "Semester 1"
```

Provider options:

```powershell
python generate_unit.py --description-file course_description.txt --provider qwen --model qwen3.5-plus --temperature 0.2
```

Gemini is also available:

```powershell
python generate_unit.py --description-file course_description.txt --provider gemini --model gemini-2.5-flash --temperature 0.2
```

The CLI prints live progress as it works: run creation, each model-call stage, each generated file path, and the final review/ingestion command. Use `--quiet` only when you want the final summary without the stage-by-stage console output.

Example progress output:

```text
[unit-gen] Created curriculum generation run 12 using qwen:qwen3.5-plus.
[unit-gen] Starting course schema generation (step_id=41).
[unit-gen] Completed course schema generation (step_id=41).
[unit-gen] Schema resolved for COMP3001: 2 assignment(s), 12 topic week(s).
[unit-gen] Preparing unit folder: documents\units\COMP3001
[unit-gen] Wrote schema: documents\units\COMP3001\schema.json
[unit-gen] Starting A1 assignment specification (step_id=42).
...
[unit-gen] Completed curriculum generation run 12.
```

Suppress live progress if you are running from a script:

```powershell
python generate_unit.py --description-file course_description.txt --quiet
```

## Required Setup

Initialise the database first:

```powershell
python build.py
```

Set the provider API key before generation. For Qwen:

```powershell
$env:QWEN_API_KEY="your_key_here"
```

For Gemini:

```powershell
$env:GEMINI_API_KEY="your_key_here"
```

## Review-First Workflow

1. Run `generate_unit.py`.
2. Open the generated folder under `documents/units/{COURSE_CODE}/`.
3. Review and edit `schema.json`, assignment specs, rubrics, lecture transcripts, worksheets, sample answers, synthetic submissions, and `consistency_audit.txt`.
4. Run a dry-run import:

   ```powershell
   python ingest_unit.py documents/units/{COURSE_CODE} --dry-run
   ```

5. Import the reviewed package:

   ```powershell
   python ingest_unit.py documents/units/{COURSE_CODE}
   ```

6. Use the printed `submission_id` values for feedback generation:

   ```powershell
   python generate_feedback.py <submission_id> --provider qwen
   ```

## What Is Stored During Generation

Generation records are stored in SQLite for traceability:

- `curriculum_generation_runs` records the high-level generation run, source description, provider, model, status, output folder, and generated schema JSON.
- `curriculum_generation_steps` records each stage prompt, raw response, parsed output where applicable, status, and lock timestamp.
- `curriculum_artifacts` records generated file paths, text content, and content hashes.

Generated artifacts are linked to imported spec/rubric/material/submission rows only after you run `ingest_unit.py`.

## Stage Summary

The generator follows these stages:

1. Course schema JSON.
2. Assignment specifications and rubrics.
3. Lecture transcripts, tutorial worksheets, and sample answers.
4. HD, D, C, and P synthetic submissions per assignment.
5. Consistency audit report.

Each model call is recorded as a separate generation step. Synthetic submissions are generated with separate calls for each grade band.

The console progress mirrors these database steps, so the user can see which part of the run is currently active without opening SQLite.

## Review Checklist

Before ingestion, check:

- `schema.json` has the expected course code, title, weeks, learning outcomes, topics, and assignments.
- Assignment weights, due weeks, linked topics, and assessed learning outcomes make sense.
- Specs and rubrics align with each other.
- Lecture transcripts cover the content needed for linked assignments.
- Tutorial worksheets and sample answers are useful and not answer-leaking in student-facing materials.
- Synthetic submissions are clearly differentiated as HD, D, C, and P.
- `consistency_audit.txt` does not flag issues you want to fix first.

## After Ingestion

`ingest_unit.py` creates or reuses the unit and assignment rows, imports specs/rubrics/submissions, and embeds unit materials into ChromaDB.

After ingestion:

- assignment specs are available in `assignment_specs`
- rubrics and parsed criteria are available in `rubrics` and `rubric_criteria`
- lectures/tutorials/sample answers are available in `unit_materials` and ChromaDB
- synthetic submissions are available in `student_submissions`

Feedback generation works from those imported records, not directly from generated files.

## Troubleshooting

If feedback generation says no submission, spec, rubric, or course materials were found, confirm you have run:

```powershell
python ingest_unit.py documents/units/{COURSE_CODE}
```

If generation succeeds but the course code is not what you expected, edit `schema.json` and `unit_manifest.json` before ingestion.

If repeated ingestion creates more versions than expected, use the default `ingest_unit.py` behavior to skip unchanged files by content hash. Use `--force` only when you intentionally want new versions.
