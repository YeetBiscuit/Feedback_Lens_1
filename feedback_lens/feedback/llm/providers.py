from feedback_lens.feedback.llm.qwen import QwenProvider


_PROVIDER_FACTORIES = {
    "qwen": QwenProvider,
}


def list_provider_names() -> list[str]:
    return sorted(_PROVIDER_FACTORIES)


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
