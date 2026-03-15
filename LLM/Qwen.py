import os
from openai import OpenAI

QWEN_API_KEY_ENV = "QWEN_API_KEY"
QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QWEN_MODEL = "qwen3.5-plus"


def ask_qwen(prompt, model=QWEN_MODEL):
    api_key = os.getenv(QWEN_API_KEY_ENV)
    if not api_key:
        raise RuntimeError(
            f"Missing API key. Please set environment variable {QWEN_API_KEY_ENV}."
        )

    client = OpenAI(api_key=api_key, base_url=QWEN_BASE_URL)
    completion = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    return completion.choices[0].message.content


if __name__ == "__main__":
    print(ask_qwen("Who are you?"))