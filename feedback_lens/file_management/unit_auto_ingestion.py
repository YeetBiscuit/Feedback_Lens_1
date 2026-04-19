import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SUPPORTED_TEXT_DOCUMENTS = {".pdf", ".txt"}


def hash_file(path: str | Path) -> str:
    from feedback_lens.file_management.document_io import hash_file as _hash_file

    return _hash_file(path)


def normalise_source_path(path: str | Path) -> str:
    from feedback_lens.file_management.document_io import (
        normalise_source_path as _normalise_source_path,
    )

    return _normalise_source_path(path)


@dataclass(slots=True)
class IngestionItem:
    item_type: str
    file_path: Path
    action: str
    status: str
    message: str = ""
    assignment_id: int | None = None
    spec_id: int | None = None
    rubric_id: int | None = None
    material_id: int | None = None
    submission_id: int | None = None
    source_content_hash: str | None = None


@dataclass(slots=True)
class UnitIngestionResult:
    unit_id: int | None
    course_code: str
    unit_directory: Path
    dry_run: bool
    force: bool
    ingestion_run_id: int | None = None
    items: list[IngestionItem] = field(default_factory=list)

    @property
    def imported_count(self) -> int:
        return sum(1 for item in self.items if item.action == "imported")

    @property
    def skipped_count(self) -> int:
        return sum(1 for item in self.items if item.action == "skipped")

    @property
    def planned_count(self) -> int:
        return sum(1 for item in self.items if item.action == "planned")

    @property
    def failed_count(self) -> int:
        return sum(1 for item in self.items if item.status == "failed")

    @property
    def submission_ids(self) -> list[int]:
        return [
            item.submission_id
            for item in self.items
            if item.submission_id is not None
        ]

    def summary(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "course_code": self.course_code,
            "unit_directory": str(self.unit_directory),
            "dry_run": self.dry_run,
            "force": self.force,
            "planned": self.planned_count,
            "imported": self.imported_count,
            "skipped": self.skipped_count,
            "failed": self.failed_count,
            "submission_ids": self.submission_ids,
        }


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "item"


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return value


def _manifest_path(unit_dir: Path, relative_path: str) -> str:
    return Path(relative_path).as_posix()


def _json_or_none(value: object) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _normalise_assignment_folder(value: str) -> str:
    return slugify(value)


