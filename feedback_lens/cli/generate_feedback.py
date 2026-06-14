import argparse

from feedback_lens.db.connection import connect_db
from feedback_lens.feedback.pipeline import (
    DEFAULT_FEEDBACK_PROVIDER,
    generate_feedback_for_submission,
)
from feedback_lens.feedback.prompt import (
    DEFAULT_FEEDBACK_LENGTH,
    DEFAULT_FEEDBACK_TONE,
    FEEDBACK_PROMPT_TEMPLATE_CHOICES,
    FEEDBACK_LENGTH_OPTIONS,
    FEEDBACK_TONE_OPTIONS,
)
from feedback_lens.feedback.retrieval import (
    DEFAULT_MAX_FINAL_CHUNKS,
    DEFAULT_PER_CUE_TOP_K,
)


DEFAULT_RETRIEVAL_PROMPT_TEMPLATE_ALIAS = "unit-grounded-v2"
DEFAULT_RETRIEVAL_STRATEGY_ALIAS = "planned"


class _PromptTemplateAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        setattr(namespace, "prompt_template_explicit", True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate rubric-aligned feedback for an existing student submission.",
    )
    parser.add_argument("submission_id", type=int)
    parser.add_argument("--provider", default=DEFAULT_FEEDBACK_PROVIDER)
    parser.add_argument("--model")
    parser.add_argument(
        "--feedback-length",
        choices=sorted(FEEDBACK_LENGTH_OPTIONS),
        default=DEFAULT_FEEDBACK_LENGTH,
        help="Controls feedback density and detail.",
    )
    parser.add_argument(
        "--feedback-tone",
        choices=sorted(FEEDBACK_TONE_OPTIONS),
        default=DEFAULT_FEEDBACK_TONE,
        help="Controls feedback directness and supportiveness.",
    )
    parser.set_defaults(prompt_template_explicit=False)
    parser.add_argument(
        "--prompt",
        dest="prompt_template_version",
        choices=FEEDBACK_PROMPT_TEMPLATE_CHOICES,
        default=DEFAULT_RETRIEVAL_PROMPT_TEMPLATE_ALIAS,
        action=_PromptTemplateAction,
        help=(
            "Feedback prompt template version. Defaults to unit-grounded-v2 in "
            "retrieval mode and the direct-mode default when --mode direct is "
            "used without an explicit prompt."
        ),
    )
    parser.add_argument(
        "--prompt-template-version",
        dest="prompt_template_version",
        choices=FEEDBACK_PROMPT_TEMPLATE_CHOICES,
        action=_PromptTemplateAction,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--per-cue-top-k",
        "--top-k",
        dest="per_cue_top_k",
        type=int,
        default=DEFAULT_PER_CUE_TOP_K,
        help=(
            "How many chunks to retrieve for each retrieval cue. "
            "--top-k is kept as a backwards-compatible alias."
        ),
    )
    parser.add_argument(
        "--max-final-chunks",
        type=int,
        default=DEFAULT_MAX_FINAL_CHUNKS,
        help="Maximum deduplicated chunks to pass to the feedback generator.",
    )
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument(
        "--strategy",
        dest="retrieval_strategy",
        choices=[
            "baseline",
            "planned",
            "assignment_spec_multi_cue_v1",
            "llm_planned_cue_v1",
        ],
        default=DEFAULT_RETRIEVAL_STRATEGY_ALIAS,
        help=(
            "Retrieval strategy for retrieval mode. baseline uses imported "
            "assignment-spec cues; planned uses an LLM planner to generate "
            "targeted retrieval cues from the spec, rubric, and submission."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["retrieval", "direct"],
        default="retrieval",
        help=(
            "retrieval uses the current course-context retrieval pipeline; "
            "direct sends only the submission, rubric, and assignment spec."
        ),
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    prompt_template_version = args.prompt_template_version
    if args.mode == "direct" and not args.prompt_template_explicit:
        prompt_template_version = None

    with connect_db() as conn:
        result = generate_feedback_for_submission(
            conn,
            submission_id=args.submission_id,
            provider=args.provider,
            model=args.model,
            per_cue_top_k=args.per_cue_top_k,
            max_final_chunks=args.max_final_chunks,
            temperature=args.temperature,
            prompt_template_version=prompt_template_version,
            context_mode=args.mode,
            retrieval_strategy=args.retrieval_strategy,
            feedback_length=args.feedback_length,
            feedback_tone=args.feedback_tone,
        )

    print(
        f"Completed generation_run={result.generation_id} using "
        f"{result.provider}:{result.model} in {result.context_mode} mode. "
        f"retrieval_strategy={result.retrieval_strategy}, "
        f"retrieval_cues={result.retrieval_cue_count}, "
        f"per_cue_top_k={result.per_cue_top_k}, "
        f"max_final_chunks={result.max_final_chunks}, "
        f"feedback_length={result.feedback_length}, "
        f"feedback_tone={result.feedback_tone}, "
        f"deduplicated_chunks={result.deduplicated_chunk_count}, "
        f"criterion_count={result.criterion_count}, "
        f"overall_grade_band={result.overall_grade_band or 'None'}."
    )


if __name__ == "__main__":
    main()
