"""Independent backend model settings; API credentials stay in the environment."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from feedback_lens.paths import PROJECT_ROOT


DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "llm.json"
STAGES = ("planner", "generator", "revision")


@dataclass(frozen=True)
class StageModelConfig:
    provider: str
    model: str
    request_options: dict

    def call_options(self) -> dict:
        return {"request_options": self.request_options} if self.request_options else {}


def get_stage_config(stage: str) -> StageModelConfig:
    if stage not in STAGES:
        raise ValueError(f"Unknown LLM stage: {stage}")
    path = Path(os.environ.get("FEEDBACK_LENS_LLM_CONFIG") or DEFAULT_CONFIG_PATH)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read LLM configuration: {path}") from exc
    entry = data.get(stage) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        raise ValueError(f"LLM configuration must contain a '{stage}' object.")
    provider = entry.get("provider")
    model = entry.get("model")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError(f"{stage}.provider must be a non-empty string.")
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f"{stage}.model must be a non-empty string.")
    options = entry.get("request_options", {})
    if not isinstance(options, dict):
        raise ValueError(f"{stage}.request_options must be an object.")
    return StageModelConfig(provider.strip().lower(), model.strip(), options)


def resolve_stage_config(
    stage: str, provider: str | None = None, model: str | None = None,
) -> StageModelConfig:
    """An explicit/saved model wins; never apply another model's request options."""
    from feedback_lens.feedback.llm.providers import resolve_model_name

    configured = get_stage_config(stage)
    selected_provider = (provider or configured.provider).strip().lower()
    selected_model = model or (
        configured.model if selected_provider == configured.provider else None
    )
    selected_model = resolve_model_name(selected_provider, selected_model)
    options = (
        configured.request_options
        if (selected_provider, selected_model) == (configured.provider, configured.model)
        else {}
    )
    return StageModelConfig(selected_provider, selected_model, options)