def _extract_week_number(path: Path) -> int | None:
    match = re.search(r"week[_ -]?0*(\d+)", path.stem, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _title_from_path(path: Path) -> str:
    title = re.sub(r"[_-]+", " ", path.stem).strip()
    return title.title() if title else path.name


def _student_identifier_from_file(
    file_path: Path,
    assignment_slug: str,
    assignment_manifest: dict,
) -> str:
    identifiers = assignment_manifest.get("student_identifiers") or {}
    if isinstance(identifiers, dict):
        identifier = identifiers.get(file_path.name)
        if identifier:
            return str(identifier)

    grade_match = re.search(r"(HD|D|C|P)(?=\.|_|-|$)", file_path.stem, re.IGNORECASE)
    if grade_match:
        return f"{assignment_slug}_{grade_match.group(1).upper()}_synthetic"
    return file_path.stem


def _find_existing_unit(
    conn: sqlite3.Connection,
    course_code: str,
    year: int | None,
    semester: str | None,
) -> sqlite3.Row | None:
    rows = conn.execute(
        """
        SELECT *
        FROM units
        WHERE unit_code = ?
        ORDER BY unit_id
        """,
        (course_code,),
    ).fetchall()
    for row in rows:
        if row["year"] == year and row["semester"] == semester:
            return row
    return rows[0] if rows and year is None and semester is None else None


def _create_or_update_unit(
    conn: sqlite3.Connection,
    unit_info: dict,
    schema: dict,
) -> int:
    course_code = str(unit_info["course_code"]).upper()
    course_title = unit_info.get("course_title") or schema.get("course_title") or course_code
    year = unit_info.get("year")
    semester = unit_info.get("semester")
    row = _find_existing_unit(conn, course_code, year, semester)

    values = {
        "unit_name": course_title,
        "semester": semester,
        "year": year,
        "level": unit_info.get("level") or schema.get("level"),
        "discipline": unit_info.get("discipline") or schema.get("discipline"),
        "credit_points": unit_info.get("credit_points") or schema.get("credit_points"),
        "weeks": unit_info.get("weeks") or schema.get("weeks"),
        "learning_outcomes_json": _json_or_none(
            unit_info.get("learning_outcomes") or schema.get("learning_outcomes")
        ),
    }

    if row is None:
        cur = conn.execute(
            """
            INSERT INTO units
                (unit_code, unit_name, semester, year, level, discipline,
                 credit_points, weeks, learning_outcomes_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                course_code,
                values["unit_name"],
                values["semester"],
                values["year"],
                values["level"],
                values["discipline"],
                values["credit_points"],
                values["weeks"],
                values["learning_outcomes_json"],
            ),
        )
        return int(cur.lastrowid)

    conn.execute(
        """
        UPDATE units
        SET unit_name = COALESCE(?, unit_name),
            semester = COALESCE(?, semester),
            year = COALESCE(?, year),
            level = COALESCE(?, level),
            discipline = COALESCE(?, discipline),
            credit_points = COALESCE(?, credit_points),
            weeks = COALESCE(?, weeks),
            learning_outcomes_json = COALESCE(?, learning_outcomes_json)
        WHERE unit_id = ?
        """,
        (
            values["unit_name"],
            values["semester"],
            values["year"],
            values["level"],
            values["discipline"],
            values["credit_points"],
            values["weeks"],
            values["learning_outcomes_json"],
            row["unit_id"],
        ),
    )
    return int(row["unit_id"])


def _find_existing_assignment(
    conn: sqlite3.Connection,
    unit_id: int,
    assignment_code: str | None,
    assignment_name: str,
) -> sqlite3.Row | None:
    if assignment_code:
        row = conn.execute(
            """
            SELECT *
            FROM assignments
            WHERE unit_id = ? AND assignment_code = ?
            ORDER BY assignment_id
            LIMIT 1
            """,
            (unit_id, assignment_code),
        ).fetchone()
        if row is not None:
            return row

    return conn.execute(
        """
        SELECT *
        FROM assignments
        WHERE unit_id = ? AND assignment_name = ?
        ORDER BY assignment_id
        LIMIT 1
        """,
        (unit_id, assignment_name),
    ).fetchone()


def _create_or_update_assignment(
    conn: sqlite3.Connection,
    unit_id: int,
    assignment_info: dict,
) -> int:
    assignment_code = assignment_info.get("assignment_code") or assignment_info.get("id")
    assignment_name = (
        assignment_info.get("title")
        or assignment_info.get("assignment_name")
        or assignment_code
        or "Assignment"
    )
    row = _find_existing_assignment(conn, unit_id, assignment_code, assignment_name)
    params = {
        "assignment_code": assignment_code,
        "assignment_name": assignment_name,
        "assignment_type": assignment_info.get("type") or assignment_info.get("assignment_type"),
        "description": assignment_info.get("description"),
        "weight": assignment_info.get("weight"),
        "due_week": assignment_info.get("due_week"),
        "word_count_or_equivalent": assignment_info.get("word_count_or_equivalent"),
        "linked_topics_json": _json_or_none(assignment_info.get("linked_topics")),
        "learning_outcomes_assessed_json": _json_or_none(
            assignment_info.get("learning_outcomes_assessed")
        ),
    }

    if row is None:
        cur = conn.execute(
            """
            INSERT INTO assignments
                (unit_id, assignment_code, assignment_name, assignment_type,
                 description, weight, due_week, word_count_or_equivalent,
                 linked_topics_json, learning_outcomes_assessed_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                unit_id,
                params["assignment_code"],
                params["assignment_name"],
                params["assignment_type"],
                params["description"],
                params["weight"],
                params["due_week"],
                params["word_count_or_equivalent"],
                params["linked_topics_json"],
                params["learning_outcomes_assessed_json"],
            ),
        )
        return int(cur.lastrowid)

    conn.execute(
        """
        UPDATE assignments
        SET assignment_code = COALESCE(?, assignment_code),
            assignment_name = COALESCE(?, assignment_name),
            assignment_type = COALESCE(?, assignment_type),
            description = COALESCE(?, description),
            weight = COALESCE(?, weight),
            due_week = COALESCE(?, due_week),
            word_count_or_equivalent = COALESCE(?, word_count_or_equivalent),
            linked_topics_json = COALESCE(?, linked_topics_json),
            learning_outcomes_assessed_json =
                COALESCE(?, learning_outcomes_assessed_json)
        WHERE assignment_id = ?
        """,
        (
            params["assignment_code"],
            params["assignment_name"],
            params["assignment_type"],
            params["description"],
            params["weight"],
            params["due_week"],
            params["word_count_or_equivalent"],
            params["linked_topics_json"],
            params["learning_outcomes_assessed_json"],
            row["assignment_id"],
        ),
    )
    return int(row["assignment_id"])


def _assignment_infos_by_slug(schema: dict, manifest: dict, unit_dir: Path) -> dict[str, dict]:
    assignment_infos: dict[str, dict] = {}
    for assignment in schema.get("assignments") or []:
        if not isinstance(assignment, dict):
            continue
        slug = slugify(f"{assignment.get('id', '')}-{assignment.get('title', '')}")
        assignment_infos[slug] = dict(assignment)
        if assignment.get("id"):
            assignment_infos[slug]["assignment_code"] = assignment.get("id")

    manifest_assignments = manifest.get("assignments") or {}
    if isinstance(manifest_assignments, dict):
        for raw_slug, info in manifest_assignments.items():
            slug = _normalise_assignment_folder(raw_slug)
            assignment_infos.setdefault(slug, {})
            if isinstance(info, dict):
                assignment_infos[slug].update(info)

    assignments_dir = unit_dir / "assignments"
    if assignments_dir.exists():
        for child in assignments_dir.iterdir():
            if child.is_dir():
                slug = _normalise_assignment_folder(child.name)
                assignment_infos.setdefault(
                    slug,
                    {
                        "assignment_code": child.name.split("-")[0].upper(),
                        "title": _title_from_path(child),
                    },
                )

    return assignment_infos


def _record_item(
    conn: sqlite3.Connection,
    ingestion_run_id: int | None,
    item: IngestionItem,
) -> None:
    if ingestion_run_id is None:
        return
    conn.execute(
        """
        INSERT INTO unit_ingestion_items
            (ingestion_run_id, item_type, file_path, source_content_hash,
             action, status, message, assignment_id, spec_id, rubric_id,
             material_id, submission_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ingestion_run_id,
            item.item_type,
            normalise_source_path(item.file_path),
            item.source_content_hash,
            item.action,
            item.status,
            item.message,
            item.assignment_id,
            item.spec_id,
            item.rubric_id,
            item.material_id,
            item.submission_id,
        ),
    )


def _same_hash_exists(
    conn: sqlite3.Connection,
    table_name: str,
    owner_column: str,
    owner_id: int,
    path_column: str,
    source_path: str,
    content_hash: str,
    extra_where: str = "",
    extra_params: tuple = (),
) -> bool:
    query = (
        f'SELECT 1 FROM "{table_name}" '
        f'WHERE "{owner_column}" = ? '
        f'AND "{path_column}" = ? '
        "AND source_content_hash = ? "
        f"{extra_where} "
        "LIMIT 1"
    )
    row = conn.execute(
        query,
        (owner_id, source_path, content_hash, *extra_params),
    ).fetchone()
    return row is not None


def _should_skip_spec(
    conn: sqlite3.Connection,
    assignment_id: int,
    source_path: str,
    content_hash: str,
) -> bool:
    return _same_hash_exists(
        conn,
        "assignment_specs",
        "assignment_id",
        assignment_id,
        "source_file_path",
        source_path,
        content_hash,
    )


def _should_skip_rubric(
    conn: sqlite3.Connection,
    assignment_id: int,
    source_path: str,
    content_hash: str,
) -> bool:
    return _same_hash_exists(
        conn,
        "rubrics",
        "assignment_id",
        assignment_id,
        "source_file_path",
        source_path,
        content_hash,
    )


def _should_skip_material(
    conn: sqlite3.Connection,
    unit_id: int,
    source_path: str,
    content_hash: str,
    assignment_id: int | None,
) -> bool:
    if assignment_id is None:
        extra_where = "AND assignment_id IS NULL"
        extra_params = ()
    else:
        extra_where = "AND assignment_id = ?"
        extra_params = (assignment_id,)
    return _same_hash_exists(
        conn,
        "unit_materials",
        "unit_id",
        unit_id,
        "source_file_path",
        source_path,
        content_hash,
        extra_where,
        extra_params,
    )


def _should_skip_submission(
    conn: sqlite3.Connection,
    assignment_id: int,
    student_identifier: str,
    source_path: str,
    content_hash: str,
) -> bool:
    return _same_hash_exists(
        conn,
        "student_submissions",
        "assignment_id",
        assignment_id,
        "original_file_path",
        source_path,
        content_hash,
        "AND student_identifier = ?",
        (student_identifier,),
    )


def _iter_supported(paths: list[Path]) -> list[Path]:
    return sorted(
        path
        for path in paths
        if path.is_file() and path.suffix.lower() in SUPPORTED_TEXT_DOCUMENTS
    )


def _collect_ingestion_targets(
    unit_dir: Path,
    manifest: dict,
    assignment_ids: dict[str, int],
    assignment_infos: dict[str, dict],
) -> list[dict]:
    targets: list[dict] = []
    assignments_dir = unit_dir / "assignments"
    if assignments_dir.exists():
        for assignment_dir in sorted(path for path in assignments_dir.iterdir() if path.is_dir()):
            slug = _normalise_assignment_folder(assignment_dir.name)
            assignment_id = assignment_ids.get(slug)
            if assignment_id is None:
                continue
            assignment_manifest = assignment_infos.get(slug, {})

            for spec_path in _iter_supported(list(assignment_dir.glob("spec.*"))):
                targets.append(
                    {
                        "item_type": "assignment_spec",
                        "file_path": spec_path,
                        "assignment_id": assignment_id,
                    }
                )

            for rubric_path in sorted(assignment_dir.glob("rubric.pdf")):
                targets.append(
                    {
                        "item_type": "rubric",
                        "file_path": rubric_path,
                        "assignment_id": assignment_id,
                    }
                )

            submissions_dir = assignment_dir / "submissions"
            if submissions_dir.exists():
                for submission_path in _iter_supported(list(submissions_dir.iterdir())):
                    targets.append(
                        {
                            "item_type": "student_submission",
                            "file_path": submission_path,
                            "assignment_id": assignment_id,
                            "student_identifier": _student_identifier_from_file(
                                submission_path,
                                slug,
                                assignment_manifest,
                            ),
                        }
                    )

    lectures_dir = unit_dir / "lectures"
    if lectures_dir.exists():
        for lecture_path in _iter_supported(list(lectures_dir.iterdir())):
            targets.append(
                {
                    "item_type": "unit_material",
                    "file_path": lecture_path,
                    "material_type": "lecture_transcript",
                    "title": _title_from_path(lecture_path),
                    "week_number": _extract_week_number(lecture_path),
                    "assignment_id": None,
                }
            )

    tutorials_dir = unit_dir / "tutorials"
    if tutorials_dir.exists():
        for tutorial_path in _iter_supported(list(tutorials_dir.iterdir())):
            stem = tutorial_path.stem
            if stem.endswith("_worksheet"):
                material_type = "tutorial_sheet"
                slug = _normalise_assignment_folder(stem[: -len("_worksheet")])
            elif stem.endswith("_sample_answers"):
                material_type = "sample_solution"
                slug = _normalise_assignment_folder(stem[: -len("_sample_answers")])
            else:
                material_type = "tutorial_material"
                slug = ""
            targets.append(
                {
                    "item_type": "unit_material",
                    "file_path": tutorial_path,
                    "material_type": material_type,
                    "title": _title_from_path(tutorial_path),
                    "week_number": None,
                    "assignment_id": assignment_ids.get(slug),
                }
            )

    resources_dir = unit_dir / "resources"
    material_manifest = manifest.get("materials") or {}
    if resources_dir.exists():
        for resource_path in _iter_supported(list(resources_dir.rglob("*"))):
            relative = _manifest_path(unit_dir, resource_path.relative_to(unit_dir))
            override = material_manifest.get(relative, {}) if isinstance(material_manifest, dict) else {}
            if not isinstance(override, dict):
                override = {}
            targets.append(
                {
                    "item_type": "unit_material",
                    "file_path": resource_path,
                    "material_type": override.get("type") or "resource",
                    "title": override.get("title") or _title_from_path(resource_path),
                    "week_number": override.get("week_number"),
                    "assignment_id": None,
                }
            )

    return targets


def _unit_info_from_sources(unit_dir: Path, schema: dict, manifest: dict) -> dict:
    manifest_unit = manifest.get("unit") if isinstance(manifest.get("unit"), dict) else {}
    course_code = (
        manifest_unit.get("course_code")
        or schema.get("course_code")
        or unit_dir.name
    )
    course_title = (
        manifest_unit.get("course_title")
        or manifest_unit.get("unit_name")
        or schema.get("course_title")
        or course_code
    )
    unit_info = dict(schema)
    unit_info.update(manifest_unit)
    unit_info["course_code"] = str(course_code).upper()
    unit_info["course_title"] = course_title
    return unit_info


def ingest_unit_directory(
    conn: sqlite3.Connection,
    unit_directory: str | Path,
    dry_run: bool = False,
    force: bool = False,
) -> UnitIngestionResult:
    unit_dir = Path(unit_directory)
    if not unit_dir.exists() or not unit_dir.is_dir():
        raise ValueError(f"Unit directory does not exist: {unit_dir}")

    schema = _load_json(unit_dir / "schema.json")
    manifest = _load_json(unit_dir / "unit_manifest.json")
    unit_info = _unit_info_from_sources(unit_dir, schema, manifest)
    course_code = unit_info["course_code"]

    unit_id: int | None = None
    ingestion_run_id: int | None = None
    assignment_ids: dict[str, int] = {}
    assignment_infos = _assignment_infos_by_slug(schema, manifest, unit_dir)

    if not dry_run:
        unit_id = _create_or_update_unit(conn, unit_info, schema)
        for slug, assignment_info in assignment_infos.items():
            assignment_ids[slug] = _create_or_update_assignment(
                conn,
                unit_id,
                assignment_info,
            )
        cur = conn.execute(
            """
            INSERT INTO unit_ingestion_runs
                (unit_id, unit_directory, dry_run, force, status)
            VALUES (?, ?, ?, ?, 'running')
            """,
            (unit_id, normalise_source_path(unit_dir), 0, 1 if force else 0),
        )
        ingestion_run_id = int(cur.lastrowid)
        conn.commit()
    else:
        assignment_ids = {slug: index + 1 for index, slug in enumerate(assignment_infos)}

    result = UnitIngestionResult(
        unit_id=unit_id,
        course_code=course_code,
        unit_directory=unit_dir,
        dry_run=dry_run,
        force=force,
        ingestion_run_id=ingestion_run_id,
    )

    targets = _collect_ingestion_targets(
        unit_dir,
        manifest,
        assignment_ids,
        assignment_infos,
    )

    try:
        for target in targets:
            file_path = target["file_path"]
            source_path = normalise_source_path(file_path)
            content_hash = hash_file(file_path)
            item = IngestionItem(
                item_type=target["item_type"],
                file_path=file_path,
                action="planned" if dry_run else "imported",
                status="planned" if dry_run else "completed",
                assignment_id=target.get("assignment_id"),
                source_content_hash=content_hash,
            )

            if dry_run:
                result.items.append(item)
                continue

            try:
                if target["item_type"] == "assignment_spec":
                    from feedback_lens.file_management.importers import (
                        import_assignment_spec,
                    )

                    if not force and _should_skip_spec(
                        conn,
                        target["assignment_id"],
                        source_path,
                        content_hash,
                    ):
                        item.action = "skipped"
                        item.message = "unchanged"
                    else:
                        imported = import_assignment_spec(
                            conn,
                            target["assignment_id"],
                            file_path,
                        )
                        item.spec_id = imported["spec_id"]
                elif target["item_type"] == "rubric":
                    from feedback_lens.file_management.importers import import_rubric

                    if not force and _should_skip_rubric(
                        conn,
                        target["assignment_id"],
                        source_path,
                        content_hash,
                    ):
                        item.action = "skipped"
                        item.message = "unchanged"
                    else:
                        imported = import_rubric(conn, target["assignment_id"], file_path)
                        item.rubric_id = imported["rubric_id"]
                elif target["item_type"] == "student_submission":
                    from feedback_lens.file_management.importers import (
                        import_student_submission,
                    )

                    student_identifier = target["student_identifier"]
                    if not force and _should_skip_submission(
                        conn,
                        target["assignment_id"],
                        student_identifier,
                        source_path,
                        content_hash,
                    ):
                        item.action = "skipped"
                        item.message = "unchanged"
                    else:
                        imported = import_student_submission(
                            conn,
                            target["assignment_id"],
                            student_identifier,
                            file_path,
                        )
                        item.submission_id = imported["submission_id"]
                elif target["item_type"] == "unit_material":
                    from feedback_lens.file_management.ingestion import ingest_material

                    if unit_id is None:
                        raise ValueError("Cannot ingest material without a unit_id")
                    assignment_id = target.get("assignment_id")
                    if not force and _should_skip_material(
                        conn,
                        unit_id,
                        source_path,
                        content_hash,
                        assignment_id,
                    ):
                        item.action = "skipped"
                        item.message = "unchanged"
                    else:
                        item.material_id = ingest_material(
                            conn,
                            file_path,
                            unit_id,
                            target["material_type"],
                            target["title"],
                            week_number=target.get("week_number"),
                            assignment_id=assignment_id,
                        )
                else:
                    item.action = "skipped"
                    item.message = "unknown item type"
            except Exception as err:
                item.action = "failed"
                item.status = "failed"
                item.message = str(err)

            result.items.append(item)
            _record_item(conn, ingestion_run_id, item)
            conn.commit()

        if ingestion_run_id is not None:
            status = "completed" if result.failed_count == 0 else "failed"
            conn.execute(
                """
                UPDATE unit_ingestion_runs
                SET unit_id = ?,
                    status = ?,
                    summary_json = ?,
                    completed_at = CURRENT_TIMESTAMP
                WHERE ingestion_run_id = ?
                """,
                (
                    result.unit_id,
                    status,
                    json.dumps(result.summary(), ensure_ascii=False),
                    ingestion_run_id,
                ),
            )
            conn.commit()
        return result
    except Exception as err:
        if ingestion_run_id is not None:
            conn.execute(
                """
                UPDATE unit_ingestion_runs
                SET status = 'failed',
                    error_message = ?,
                    completed_at = CURRENT_TIMESTAMP
                WHERE ingestion_run_id = ?
                """,
                (str(err), ingestion_run_id),
            )
            conn.commit()
        raise
