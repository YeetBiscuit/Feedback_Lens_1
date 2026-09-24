# System Evolution Log

This log records material changes to the Feedback Lens system, including the
reason for each change, implementation decisions, data impact, compatibility,
verification, known limitations, and follow-up work.

Update this file whenever a change affects system behaviour, persisted data,
retrieval or generation results, operational workflows, or compatibility with
existing Units.

## Privacy and Scope Rules

This log must describe system evolution without exposing live or identifying
data. Every future entry must follow these rules:

- Do not include real Unit or course codes, names, teaching periods, or titles.
- Do not include material titles, source filenames, material IDs, job IDs,
  collection names, user identities, email addresses, or student information.
- Do not include live record counts, vector counts, query text, retrieved
  excerpts, or results that can be tied to a specific Unit or uploaded source.
- Do not reproduce paths containing user names or private storage locations.
- Describe verification with anonymous terms such as "a new Unit", "a legacy
  Unit", "a processed-slide document", or "the test fixture".
- Use synthetic examples whenever an identifier, filename, query, or result is
  needed to explain behaviour.
- Record only reusable system behaviour, implementation decisions,
  compatibility boundaries, generic data impact, tests, and known limitations.
- Before saving an entry, review it for information that could identify a real
  course, material, user, upload, or operational dataset.

## Entry Format

Each entry should include, where relevant:

- **Status**: planned, in progress, complete, superseded, or rolled back
- **Purpose**: the problem or requirement that motivated the change
- **Decision**: the behaviour and boundaries chosen
- **Implementation**: the major system components changed
- **Data impact**: migrations, re-indexing, or live-data changes
- **Compatibility**: effects on existing Units and workflows
- **Verification**: tests and live checks performed
- **Known limitations**: intentionally deferred capabilities or open risks
- **Follow-up**: concrete work that remains

---

## 2026-09-24 — Retrieval Query Semantics and Aggregation Ranking

**Status:** Complete

### Purpose

Separate descriptive cue metadata from retrieval semantics and make final chunk
ranking less sensitive to the number and overlap of retrieval queries.

### Decision

- Retrieval embedding input now uses `cue.text` only; `cue.label` is retained
  as metadata for logging, matched-cue reporting, interfaces, debugging, and
  traceability.
- The fixed repeated-hit ranking bonus was removed from the default aggregation
  score.
- Final chunk ranking is based primarily on the best similarity score, with
  best per-query local rank and `chunk_id` used as deterministic tie-breakers.
- `hit_count`, matched cues, and matched query texts remain available as
  diagnostic metadata but do not affect ranking.

### Rationale

- Separate cue metadata from the content that defines retrieval semantics.
- Improve transparency and interpretability of the retrieval pipeline.
- Reduce ranking sensitivity to the number and overlap of retrieval queries.
- Avoid relying on an unvalidated fixed repeated-hit bonus.
- Preserve repeated-hit and matched-cue information for diagnostics and
  possible future evaluation of retrieval-fusion strategies.
- This design also provides a cleaner basis for controlled evaluation of
  alternative retrieval strategies.

### Data Impact and Compatibility

- No schema migration or vector re-indexing is required.
- Existing cue labels, raw retrieval records, and diagnostic metadata remain
  compatible.
- Future retrieval runs may select or order final prompt chunks differently;
  previously stored generation and retrieval records are unchanged.

### Verification

- Added regression coverage for cue-text-only vector queries, retained label
  metadata, exact chunk deduplication, repeated-hit diagnostics, similarity-led
  ordering, deterministic tie-breakers, final truncation, and raw-hit
  persistence with prompt-use markers.

---

## 2026-09-23 — BGE-M3 Embedding Rollout

**Status:** Complete

### Purpose

Adopt a stronger multilingual embedding model for newly created Units without
invalidating or rebuilding existing MiniLM retrieval indexes.

### Decision

- Existing Units with MiniLM embeddings remain on `all-MiniLM-L6-v2`.
- Existing MiniLM Unit materials are retrieval-only and cannot accept new
  scoping materials.
- New Units use `BAAI/bge-m3` dense embeddings.
- Sparse and ColBERT capabilities remain disabled.
- Embedding model versions and vector collections must not be mixed.

