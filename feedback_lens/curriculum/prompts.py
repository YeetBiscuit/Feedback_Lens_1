import json

GRADE_BAND_PROFILES = {
    "HD": """
Target grade: HD (80-100%)

Use the rubric to satisfy the High Distinction descriptors across the submission.

The generated submission should:
- address all major task requirements;
- show deep understanding of the core task;
- use the available materials precisely and consistently;
- provide strong analysis, synthesis, original connections, and detailed justification;
- use specific evidence rather than general claims;
- sound like a genuine high-performing student submission, with only realistic minor imperfections that do not reduce the grade.
""".strip(),
    "D": """
Target grade: D (70-79%)

Use the rubric to satisfy most Distinction-level descriptors. Treat D as the grade ceiling.

The generated submission should:
- meet almost all major task requirements;
- show clear understanding of the core task;
- use most relevant materials accurately;
- include some synthesis and justification, but not sustained HD-level insight;
- have minor gaps in depth, precision, or originality;
- avoid the sustained originality, polish, or comprehensive evidence that would justify HD;
- sound like a genuine capable student submission, not a deliberately weakened answer.
""".strip(),
    "C": """
Target grade: C (60-69%)

Use the rubric to satisfy the core Credit-level descriptors. Treat C as the grade ceiling.

The generated submission should:
- meet the central task requirements but leave some secondary requirements underdeveloped;
- show adequate understanding of the core task;
- use mostly relevant materials, but unevenly;
- include some analysis, while still relying on description or summary in places;
- include several omissions compared with D or HD descriptors;
- avoid strong synthesis, original connections, or highly detailed justification;
- sound like a genuine mid-range student submission, not an intentionally bad parody.

The submission must contain clear grade-limiting weaknesses. A judge should have obvious evidence for why it should not receive D or HD.
""".strip(),
    "P": """
Target grade: Pass (50-59%)

Use the rubric, but intentionally satisfy only the minimum Pass-level descriptors. Treat P as the grade ceiling.

The generated submission should:
- meet enough requirements to avoid failing;
- show limited understanding of the core task;
- use some relevant materials, but inconsistently and sometimes superficially;
- mostly summarise rather than analyse;
- include several visible omissions compared with C, D, or HD descriptors;
- include one or two plausible misunderstandings or weak interpretations;
- make some vague claims without enough evidence;
- provide limited or generic justification;
- avoid strong synthesis, original connections, or detailed justification;
- sound like a genuine student submission, not an intentionally bad parody.

Do not accidentally include evidence that would justify C, D, or HD.

The submission must contain clear grade-limiting weaknesses. A judge should have obvious evidence for why it should not receive C, D, or HD.
""".strip(),
}


def schema_messages(description: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are an academic curriculum designer. Respond only in valid "
                "JSON. No markdown fences, no preamble."
            ),
        },
        {
            "role": "user",
            "content": f"""
Using the course description below, generate a structured course schema.

Course description:
\"\"\"
{description}
\"\"\"

The schema must follow this exact structure:
{{
  "course_code": "string",
  "course_title": "string",
  "level": "undergraduate_year_N | postgraduate",
  "discipline": "string",
  "credit_points": number,
  "weeks": number,
  "learning_outcomes": ["LO1...", "LO2...", "..."],
  "topics": [
    {{ "week": number, "title": "string", "summary": "1-2 sentences" }}
  ],
  "assignments": [
    {{
      "id": "A1",
      "title": "string",
      "type": "essay | report | case_study | project | lab_report | presentation",
      "weight": number,
      "due_week": number,
      "word_count_or_equivalent": "string",
      "linked_topics": [1, 2],
      "learning_outcomes_assessed": ["LO1"]
    }}
  ]
}}

Return only the JSON object.
""".strip(),
        },
    ]


def assignment_spec_messages(schema: dict, assignment_id: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are a university unit coordinator writing formal assessment "
                "documents. Write in plain formal English. Use clear heading "
                "labels. Do not use markdown."
            ),
        },
        {
            "role": "user",
            "content": f"""
Generate a complete assignment specification document for the following assignment.

Course schema:
\"\"\"
{json.dumps(schema, ensure_ascii=False, indent=2)}
\"\"\"

Target assignment: {assignment_id}

Include these sections in order:
1. ASSIGNMENT OVERVIEW - title, type, weight, due week, word count
2. LEARNING OUTCOMES ASSESSED - list the LOs from the schema
3. TASK DESCRIPTION - detailed explanation of what students must do
4. SPECIFIC REQUIREMENTS - numbered list of mandatory elements
5. SUBMISSION FORMAT - file format, naming convention, submission method placeholder
6. ACADEMIC INTEGRITY NOTE - one paragraph
7. SUPPORT RESOURCES - relevant lecture weeks from linked_topics

Length: approximately 600-800 words total.
""".strip(),
        },
    ]


def rubric_messages(spec_text: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are a university unit coordinator writing formal assessment "
                "rubrics. Output a plain text table using pipe characters for "
                "columns. Do not use markdown prose."
            ),
        },
        {
            "role": "user",
            "content": f"""
Generate a complete marking rubric for the assignment specification below.

Assignment specification:
\"\"\"
{spec_text}
\"\"\"

Column headers must be:
CRITERION | WEIGHT | HD (80-100%) | D (70-79%) | C (60-69%) | P (50-59%) | FAIL (<50%)

Rules:
- Include 4-6 criteria that together sum to 100%.
- Each criterion must correspond to a specific requirement in the spec.
- Each grade band descriptor must be 1-2 specific sentences.
- Add a final row: TOTAL | 100% | | | | | |
""".strip(),
        },
    ]


