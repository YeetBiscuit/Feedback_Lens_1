import os

from openai import OpenAI

from feedback_lens.feedback.llm.base import LLMProvider

GEMINI_PROVIDER = "gemini"
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
GEMINI_MODEL = "gemini-3-flash-preview"


class GeminiProvider(LLMProvider):
    name = GEMINI_PROVIDER
    default_model = GEMINI_MODEL

    def __init__(
        self,
        api_key_env: str = GEMINI_API_KEY_ENV,
        base_url: str = GEMINI_BASE_URL,
    ) -> None:
        self.api_key_env = api_key_env
        self.base_url = base_url

    def _build_client(self) -> OpenAI:
        api_key = os.getenv(self.api_key_env)
        if not api_key:
            raise RuntimeError(
                f"Missing API key. Please set environment variable {self.api_key_env}."
            )

        return OpenAI(api_key=api_key, base_url=self.base_url)

    def generate(
        self,
        prompt,
        model: str | None = None,
        temperature: float = 0.2,
    ) -> str:
        client = self._build_client()
        completion = client.chat.completions.create(
            model=model or self.default_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
        )
        content = completion.choices[0].message.content
        if not content:
            raise RuntimeError("Gemini returned an empty response.")
        return content

    def generate_chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.2,
    ) -> str:
        client = self._build_client()
        completion = client.chat.completions.create(
            model=model or self.default_model,
            messages=messages,
            temperature=temperature,
        )
        content = completion.choices[0].message.content
        if not content:
            raise RuntimeError("Gemini returned an empty response.")
        return content


def ask_gemini(prompt, model=GEMINI_MODEL, temperature: float = 0.2):
    return GeminiProvider().generate(prompt, model=model, temperature=temperature)
