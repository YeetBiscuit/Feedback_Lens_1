import re
from pathlib import Path

from feedback_lens.file_management.readers.pdf_reader import extract_pages
from feedback_lens.file_management.readers.text_reader import read_transcript
from feedback_lens.paths import PROJECT_ROOT


READERS = {
    ".pdf": extract_pages,
    ".txt": read_transcript,
}


def read_document_pages(file_path: str | Path) -> list[dict]:
    path = Path(file_path)
    reader = READERS.get(path.suffix.lower())
    if reader is None:
        raise ValueError(
            f"Unsupported file type '{path.suffix}'. Supported types: {', '.join(READERS)}"
        )
    return reader(str(path))


def pages_to_text(pages: list[dict]) -> str:
    return "\n\n".join(page["text"] for page in pages if page["text"]).strip()


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalise_source_path(file_path: str | Path) -> str:
    path = Path(file_path).expanduser()
    resolved_path = path.resolve(strict=False)

    try:
        return str(resolved_path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(resolved_path)


def extract_document(file_path: str | Path) -> dict:
    path = Path(file_path)
    pages = read_document_pages(path)
    raw_text = pages_to_text(pages)
    cleaned_text = clean_text(raw_text)
    return {
        "file_name": path.name,
        "file_path": normalise_source_path(path),
        "page_count": len(pages),
        "pages": pages,
        "raw_text": raw_text,
        "cleaned_text": cleaned_text,
    }
