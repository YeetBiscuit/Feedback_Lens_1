import os
import unittest
from unittest.mock import patch

from feedback_lens.feedback.llm.gemini import (
    GEMINI_API_KEY_ENV,
    GEMINI_BASE_URL,
    GEMINI_MODEL,
    GeminiProvider,
)
from feedback_lens.feedback.llm.providers import list_provider_names, resolve_model_name


class LLMProviderRegistryTests(unittest.TestCase):
    def test_gemini_is_registered_with_default_model(self) -> None:
        self.assertIn("gemini", list_provider_names())
        self.assertEqual(resolve_model_name("gemini"), GEMINI_MODEL)

    def test_gemini_missing_key_error_names_expected_env_var(self) -> None:
        provider = GeminiProvider()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, GEMINI_API_KEY_ENV):
                provider.generate("Hello")

    @patch("feedback_lens.feedback.llm.gemini.OpenAI")
    def test_gemini_uses_openai_compatible_endpoint(self, mock_openai) -> None:
        provider = GeminiProvider()
        mock_choice = mock_openai.return_value.chat.completions.create.return_value
        mock_choice.choices[0].message.content = "ok"

        with patch.dict(os.environ, {GEMINI_API_KEY_ENV: "test-key"}, clear=True):
            result = provider.generate_chat(
                [{"role": "user", "content": "Hello"}],
                temperature=0.1,
            )

        self.assertEqual(result, "ok")
        mock_openai.assert_called_once_with(
            api_key="test-key",
            base_url=GEMINI_BASE_URL,
        )
        mock_openai.return_value.chat.completions.create.assert_called_once_with(
            model=GEMINI_MODEL,
            messages=[{"role": "user", "content": "Hello"}],
            temperature=0.1,
        )


if __name__ == "__main__":
    unittest.main()
