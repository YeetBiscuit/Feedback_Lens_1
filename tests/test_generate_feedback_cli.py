import unittest

from feedback_lens.cli.generate_feedback import build_parser


class GenerateFeedbackCliTests(unittest.TestCase):
    def test_strategy_argument_sets_retrieval_strategy(self) -> None:
        args = build_parser().parse_args(["1", "--strategy", "planned"])

        self.assertEqual(args.submission_id, 1)
        self.assertEqual(args.retrieval_strategy, "planned")
        self.assertEqual(args.per_cue_top_k, 5)
        self.assertEqual(args.max_final_chunks, 10)

    def test_retrieval_limit_arguments_are_configurable(self) -> None:
        args = build_parser().parse_args(
            ["1", "--per-cue-top-k", "4", "--max-final-chunks", "12"]
        )

        self.assertEqual(args.per_cue_top_k, 4)
        self.assertEqual(args.max_final_chunks, 12)

    def test_legacy_top_k_argument_sets_per_cue_top_k(self) -> None:
        args = build_parser().parse_args(["1", "--top-k", "7"])

        self.assertEqual(args.per_cue_top_k, 7)
        self.assertEqual(args.max_final_chunks, 10)

    def test_prompt_argument_sets_prompt_template_version(self) -> None:
        args = build_parser().parse_args(["1", "--prompt", "unit-grounded-v2"])

        self.assertEqual(args.prompt_template_version, "unit-grounded-v2")

    def test_legacy_prompt_template_version_argument_still_works(self) -> None:
        args = build_parser().parse_args(
            ["1", "--prompt-template-version", "unit-grounded-v2"]
        )

        self.assertEqual(args.prompt_template_version, "unit-grounded-v2")

    def test_help_uses_strategy_argument_name(self) -> None:
        help_text = build_parser().format_help()

        self.assertIn("--strategy", help_text)
        self.assertIn("--per-cue-top-k", help_text)
        self.assertIn("--max-final-chunks", help_text)
        self.assertIn("--prompt", help_text)
        self.assertNotIn("--prompt-template-version", help_text)
        self.assertNotIn("--retrieval-strategy", help_text)


if __name__ == "__main__":
    unittest.main()