def lecture_messages(schema: dict, topic: dict) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are a university lecturer. Write in a natural spoken "
                "academic register as if transcribed from a recorded lecture. "
                "Do not use markdown or bullet points."
            ),
        },
        {
            "role": "user",
            "content": f"""
Generate a pseudo lecture transcript for the following topic.

Course schema:
\"\"\"
{json.dumps(schema, ensure_ascii=False, indent=2)}
\"\"\"

Topic: Week {topic.get("week")} - {topic.get("title")}
Topic summary: {topic.get("summary")}

Structure the transcript with these headings on their own lines:
INTRODUCTION
SECTION 1: CORE CONCEPT
SECTION 2: APPLICATION AND NUANCE
SECTION 3: IMPLICATIONS / APPLICATIONS
WRAP-UP

Total target length: 1000-1200 words.
""".strip(),
        },
    ]


def worksheet_messages(spec_text: str, transcripts: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are a university tutor designing a guided practice worksheet. "
                "Use plain text with numbered questions. No markdown."
            ),
        },
        {
            "role": "user",
            "content": f"""
Generate a tutorial worksheet that prepares students for the assignment below.

Assignment specification:
\"\"\"
{spec_text}
\"\"\"

Relevant lecture transcripts:
\"\"\"
{transcripts}
\"\"\"

The worksheet must contain:
1. WORKSHEET TITLE and which assignment it prepares for
2. LEARNING OBJECTIVES - 3 bullet points stating what students will practise
3. WARM-UP - 2 short questions
4. GUIDED ACTIVITIES - 3 scaffolded tasks with estimated times
5. REFLECTION PROMPT - one open-ended question
6. EXTENSION TASK - one harder optional task

Do not include answers.
""".strip(),
        },
    ]


def sample_answer_messages(worksheet_text: str, transcripts: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are a university tutor writing a sample answer guide for "
                "internal use. Write answers at the level of a strong "
                "Distinction student. Use plain text."
            ),
        },
        {
            "role": "user",
            "content": f"""
Generate sample answers for the tutorial worksheet below.

Worksheet:
\"\"\"
{worksheet_text}
\"\"\"

Relevant lecture transcripts:
\"\"\"
{transcripts}
\"\"\"

Rules:
- Warm-up answers: 2-4 sentences each.
- Guided activity answers: 100-200 words each.
- Reflection prompt answer: 80-120 words.
- Extension answer: 150-200 words.
- Add a NOTE TO TUTORS section with 2-3 common mistakes.
""".strip(),
        },
    ]


def submission_messages(
    spec_text: str,
    rubric_text: str,
    transcripts: str,
    worksheet_text: str,
    sample_answer_text: str,
    grade_band: str,
    variation_note: str | None = None,
) -> list[dict[str, str]]:
    profile = GRADE_BAND_PROFILES[grade_band]
    user_content = f"""
Write a complete student submission for the assignment below.

ASSIGNMENT SPECIFICATION:
\"\"\"
{spec_text}
\"\"\"

MARKING RUBRIC:
\"\"\"
{rubric_text}
\"\"\"

COURSE MATERIALS AVAILABLE TO THIS STUDENT:
Lecture transcripts:
\"\"\"
{transcripts}
\"\"\"

Tutorial worksheet and sample answers:
\"\"\"
{worksheet_text}

{sample_answer_text}
\"\"\"

GRADE CALIBRATION PROFILE:
Grade band target: {grade_band}
{profile}

The submission must be realistic for the target grade band when marked against
the rubric. Do not produce a perfect submission for any band below HD.

Before writing, internally compare the planned submission against the rubric and
reduce or improve it until the evidence supports the target band, not a higher
band. Do not output this check.
""".strip()
    if variation_note:
        user_content = (
            f"{user_content}\n\n"
            "VARIATION REQUIREMENTS:\n"
            "Variation requirements must not override the target grade calibration.\n"
            f"{variation_note.strip()}"
        )

    return [
        {
            "role": "system",
            "content": (
                "You are simulating a university student completing an assignment. "
                "Write in first-person student voice. Do not break character. "
                "Output only the complete submission document. No markdown."
            ),
        },
        {
            "role": "user",
            "content": user_content,
        },
    ]


def audit_messages(schema: dict, assignment_payloads: list[dict]) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are a quality assurance reviewer for educational content. "
                "Be direct and specific. Flag issues with precise references."
            ),
        },
        {
            "role": "user",
            "content": f"""
Review the following course content package for internal consistency.

Course schema:
\"\"\"
{json.dumps(schema, ensure_ascii=False, indent=2)}
\"\"\"

Assignment package:
\"\"\"
{json.dumps(assignment_payloads, ensure_ascii=False, indent=2)}
\"\"\"

Check and report on:
1. RUBRIC-SPEC ALIGNMENT
2. GRADE BAND DIFFERENTIATION
3. MATERIAL COVERAGE
4. PERSONA REALISM
5. OVERALL VERDICT - Pass or Revise with specific changes needed
""".strip(),
        },
    ]
