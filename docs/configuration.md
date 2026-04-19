# Configuration Guide

This guide covers the main runtime settings that affect provider selection, model choice, retrieval size, and local storage.

## Local Paths

Current defaults in the codebase:

- SQLite database: `feedback_system.db`
- vector store: `chromadb/`
- local document root: `documents/`

These are local machine paths and are intentionally not shared through git.

## LLM Provider Selection

Feedback generation uses a provider registry in `feedback_lens/feedback/llm/providers.py`.

Current registered providers:

- `qwen`
- `gemini`

You can select a provider at runtime with:

```bash
python generate_feedback.py <submission_id> --provider qwen
```

Gemini can be selected the same way:

```bash
python generate_feedback.py <submission_id> --provider gemini
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

- `gemini-2.5-flash`

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
- default model: `gemini-2.5-flash`

Set the API key before generation:

```powershell
$env:GEMINI_API_KEY="your_key_here"
```

Run feedback generation with:

```bash
python generate_feedback.py 1 --provider gemini
```

## Retrieval Configuration

The feedback generation CLI exposes:

- `--top-k` - how many unit-material chunks to retrieve
- `--temperature` - model temperature during generation

Example:

```bash
python generate_feedback.py 1 --provider qwen --top-k 8 --temperature 0.1
```

Defaults:

- `top_k = 5`
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
