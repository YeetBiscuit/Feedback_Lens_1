from pathlib import Path
import re

import fitz


_CRITERION_KEYWORDS = (
    "criterion",
    "criteria",
    "aspect",
    "dimension",
    "component",
    "task",
    "section",
)
_DESCRIPTION_KEYWORDS = (
    "description",
    "descriptor",
    "descriptors",
    "details",
    "comments",
    "expectation",
    "expectations",
)


def _clean_cell(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def _normalise_header(header: str, index: int) -> str:
    cleaned = _clean_cell(header)
    return cleaned or f"column_{index + 1}"


def _dedupe_headers(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    deduped = []

    for index, header in enumerate(headers):
        candidate = _normalise_header(header, index)
        key = candidate.casefold()
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            candidate = f"{candidate}_{seen[key]}"
        deduped.append(candidate)

    return deduped


def _row_matches_headers(row: list[str], headers: list[str]) -> bool:
    row_values = [_clean_cell(value).casefold() for value in row[: len(headers)]]
    header_values = [header.casefold() for header in headers]
    return bool(row_values) and row_values == header_values


def _extract_table_headers_and_rows(table) -> tuple[list[str], list[list[str]]]:
    extracted_rows = [
        [_clean_cell(cell) for cell in row]
        for row in table.extract()
        if any(_clean_cell(cell) for cell in row)
    ]

    header_names = []
    if getattr(table, "header", None) is not None:
        header_names = [_clean_cell(name) for name in table.header.names]

    if header_names and any(header_names):
        headers = _dedupe_headers(header_names)
        rows = (
            extracted_rows[1:]
            if extracted_rows and _row_matches_headers(extracted_rows[0], headers)
            else extracted_rows
        )
        return headers, rows

    if extracted_rows:
        headers = _dedupe_headers(extracted_rows[0])
        return headers, extracted_rows[1:]

    return [], []


def _rows_to_dicts(headers: list[str], rows: list[list[str]]) -> list[dict]:
    row_dicts = []
    width = len(headers)

    for row in rows:
        padded = row + [""] * max(0, width - len(row))
        row_dict = {headers[index]: padded[index] for index in range(width)}
        if any(value for value in row_dict.values()):
            row_dicts.append(row_dict)

    return row_dicts


def extract_pipe_rubric_tables(text: str) -> list[dict]:
    rows = _extract_pipe_rows(text)
    if len(rows) < 2:
        return []

    headers = _dedupe_headers(rows[0])
    body_rows = rows[1:]
    return [
        {
            "page_number": 1,
            "table_index": 1,
            "bbox": [],
            "row_count": len(rows),
            "column_count": len(headers),
            "headers": headers,
            "rows": _rows_to_dicts(headers, body_rows),
            "markdown": _rows_to_markdown([headers, *body_rows]),
        }
    ]


def extract_rubric_tables(file_path: str | Path) -> list[dict]:
    path = Path(file_path)
    if path.suffix.lower() != ".pdf":
        raise ValueError("Rubric import expects a PDF file so table extraction can run.")

    tables_json = []

    with fitz.open(path) as doc:
        for page_index in range(doc.page_count):
            page = doc.load_page(page_index)
            finder = page.find_tables()
            tables = list(getattr(finder, "tables", []))

            if not tables:
                fallback = page.find_tables(
                    vertical_strategy="text",
                    horizontal_strategy="text",
                )
                tables = list(getattr(fallback, "tables", []))

            for table_index, table in enumerate(tables, start=1):
                headers, rows = _extract_table_headers_and_rows(table)
                tables_json.append(
                    {
                        "page_number": page_index + 1,
                        "table_index": table_index,
                        "bbox": list(table.bbox),
                        "row_count": table.row_count,
                        "column_count": table.col_count,
                        "headers": headers,
                        "rows": _rows_to_dicts(headers, rows),
                        "markdown": table.to_markdown().strip(),
                    }
                )

    if tables_json:
        return tables_json

    extracted_text = []
    with fitz.open(path) as doc:
        for page in doc:
            extracted_text.append(page.get_text())
    return extract_pipe_rubric_tables("\n".join(extracted_text))


def _extract_pipe_rows(text: str) -> list[list[str]]:
    rows = []
    expected_width = None

    for line in text.splitlines():
        if "|" not in line:
            continue
        cells = [_clean_cell(cell) for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or _is_pipe_separator_row(cells):
            continue
        if expected_width is None:
            expected_width = len(cells)
        rows.append(_normalise_pipe_row_width(cells, expected_width))

    return rows


def _is_pipe_separator_row(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def _normalise_pipe_row_width(cells: list[str], width: int) -> list[str]:
    if len(cells) < width:
        return cells + [""] * (width - len(cells))
    if len(cells) > width:
        return cells[: width - 1] + [" | ".join(cells[width - 1 :])]
    return cells


def _rows_to_markdown(rows: list[list[str]]) -> str:
    return "\n".join("| " + " | ".join(row) + " |" for row in rows)


def _find_header_index(headers: list[str], keywords: tuple[str, ...]) -> int | None:
    for index, header in enumerate(headers):
        header_lower = header.casefold()
        if any(keyword in header_lower for keyword in keywords):
            return index
    return None


def _fallback_criterion_index(rows: list[dict], headers: list[str]) -> int | None:
    for index, header in enumerate(headers):
        values = [_clean_cell(row.get(header, "")) for row in rows]
        non_empty = [value for value in values if value]
        if non_empty and len(set(non_empty)) > 1:
            return index
    return 0 if headers else None


def extract_rubric_criteria(tables: list[dict]) -> list[dict]:
    criteria = []
    criterion_order = 1

    for table in tables:
        headers = table["headers"]
        rows = table["rows"]
        if not headers or not rows:
            continue

        criterion_index = _find_header_index(headers, _CRITERION_KEYWORDS)
        description_index = _find_header_index(headers, _DESCRIPTION_KEYWORDS)

        if criterion_index is None:
            criterion_index = _fallback_criterion_index(rows, headers)

        for row in rows:
            values = [_clean_cell(row.get(header, "")) for header in headers]
            if not any(values):
                continue

            if criterion_index is None:
                criterion_name = next((value for value in values if value), "")
            else:
                criterion_name = values[criterion_index]

            if not criterion_name:
                continue

            criterion_name_lower = criterion_name.casefold()
            if criterion_name_lower in {"criterion", "criteria", "total"}:
                continue

            criterion_description = None
            if description_index is not None:
                criterion_description = values[description_index] or None

            performance_levels = {}
            for index, header in enumerate(headers):
                if index in {criterion_index, description_index}:
                    continue
                if values[index]:
                    performance_levels[header] = values[index]

            criteria.append(
                {
                    "criterion_name": criterion_name,
                    "criterion_description": criterion_description,
                    "criterion_order": criterion_order,
                    "performance_levels": performance_levels or None,
                    "source_page_number": table["page_number"],
                    "source_table_index": table["table_index"],
                }
            )
            criterion_order += 1

    return criteria
