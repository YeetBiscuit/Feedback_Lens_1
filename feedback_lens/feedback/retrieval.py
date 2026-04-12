import json
import sqlite3

from feedback_lens.file_management.indexing.embedding import (
    build_collection_name,
    query_collection,
)


def load_assignment_spec_cues(assignment_spec_row: sqlite3.Row) -> list[dict]:
    raw_json = assignment_spec_row["retrieval_cues_json"]
    if raw_json:
        try:
            value = json.loads(raw_json)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, list):
            cues = []
            for index, item in enumerate(value, start=1):
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text", "")).strip()
                if not text:
                    continue
                cues.append(
                    {
                        "order": int(item.get("order", index)),
                        "label": str(item.get("label", f"Cue {index}")).strip()
                        or f"Cue {index}",
                        "text": text,
                        "source_sections": item.get("source_sections") or [],
                    }
                )
            if cues:
                return sorted(cues, key=lambda cue: (cue["order"], cue["label"]))

    return [
        {
            "order": 1,
            "label": "Assignment Specification",
            "text": assignment_spec_row["cleaned_text"],
            "source_sections": ["Assignment Specification"],
        }
    ]


def _fetch_rows_by_vector_id(
    conn: sqlite3.Connection,
    collection_name: str,
    vector_ids: list[str],
) -> dict[str, sqlite3.Row]:
    if not vector_ids:
        return {}

    placeholders = ", ".join("?" for _ in vector_ids)
    rows = conn.execute(
        f"""
        SELECT
            cem.vector_id,
            mc.chunk_id,
            mc.chunk_text,
            mc.page_number_start,
            mc.page_number_end,
            um.material_id,
            um.title,
            um.material_type,
            um.week_number
        FROM chunk_embedding_map AS cem
        JOIN material_chunks AS mc ON mc.chunk_id = cem.chunk_id
        JOIN unit_materials AS um ON um.material_id = mc.material_id
        WHERE cem.vector_store_name = ?
          AND cem.vector_id IN ({placeholders})
        """,
        (collection_name, *vector_ids),
    ).fetchall()
    return {row["vector_id"]: row for row in rows}


def _build_chunk_query_text(cue: dict) -> str:
    return f"{cue['label']}\n{cue['text']}".strip()


def retrieve_relevant_chunks(
    conn: sqlite3.Connection,
    unit_row: sqlite3.Row,
    retrieval_cues: list[dict],
    top_k: int = 5,
) -> tuple[str, list[dict], list[dict]]:
    collection_name = build_collection_name(
        unit_row["unit_code"],
        unit_row["year"],
        unit_row["semester"],
    )

    raw_hits: list[dict] = []
    all_vector_ids: list[str] = []

    for cue in retrieval_cues:
        cue_query_text = _build_chunk_query_text(cue)
        query_results = query_collection(
            cue_query_text,
            collection_name,
            n_results=max(top_k, 1),
        )
        for rank_position, result in enumerate(query_results, start=1):
            raw_hits.append(
                {
                    "cue_order": cue["order"],
                    "cue_label": cue["label"],
                    "query_text": cue_query_text,
                    "rank_position": rank_position,
                    "vector_id": result["vector_id"],
                    "distance": result["distance"],
                    "similarity_score": None
                    if result["distance"] is None
                    else round(1 / (1 + result["distance"]), 6),
                }
            )
            all_vector_ids.append(result["vector_id"])

    if not raw_hits:
        return collection_name, [], []

    row_by_vector_id = _fetch_rows_by_vector_id(
        conn,
        collection_name,
        sorted(set(all_vector_ids)),
    )

    aggregated_by_chunk_id: dict[int, dict] = {}
    resolved_raw_hits: list[dict] = []

    for hit in raw_hits:
        row = row_by_vector_id.get(hit["vector_id"])
        if row is None:
            continue

        resolved_raw_hits.append({**hit, "chunk_id": row["chunk_id"]})

        state = aggregated_by_chunk_id.get(row["chunk_id"])
        if state is None:
            state = {
                "chunk_id": row["chunk_id"],
                "chunk_text": row["chunk_text"],
                "page_number_start": row["page_number_start"],
                "page_number_end": row["page_number_end"],
                "material_id": row["material_id"],
                "title": row["title"],
                "material_type": row["material_type"],
                "week_number": row["week_number"],
                "matched_cues": [],
                "matched_query_texts": [],
                "best_similarity_score": hit["similarity_score"],
                "best_rank_position": hit["rank_position"],
                "hit_count": 0,
            }
            aggregated_by_chunk_id[row["chunk_id"]] = state

        state["hit_count"] += 1
        if hit["cue_label"] not in state["matched_cues"]:
            state["matched_cues"].append(hit["cue_label"])
        if hit["query_text"] not in state["matched_query_texts"]:
            state["matched_query_texts"].append(hit["query_text"])

        score = hit["similarity_score"]
        if score is not None and (
            state["best_similarity_score"] is None
            or score > state["best_similarity_score"]
        ):
            state["best_similarity_score"] = score

        if hit["rank_position"] < state["best_rank_position"]:
            state["best_rank_position"] = hit["rank_position"]

    ranked_chunks = []
    for state in aggregated_by_chunk_id.values():
        best_score = state["best_similarity_score"] or 0.0
        aggregate_score = round(best_score + 0.05 * (state["hit_count"] - 1), 6)
        ranked_chunks.append({**state, "similarity_score": aggregate_score})

    ranked_chunks.sort(
        key=lambda chunk: (
            -(chunk["similarity_score"] or 0.0),
            -chunk["hit_count"],
            chunk["best_rank_position"],
            chunk["chunk_id"],
        )
    )

    final_chunks = []
    for rank_position, chunk in enumerate(ranked_chunks[:top_k], start=1):
        final_chunks.append({**chunk, "rank_position": rank_position})

    return collection_name, final_chunks, resolved_raw_hits
