# Configuration Guide

This guide covers the main runtime settings that affect provider selection, model choice, retrieval size, and local storage.

## Local Paths

Current defaults in the codebase:

- SQLite database: `feedback_system.db`
- vector store: `chromadb/`
- local document root: `documents/`

These are local machine paths and are intentionally not shared through git.

## Web Admin And Uploads

The Admin console stores uploaded source files under `uploads/` by default.
This directory is local operational data and is ignored by git.

Relevant environment variables:

- `FEEDBACK_LENS_ENV` - use `production` on a deployed server; development is
  the default
- `FEEDBACK_LENS_UPLOAD_ROOT` - upload storage directory
- `FEEDBACK_LENS_DOCUMENT_LIMIT_BYTES` - individual document limit; default
  25 MiB
- `FEEDBACK_LENS_ZIP_LIMIT_BYTES` - Moodle ZIP limit; default 1 GiB
- `FEEDBACK_LENS_ZIP_ENTRY_LIMIT` - maximum ZIP entries; default 5,000
- `FEEDBACK_LENS_ZIP_UNCOMPRESSED_LIMIT_BYTES` - maximum expanded ZIP size;
  default 4 GiB
- `FEEDBACK_LENS_ZIP_COMPRESSION_RATIO_LIMIT` - maximum permitted compression
  ratio; default 100
- `FEEDBACK_LENS_PUBLIC_BASE_URL` - public origin used in activation and reset
  links; local default `http://127.0.0.1:5001`
- `FEEDBACK_LENS_START_WORKER` - set to `0` when the web process must not start
  the local background worker
- `FEEDBACK_LENS_JOB_STALE_SECONDS` - time before an abandoned running job is
  recovered; default 300 seconds

`python app.py` starts the worker in a background thread for local development.
For a deployed service, run `python worker.py` independently and configure the
web process with `FEEDBACK_LENS_START_WORKER=0`.

The Moodle summative importer expects the original **Download submissions in
folders** ZIP. It rejects unsafe paths, symbolic links, deceptive PDF
extensions, excessive entry counts, expanded sizes, and compression ratios.

## Student Account Email

Set `FEEDBACK_LENS_MAIL_BACKEND` to one of:

- `memory` - tests only
- `console` - local development; prints the email to the server console
- `smtp` - deployed environment
- `disabled` - account activation is shown as unavailable, while uploads remain
  available

Development defaults to `console`. When `FEEDBACK_LENS_ENV=production`, the
mail backend defaults to `disabled` unless it is explicitly configured.

SMTP requires:

- `FEEDBACK_LENS_SMTP_HOST`
- `FEEDBACK_LENS_SMTP_PORT` - default `587`
- `FEEDBACK_LENS_MAIL_FROM`
- `FEEDBACK_LENS_SMTP_USERNAME` and `FEEDBACK_LENS_SMTP_PASSWORD` when the
  approved server requires authentication
- `FEEDBACK_LENS_SMTP_STARTTLS` - default `1`

Production email should use an SMTP service approved by the school. Feedback
Lens does not require a commercial email subscription if the institution
provides an approved SMTP relay.

Activation links are valid for 72 hours by default and password-reset links for
60 minutes. Override these with `FEEDBACK_LENS_ACTIVATION_TTL_HOURS` and
`FEEDBACK_LENS_PASSWORD_RESET_TTL_MINUTES`.

## Web Security

Set a long random `FEEDBACK_LENS_SECRET_KEY` in every deployed environment. Do
not use the development fallback in production. Set
`FEEDBACK_LENS_SECURE_COOKIES=1` when the site is served over HTTPS.

Account request limits default to five requests per identity and 30 per source
address per hour. Configure them with `FEEDBACK_LENS_IDENTITY_RATE_LIMIT` and
`FEEDBACK_LENS_SOURCE_RATE_LIMIT`. Only keyed, irreversible request hashes are
recorded for these limits.

