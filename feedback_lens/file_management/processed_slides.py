import json
from pathlib import Path


SLIDE_CHUNK_SIZE = 5
SLIDE_CHUNK_OVERLAP = 1
SLIDE_CHUNKING_STRATEGY = "ai_visual_to_text_slides_5_overlap_1"

VISUAL_ELEMENT_TYPES = {
    "table",
    "chart",
    "graph",
    "diagram",
    "flowchart",
    "map",
    "image",
    "illustration",
    "screenshot",
    "equation",
    "other",
}

TOP_LEVEL_FIELDS = {"lecture", "source_file", "slides"}
OPTIONAL_TOP_LEVEL_FIELDS = {"schema_version", "preprocessing"}
SLIDE_FIELDS = {
    "slide_number",
    "title",
    "text_content",
    "visual_elements",
    "unclear_content",
    "has_instructional_content",
}
VISUAL_FIELDS = {"type", "title", "description"}


class ProcessedSlidesValidationError(ValueError):
    """Raised when an AI-assisted slide JSON document is invalid."""


def _field_names(value: dict) -> set[str]:
    return {str(key) for key in value}


def _require_exact_fields(
    value: dict,
    required: set[str],
    *,
    location: str,
    optional: set[str] | None = None,
) -> None:
    fields = _field_names(value)
    missing = sorted(required - fields)
    unexpected = sorted(fields - required - (optional or set()))
    if missing:
        raise ProcessedSlidesValidationError(
            f"{location} is missing required field(s): {', '.join(missing)}."
        )
    if unexpected:
        raise ProcessedSlidesValidationError(
            f"{location} has unexpected field(s): {', '.join(unexpected)}."
        )


