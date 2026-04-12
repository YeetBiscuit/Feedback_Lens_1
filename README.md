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
- optional Qwen API key if you want to run feedback generation

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
6. Generate feedback with `python generate_feedback.py <submission_id> --provider qwen`.

The detailed end-to-end workflow lives in [docs/usage.md](docs/usage.md).

## Code Layout

- `feedback_lens/setup/` - setup and schema logic
- `feedback_lens/db/` - database connection and schema-update helpers
- `feedback_lens/file_management/` - document readers, importers, parsing, ingestion, chunking, and embedding
- `feedback_lens/feedback/` - retrieval, prompting, LLM providers, and feedback pipeline
- `feedback_lens/cli/` - internal CLI implementations
- root `build.py`, `main.py`, `ingest.py`, `import_documents.py`, and `generate_feedback.py` remain as thin user-facing entry points
- `documents/` - local document root
- `chromadb/` - local vector store
