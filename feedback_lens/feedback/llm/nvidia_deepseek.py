import os

from openai import OpenAI

from feedback_lens.feedback.llm.base import LLMProvider

NVIDIA_DEEPSEEK_PROVIDER = "nvidia_deepseek"
NVIDIA_API_KEY_ENV = "NVIDIA_API_KEY"
NVIDIA_DEEPSEEK_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_DEEPSEEK_MODEL = "deepseek-ai/deepseek-v4-pro"


class NvidiaDeepSeekProvider(LLMProvider):
    name = NVIDIA_DEEPSEEK_PROVIDER
    default_model = NVIDIA_DEEPSEEK_MODEL

    def __init__(
        self,
        api_key_env: str = NVIDIA_API_KEY_ENV,
        base_url: str = NVIDIA_DEEPSEEK_BASE_URL,
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
            raise RuntimeError("NVIDIA DeepSeek returned an empty response.")
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
            raise RuntimeError("NVIDIA DeepSeek returned an empty response.")
        return content


def ask_nvidia_deepseek(
    prompt,
    model=NVIDIA_DEEPSEEK_MODEL,
    temperature: float = 0.2,
):
    return NvidiaDeepSeekProvider().generate(
        prompt,
        model=model,
        temperature=temperature,
    )
