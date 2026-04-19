from pathlib import Path

import fitz


def write_plain_pdf(text: str, output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    doc = fitz.open()
    margin = 54
    font_size = 10.5
    line_height = font_size * 1.35
    page_rect = fitz.paper_rect("a4")
    usable_width = page_rect.width - (margin * 2)
    usable_height = page_rect.height - (margin * 2)

    for logical_page in _paginate_text(text, usable_width, usable_height, font_size, line_height):
        page = doc.new_page(width=page_rect.width, height=page_rect.height)
        rect = fitz.Rect(margin, margin, page_rect.width - margin, page_rect.height - margin)
        page.insert_textbox(
            rect,
            logical_page,
            fontsize=font_size,
            fontname="courier",
            lineheight=1.35,
        )

    if doc.page_count == 0:
        page = doc.new_page(width=page_rect.width, height=page_rect.height)
        page.insert_text((margin, margin), "", fontsize=font_size)

    doc.save(path)
    doc.close()
    return path


def _paginate_text(
    text: str,
    usable_width: float,
    usable_height: float,
    font_size: float,
    line_height: float,
) -> list[str]:
    average_char_width = font_size * 0.62
    max_chars = max(40, int(usable_width / average_char_width))
    max_lines = max(20, int(usable_height / line_height))

    wrapped_lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        if not paragraph:
            wrapped_lines.append("")
            continue
        current = paragraph
        while len(current) > max_chars:
            break_at = current.rfind(" ", 0, max_chars)
            if break_at < max_chars // 2:
                break_at = max_chars
            wrapped_lines.append(current[:break_at].rstrip())
            current = current[break_at:].lstrip()
        wrapped_lines.append(current)

    pages = []
    for index in range(0, len(wrapped_lines), max_lines):
        pages.append("\n".join(wrapped_lines[index : index + max_lines]))
    return pages or [""]
