"""Curriculum generation and unit-level file assembly."""

from feedback_lens.curriculum.pipeline import (
    CurriculumGenerationResult,
    SyntheticSubmissionGenerationResult,
    generate_synthetic_submissions,
    generate_unit,
)

__all__ = [
    "CurriculumGenerationResult",
    "SyntheticSubmissionGenerationResult",
    "generate_synthetic_submissions",
    "generate_unit",
]
