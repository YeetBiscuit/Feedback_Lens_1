# Feedback Lens

Feedback Lens is a Python-based foundation for an AI-assisted academic feedback pipeline.
It currently provides data modelling, content ingestion, chunking, and vector indexing for unit materials.

## Current capabilities

- SQLite schema for units, assignments, rubrics, submissions, generation runs, and review records
- Initialisation script for creating the database from `schema.sql`
- Ingestion pipeline for `.pdf` and `.txt` teaching materials
- Naive sliding-window chunking for extracted text
- Embedding generation using `sentence-transformers` (`all-MiniLM-L6-v2`)
- ChromaDB persistence for vector storage
- Interactive database console for listing, inserting, updating, deleting, and custom SQL

## Project layout

- `schema.sql` - full relational schema
- `build.py` - database initialisation logic
- `main.py` - interactive SQLite console
- `ingest.py` - end-to-end ingestion pipeline
- `pdf_reader.py` - PDF page extraction (PyMuPDF)
- `txt_reader.py` - transcript reader (normalised to page format)
- `chunking.py` - chunking strategies (current default: naive sliding window)
- `embedding.py` - embedding and ChromaDB storage
- `LLM/Qwen.py` - helper for calling Qwen via compatible OpenAI API
- `resources/` - sample source material files

## Prerequisites

- Python 3.10+
- Windows, macOS, or Linux
- Optional API key for Qwen integration (`QWEN_API_KEY`)

## Installation

```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
pip install -U pip
pip install chromadb sentence-transformers pymupdf openai
```

## Quick start

### 1) Initialise the database

```bash
python build.py
```

### 2) Add a unit record (required before ingestion)

Use the DB console:

```bash
python main.py
```

Choose option `4` (Insert row), select `units`, then provide values such as:

- `unit_code`: FIT2002
- `unit_name`: Data Structures and Algorithms
- `semester`: Semester 1
- `year`: 2026

### 3) Ingest material (PDF or TXT)

```bash
python ingest.py "resources/your_file.txt" 1 lecture_transcript "Week 1 Transcript" 1
```

Arguments:

- `file_path`
- `unit_id`
- `material_type`
- `title`
- `week_number` (optional)

Pipeline performed:

1. Extract text
2. Insert into `unit_materials`
3. Chunk text into `material_chunks`
4. Embed chunks into ChromaDB collection
5. Map chunk IDs to vector IDs in `chunk_embedding_map`

### 4) Inspect data

Run:

```bash
python main.py
```

Then use options to list tables, view rows, or execute custom SQL.

## Embedding and collection naming

- Embedding model: `all-MiniLM-L6-v2`
- Vectors stored in local `chromadb/`
- Collection names are derived from unit metadata (`unit_code`, `year`, `semester`) and normalised for ChromaDB compatibility

## Notes and limitations

- Preprocessing is currently a placeholder (`cleaned_text = raw_text` in ingestion)
- Chunking is currently naive and word-count based
- Retrieval and feedback generation orchestration are schema-ready but not yet wired end-to-end
- No automated tests are included yet