## Feedback Stage Configuration

Edit `config/llm.json` to independently select the `planner`, `generator`, and
`revision` provider, model ID, and OpenAI `request_options`. All three initially
use `openai` / `gpt-5.4-mini`, with reasoning `none`. The Planner output limit is
4096 tokens; Generator and Revision use 8192. These are integration-test settings,
not a feedback-quality recommendation.

To use a local configuration outside git, set `FEEDBACK_LENS_LLM_CONFIG` to its
path. Relative paths resolve against the project root. API keys must remain in
environment variables (`OPENAI_API_KEY`, `NVIDIA_API_KEY`, etc.), never this file.

Each role has the same configuration shape. For example, a stronger Generator
can be selected without changing the Planner or Revision entries:

```json
"generator": {
  "provider": "openai",
  "model": "gpt-6.1-sol",
  "request_options": {
    "reasoning": {"effort": "medium"},
    "max_output_tokens": 16384
  }
}
```

This is an example, not the checked-in default. OpenAI model IDs are passed
through to the Responses API without a model allowlist. Select a text model that
supports Responses with strict Structured Outputs and is accessible to your project. Model availability and
parameter compatibility are validated by the API. Supported configurable options
are `reasoning`, `text`, `max_output_tokens`, `temperature`, and `top_p`.
Other providers retain their existing adapters and should omit `request_options`.

The OpenAI adapter deliberately does not forward the legacy pipeline temperature
automatically. To use temperature with a compatible model, explicitly include it
in `request_options`. Remove `temperature` and `top_p` when reasoning is enabled.
Changing model may require changing its options too (for example, Sol does not
support reasoning `none`). Output-token budgets include reasoning tokens; an
incomplete or empty response raises an error instead of saving partial feedback.
Requests use `store=False`; no prompt-cache configuration is added in this change.

### Strict structured output

OpenAI pipeline calls automatically supply Responses API `text.format` with
`type: "json_schema"` and `strict: true`. The Planner uses the
`retrieval_plan_v1` contract; Generator, automatic Revision, and teacher criterion
regeneration use `feedback_v1`. The contracts live in
`feedback_lens/feedback/llm/structured_output.py` and match the existing prompt
fields. All object properties are required, extra properties are disallowed,
and grade bands are restricted to `N`, `P`, `C`, `D`, and `HD`.

The pipeline sets this format after resolving model options, including saved
options for older feedback records. It overrides any configured `text.format`
while preserving other text settings such as verbosity. The effective schema
and strict flag are recorded with the existing configuration audit events.
Do not copy schemas into `config/llm.json`.

This constrains completed, non-refusal output structure, not feedback quality.
Refusals, incomplete responses, API errors, and parsing errors still fail the
operation; there is no silent fallback to unconstrained text or automatic JSON
repair. Existing cue and rubric-mapping checks remain in place. Other providers
and NVIDIA judges retain their existing behaviour. See the
[OpenAI Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs).

### Configuration precedence

Configuration precedence and scope:

- Existing assessments keep their saved Generator provider/model. No historical
  settings or feedback are rewritten. They may still require the old API key.
- New web assessments copy the current Generator provider/model when created.
  Subsequent backend model changes do not overwrite that saved selection.
- CLI `--provider` / `--model` and explicit function arguments override the
  Generator default. Matching stage settings supply its request options. An
  override selecting a different model/provider does not inherit another model's
  options; OpenAI then uses server defaults and an 8192-token output limit,
  plus the pipeline's strict output contract.
- The Planner always uses its independent backend configuration, including
  when planning retrieval again for an automatic revision.
- Automatic quality-gate retries use Revision instead of the old NVIDIA Flash.
- Teacher criterion regeneration follows the provider/model on the selected
  feedback record, including when that draft came from Revision. Recorded request
  options are reused when available; older records without that audit entry use
  matching Generator options or provider defaults. No automatic model fallback
  occurs if an old endpoint is unavailable. Original generation metadata is
  retained and the actual regeneration model is recorded in `audit_events`.
