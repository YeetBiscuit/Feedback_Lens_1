from pathlib import Path
import re
import textwrap

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


def write_rubric_table_pdf(text: str, output_path: str | Path) -> Path:
    rows = _parse_pipe_rows(text)
    if len(rows) < 2 or len(rows[0]) < 2:
        return write_plain_pdf(text, output_path)

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    doc = fitz.open()
    page_rect = fitz.paper_rect("a4-l")
    margin = 28
    usable_width = page_rect.width - (margin * 2)
    usable_height = page_rect.height - (margin * 2)
    font_size = 7.2
    header_font_size = 7.4
    line_height = font_size * 1.24
    vertical_padding = 4
    horizontal_padding = 3.5
    minimum_row_height = 24

    headers = rows[0]
    body_rows = rows[1:]
    column_widths = _rubric_column_widths(len(headers), usable_width)
    header_height = _row_height(
        headers,
        column_widths,
        header_font_size,
        line_height,
        vertical_padding,
        horizontal_padding,
        minimum_row_height,
    )

    page: fitz.Page | None = None
    current_y = margin

    def add_page() -> fitz.Page:
        nonlocal current_y
        new_page = doc.new_page(width=page_rect.width, height=page_rect.height)
        current_y = margin
        _draw_table_row(
            new_page,
            margin,
            current_y,
            headers,
            column_widths,
            header_height,
            header_font_size,
            vertical_padding,
            horizontal_padding,
            fill=(0.9, 0.9, 0.9),
        )
        current_y += header_height
        return new_page

    page = add_page()
    for row in body_rows:
        row_height = _row_height(
            row,
            column_widths,
            font_size,
            line_height,
            vertical_padding,
            horizontal_padding,
            minimum_row_height,
        )
        if current_y + row_height > margin + usable_height and current_y > margin + header_height:
            page = add_page()

        max_row_height = margin + usable_height - current_y
        _draw_table_row(
            page,
            margin,
            current_y,
            row,
            column_widths,
            min(row_height, max_row_height),
            font_size,
            vertical_padding,
            horizontal_padding,
        )
        current_y += min(row_height, max_row_height)

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


def _parse_pipe_rows(text: str) -> list[list[str]]:
    rows = []
    expected_width = None

    for line in text.splitlines():
        if "|" not in line:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or _is_separator_row(cells):
            continue
        if expected_width is None:
            expected_width = len(cells)
        rows.append(_normalise_row_width(cells, expected_width))

    return rows


def _is_separator_row(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def _normalise_row_width(cells: list[str], width: int) -> list[str]:
    if len(cells) < width:
        return cells + [""] * (width - len(cells))
    if len(cells) > width:
        return cells[: width - 1] + [" | ".join(cells[width - 1 :])]
    return cells


def _rubric_column_widths(column_count: int, usable_width: float) -> list[float]:
    if column_count <= 0:
        return []
    if column_count == 1:
        return [usable_width]

    criterion_width = usable_width * 0.17
    weight_width = usable_width * 0.08
    remaining = usable_width - criterion_width - weight_width
    descriptor_count = max(1, column_count - 2)
    widths = [criterion_width, weight_width]
    widths.extend([remaining / descriptor_count] * descriptor_count)
    return widths[:column_count]


def _row_height(
    cells: list[str],
    column_widths: list[float],
    font_size: float,
    line_height: float,
    vertical_padding: float,
    horizontal_padding: float,
    minimum_row_height: float,
) -> float:
    max_lines = 1
    for cell, width in zip(cells, column_widths):
        max_text_width = max(20, width - (horizontal_padding * 2))
        max_lines = max(max_lines, len(_wrap_cell_text(cell, max_text_width, font_size)))
    return max(minimum_row_height, (max_lines * line_height) + (vertical_padding * 2))


def _draw_table_row(
    page: fitz.Page,
    x: float,
    y: float,
    cells: list[str],
    column_widths: list[float],
    row_height: float,
    font_size: float,
    vertical_padding: float,
    horizontal_padding: float,
    fill: tuple[float, float, float] | None = None,
) -> None:
    current_x = x
    for cell, width in zip(cells, column_widths):
        rect = fitz.Rect(current_x, y, current_x + width, y + row_height)
        page.draw_rect(rect, color=(0, 0, 0), fill=fill, width=0.5)
        text_rect = fitz.Rect(
            rect.x0 + horizontal_padding,
            rect.y0 + vertical_padding,
            rect.x1 - horizontal_padding,
            rect.y1 - vertical_padding,
        )
        page.insert_textbox(
            text_rect,
            "\n".join(_wrap_cell_text(cell, text_rect.width, font_size)),
            fontsize=font_size,
            fontname="helv",
            lineheight=1.15,
            align=fitz.TEXT_ALIGN_LEFT,
        )
        current_x += width


def _wrap_cell_text(text: str, max_width: float, font_size: float) -> list[str]:
    words = text.split()
    if not words:
        return [""]

    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if _text_width(candidate, font_size) <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)
        if _text_width(word, font_size) <= max_width:
            current = word
        else:
            split_words = _split_long_word(word, max_width, font_size)
            lines.extend(split_words[:-1])
            current = split_words[-1]

    if current:
        lines.append(current)
    return lines or [""]


def _split_long_word(word: str, max_width: float, font_size: float) -> list[str]:
    average_char_width = max(1, font_size * 0.5)
    chunk_size = max(4, int(max_width / average_char_width))
    return textwrap.wrap(word, width=chunk_size, break_long_words=True) or [word]


def _text_width(text: str, font_size: float) -> float:
    return fitz.get_text_length(text, fontname="helv", fontsize=font_size)
