import os

from openai import OpenAI

from feedback_lens.feedback.llm.base import LLMProvider

DEEPSEEK_PROVIDER = "deepseek"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_PRO_MODEL = "deepseek-v4-pro"
DEEPSEEK_FLASH_MODEL = "deepseek-v4-flash"
DEEPSEEK_MODELS = (DEEPSEEK_PRO_MODEL, DEEPSEEK_FLASH_MODEL)
DEEPSEEK_DEFAULT_MODEL = DEEPSEEK_PRO_MODEL


class DeepSeekProvider(LLMProvider):
    name = DEEPSEEK_PROVIDER
    default_model = DEEPSEEK_DEFAULT_MODEL

    def __init__(
        self,
        api_key_env: str = DEEPSEEK_API_KEY_ENV,
        base_url: str = DEEPSEEK_BASE_URL,
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
        prompt: str,
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
            raise RuntimeError("DeepSeek returned an empty response.")
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
            raise RuntimeError("DeepSeek returned an empty response.")
        return content


def ask_deepseek(
    prompt: str,
    model: str = DEEPSEEK_DEFAULT_MODEL,
    temperature: float = 0.2,
) -> str:
    return DeepSeekProvider().generate(
        prompt,
        model=model,
        temperature=temperature,
    )
