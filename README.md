# Feedback Lens

Feedback Lens is a Python-based baseline Retrieval-Augmented Generation pipeline for academic feedback. It ingests course materials, imports assignment documents, retrieves relevant teaching content, and generates structured rubric-aligned feedback for student submissions.

## What The Project Does

- stores units, assignments, rubrics, submissions, retrieval records, and feedback outputs in SQLite
- ingests unit materials from `.pdf` and `.txt` files
- auto-chunks and embeds course materials into a local Chroma vector store
- imports assignment specifications, rubric PDFs, and student submissions into the database
- prepares retrieval-ready cue lists from imported assignment specifications
- extracts rubric tables into JSON and parsed rubric criteria
- generates structured feedback with a pluggable LLM interface

## Local Data Model

This repository shares code, not operational data.

Each user is expected to maintain their own local:

- `feedback_system.db`
- `chromadb/`
- `documents/` files

These local files are intentionally ignored by git and should be managed separately on each machine.

## Prerequisites

- Python 3.10+
- `pip`
- optional Qwen or Gemini API key if you want to run feedback generation

## Setup

```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
pip install -U pip
pip install chromadb sentence-transformers pymupdf openai
python build.py
```

If you are using Qwen for generation:

```powershell
$env:QWEN_API_KEY="your_key_here"
```

If you are using Gemini for generation:

```powershell
$env:GEMINI_API_KEY="your_key_here"
```

The default feedback provider uses DeepSeek's official API:

```powershell
$env:DEEPSEEK_API_KEY="your_key_here"
```

It defaults to `deepseek-v4-pro`. The lower-cost `deepseek-v4-flash` model can
be selected explicitly with `--model deepseek-v4-flash`.

If you are using NVIDIA's hosted provider (retained as `nvidia_deepseek` for
backwards compatibility):

```powershell
$env:NVIDIA_API_KEY="your_key_here"
```

## Documentation

- [Database guide](docs/database.md)
- [Document ingestion guide](docs/document_ingestion.md)
- [Usage guide](docs/usage.md)
- [Configuration guide](docs/configuration.md)
- [Troubleshooting guide](docs/troubleshooting.md)

## Quick Start

1. Run `python build.py` to initialise your local database.
2. Use `python main.py`, then choose option `2` to add a unit and option `3` to add an assignment.
3. Import assignment documents with `python import_documents.py`.
4. Ingest course materials with `python ingest.py`.
5. Import a student submission with `python import_documents.py submission ...`.
6. Generate feedback with `python generate_feedback.py <submission_id>`. The
   default provider is the official DeepSeek API; `qwen`, `gemini`, and
   `nvidia_deepseek` remain available through `--provider`.
7. Review saved prompts, retrievals, and outputs with `python review_generation.py`.

The detailed end-to-end workflow lives in [docs/usage.md](docs/usage.md).

## Admin Upload Workflow

The web Admin console replaces the per-file `ingest_unit.py` workflow for new
teaching data:

1. Run `python app.py` and log in as a Chief Admin or Unit Admin.
2. Open `/admin/units`.
3. Create the Unit and assessment, import the Moodle roster CSV, and upload
   scoping notes, the assignment specification, and the rubric.
4. Upload Moodle's original **Download submissions in folders** ZIP on the
   assessment page.
5. Review unmatched or invalid records while valid matched submissions
   continue into the system.

`python app.py` starts a local background worker automatically. In a deployed
environment, run `python worker.py` as a separate long-running service and set
`FEEDBACK_LENS_START_WORKER=0` for the web process.

Student accounts are activated from a shared Unit link published in Moodle.
The link only sends an activation email when the submitted student ID and
institutional email match an active roster record. See
[docs/configuration.md](docs/configuration.md) for upload, email, URL, and
security settings.

## Demo Educator Account

`python build.py` seeds a demo educator account when it initialises the
repository's default local database:

- Email: `educator@test.com`
- Password: `123456`
- Role: `educator`
- Display name: `Demo Educator`

The account is linked to tutor identifier `DEV-TUTOR-001` and is assigned to
all units currently present in the local database. Opening an ordinary database
connection never creates accounts or changes the schema.

## Code Layout

- `feedback_lens/setup/` - baseline schema and versioned migration SQL
- `feedback_lens/db/` - database connections, schema validation, and migration runner
- `feedback_lens/file_management/` - document readers, importers, parsing, ingestion, chunking, and embedding
- `feedback_lens/feedback/` - retrieval, prompting, LLM providers, and feedback pipeline
- `feedback_lens/cli/` - internal CLI implementations
- root `build.py`, `main.py`, `ingest.py`, `import_documents.py`, and `generate_feedback.py` remain as thin user-facing entry points
- `documents/` - local document root
- `chromadb/` - local vector store
