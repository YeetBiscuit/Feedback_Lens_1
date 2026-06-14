import json
import re
from json import JSONDecodeError


REQUIRED_SCHEMA_FIELDS = {
    "course_code",
    "course_title",
    "level",
    "discipline",
    "credit_points",
    "weeks",
    "learning_outcomes",
    "topics",
    "assignments",
}


def extract_json_object(text: str) -> dict:
    candidate = _extract_balanced_json_object(text)
    try:
        value = json.loads(candidate)
    except JSONDecodeError as original_error:
        repaired = _repair_common_json_issues(candidate)
        if repaired == candidate:
            raise
        try:
            value = json.loads(repaired)
        except JSONDecodeError:
            raise original_error

    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object.")
    return value


def _extract_balanced_json_object(text: str) -> str:
    start = text.find("{")
    if start < 0:
        raise ValueError("Response did not contain a JSON object.")

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    return text[start:].strip()


def _repair_common_json_issues(candidate: str) -> str:
    repaired = (
        candidate.replace("\ufeff", "")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )
    repaired = re.sub(r",(\s*[}\]])", r"\1", repaired)
    return _insert_missing_commas(repaired)


def _insert_missing_commas(candidate: str) -> str:
    parts: list[str] = []
    in_string = False
    escaped = False
    length = len(candidate)
    for index, char in enumerate(candidate):
        parts.append(char)
        was_closing_quote = False

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
                was_closing_quote = True
        elif char == '"':
            in_string = True

        if in_string:
            continue

        current_ends_value = char in "}]0123456789" or was_closing_quote
        if not current_ends_value:
            continue

        next_index = index + 1
        while next_index < length and candidate[next_index].isspace():
            next_index += 1
        if next_index >= length:
            continue

        next_char = candidate[next_index]
        if next_char in "{[\"":
            parts.append(",")

    return "".join(parts)


def validate_course_schema(schema: dict) -> None:
    missing = sorted(REQUIRED_SCHEMA_FIELDS - set(schema))
    if missing:
        raise ValueError(f"Course schema is missing required fields: {missing}")

    if not isinstance(schema["topics"], list) or not schema["topics"]:
        raise ValueError("Course schema must include at least one topic.")
    if not isinstance(schema["assignments"], list) or not schema["assignments"]:
        raise ValueError("Course schema must include at least one assignment.")

    weeks = int(schema["weeks"])
    topic_weeks = set()
    for topic in schema["topics"]:
        if not isinstance(topic, dict):
            raise ValueError("Each topic must be an object.")
        week = int(topic.get("week", 0))
        if week < 1 or week > weeks:
            raise ValueError(f"Topic week {week} is outside the unit week range.")
        topic_weeks.add(week)

    assignment_ids = set()
    for assignment in schema["assignments"]:
        if not isinstance(assignment, dict):
            raise ValueError("Each assignment must be an object.")
        assignment_id = str(assignment.get("id") or "").strip()
        if not assignment_id:
            raise ValueError("Each assignment must include an id.")
        if assignment_id in assignment_ids:
            raise ValueError(f"Duplicate assignment id: {assignment_id}")
        assignment_ids.add(assignment_id)
        for week in assignment.get("linked_topics") or []:
            if int(week) not in topic_weeks:
                raise ValueError(
                    f"Assignment {assignment_id} links to unknown topic week {week}."
                )