### Implementation

- Default model: `BAAI/bge-m3`
- Pinned revision: `5617a9f61b028005a4858fdac845db406aefb181`
- Vector dimension: 1024
- Distance metric: cosine
- Embeddings: normalized
- New collection suffix: `_bge_m3_5617a9f`
- Database migration: `011_embedding_model_rollout.sql`
- Unit-level model resolution prevents MiniLM/BGE-M3 mixing.
- The administration interface identifies legacy MiniLM Units as read-only.

### Data Impact

- Existing MiniLM vectors were retained without re-embedding.
- Existing MiniLM collection names were retained.
- Index provenance now records the embedding model and pinned revision.
- New BGE-M3 materials are written to model-specific collections.

### Compatibility

- Existing MiniLM retrieval continues to work.
- Legacy Unit materials can still be retrieved, downloaded, deactivated, and
  deleted according to the existing lifecycle rules.
- New uploads and material restoration are blocked for MiniLM Units.

### Verification

- Confirmed that a newly created Unit resolved to the pinned BGE-M3
  configuration.
- Confirmed 1024-dimensional vectors and cosine collection metadata.
- Confirmed English and Chinese retrieval against English lecture transcripts.
- Confirmed that a legacy MiniLM Unit rejected new material.
- Confirmed SQLite foreign-key integrity.

### Known Limitations

- BGE-M3 sparse retrieval is not enabled.
- BGE-M3 ColBERT multi-vector retrieval is not enabled.
- Legacy MiniLM Units require creation of a new Unit or a separately planned
  re-indexing migration before accepting new materials.

---

## 2026-09-23 — AI-Assisted Lecture-Slide JSON Ingestion

**Status:** Complete

### Added Function

Added `ingest_processed_slides()` as the reusable ingestion entry point for
AI-assisted visual-to-text lecture-slide JSON files.

### How the Function Works

1. Load the source file as UTF-8 JSON.
2. Validate the lecture metadata, required slide fields, sequential slide
   numbers, text arrays, visual-element objects, uncertainty notes, and boolean
   instructional-content flags.
3. Preserve the complete source JSON in the material record.
4. Exclude slides marked `has_instructional_content: false` from chunking and
   embedding while retaining them in the source JSON.
5. Create chunks containing five instructional slides with one instructional
   slide of overlap. The final chunk may contain fewer than five slides.
6. Render each chunk deterministically, keeping original slide text, visual
   descriptions, and preprocessing uncertainty in separate labelled sections.
7. Preserve the original first and last slide numbers as the chunk range and
   record the strategy as `ai_visual_to_text_slides_5_overlap_1`.
8. Generate dense embeddings with the Unit's assigned embedding configuration.
9. Store the material, chunks, vectors, vector mappings, and index provenance.
10. Use the source-file hash to reject duplicate active imports.

### How the JSON Is Created

The JSON is produced before ingestion by applying the following reusable prompt
to one lecture-slide deck at a time:

