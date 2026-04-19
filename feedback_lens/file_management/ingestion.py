import sqlite3
from pathlib import Path

from feedback_lens.file_management.document_io import (
    clean_text,
    hash_file,
    normalise_source_path,
    read_document_pages,
)
from feedback_lens.file_management.indexing.chunking import chunk_pages
from feedback_lens.file_management.indexing.embedding import (
    MODEL_NAME,
    build_collection_name,
    embed_and_store,
)

def _build_collection_name(
    unit_code: str,
    year: int | None,
    semester: str | None,
) -> int:
    return build_collection_name(unit_code, year, semester)

    """
    Derive a ChromaDB-safe collection name from unit fields.
    Example: 'FIT1008', 2026, 'Semester 1' → 'fit1008_2026_semester_1'
    ChromaDB requires: 3–63 chars, alphanumeric / hyphens / underscores,
    must start and end with alphanumeric.
    """
    parts = [unit_code or "unit"]
    if year is not None:
        parts.append(str(year))
    if semester:
        parts.append(semester)

    raw = "_".join(parts)
    normalised = raw.lower()
    normalised = re.sub(r"[^a-z0-9_-]", "_", normalised)
    normalised = re.sub(r"_+", "_", normalised)
    normalised = normalised.strip("_-")
    return normalised[:63]


def ingest_material(
    conn: sqlite3.Connection,
    file_path: str | Path,
    unit_id: int,
    material_type: str,
    title: str,
    week_number: int | None = None,
    assignment_id: int | None = None,
) -> int:
    """
    Ingest a unit material (PDF or TXT) through the full pipeline:
        extract → DB record → chunk → embed → mapping

    Returns the new material_id.
    """
    file_path = Path(file_path)

    # 1. Resolve unit details for collection naming.
    unit = conn.execute(
        "SELECT unit_code, year, semester FROM units WHERE unit_id = ?",
        (unit_id,),
    ).fetchone()
    if unit is None:
        raise ValueError(f"No unit found with unit_id={unit_id}")

    collection_name = build_collection_name(
        unit["unit_code"], unit["year"], unit["semester"]
    )

    # 2. Extract text from the source file.
    print(f"Extracting text from '{file_path.name}'...")
    pages = read_document_pages(file_path)
    raw_text = "\n".join(p["text"] for p in pages)
    cleaned_text = clean_text(raw_text)

    # 3. Insert unit_materials record.
    cur = conn.execute(
        """
        INSERT INTO unit_materials
            (unit_id, assignment_id, material_type, title,
             week_number, source_file_path, source_content_hash, raw_text, cleaned_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            unit_id,
            assignment_id,
            material_type,
            title,
            week_number,
            normalise_source_path(file_path),
            hash_file(file_path),
            raw_text,
            cleaned_text,
        ),
    )
    material_id = cur.lastrowid
    print(f"Inserted unit_materials record: material_id={material_id}")

    # 4. Chunk the extracted pages.
    chunks = chunk_pages(pages)
    print(f"Produced {len(chunks)} chunk(s).")

    # 5. Insert material_chunks and capture SQLite PKs.
    for i, chunk in enumerate(chunks):
        cur = conn.execute(
            """
            INSERT INTO material_chunks
                (material_id, chunk_index, chunk_text,
                 page_number_start, page_number_end,
                 token_count, chunking_strategy)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                material_id,
                i,
                chunk["text"],
                chunk["page_start"],
                chunk["page_end"],
                chunk["word_count"],
                "naive_sliding_window",
            ),
        )
        chunk["chunk_id"] = cur.lastrowid  # tag with SQLite PK for embedding step

    # 6. Embed chunks and store in ChromaDB.
    print(f"Embedding into ChromaDB collection '{collection_name}'...")
    vector_ids = embed_and_store(chunks, collection_name)

    # 7. Insert chunk_embedding_map records.
    for chunk, vector_id in zip(chunks, vector_ids):
        conn.execute(
            """
            INSERT INTO chunk_embedding_map
                (chunk_id, embedding_model, vector_store_name, vector_id)
            VALUES (?, ?, ?, ?)
            """,
            (chunk["chunk_id"], MODEL_NAME, collection_name, vector_id),
        )

    conn.commit()
    print(
        f"Ingestion complete. material_id={material_id}, "
        f"{len(chunks)} chunks in '{collection_name}'."
    )
    return material_id


if __name__ == "__main__":
    # Usage: python ingest.py <file_path> <unit_id> <material_type> <title> [week_number]
    if len(sys.argv) < 5:
        print(
            "Usage: python ingest.py <file_path> <unit_id> <material_type> <title> [week_number]"
        )
        sys.exit(1)

    pdf_path = sys.argv[1]
    _unit_id = int(sys.argv[2])
    _material_type = sys.argv[3]
    _title = sys.argv[4]
    _week = int(sys.argv[5]) if len(sys.argv) > 5 else None

    db_conn = sqlite3.connect("feedback_system.db")
    db_conn.row_factory = sqlite3.Row
    db_conn.execute("PRAGMA foreign_keys = ON;")

    try:
        ingest_material(db_conn, pdf_path, _unit_id, _material_type, _title, _week)
    finally:
        db_conn.close()
