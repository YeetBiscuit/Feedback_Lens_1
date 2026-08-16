from feedback_lens.feedback.llm.deepseek import (
    DEEPSEEK_DEFAULT_MODEL,
    DEEPSEEK_MODELS,
    DeepSeekProvider,
)
from feedback_lens.feedback.llm.gemini import GEMINI_MODEL, GeminiProvider
from feedback_lens.feedback.llm.nvidia import NVIDIA_MODEL, NvidiaProvider
from feedback_lens.feedback.llm.qwen import QWEN_MODEL, QwenProvider


_PROVIDER_FACTORIES = {
    "deepseek": DeepSeekProvider,
    "gemini": GeminiProvider,
    "nvidia": NvidiaProvider,
    "qwen": QwenProvider,
}

_PROVIDER_LABELS = {
    "deepseek": "DeepSeek",
    "gemini": "Gemini",
    "nvidia": "NVIDIA",
    "qwen": "Qwen",
}

_PROVIDER_MODELS = {
    "deepseek": DEEPSEEK_MODELS,
    "gemini": (GEMINI_MODEL,),
    "nvidia": (NVIDIA_MODEL,),
    "qwen": (QWEN_MODEL,),
}

DEFAULT_FEEDBACK_PROVIDER = "deepseek"
DEFAULT_FEEDBACK_MODEL = DEEPSEEK_DEFAULT_MODEL


def list_provider_names() -> list[str]:
    return sorted(_PROVIDER_FACTORIES)


def list_feedback_models() -> list[dict[str, str]]:
    return [
        {
            "provider": provider,
            "provider_label": _PROVIDER_LABELS[provider],
            "model": model,
            "label": f"{_PROVIDER_LABELS[provider]} · {model}",
        }
        for provider in list_provider_names()
        for model in _PROVIDER_MODELS[provider]
    ]


def validate_feedback_model(provider: str, model: str) -> tuple[str, str]:
    provider_key = str(provider or "").strip().lower()
    model_name = str(model or "").strip()
    if (
        provider_key not in _PROVIDER_MODELS
        or model_name not in _PROVIDER_MODELS[provider_key]
    ):
        raise ValueError("Choose a model from the available feedback models.")
    return provider_key, model_name


def get_provider(provider: str):
    provider_key = provider.strip().lower()
    factory = _PROVIDER_FACTORIES.get(provider_key)
    if factory is None:
        raise ValueError(
            f"Unsupported LLM provider '{provider}'. Available providers: {', '.join(list_provider_names())}"
        )
    return factory()


def resolve_model_name(provider: str, model: str | None = None) -> str:
    client = get_provider(provider)
    return model or client.default_model


def generate_text(
    prompt: str,
    provider: str = "qwen",
    model: str | None = None,
    temperature: float = 0.2,
) -> str:
    client = get_provider(provider)
    return client.generate(prompt, model=model, temperature=temperature)


def generate_chat(
    messages: list[dict[str, str]],
    provider: str = "qwen",
    model: str | None = None,
    temperature: float = 0.2,
) -> str:
    client = get_provider(provider)
    if hasattr(client, "generate_chat"):
        return client.generate_chat(messages, model=model, temperature=temperature)
    return client.generate(_messages_to_prompt(messages), model=model, temperature=temperature)


def _messages_to_prompt(messages: list[dict[str, str]]) -> str:
    return "\n\n".join(
        f"{message.get('role', 'user').upper()}:\n{message.get('content', '')}"
        for message in messages
    )
