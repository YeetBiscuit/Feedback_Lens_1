import os
import unittest
from unittest.mock import patch

from feedback_lens.feedback.llm.deepseek import (
    DEEPSEEK_API_KEY_ENV,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_DEFAULT_MODEL,
    DEEPSEEK_FLASH_MODEL,
    DEEPSEEK_MODELS,
    DEEPSEEK_PRO_MODEL,
    DeepSeekProvider,
)
from feedback_lens.feedback.llm.gemini import (
    GEMINI_API_KEY_ENV,
    GEMINI_BASE_URL,
    GEMINI_MODEL,
    GeminiProvider,
)
from feedback_lens.feedback.llm.nvidia import (
    NVIDIA_API_KEY_ENV,
    NVIDIA_BASE_URL,
    NVIDIA_MODEL,
    NvidiaProvider,
)
from feedback_lens.feedback.llm.providers import list_provider_names, resolve_model_name


class LLMProviderRegistryTests(unittest.TestCase):
    def test_deepseek_is_registered_with_pro_default_and_flash_available(self) -> None:
        self.assertIn("deepseek", list_provider_names())
        self.assertEqual(resolve_model_name("deepseek"), DEEPSEEK_DEFAULT_MODEL)
        self.assertEqual(DEEPSEEK_DEFAULT_MODEL, DEEPSEEK_PRO_MODEL)
        self.assertEqual(
            DEEPSEEK_MODELS,
            (DEEPSEEK_PRO_MODEL, DEEPSEEK_FLASH_MODEL),
        )

    def test_gemini_is_registered_with_default_model(self) -> None:
        self.assertIn("gemini", list_provider_names())
        self.assertEqual(resolve_model_name("gemini"), GEMINI_MODEL)

    def test_nvidia_is_registered_and_legacy_name_is_removed(self) -> None:
        self.assertIn("nvidia", list_provider_names())
        self.assertNotIn("nvidia_deepseek", list_provider_names())
        self.assertEqual(resolve_model_name("nvidia"), NVIDIA_MODEL)
        with self.assertRaisesRegex(ValueError, "Unsupported LLM provider"):
            resolve_model_name("nvidia_deepseek")

    def test_gemini_missing_key_error_names_expected_env_var(self) -> None:
        provider = GeminiProvider()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, GEMINI_API_KEY_ENV):
                provider.generate("Hello")

    def test_deepseek_missing_key_error_names_expected_env_var(self) -> None:
        provider = DeepSeekProvider()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, DEEPSEEK_API_KEY_ENV):
                provider.generate("Hello")

    @patch("feedback_lens.feedback.llm.deepseek.OpenAI")
    def test_deepseek_uses_official_openai_compatible_endpoint(self, mock_openai) -> None:
        provider = DeepSeekProvider()
        mock_choice = mock_openai.return_value.chat.completions.create.return_value
        mock_choice.choices[0].message.content = "ok"

        with patch.dict(os.environ, {DEEPSEEK_API_KEY_ENV: "test-key"}, clear=True):
            result = provider.generate_chat(
                [{"role": "user", "content": "Hello"}],
                temperature=0.1,
            )

        self.assertEqual(result, "ok")
        mock_openai.assert_called_once_with(
            api_key="test-key",
            base_url=DEEPSEEK_BASE_URL,
        )
        mock_openai.return_value.chat.completions.create.assert_called_once_with(
            model=DEEPSEEK_PRO_MODEL,
            messages=[{"role": "user", "content": "Hello"}],
            temperature=0.1,
        )

    @patch("feedback_lens.feedback.llm.deepseek.OpenAI")
    def test_deepseek_flash_can_be_selected_explicitly(self, mock_openai) -> None:
        provider = DeepSeekProvider()
        mock_choice = mock_openai.return_value.chat.completions.create.return_value
        mock_choice.choices[0].message.content = "ok"

        with patch.dict(os.environ, {DEEPSEEK_API_KEY_ENV: "test-key"}, clear=True):
            result = provider.generate("Hello", model=DEEPSEEK_FLASH_MODEL)

        self.assertEqual(result, "ok")
        mock_openai.return_value.chat.completions.create.assert_called_once_with(
            model=DEEPSEEK_FLASH_MODEL,
            messages=[{"role": "user", "content": "Hello"}],
            temperature=0.2,
        )

    def test_nvidia_missing_key_error_names_expected_env_var(self) -> None:
        provider = NvidiaProvider()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, NVIDIA_API_KEY_ENV):
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

    @patch("feedback_lens.feedback.llm.nvidia.OpenAI")
    def test_nvidia_uses_openai_compatible_endpoint(self, mock_openai) -> None:
        provider = NvidiaProvider()
        mock_choice = mock_openai.return_value.chat.completions.create.return_value
        mock_choice.choices[0].message.content = "ok"

        with patch.dict(os.environ, {NVIDIA_API_KEY_ENV: "test-key"}, clear=True):
            result = provider.generate_chat(
                [{"role": "user", "content": "Hello"}],
                temperature=0.1,
            )

        self.assertEqual(result, "ok")
        mock_openai.assert_called_once_with(
            api_key="test-key",
            base_url=NVIDIA_BASE_URL,
        )
        mock_openai.return_value.chat.completions.create.assert_called_once_with(
            model=NVIDIA_MODEL,
            messages=[{"role": "user", "content": "Hello"}],
            temperature=0.1,
        )


if __name__ == "__main__":
    unittest.main()