def _require_non_empty_string(value: object, *, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProcessedSlidesValidationError(
            f"{location} must be a non-empty string."
        )
    return value


def _validate_string_list(value: object, *, location: str) -> list[str]:
    if not isinstance(value, list):
        raise ProcessedSlidesValidationError(f"{location} must be a list.")
    for index, item in enumerate(value):
        _require_non_empty_string(
            item,
            location=f"{location}[{index}]",
        )
    return value


def validate_processed_slides(document: object) -> dict:
    """Validate and return one processed lecture-slide JSON document."""
    if not isinstance(document, dict):
        raise ProcessedSlidesValidationError(
            "The processed slide document must be a JSON object."
        )
    _require_exact_fields(
        document,
        TOP_LEVEL_FIELDS,
        optional=OPTIONAL_TOP_LEVEL_FIELDS,
        location="The processed slide document",
    )
    _require_non_empty_string(document["lecture"], location="lecture")
    _require_non_empty_string(document["source_file"], location="source_file")

    slides = document["slides"]
    if not isinstance(slides, list) or not slides:
        raise ProcessedSlidesValidationError(
            "slides must be a non-empty list."
        )

    expected_number = 1
    for index, slide in enumerate(slides):
        location = f"slides[{index}]"
        if not isinstance(slide, dict):
            raise ProcessedSlidesValidationError(
                f"{location} must be a JSON object."
            )
        _require_exact_fields(slide, SLIDE_FIELDS, location=location)

        slide_number = slide["slide_number"]
        if (
            isinstance(slide_number, bool)
            or not isinstance(slide_number, int)
            or slide_number != expected_number
        ):
            raise ProcessedSlidesValidationError(
                f"{location}.slide_number must be the integer "
                f"{expected_number}."
            )
        expected_number += 1

        title = slide["title"]
        if title is not None:
            _require_non_empty_string(title, location=f"{location}.title")
        text_content = _validate_string_list(
            slide["text_content"],
            location=f"{location}.text_content",
        )
        unclear_content = _validate_string_list(
            slide["unclear_content"],
            location=f"{location}.unclear_content",
        )

        visual_elements = slide["visual_elements"]
        if not isinstance(visual_elements, list):
            raise ProcessedSlidesValidationError(
                f"{location}.visual_elements must be a list."
            )
        for visual_index, visual in enumerate(visual_elements):
            visual_location = (
                f"{location}.visual_elements[{visual_index}]"
            )
            if not isinstance(visual, dict):
                raise ProcessedSlidesValidationError(
                    f"{visual_location} must be a JSON object."
                )
            _require_exact_fields(
                visual,
                VISUAL_FIELDS,
                location=visual_location,
            )
            visual_type = _require_non_empty_string(
                visual["type"],
                location=f"{visual_location}.type",
            )
            if visual_type not in VISUAL_ELEMENT_TYPES:
                allowed = ", ".join(sorted(VISUAL_ELEMENT_TYPES))
                raise ProcessedSlidesValidationError(
                    f"{visual_location}.type must be one of: {allowed}."
                )
            visual_title = visual["title"]
            if visual_title is not None:
                _require_non_empty_string(
                    visual_title,
                    location=f"{visual_location}.title",
                )
            _require_non_empty_string(
                visual["description"],
                location=f"{visual_location}.description",
            )

        instructional = slide["has_instructional_content"]
        if not isinstance(instructional, bool):
            raise ProcessedSlidesValidationError(
                f"{location}.has_instructional_content must be a boolean."
            )
        has_content = bool(title or text_content or visual_elements or unclear_content)
        if instructional and not has_content:
            raise ProcessedSlidesValidationError(
                f"{location} is marked as instructional but has no content."
            )
        if not instructional and (text_content or visual_elements or unclear_content):
            raise ProcessedSlidesValidationError(
                f"{location} is marked as non-instructional but contains content."
            )

    return document


def load_processed_slides(file_path: str | Path) -> dict:
    """Load and validate an AI-assisted visual-to-text JSON file."""
    path = Path(file_path)
    try:
        raw_text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ProcessedSlidesValidationError(
            "The processed slide file must be valid UTF-8 JSON."
        ) from exc
    try:
        document = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ProcessedSlidesValidationError(
            f"The processed slide file is not valid JSON: {exc.msg}."
        ) from exc
    return validate_processed_slides(document)


def render_processed_slide(slide: dict) -> str:
    """Render one validated slide without adding a derived summary."""
    lines = [f"Slide {slide['slide_number']}"]
    if slide["title"]:
        lines.append(f"Title: {slide['title']}")

    if slide["text_content"]:
        lines.append("Text:")
        lines.extend(slide["text_content"])

    for index, visual in enumerate(slide["visual_elements"], start=1):
        lines.append(f"Visual element {index} ({visual['type']}):")
        if visual["title"]:
            lines.append(f"Title: {visual['title']}")
        lines.append(visual["description"])

    if slide["unclear_content"]:
        lines.append("Preprocessing uncertainty:")
        lines.extend(slide["unclear_content"])

    return "\n".join(lines).strip()


def render_processed_slide_document(document: dict) -> str:
    """Render all instructional slides as searchable plain text."""
    validate_processed_slides(document)
    rendered = [
        render_processed_slide(slide)
        for slide in document["slides"]
        if slide["has_instructional_content"]
    ]
    return "\n\n".join(rendered)


def chunk_processed_slides(
    document: dict,
    chunk_size: int = SLIDE_CHUNK_SIZE,
    overlap: int = SLIDE_CHUNK_OVERLAP,
) -> list[dict]:
    """Create fixed-slide windows while excluding non-instructional slides."""
    validate_processed_slides(document)
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive.")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be between 0 and chunk_size - 1.")

    slides = [
        slide
        for slide in document["slides"]
        if slide["has_instructional_content"]
    ]
    if not slides:
        raise ProcessedSlidesValidationError(
            "The processed slide document has no instructional content."
        )

    chunks = []
    step = chunk_size - overlap
    start = 0
    while start < len(slides):
        window = slides[start : start + chunk_size]
        first_number = int(window[0]["slide_number"])
        last_number = int(window[-1]["slide_number"])
        body = "\n\n".join(render_processed_slide(slide) for slide in window)
        text = (
            f"Lecture: {document['lecture']}\n"
            f"Slides: {first_number}-{last_number}\n\n"
            f"{body}"
        )
        chunks.append(
            {
                "text": text,
                "page_start": first_number,
                "page_end": last_number,
                "word_count": len(text.split()),
                "section_title": f"Slides {first_number}-{last_number}",
                "chunking_strategy": SLIDE_CHUNKING_STRATEGY,
                "slide_numbers": [
                    int(slide["slide_number"]) for slide in window
                ],
            }
        )
        if start + chunk_size >= len(slides):
            break
        start += step
    return chunks
