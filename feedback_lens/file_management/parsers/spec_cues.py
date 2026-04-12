import re


_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+(.+?)\s*$")
_ADMIN_TITLE_KEYWORDS = (
    "submission",
    "format",
    "due",
    "weight",
    "length",
    "word count",
    "word limit",
    "late",
    "penalt",
    "extension",
    "cover sheet",
    "academic integrity",
    "referenc",
)
_ADMIN_CONTENT_KEYWORDS = (
    "lms",
    "pdf",
    "word",
    "words",
    "heading",
    "due",
    "submit",
    "submission",
    "upload",
    "late",
    "penalt",
)
_GOAL_SECTION_KEYWORDS = (
    "overview",
    "objective",
    "task description",
    "brief",
    "aim",
    "purpose",
)
_LIGHTWEIGHT_MERGE_TITLES = ("notes", "tips", "guidance")


def _clean_line(text: str) -> str:
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    text = re.sub(r"(?<=\d)\u6bcf(?=\d)", "-", text)
    text = " ".join(text.split())
    return text.strip()


def _normalise_title(title: str) -> str:
    title = _clean_line(title)
    match = _HEADING_RE.match(title)
    if match is not None:
        return match.group(2)
    return title


def _split_spec_sections(pages: list[dict], cleaned_text: str) -> list[dict]:
    sections: list[dict] = []
    current: dict | None = None

    for page in pages:
        page_number = page["page"]
        lines = [_clean_line(line) for line in page["text"].splitlines()]

        for line in lines:
            if not line:
                continue

            heading_match = _HEADING_RE.match(line)
            if heading_match is not None:
                if current is not None and current["lines"]:
                    sections.append(current)
                current = {
                    "title": heading_match.group(2),
                    "source_heading": line,
                    "page_start": page_number,
                    "page_end": page_number,
                    "lines": [],
                }
                continue

            if current is None:
                continue

            current["lines"].append(line)
            current["page_end"] = page_number

    if current is not None and current["lines"]:
        sections.append(current)

    if sections:
        return sections

    paragraphs = [
        _clean_line(paragraph)
        for paragraph in re.split(r"\n\s*\n", cleaned_text)
        if _clean_line(paragraph)
    ]
    if not paragraphs:
        return []

    return [
        {
            "title": "Assignment Specification",
            "source_heading": "Assignment Specification",
            "page_start": 1,
            "page_end": len(pages) or 1,
            "lines": paragraphs,
        }
    ]


def _is_admin_section(title: str, text: str) -> bool:
    title_lower = title.casefold()
    text_lower = text.casefold()

    if any(keyword in title_lower for keyword in _ADMIN_TITLE_KEYWORDS):
        return True

    matches = sum(keyword in text_lower for keyword in _ADMIN_CONTENT_KEYWORDS)
    return matches >= 3 and len(text_lower.split()) <= 80


def _cue_label_for_title(title: str) -> str:
    title = _normalise_title(title)
    words = title.split()
    if not words:
        return "Assignment Cue"
    return " ".join(word[:1].upper() + word[1:] for word in words)


def _build_goal_cue(goal_sections: list[dict]) -> dict | None:
    if not goal_sections:
        return None

    texts = []
    source_sections = []
    page_start = goal_sections[0]["page_start"]
    page_end = goal_sections[0]["page_end"]

    for section in goal_sections:
        text = " ".join(section["lines"]).strip()
        if text:
            texts.append(text)
        source_sections.append(section["title"])
        page_start = min(page_start, section["page_start"])
        page_end = max(page_end, section["page_end"])

    if not texts:
        return None

    return {
        "order": 0,
        "label": "Assignment Goal",
        "cue_type": "assignment_goal",
        "source_sections": source_sections,
        "source_page_start": page_start,
        "source_page_end": page_end,
        "text": " ".join(texts),
    }


def build_assignment_spec_cues(pages: list[dict], cleaned_text: str) -> list[dict]:
    sections = _split_spec_sections(pages, cleaned_text)
    if not sections:
        return []

    goal_sections: list[dict] = []
    remaining_sections: list[dict] = []

    for section in sections:
        title = _normalise_title(section["title"])
        text = " ".join(section["lines"]).strip()
        if not text:
            continue

        title_lower = title.casefold()
        if any(keyword in title_lower for keyword in _GOAL_SECTION_KEYWORDS):
            goal_sections.append({**section, "title": title})
            continue

        if _is_admin_section(title, text):
            continue

        remaining_sections.append({**section, "title": title})

    cues: list[dict] = []
    goal_cue = _build_goal_cue(goal_sections)
    if goal_cue is not None:
        cues.append(goal_cue)

    for section in remaining_sections:
        cue = {
            "order": 0,
            "label": _cue_label_for_title(section["title"]),
            "cue_type": "section",
            "source_sections": [section["title"]],
            "source_page_start": section["page_start"],
            "source_page_end": section["page_end"],
            "text": " ".join(section["lines"]).strip(),
        }

        if (
            cues
            and cue["label"].casefold() in _LIGHTWEIGHT_MERGE_TITLES
            and len(cue["text"].split()) <= 40
        ):
            previous = cues[-1]
            previous["text"] = f"{previous['text']} {cue['text']}".strip()
            previous["source_sections"] = previous["source_sections"] + cue["source_sections"]
            previous["source_page_end"] = max(
                previous["source_page_end"],
                cue["source_page_end"],
            )
            continue

        cues.append(cue)

    if not cues and cleaned_text.strip():
        cues.append(
            {
                "order": 0,
                "label": "Assignment Specification",
                "cue_type": "fallback_full_text",
                "source_sections": ["Assignment Specification"],
                "source_page_start": 1,
                "source_page_end": len(pages) or 1,
                "text": cleaned_text.strip(),
            }
        )

    for index, cue in enumerate(cues, start=1):
        cue["order"] = index

    return cues
