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

## LLM Provider Selection

Feedback generation uses a provider registry in `feedback_lens/feedback/llm/providers.py`.

Current registered providers:

- `deepseek` (default)
- `qwen`
- `gemini`
- `nvidia`

Feedback generation uses the official DeepSeek API by default:

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

The default provider uses DeepSeek's OpenAI-compatible endpoint in
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
python generate_feedback.py 1
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
- `max_final_chunks = 10`
- `retrieval_strategy = baseline`
- `temperature = 0.2`

## Embedding Configuration

Embedding is configured in `feedback_lens/file_management/indexing/embedding.py`.

Current defaults:

- model: `all-MiniLM-L6-v2`
- persistence directory: `chromadb/`

Unit-material collections are named from:

- `unit_code`
- `year`
- `semester`

The name is normalised into a Chroma-safe collection string.

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
