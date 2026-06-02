import json
import sqlite3
from html import escape
from typing import Any

DETAIL_RESULT_ONLY = "result_only"
DETAIL_FULL = "full"
DETAIL_MODES = {DETAIL_RESULT_ONLY, DETAIL_FULL}


def list_generation_runs(
    conn: sqlite3.Connection,
    limit: int = 10,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            gr.generation_id,
            gr.status,
            gr.started_at,
            gr.completed_at,
            gr.llm_provider,
            gr.llm_model,
            gr.top_k,
            gr.temperature,
            ss.student_identifier,
            a.assignment_name,
            u.unit_code,
            of.overall_grade_band
        FROM generation_runs AS gr
        JOIN student_submissions AS ss ON ss.submission_id = gr.submission_id
        JOIN assignments AS a ON a.assignment_id = gr.assignment_id
        JOIN units AS u ON u.unit_id = a.unit_id
        LEFT JOIN overall_feedback AS of ON of.generation_id = gr.generation_id
        ORDER BY gr.generation_id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def list_generation_run_ids(
    conn: sqlite3.Connection,
    limit: int | None = None,
) -> list[int]:
    query = """
        SELECT generation_id
        FROM generation_runs
        ORDER BY generation_id DESC
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (limit,)

    return [row["generation_id"] for row in conn.execute(query, params).fetchall()]


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return (
        conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (table_name,),
        ).fetchone()
        is not None
    )


def fetch_generation_review(
    conn: sqlite3.Connection,
    generation_id: int,
) -> dict:
    run = conn.execute(
        """
        SELECT
            gr.*,
            ss.student_identifier,
            ss.original_file_path,
            a.assignment_name,
            a.assignment_type,
            a.description AS assignment_description,
            a.due_date,
            u.unit_code,
            u.unit_name,
            u.semester,
            u.year,
            r.version AS rubric_version,
            s.version AS spec_version
        FROM generation_runs AS gr
        JOIN student_submissions AS ss ON ss.submission_id = gr.submission_id
        JOIN assignments AS a ON a.assignment_id = gr.assignment_id
        JOIN units AS u ON u.unit_id = a.unit_id
        LEFT JOIN rubrics AS r ON r.rubric_id = gr.rubric_id
        LEFT JOIN assignment_specs AS s
            ON s.assignment_id = gr.assignment_id
           AND s.version = (
                SELECT MAX(version)
                FROM assignment_specs
                WHERE assignment_id = gr.assignment_id
           )
        WHERE gr.generation_id = ?
        """,
        (generation_id,),
    ).fetchone()
    if run is None:
        raise ValueError(f"No generation run found with generation_id={generation_id}")

    overall_feedback = conn.execute(
        """
        SELECT *
        FROM overall_feedback
        WHERE generation_id = ?
        """,
        (generation_id,),
    ).fetchone()

    criterion_feedback = conn.execute(
        """
        SELECT
            rc.criterion_id,
            rc.criterion_name,
            rc.criterion_description,
            rc.criterion_order,
            cf.strengths,
            cf.areas_for_improvement,
            cf.improvement_suggestion,
            cf.suggested_level,
            cf.evidence_summary,
            cf.mark
        FROM criterion_feedback AS cf
        JOIN rubric_criteria AS rc ON rc.criterion_id = cf.criterion_id
        WHERE cf.generation_id = ?
        ORDER BY rc.criterion_order, rc.criterion_id
        """,
        (generation_id,),
    ).fetchall()

    retrieval_records = conn.execute(
        """
        SELECT
            rr.retrieval_record_id,
            rr.query_text,
            rr.chunk_id,
            rr.rank_position,
            rr.similarity_score,
            rr.used_in_prompt,
            mc.chunk_index,
            mc.chunk_text,
            mc.page_number_start,
            mc.page_number_end,
            um.material_id,
            um.title AS material_title,
            um.material_type,
            um.week_number,
            um.source_file_path
        FROM retrieval_records AS rr
        JOIN material_chunks AS mc ON mc.chunk_id = rr.chunk_id
        JOIN unit_materials AS um ON um.material_id = mc.material_id
        WHERE rr.generation_id = ?
        ORDER BY rr.rank_position, rr.retrieval_record_id
        """,
        (generation_id,),
    ).fetchall()

    retrieval_planning_records = []
    if _table_exists(conn, "retrieval_planning_records"):
        retrieval_planning_records = conn.execute(
            """
            SELECT *
            FROM retrieval_planning_records
            WHERE generation_id = ?
            ORDER BY planning_record_id
            """,
            (generation_id,),
        ).fetchall()

    return {
        "run": run,
        "overall_feedback": overall_feedback,
        "criterion_feedback": criterion_feedback,
        "retrieval_records": retrieval_records,
        "retrieval_planning_records": retrieval_planning_records,
    }


def parse_json_value(raw_value: str | None) -> Any:
    if not raw_value:
        return None

    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        return raw_value


def parse_json_text_list(raw_value: str | None) -> list[str]:
    if not raw_value:
        return []

    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return [raw_value]

    if not isinstance(parsed, list):
        return [str(parsed)]

    return [str(item) for item in parsed]


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def generation_review_to_export_dict(
    review: dict,
    detail_mode: str = DETAIL_RESULT_ONLY,
) -> dict[str, Any]:
    if detail_mode not in DETAIL_MODES:
        raise ValueError(f"detail_mode must be one of: {', '.join(sorted(DETAIL_MODES))}")

    run = row_to_dict(review["run"]) or {}
    prompt_text = run.pop("prompt_text", None)
    raw_response_text = run.pop("raw_response_text", None)

    include_details = detail_mode == DETAIL_FULL
    if include_details:
        run["prompt_text"] = prompt_text
        run["raw_response_text"] = raw_response_text

    overall = row_to_dict(review["overall_feedback"])
    if overall is not None:
        overall["key_strengths"] = parse_json_text_list(overall.get("key_strengths"))
        overall["priority_improvements"] = parse_json_text_list(
            overall.get("priority_improvements")
        )

    criteria = [row_to_dict(row) for row in review["criterion_feedback"]]

    payload: dict[str, Any] = {
        "export_version": 2,
        "export_mode": detail_mode,
        "generation_run": run,
        "overall_feedback": overall,
        "criterion_feedback": criteria,
    }

    if include_details:
        retrievals = []
        for row in review["retrieval_records"]:
            retrievals.append(row_to_dict(row) or {})

        planning_records = []
        for row in review.get("retrieval_planning_records", []):
            item = row_to_dict(row) or {}
            item["planned_cues"] = parse_json_value(item.get("planned_cues_json")) or []
            planning_records.append(item)

        payload["retrieval_planning_records"] = planning_records
        payload["retrieval_records"] = retrievals

    return payload


def _markdown_list(items: list[str]) -> str:
    if not items:
        return "(none)"
    return "\n".join(f"- {item}" for item in items)


def _markdown_value(value: Any) -> str:
    if value is None or value == "":
        return "None"
    return str(value)


def _markdown_json(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def _run_retrieval_limit(run: dict[str, Any], key: str) -> Any:
    value = run.get(key)
    return run.get("top_k") if value is None else value


def _html_value(value: Any) -> str:
    if value is None or value == "":
        return "None"
    return escape(str(value))


def _html_text_block(value: Any) -> str:
    return escape("" if value is None else str(value))


def _grade_class(value: Any) -> str:
    grade = str(value or "none").strip().lower()
    if grade in {"hd", "d", "c", "p", "n"}:
        return f"grade-{grade}"
    return "grade-none"


def _html_list(items: list[str]) -> str:
    if not items:
        return '<p class="muted">None</p>'
    return "<ul>" + "".join(f"<li>{_html_value(item)}</li>" for item in items) + "</ul>"


def _html_meta_item(label: str, value: Any) -> str:
    return (
        '<div class="meta-item">'
        f'<span class="meta-label">{escape(label)}</span>'
        f'<span class="meta-value">{_html_value(value)}</span>'
        "</div>"
    )


def _foldable_html_section(title: str, body: str) -> str:
    return f"""
      <details class="section-details">
        <summary>{escape(title)}</summary>
        <div class="folded-content">
          {body}
        </div>
      </details>
    """


def _format_generation_review_html_section(export_payload: dict[str, Any]) -> str:
    run = export_payload["generation_run"]
    overall = export_payload["overall_feedback"]
    criteria = export_payload["criterion_feedback"]
    retrievals = export_payload.get("retrieval_records")
    planning_records = export_payload.get("retrieval_planning_records")
    grade = (overall or {}).get("overall_grade_band")

    metadata = [
        ("Unit", f"{_markdown_value(run.get('unit_code'))} - {_markdown_value(run.get('unit_name'))}"),
        ("Assignment", run.get("assignment_name")),
        ("Student", run.get("student_identifier")),
        ("Status", run.get("status")),
        ("Provider", f"{_markdown_value(run.get('llm_provider'))}:{_markdown_value(run.get('llm_model'))}"),
        ("Pipeline", run.get("pipeline_version")),
        ("Prompt Template", run.get("prompt_template_version")),
        ("Retrieval Strategy", run.get("retrieval_strategy")),
        ("Per Cue Top K", _run_retrieval_limit(run, "per_cue_top_k")),
        ("Max Final Chunks", _run_retrieval_limit(run, "max_final_chunks")),
        ("Temperature", run.get("temperature")),
        ("Started", run.get("started_at")),
        ("Completed", run.get("completed_at")),
        ("Submission File", run.get("original_file_path")),
    ]

    metadata_html = "".join(_html_meta_item(label, value) for label, value in metadata)
    if run.get("error_message"):
        metadata_html += _html_meta_item("Error", run.get("error_message"))

    if overall is None:
        overall_html = '<p class="muted">None</p>'
    else:
        overall_html = f"""
        <div class="overall-grid">
          <section class="panel">
            <h3>Overall Comment</h3>
            <p>{_html_value(overall.get("overall_comment"))}</p>
          </section>
          <section class="panel">
            <h3>Key Strengths</h3>
            {_html_list(overall.get("key_strengths") or [])}
          </section>
          <section class="panel">
            <h3>Priority Improvements</h3>
            {_html_list(overall.get("priority_improvements") or [])}
          </section>
        </div>
        """

    if not criteria:
        criteria_html = '<p class="muted">None</p>'
    else:
        criteria_html = "".join(
            f"""
            <article class="criterion-card">
              <div class="criterion-head">
                <h3>{_html_value(item.get("criterion_order"))}. {_html_value(item.get("criterion_name"))}</h3>
                <span class="badge {_grade_class(item.get("suggested_level"))}">{_html_value(item.get("suggested_level"))}</span>
              </div>
              <dl class="feedback-pairs">
                <dt>Strengths</dt>
                <dd>{_html_value(item.get("strengths"))}</dd>
                <dt>Areas For Improvement</dt>
                <dd>{_html_value(item.get("areas_for_improvement"))}</dd>
                <dt>Improvement Suggestion</dt>
                <dd>{_html_value(item.get("improvement_suggestion"))}</dd>
                <dt>Evidence Summary</dt>
                <dd>{_html_value(item.get("evidence_summary"))}</dd>
              </dl>
            </article>
            """
            for item in criteria
        )

    planner_section = ""
    if planning_records is not None:
        if not planning_records:
            planner_html = '<p class="muted">None for this run.</p>'
        else:
            planner_html = "".join(
                _format_planning_html(item)
                for item in planning_records
            )
        planner_section = _foldable_html_section(
            "Retrieval Planner",
            f"""
        <div class="planning-list">
          {planner_html}
        </div>
            """,
        )

    retrieval_section = ""
    if retrievals is not None:
        if not retrievals:
            retrieval_html = '<p class="muted">None</p>'
        else:
            retrieval_html = "".join(
                _format_retrieval_html(item)
                for item in retrievals
            )
        retrieval_section = _foldable_html_section(
            "Retrieved Chunks",
            f"""
        <div class="retrieval-list">
          {retrieval_html}
        </div>
            """,
        )

    raw_llm_items = []
    if "prompt_text" in run:
        raw_llm_items.append(
            f"""
        <details class="text-details">
          <summary>Feedback Generation Prompt</summary>
          <pre>{_html_text_block(run.get("prompt_text"))}</pre>
        </details>
            """
        )

    if "raw_response_text" in run:
        raw_llm_items.append(
            f"""
        <details class="text-details">
          <summary>Feedback Generation Raw Response</summary>
          <pre>{_html_text_block(run.get("raw_response_text"))}</pre>
        </details>
            """
        )

    raw_llm_section = ""
    if raw_llm_items:
        raw_llm_section = _foldable_html_section(
            "Raw LLM Details",
            "".join(raw_llm_items),
        )

    return f"""
    <section class="run-section" id="generation-run-{_html_value(run.get("generation_id"))}">
      <header class="run-hero">
        <div>
          <p class="eyebrow">Feedback Generation Run</p>
          <h2>Run {_html_value(run.get("generation_id"))}</h2>
          <p class="subtitle">{_html_value(run.get("unit_code"))} | {_html_value(run.get("assignment_name"))} | {_html_value(run.get("student_identifier"))}</p>
        </div>
        <span class="badge grade-badge {_grade_class(grade)}">{_html_value(grade)}</span>
      </header>

      <section class="meta-grid" aria-label="Run metadata">
        {metadata_html}
      </section>

      <section class="section-block">
        <h2>Overall Feedback</h2>
        {overall_html}
      </section>

      <section class="section-block">
        <h2>Criterion Feedback</h2>
        <div class="criteria-grid">
          {criteria_html}
        </div>
      </section>

      {planner_section}
      {retrieval_section}
      {raw_llm_section}
    </section>
    """


def _format_planning_html(item: dict[str, Any]) -> str:
    cues = item.get("planned_cues")
    if not isinstance(cues, list):
        cues = []

    cues_html = (
        "".join(_format_planned_cue_html(cue) for cue in cues)
        if cues
        else '<p class="muted">No normalized cues were recorded.</p>'
    )
    error_html = ""
    if item.get("error_message"):
        error_html = _html_meta_item("Error", item.get("error_message"))

    return f"""
    <article class="planning-card">
      <div class="retrieval-head">
        <h3>Planner Record {_html_value(item.get("planning_record_id"))}</h3>
        <span class="used-pill">Status: {_html_value(item.get("status"))}</span>
      </div>
      <section class="meta-grid compact" aria-label="Retrieval planner metadata">
        {_html_meta_item("Strategy", item.get("strategy"))}
        {_html_meta_item("Provider", f"{_markdown_value(item.get('provider'))}:{_markdown_value(item.get('model'))}")}
        {_html_meta_item("Prompt Template", item.get("prompt_template_version"))}
        {_html_meta_item("Started", item.get("started_at"))}
        {_html_meta_item("Completed", item.get("completed_at"))}
        {error_html}
      </section>
      <h4>Planned Cues</h4>
      <div class="cue-list">
        {cues_html}
      </div>
      <details class="text-details nested">
        <summary>Retrieval Planner Prompt</summary>
        <pre>{_html_text_block(item.get("prompt_text"))}</pre>
      </details>
      <details class="text-details nested">
        <summary>Retrieval Planner Raw Response</summary>
        <pre>{_html_text_block(item.get("raw_response_text"))}</pre>
      </details>
      <details class="text-details nested">
        <summary>Normalized Planned Cues JSON</summary>
        <pre>{_html_text_block(item.get("planned_cues_json"))}</pre>
      </details>
    </article>
    """


def _format_planned_cue_html(cue: Any) -> str:
    if not isinstance(cue, dict):
        return f"""
        <article class="cue-card">
          <p>{_html_value(cue)}</p>
        </article>
        """

    criterion_ids = cue.get("rubric_criterion_ids")
    if isinstance(criterion_ids, list):
        criterion_ids_value = ", ".join(str(item) for item in criterion_ids)
    else:
        criterion_ids_value = criterion_ids

    return f"""
    <article class="cue-card">
      <h5>{_html_value(cue.get("order"))}. {_html_value(cue.get("label"))}</h5>
      <p class="query-label">Cue Text</p>
      <p class="query-text">{_html_value(cue.get("text"))}</p>
      <p class="query-label">Rationale</p>
      <p>{_html_value(cue.get("rationale"))}</p>
      <p class="muted">Rubric criteria: {_html_value(criterion_ids_value)}</p>
    </article>
    """


def _format_retrieval_html(item: dict[str, Any]) -> str:
    source_bits = [
        item.get("material_title"),
        item.get("material_type"),
    ]
    if item.get("week_number") is not None:
        source_bits.append(f"week {item['week_number']}")
    source = " | ".join(str(bit) for bit in source_bits if bit)
    pages = (
        f"{_markdown_value(item.get('page_number_start'))}-{_markdown_value(item.get('page_number_end'))}"
    )
    chunk_text = ""
    if "chunk_text" in item:
        chunk_text = f"""
        <details class="chunk-text">
          <summary>Chunk Text</summary>
          <pre>{_html_text_block(item.get("chunk_text"))}</pre>
        </details>
        """

    return f"""
    <article class="retrieval-card">
      <div class="retrieval-head">
        <h3>Rank {_html_value(item.get("rank_position"))} | Chunk {_html_value(item.get("chunk_id"))}</h3>
        <span class="used-pill">Used: {_html_value(item.get("used_in_prompt"))}</span>
      </div>
      <div class="retrieval-meta">
        <span>Score: {_html_value(item.get("similarity_score"))}</span>
        <span>Source: {_html_value(source)}</span>
        <span>Pages: {_html_value(pages)}</span>
      </div>
      <p class="query-label">Query</p>
      <p class="query-text">{_html_value(item.get("query_text"))}</p>
      {chunk_text}
    </article>
    """


def format_generation_reviews_html(export_payloads: list[dict[str, Any]]) -> str:
    title = (
        f"Feedback Generation Run {export_payloads[0]['generation_run'].get('generation_id')}"
        if len(export_payloads) == 1
        else f"Feedback Generation Runs ({len(export_payloads)})"
    )
    nav_items = "".join(
        f'<a href="#generation-run-{_html_value(payload["generation_run"].get("generation_id"))}">Run {_html_value(payload["generation_run"].get("generation_id"))}</a>'
        for payload in export_payloads
    )
    sections = "\n".join(
        _format_generation_review_html_section(payload)
        for payload in export_payloads
    )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_html_value(title)}</title>
  <style>
    :root {{
      color-scheme: light dark;
      --bg: #f6f7f9;
      --surface: #ffffff;
      --surface-2: #eef2f6;
      --text: #16202a;
      --muted: #5d6b78;
      --border: #d8dee6;
      --accent: #2563eb;
      --accent-2: #0f766e;
      --shadow: 0 10px 30px rgba(22, 32, 42, 0.08);
      --code-bg: #101827;
      --code-text: #e6edf6;
    }}
    @media (prefers-color-scheme: dark) {{
      :root:not([data-theme="light"]) {{
        --bg: #0f141a;
        --surface: #171e26;
        --surface-2: #202a34;
        --text: #eef3f8;
        --muted: #a7b2bf;
        --border: #2f3b48;
        --accent: #7aa7ff;
        --accent-2: #5bd3c7;
        --shadow: 0 12px 32px rgba(0, 0, 0, 0.28);
        --code-bg: #080c12;
        --code-text: #eef3f8;
      }}
    }}
    :root[data-theme="dark"] {{
      --bg: #0f141a;
      --surface: #171e26;
      --surface-2: #202a34;
      --text: #eef3f8;
      --muted: #a7b2bf;
      --border: #2f3b48;
      --accent: #7aa7ff;
      --accent-2: #5bd3c7;
      --shadow: 0 12px 32px rgba(0, 0, 0, 0.28);
      --code-bg: #080c12;
      --code-text: #eef3f8;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.55;
    }}
    a {{ color: var(--accent); }}
    .page-shell {{ max-width: 1180px; margin: 0 auto; padding: 28px; }}
    .topbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 24px;
    }}
    .topbar h1 {{ margin: 0; font-size: clamp(1.7rem, 3vw, 2.5rem); letter-spacing: 0; }}
    .theme-controls {{
      display: inline-flex;
      gap: 4px;
      padding: 4px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: var(--shadow);
    }}
    .theme-controls button {{
      border: 0;
      border-radius: 6px;
      padding: 8px 12px;
      background: transparent;
      color: var(--muted);
      cursor: pointer;
      font: inherit;
    }}
    .theme-controls button[aria-pressed="true"] {{
      background: var(--accent);
      color: #fff;
    }}
    .run-nav {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 22px;
    }}
    .run-nav a {{
      display: inline-flex;
      padding: 7px 10px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      text-decoration: none;
      font-size: 0.92rem;
    }}
    .run-section {{
      margin-bottom: 34px;
      padding-bottom: 30px;
      border-bottom: 1px solid var(--border);
    }}
    .run-hero {{
      display: flex;
      justify-content: space-between;
      align-items: start;
      gap: 18px;
      padding: 24px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: var(--shadow);
    }}
    .eyebrow {{ margin: 0 0 4px; color: var(--accent-2); font-weight: 700; text-transform: uppercase; font-size: 0.78rem; }}
    .run-hero h2 {{ margin: 0; font-size: 1.65rem; }}
    .subtitle {{ margin: 6px 0 0; color: var(--muted); }}
    .badge {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 42px;
      border-radius: 999px;
      padding: 4px 10px;
      font-weight: 700;
      border: 1px solid var(--border);
      background: var(--surface-2);
    }}
    .grade-badge {{ min-width: 58px; min-height: 58px; font-size: 1.1rem; }}
    .grade-hd {{ background: #fee2e2; color: #991b1b; border-color: #fecaca; }}
    .grade-d {{ background: #ffedd5; color: #9a3412; border-color: #fed7aa; }}
    .grade-c {{ background: #dcfce7; color: #166534; border-color: #bbf7d0; }}
    .grade-p {{ background: #dbeafe; color: #1e40af; border-color: #bfdbfe; }}
    .grade-n, .grade-none {{ background: var(--surface-2); color: var(--muted); }}
    .meta-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 10px;
      margin: 18px 0;
    }}
    .meta-item {{
      padding: 12px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
    }}
    .meta-label {{ display: block; color: var(--muted); font-size: 0.78rem; text-transform: uppercase; }}
    .meta-value {{ display: block; margin-top: 3px; overflow-wrap: anywhere; }}
    .section-block {{ margin-top: 24px; }}
    .section-block h2 {{ margin: 0 0 12px; font-size: 1.3rem; }}
    .section-details {{
      margin-top: 24px;
      border-top: 1px solid var(--border);
      border-bottom: 1px solid var(--border);
      padding: 0;
    }}
    .section-details > summary {{
      display: flex;
      align-items: center;
      min-height: 54px;
      color: var(--text);
      font-size: 1.3rem;
      font-weight: 700;
      list-style-position: inside;
    }}
    .section-details > .folded-content {{
      padding: 4px 0 18px;
    }}
    .overall-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1.4fr) repeat(2, minmax(220px, 1fr));
      gap: 14px;
    }}
    .panel, .criterion-card, .retrieval-card, .planning-card, .cue-card, .text-details {{
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: var(--shadow);
    }}
    .panel {{ padding: 18px; }}
    .panel h3, .criterion-card h3, .retrieval-card h3, .planning-card h3 {{ margin: 0 0 10px; font-size: 1rem; }}
    .planning-card h4 {{ margin: 18px 0 10px; font-size: 1rem; }}
    .cue-card h5 {{ margin: 0 0 8px; font-size: 0.95rem; }}
    .muted {{ color: var(--muted); }}
    .criteria-grid {{ display: grid; gap: 14px; }}
    .criterion-card {{ padding: 18px; }}
    .criterion-head, .retrieval-head {{
      display: flex;
      justify-content: space-between;
      align-items: start;
      gap: 12px;
      margin-bottom: 12px;
    }}
    .feedback-pairs {{
      display: grid;
      grid-template-columns: minmax(150px, 220px) minmax(0, 1fr);
      gap: 10px 16px;
      margin: 0;
    }}
    .feedback-pairs dt {{ color: var(--muted); font-weight: 700; }}
    .feedback-pairs dd {{ margin: 0; }}
    .retrieval-list, .planning-list, .cue-list {{ display: grid; gap: 12px; }}
    .retrieval-card, .planning-card, .cue-card {{ padding: 16px; }}
    .meta-grid.compact {{ margin: 8px 0 0; }}
    .retrieval-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px 16px;
      color: var(--muted);
      font-size: 0.92rem;
    }}
    .used-pill {{
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 3px 8px;
      color: var(--muted);
      white-space: nowrap;
    }}
    .query-label {{ margin: 14px 0 3px; color: var(--muted); font-weight: 700; }}
    .query-text {{ margin: 0; overflow-wrap: anywhere; }}
    details {{ margin-top: 14px; }}
    summary {{ cursor: pointer; font-weight: 700; color: var(--accent); }}
    .text-details {{ padding: 16px; margin-top: 18px; }}
    .text-details.nested {{ box-shadow: none; }}
    pre {{
      margin: 12px 0 0;
      padding: 14px;
      overflow: auto;
      border-radius: 8px;
      background: var(--code-bg);
      color: var(--code-text);
      white-space: pre-wrap;
      word-break: break-word;
      font: 0.9rem ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }}
    @media (max-width: 860px) {{
      .page-shell {{ padding: 18px; }}
      .topbar, .run-hero, .criterion-head, .retrieval-head {{ flex-direction: column; }}
      .overall-grid {{ grid-template-columns: 1fr; }}
      .feedback-pairs {{ grid-template-columns: 1fr; }}
      .grade-badge {{ min-width: 48px; min-height: 48px; }}
    }}
  </style>
</head>
<body>
  <main class="page-shell">
    <header class="topbar">
      <h1>{_html_value(title)}</h1>
      <div class="theme-controls" role="group" aria-label="Theme">
        <button type="button" data-theme-choice="system" aria-pressed="true">System</button>
        <button type="button" data-theme-choice="light" aria-pressed="false">Light</button>
        <button type="button" data-theme-choice="dark" aria-pressed="false">Dark</button>
      </div>
    </header>
    <nav class="run-nav" aria-label="Generation runs">
      {nav_items}
    </nav>
    {sections}
  </main>
  <script>
    const root = document.documentElement;
    const buttons = Array.from(document.querySelectorAll("[data-theme-choice]"));
    const storageKey = "feedbackLensTheme";
    function applyTheme(theme) {{
      if (theme === "system") {{
        root.removeAttribute("data-theme");
      }} else {{
        root.setAttribute("data-theme", theme);
      }}
      buttons.forEach((button) => {{
        button.setAttribute("aria-pressed", String(button.dataset.themeChoice === theme));
      }});
      localStorage.setItem(storageKey, theme);
    }}
    buttons.forEach((button) => {{
      button.addEventListener("click", () => applyTheme(button.dataset.themeChoice));
    }});
    applyTheme(localStorage.getItem(storageKey) || "system");
  </script>
</body>
</html>
"""


def format_generation_review_markdown(export_payload: dict[str, Any]) -> str:
    run = export_payload["generation_run"]
    overall = export_payload["overall_feedback"]
    criteria = export_payload["criterion_feedback"]
    planning_records = export_payload.get("retrieval_planning_records")
    retrievals = export_payload.get("retrieval_records")

    lines = [
        f"# Feedback Generation Run {run['generation_id']}",
        "",
        "## Metadata",
        "",
        f"- Unit: {_markdown_value(run.get('unit_code'))} - {_markdown_value(run.get('unit_name'))}",
        f"- Assignment: {_markdown_value(run.get('assignment_name'))}",
        f"- Student: {_markdown_value(run.get('student_identifier'))}",
        f"- Status: {_markdown_value(run.get('status'))}",
        f"- Overall grade band: {_markdown_value((overall or {}).get('overall_grade_band'))}",
        f"- Provider: {_markdown_value(run.get('llm_provider'))}:{_markdown_value(run.get('llm_model'))}",
        f"- Pipeline: {_markdown_value(run.get('pipeline_version'))}",
        f"- Prompt template: {_markdown_value(run.get('prompt_template_version'))}",
        f"- Retrieval strategy: {_markdown_value(run.get('retrieval_strategy'))}",
        f"- Per cue top K: {_markdown_value(_run_retrieval_limit(run, 'per_cue_top_k'))}",
        f"- Max final chunks: {_markdown_value(_run_retrieval_limit(run, 'max_final_chunks'))}",
        f"- Temperature: {_markdown_value(run.get('temperature'))}",
        f"- Started: {_markdown_value(run.get('started_at'))}",
        f"- Completed: {_markdown_value(run.get('completed_at'))}",
        f"- Submission file: {_markdown_value(run.get('original_file_path'))}",
    ]

    if run.get("error_message"):
        lines.append(f"- Error: {run['error_message']}")

    lines.extend(["", "## Overall Feedback", ""])
    if overall is None:
        lines.append("(none)")
    else:
        lines.extend(
            [
                f"Grade band: {_markdown_value(overall.get('overall_grade_band'))}",
                "",
                "### Overall Comment",
                "",
                _markdown_value(overall.get("overall_comment")),
                "",
                "### Key Strengths",
                "",
                _markdown_list(overall.get("key_strengths") or []),
                "",
                "### Priority Improvements",
                "",
                _markdown_list(overall.get("priority_improvements") or []),
            ]
        )

    lines.extend(["", "## Criterion Feedback", ""])
    if not criteria:
        lines.append("(none)")
    else:
        for item in criteria:
            lines.extend(
                [
                    f"### {item.get('criterion_order')}. {item.get('criterion_name')}",
                    "",
                    f"Suggested level: {_markdown_value(item.get('suggested_level'))}",
                    "",
                    f"Strengths: {_markdown_value(item.get('strengths'))}",
                    "",
                    f"Areas for improvement: {_markdown_value(item.get('areas_for_improvement'))}",
                    "",
                    f"Improvement suggestion: {_markdown_value(item.get('improvement_suggestion'))}",
                    "",
                    f"Evidence summary: {_markdown_value(item.get('evidence_summary'))}",
                    "",
                ]
            )

    if planning_records is not None:
        lines.extend(["## Retrieval Planner", ""])
        if not planning_records:
            lines.append("None for this run.")
            lines.append("")
        else:
            for item in planning_records:
                lines.extend(
                    [
                        f"### Planner Record {item.get('planning_record_id')}",
                        "",
                        f"- Strategy: {_markdown_value(item.get('strategy'))}",
                        f"- Provider: {_markdown_value(item.get('provider'))}:{_markdown_value(item.get('model'))}",
                        f"- Prompt template: {_markdown_value(item.get('prompt_template_version'))}",
                        f"- Status: {_markdown_value(item.get('status'))}",
                        f"- Started: {_markdown_value(item.get('started_at'))}",
                        f"- Completed: {_markdown_value(item.get('completed_at'))}",
                    ]
                )
                if item.get("error_message"):
                    lines.append(f"- Error: {_markdown_value(item.get('error_message'))}")
                lines.extend(["", "Planned cues:", ""])
                cues = item.get("planned_cues")
                if not isinstance(cues, list) or not cues:
                    lines.append("(none)")
                else:
                    for cue in cues:
                        if isinstance(cue, dict):
                            lines.extend(
                                [
                                    f"- {cue.get('order')}. {_markdown_value(cue.get('label'))}",
                                    f"  Cue text: {_markdown_value(cue.get('text'))}",
                                    f"  Rationale: {_markdown_value(cue.get('rationale'))}",
                                    f"  Rubric criteria: {_markdown_value(cue.get('rubric_criterion_ids'))}",
                                ]
                            )
                        else:
                            lines.append(f"- {_markdown_value(cue)}")
                lines.extend(
                    [
                        "",
                        "Retrieval planner prompt:",
                        "",
                        "```text",
                        item.get("prompt_text") or "",
                        "```",
                        "",
                        "Retrieval planner raw response:",
                        "",
                        "```text",
                        item.get("raw_response_text") or "",
                        "```",
                        "",
                        "Normalized planned cues JSON:",
                        "",
                        "```json",
                        _markdown_json(item.get("planned_cues_json")),
                        "```",
                        "",
                    ]
                )

    if retrievals is not None:
        lines.extend(["## Retrieved Chunks", ""])
        if not retrievals:
            lines.append("(none)")
        else:
            for item in retrievals:
                source_bits = [
                    item.get("material_title"),
                    item.get("material_type"),
                ]
                if item.get("week_number") is not None:
                    source_bits.append(f"week {item['week_number']}")
                source = " | ".join(str(bit) for bit in source_bits if bit)
                lines.extend(
                    [
                        f"### Chunk {item.get('chunk_id')}",
                        "",
                        f"- Rank: {_markdown_value(item.get('rank_position'))}",
                        f"- Similarity score: {_markdown_value(item.get('similarity_score'))}",
                        f"- Used in prompt: {_markdown_value(item.get('used_in_prompt'))}",
                        f"- Source: {_markdown_value(source)}",
                        f"- Pages: {_markdown_value(item.get('page_number_start'))}-{_markdown_value(item.get('page_number_end'))}",
                        "",
                        "Query:",
                        "",
                        _markdown_value(item.get("query_text")),
                        "",
                    ]
                )
                if "chunk_text" in item:
                    lines.extend(["Chunk text:", "", _markdown_value(item.get("chunk_text")), ""])

    if "prompt_text" in run:
        lines.extend(
            [
                "## Feedback Generation Prompt",
                "",
                "```text",
                run.get("prompt_text") or "",
                "```",
                "",
            ]
        )

    if "raw_response_text" in run:
        lines.extend(
            [
                "## Feedback Generation Raw Response",
                "",
                "```text",
                run.get("raw_response_text") or "",
                "```",
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"
