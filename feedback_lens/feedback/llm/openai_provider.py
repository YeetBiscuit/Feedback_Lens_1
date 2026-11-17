import os

from openai import OpenAI

from feedback_lens.feedback.llm.base import LLMProvider


OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
OPENAI_DEFAULT_MODEL = "gpt-5.4-mini"
REQUEST_OPTIONS = {"reasoning", "text", "temperature", "top_p", "max_output_tokens"}


class OpenAIProvider(LLMProvider):
    """Text generation through Responses; model IDs are not allowlisted."""

    name = "openai"
    default_model = OPENAI_DEFAULT_MODEL

    def _build_client(self) -> OpenAI:
        key = os.getenv(OPENAI_API_KEY_ENV)
        if not key or not key.strip():
            raise RuntimeError(f"Missing API key. Please set {OPENAI_API_KEY_ENV}.")
        return OpenAI(
            api_key=key, base_url=OPENAI_BASE_URL, timeout=120.0, max_retries=2,
        )

    def generate(
        self, prompt: str, model: str | None = None, temperature: float = 0.2,
        request_options: dict | None = None,
    ) -> str:
        return self.generate_chat(
            [{"role": "user", "content": prompt}], model=model,
            temperature=temperature, request_options=request_options,
        )

    def generate_chat(
        self, messages: list[dict[str, str]], model: str | None = None,
        temperature: float = 0.2, request_options: dict | None = None,
    ) -> str:
        options = dict(request_options or {})
        unsupported = set(options) - REQUEST_OPTIONS
        if unsupported:
            raise ValueError(f"Unsupported OpenAI request options: {sorted(unsupported)}")
        reasoning = options.get("reasoning") or {}
        if not isinstance(reasoning, dict):
            raise ValueError("OpenAI reasoning must be an object, e.g. {'effort': 'medium'}.")
        effort = reasoning.get("effort")
        if effort not in (None, "none") and {"temperature", "top_p"} & options.keys():
            raise ValueError("Remove temperature/top_p when enabling OpenAI reasoning.")
        # Temperature is opt-in: blindly forwarding the legacy pipeline default
        # would reject reasoning models. Configure supported options per stage.
        options.setdefault("max_output_tokens", 8192)
        limit = options["max_output_tokens"]
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("OpenAI max_output_tokens must be a positive integer.")
        with self._build_client() as client:
            response = client.responses.create(
                model=model or self.default_model, input=messages,
                store=False, **options,
            )
        if response.status != "completed":
            details = getattr(response, "incomplete_details", None)
            reason = getattr(details, "reason", None) or response.status
            raise RuntimeError(f"OpenAI response did not complete: {reason}.")
        for item in getattr(response, "output", ()) or ():
            for content in getattr(item, "content", ()) or ():
                if getattr(content, "type", None) == "refusal":
                    raise RuntimeError("OpenAI refused the structured output request.")
        if not response.output_text or not response.output_text.strip():
            raise RuntimeError("OpenAI returned no text (empty response or refusal).")
        return response.output_text
