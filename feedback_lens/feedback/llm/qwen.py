import os

from openai import OpenAI

from feedback_lens.feedback.llm.base import LLMProvider

QWEN_PROVIDER = "qwen"
QWEN_API_KEY_ENV = "QWEN_API_KEY"
QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QWEN_MODEL = "qwen3.5-plus"


class QwenProvider(LLMProvider):
    name = QWEN_PROVIDER
    default_model = QWEN_MODEL

    def __init__(
        self,
        api_key_env: str = QWEN_API_KEY_ENV,
        base_url: str = QWEN_BASE_URL,
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
            raise RuntimeError("Qwen returned an empty response.")
        return content


def ask_qwen(prompt, model=QWEN_MODEL, temperature: float = 0.2):
    return QwenProvider().generate(prompt, model=model, temperature=temperature)
