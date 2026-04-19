from pathlib import Path

from feedback_lens.paths import DOCUMENTS_DIR


def slugify(value: str) -> str:
    import re

    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "item"


def unit_root(course_code: str) -> Path:
    return DOCUMENTS_DIR / "units" / course_code.upper()


def assignment_slug(assignment: dict) -> str:
    code = str(assignment.get("id") or assignment.get("assignment_code") or "")
    title = str(assignment.get("title") or assignment.get("assignment_name") or "")
    return slugify(f"{code}-{title}")


def topic_slug(topic: dict) -> str:
    return slugify(str(topic.get("title") or f"week-{topic.get('week', '')}"))


def ensure_unit_layout(root: Path) -> None:
    for relative in (
        "lectures",
        "tutorials",
        "assignments",
        "resources",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)


def collision_safe_path(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    version = 2
    while True:
        candidate = parent / f"{stem}_v{version}{suffix}"
        if not candidate.exists():
            return candidate
        version += 1
