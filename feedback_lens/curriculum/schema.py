import json
import re


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
    stripped = text.strip()
    if stripped.startswith("{"):
        candidate = stripped
    else:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if match is None:
            raise ValueError("Response did not contain a JSON object.")
        candidate = match.group(0)

    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object.")
    return value


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
