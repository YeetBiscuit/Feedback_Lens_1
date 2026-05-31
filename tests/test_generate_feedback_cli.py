import unittest

from feedback_lens.cli.generate_feedback import build_parser


class GenerateFeedbackCliTests(unittest.TestCase):
    def test_strategy_argument_sets_retrieval_strategy(self) -> None:
        args = build_parser().parse_args(["1", "--strategy", "planned"])

        self.assertEqual(args.submission_id, 1)
        self.assertEqual(args.retrieval_strategy, "planned")

    def test_help_uses_strategy_argument_name(self) -> None:
        help_text = build_parser().format_help()

        self.assertIn("--strategy", help_text)
        self.assertNotIn("--retrieval-strategy", help_text)


if __name__ == "__main__":
    unittest.main()