- Judges, quality thresholds, and retry counts remain unchanged. NVIDIA judge
  requests still require `NVIDIA_API_KEY`; this change does not verify the live
  availability of those endpoints.

Restart the web application and worker after changing environment variables or
configuration, so all running processes use the same settings.

### Live synthetic smoke test

After the offline tests pass, you can opt in to five real generation requests
(plus any SDK retries) using the configured models:

```powershell
$env:FEEDBACK_LENS_RUN_LIVE_LLM_TESTS = "1"
python -m unittest tests.test_openai_live -v
Remove-Item Env:FEEDBACK_LENS_RUN_LIVE_LLM_TESTS
```

This incurs API usage. It uses a tiny synthetic assignment and an in-memory
database, so existing assessments are not changed. It exercises actual OpenAI
planning, initial generation, automatic revision, JSON parsing/storage, and
teacher criterion regeneration. Every request is checked for its strict format,
with raw JSON parsed directly. Retrieval uses a fixed synthetic chunk and
judge results are simulated to force one revision; it does not verify live
NVIDIA judges, the vector store, or frontend rendering. Without the opt-in
variable, normal test discovery skips these network calls.

## Available Providers

Feedback generation uses a provider registry in `feedback_lens/feedback/llm/providers.py`.

Current registered providers:

- `openai` (initial default)
- `deepseek`
- `qwen`
- `gemini`
- `nvidia`

Feedback generation uses the configured Generator by default:

```bash
python generate_feedback.py <submission_id>
```

You can select another provider at runtime with:

```bash
python generate_feedback.py <submission_id> --provider qwen
```

Gemini can be selected the same way:

```bash
python generate_feedback.py <submission_id> --provider gemini
```

NVIDIA's hosted provider can also be selected:

```bash
python generate_feedback.py <submission_id> --provider nvidia
```

If you pass an unsupported provider name, generation fails with a clear error listing the available providers.

## Model Selection

You can override the provider's default model with `--model`.

Example:

```bash
python generate_feedback.py 1 --provider qwen --model qwen3.5-plus
```

If `--model` is omitted, the provider default is used.

Current Qwen default:

- `qwen3.5-plus`

Current Gemini default:

- `gemini-3-flash-preview`

Current official DeepSeek default and available alternative:

- default: `deepseek-v4-pro`
- optional: `deepseek-v4-flash`

Current NVIDIA hosted-provider default:

- `openai/gpt-oss-120b`

## Qwen Configuration

Qwen is implemented through the OpenAI-compatible client in `feedback_lens/feedback/llm/qwen.py`.

Current Qwen settings:

- environment variable for API key: `QWEN_API_KEY`
- base URL: `https://dashscope.aliyuncs.com/compatible-mode/v1`
- default model: `qwen3.5-plus`

Set the API key before generation:

```powershell
$env:QWEN_API_KEY="your_key_here"
```

## Gemini Configuration

Gemini is implemented through Google's OpenAI-compatible endpoint in `feedback_lens/feedback/llm/gemini.py`.

Current Gemini settings:

- environment variable for API key: `GEMINI_API_KEY`
- base URL: `https://generativelanguage.googleapis.com/v1beta/openai/`
- default model: `gemini-3-flash-preview`

Set the API key before generation:

```powershell
$env:GEMINI_API_KEY="your_key_here"
```

Run feedback generation with:

```bash
python generate_feedback.py 1 --provider gemini
```

## Official DeepSeek Configuration

The DeepSeek provider uses DeepSeek's OpenAI-compatible endpoint in
`feedback_lens/feedback/llm/deepseek.py`.

Current official DeepSeek settings:

- environment variable for API key: `DEEPSEEK_API_KEY`
- base URL: `https://api.deepseek.com`
- default model: `deepseek-v4-pro`
- available alternative: `deepseek-v4-flash`