```text
You are preprocessing university lecture slides for use in a text-based
retrieval-augmented generation (RAG) system.

Convert each slide into a faithful, text-accessible representation that
preserves the instructional information available on the original slide.

Process each slide independently. Do not combine, summarise, or infer
information across multiple slides. Process one lecture at a time.

Return exactly one valid UTF-8 JSON object. Do not output Markdown, code fences,
comments, or any text outside the JSON document.

General rules:
- Preserve the original meaning, terminology, numerical values, labels,
  categories, comparisons, and relationships as closely as possible.
- Do not add background knowledge, explanations, interpretations, causal
  claims, conclusions, or inferred lecturer intentions.
- Do not use information from previous or subsequent slides to complete or
  reinterpret the current slide.
- Preserve the slide number and title where available.
- Keep text originally written on the slide separate from text generated to
  represent visual information.
- Record unclear or illegible content in `unclear_content`; never guess.
- Ignore decorative elements, logos, page furniture, page numbers, copyright
  statements, and licensing notices unless they contain instructional content.
- Retain slides with no instructional content and set
  `has_instructional_content` to false.

Text rules:
- Preserve wording and logical heading/bullet hierarchy.
- Store instructional text as individual strings in `text_content`.
- Do not complete or improve incomplete statements.

Visual rules:
- Represent every instructional visual as a separate `visual_elements` object.
- Use one of these types: table, chart, graph, diagram, flowchart, map, image,
  illustration, screenshot, equation, or other.
- For tables, preserve headings and row-column-value relationships.
- For charts and graphs, preserve visible axes, legends, groups, categories,
  variables, trends, comparisons, and legible values without interpretation.
- For diagrams and flowcharts, preserve entities, stages, arrows, ordering,
  grouping, connections, and labels.
- For maps, preserve the title, mapped variable, legend, categories, and only
  clearly visible educationally meaningful geographical patterns.
- Describe images only when they communicate instructional information, and do
  not speculate about identity, motivation, context, meaning, or causality.
- Transcribe equations accurately, preserving variables, operators, symbols,
  and labels.

Use this schema:
{
  "lecture": "<lecture identifier or number>",
  "source_file": "<original source filename>",
  "slides": [
    {
      "slide_number": 1,
      "title": "<slide title or null>",
      "text_content": ["<text item>"],
      "visual_elements": [
        {
          "type": "<table|chart|graph|diagram|flowchart|map|image|illustration|screenshot|equation|other>",
          "title": "<visual title or null>",
          "description": "<faithful text representation of the visual>"
        }
      ],
      "unclear_content": ["<description of unclear content>"],
      "has_instructional_content": true
    }
  ]
}

Formatting rules:
- Use JSON null rather than the strings "None", "null", or "N/A".
- Use [] for empty lists and true/false for booleans.
- Do not use trailing commas or comments.
- Keep all slide records in their original order and include every slide.
- Do not add summary, interpretation, key_points, embedding_text, or other
  derived fields.

Each slide representation must be sufficiently self-contained for a text-only
language model to understand the instructional information originally available
on that slide without seeing the original visual slide. Represent what the
slide contains, not what you think the slide means.
```

---

## 2026-09-23 — SQLite Worker Lock Resilience

**Status:** Complete

### Purpose

Prevent the embedded background worker from terminating when a concurrent
SQLite writer temporarily holds the database lock during material ingestion.

### Incident

A transcript completed ingestion successfully, but the worker's next
idle polling cycle raised `sqlite3.OperationalError: database is locked` while
running stale-job recovery. The material and its vectors were already durable,
but the unhandled exception terminated the background worker thread.

### Decision

- Treat SQLite lock errors as temporary operational contention.
- Retry locked worker polling with bounded exponential backoff.
- Avoid acquiring SQLite write locks during idle polling when there is no work.
- Complete model loading and embedding computation before beginning material
  database writes.
- Retain the existing SQLite journal mode; a WAL migration is not required for
  this fix.

### Implementation

- Stale-job recovery now checks for stale rows before issuing an update.
- Job claiming now checks for an eligible job before starting an immediate
  write transaction, then rechecks inside the transaction for concurrency
  safety.
- The forever worker catches temporary lock errors and retries with exponential
  backoff capped at 10 seconds instead of terminating.
- Worker database connections are explicitly closed after each phase.
- Chunk encoding and vector storage are separate operations.
- BGE-M3 encoding now occurs before the first material insert, so model loading
  and inference do not hold a SQLite write transaction open.
- Material restoration follows the same encode-before-write ordering.

### Data Impact

- No schema migration was required.
- No existing materials were re-indexed.
- No existing vectors or vector identifiers changed.
- Existing active vector collections remain unchanged.

### Compatibility

- PDF, TXT, and processed-slide JSON ingestion retain their existing chunking
  and embedding behaviour.
- Existing job retry, stale-job recovery, and maximum-attempt rules remain in
  effect.
- Multiple worker instances can still compete safely for queued jobs.

### Verification

- Confirmed idle job polling does not begin an immediate write transaction.
- Confirmed stale-job polling issues no update when no stale job exists.
- Confirmed queued jobs are still claimed atomically.
- Confirmed a simulated `database is locked` error is retried without worker
  termination.
- Confirmed embedding computation begins before any material database write.
- Passed 43 database, worker, retrieval, upload, embedding, and slide-ingestion
  regression tests.

### Operational Note

An application process whose worker thread already terminated must be restarted
once to load this fix and start a new worker thread.
