from dataclasses import dataclass
import re


@dataclass
class SubmissionScreeningResult:
    allowed: bool
    risk_level: str
    reason: str | None = None
    signals: tuple[str, ...] = ()


_INJECTION_PATTERNS = (
    (
        "instruction_override",
        re.compile(
            r"\b(ignore|disregard|forget|override)\b.{0,80}"
            r"\b(previous|prior|system|developer|original)\b.{0,40}"
            r"\b(instruction|instructions|prompt|rules?)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "grade_manipulation",
        re.compile(
            r"\b(give|assign|award|set)\b.{0,60}"
            r"\b(me|this\s+(?:submission|work|assignment))\b.{0,40}"
            r"\b(highest|maximum|full|HD|distinction|grade|mark|score)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "grader_instruction",
        re.compile(
            r"\b(AI|LLM|model|grader|marker|evaluator)\b.{0,60}"
            r"\b(ignore|disregard|override|follow|obey|award|assign)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "system_prompt_extraction",
        re.compile(
            r"\b(reveal|show|print|display|repeat|return)\b.{0,60}"
            r"\b(system\s+prompt|developer\s+message|hidden\s+instructions?)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
)

_ACADEMIC_CONTEXT_PATTERNS = (
    re.compile(
        r"\b(example|example of|such as|phrase|quoted?|quotation)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(attacker|attackers|researcher|researchers|study|studies)\b"
        r".{0,100}\b(prompt injection|AI|LLM|grader|model)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r"\b(prompt injection|jailbreak|security vulnerability|security attack)\b",
        re.IGNORECASE,
    ),
)

def _has_academic_context(text: str) -> bool:
    return any(
        pattern.search(text)
        for pattern in _ACADEMIC_CONTEXT_PATTERNS
    )

def _has_direct_assessment_manipulation(
    text: str,
    signals: list[str],
) -> bool:
    signal_set = set(signals)

    third_person_reporting = re.search(
        r"\b(attacker|attackers|student|students|user|users|"
        r"researcher|researchers)\b.{0,80}"
        r"\b(may|might|can|could|would|attempt|attempts|"
        r"tell|tells|instruct|instructs)\b",
        text,
        re.IGNORECASE | re.DOTALL,
    )

    if third_person_reporting:
        return False

    return (
        "grade_manipulation" in signal_set
        or (
            "grader_instruction" in signal_set
            and "instruction_override" in signal_set
        )
        or "system_prompt_extraction" in signal_set
    )

def screen_text(text: str | None) -> SubmissionScreeningResult:
    if not text or not text.strip():
        return SubmissionScreeningResult(
            allowed=True,
            risk_level="none",
        )

    signals = []

    for name, pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            signals.append(name)

    if not signals:
        return SubmissionScreeningResult(
            allowed=True,
            risk_level="none",
        )

    # Strong manipulation signals take priority over academic-looking
    # surrounding language. This prevents attackers from bypassing the
    # prescreen by prefixing an attack with phrases such as
    # "this is an example of prompt injection".
    if _has_direct_assessment_manipulation(text, signals):
        return SubmissionScreeningResult(
            allowed=False,
            risk_level="high",
            reason=(
                "Submission contains direct instructions that may attempt to "
                "manipulate the assessment or feedback system."
            ),
            signals=tuple(signals),
        )

    # A suspicious phrase can appear legitimately in academic writing,
    # for example when a student explains or quotes a prompt-injection attack.
    if _has_academic_context(text):
        return SubmissionScreeningResult(
            allowed=True,
            risk_level="low",
            reason=(
                "Prompt-injection-like language was detected, but it appears "
                "within explanatory or academic context."
            ),
            signals=tuple(signals),
        )

    return SubmissionScreeningResult(
        allowed=False,
        risk_level="high",
        reason=(
            "Submission contains instructions that may attempt to "
            "manipulate the assessment or feedback system."
        ),
        signals=tuple(signals),
    )

def screen_submission(conn, submission_id: int) -> SubmissionScreeningResult:
    row = conn.execute(
        """
        SELECT cleaned_text
        FROM student_submissions
        WHERE submission_id = ?
        """,
        (submission_id,),
    ).fetchone()

    if row is None:
        raise ValueError(f"Submission {submission_id} was not found.")

    return screen_text(row["cleaned_text"])