Set the API key before generation:

```powershell
$env:DEEPSEEK_API_KEY="your_key_here"
```

Run feedback generation with the default Pro model:

```bash
python generate_feedback.py 1 --provider deepseek
```

Select Flash explicitly when lower cost is preferred:

```bash
python generate_feedback.py 1 --provider deepseek --model deepseek-v4-flash
```

## NVIDIA Hosted Provider Configuration

The `nvidia` provider uses NVIDIA's OpenAI-compatible endpoint in
`feedback_lens/feedback/llm/nvidia.py`. The former `nvidia_deepseek` provider
name is no longer registered.

Current NVIDIA settings:

- environment variable for API key: `NVIDIA_API_KEY`
- base URL: `https://integrate.api.nvidia.com/v1`
- default model: `openai/gpt-oss-120b`

Set the API key before generation:

```powershell
$env:NVIDIA_API_KEY="your_key_here"
```

Run feedback generation with:

```bash
python generate_feedback.py 1 --provider nvidia
```

## Retrieval Configuration

The feedback generation CLI exposes:

- `--per-cue-top-k` - how many unit-material chunks to retrieve for each retrieval cue
- `--max-final-chunks` - maximum deduplicated chunks to pass to the feedback generator
- `--top-k` - backwards-compatible alias for `--per-cue-top-k`
- `--strategy` - `baseline` for imported assignment-spec cues, or `planned` for LLM-generated retrieval cues
- `--temperature` - model temperature during generation

Example:

```bash
python generate_feedback.py 1 --provider qwen --per-cue-top-k 5 --max-final-chunks 10 --temperature 0.1
```

Planned retrieval example:

```bash
python generate_feedback.py 1 --provider qwen --mode retrieval --strategy planned
```

Planned retrieval uses the selected provider and model to read the assignment specification, rubric, and student submission before retrieval. It records the planner prompt, raw planner response, and normalized cue list in `retrieval_planning_records`.

Defaults:

- `per_cue_top_k = 5`
- `max_final_chunks` defaults to 10 through 1,000 submission words, plus 2 per additional full 500 words, capped at 24. Set it explicitly to override the automatic limit.
- `retrieval_strategy = baseline`
- `temperature = 0.2`

## Embedding Configuration

Embedding is configured in `feedback_lens/file_management/indexing/embedding.py`.

Current defaults:

- model for new units: `BAAI/bge-m3`
- pinned revision: `5617a9f61b028005a4858fdac845db406aefb181`
- embedding mode: dense, 1024 dimensions, normalized
- Chroma distance metric: cosine
- persistence directory: `chromadb/`

Units that already contain `all-MiniLM-L6-v2` embeddings remain on their
existing 384-dimensional MiniLM collections. Their unit-material collection is
read-only: existing material can still be retrieved, deactivated, or deleted,
but new material cannot be ingested and deactivated material cannot be
restored. A unit without existing embeddings uses the pinned BGE-M3 model when
its first material is ingested.

Unit-material collections are named from:

- `unit_code`
- `year`
- `semester`

The name is normalised into a Chroma-safe collection string.

BGE-M3 collection names also include the model and short revision suffix, for
example `comp3001_2027_s1_bge_m3_5617a9f`. Legacy MiniLM collection names are
unchanged.

## Adding Another Provider

The codebase is already structured for provider swapping.

To add a new provider:

1. create a provider class in `feedback_lens/feedback/llm/` that implements the `LLMProvider` interface
2. define a provider `name` and `default_model`
3. implement `generate(...)`
4. register the provider in `feedback_lens/feedback/llm/providers.py`

After that, generation can use the new provider through `--provider`.

## Submission And Generation Inputs

Generation is driven from database records, not directly from files at runtime.

That means:

- you must import the spec, rubric, and submission before generating feedback
- you must ingest unit materials before retrieval can work
- switching models or providers does not require re-importing the documents
