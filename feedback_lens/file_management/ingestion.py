import sqlite3
import sys
from pathlib import Path

from feedback_lens.file_management.document_io import (
    clean_text,
    hash_file,
    normalise_source_path,
    read_document_pages,
)
from feedback_lens.file_management.indexing.chunking import chunk_pages
from feedback_lens.file_management.indexing.embedding import (
    EmbeddingConfig,
    build_collection_name,
    encode_chunks,
    require_writable_unit_embedding_config,
    store_chunk_embeddings,
)
from feedback_lens.file_management.processed_slides import (
    SLIDE_CHUNKING_STRATEGY,
    chunk_processed_slides,
    load_processed_slides,
    render_processed_slide_document,
)


DEFAULT_CHUNKING_STRATEGY = "naive_sliding_window"


def record_index_build(
    conn: sqlite3.Connection,
    unit_id: int,
    collection_name: str,
    embedding_config: EmbeddingConfig,
    chunks: list[dict],
    vector_ids: list[str],
    chunking_strategy: str = DEFAULT_CHUNKING_STRATEGY,
) -> None:
    """Keep the V2 index provenance tables aligned with the live index."""
    offering = conn.execute(
        "SELECT unit_offering_id FROM unit_offerings WHERE legacy_unit_id = ?",
        (unit_id,),
    ).fetchone()
    if offering is None:
        return

    conn.execute(
        """
        INSERT OR IGNORE INTO index_builds
            (unit_offering_id, vector_store_name, embedding_model,
             embedding_version, chunking_strategy, status, completed_at)
        VALUES (?, ?, ?, ?, ?, 'active', CURRENT_TIMESTAMP)
        """,
        (
            offering["unit_offering_id"],
            collection_name,
            embedding_config.model_name,
            embedding_config.model_version,
            chunking_strategy,
        ),
    )
    build = conn.execute(
        """
        SELECT index_build_id, chunking_strategy
        FROM index_builds
        WHERE unit_offering_id = ?
          AND vector_store_name = ?
          AND embedding_model = ?
          AND embedding_version IS ?
        """,
        (
            offering["unit_offering_id"],
            collection_name,
            embedding_config.model_name,
            embedding_config.model_version,
        ),
    ).fetchone()
    if build is None:
        raise RuntimeError("Could not record the retrieval index build.")
    existing_strategy = str(build["chunking_strategy"] or "")
    resolved_strategy = (
        chunking_strategy
        if not existing_strategy or existing_strategy == chunking_strategy
        else "mixed"
    )
    conn.execute(
        """
        UPDATE index_builds
        SET chunking_strategy = ?,
            status = 'active',
            completed_at = CURRENT_TIMESTAMP
        WHERE index_build_id = ?
        """,
        (resolved_strategy, build["index_build_id"]),
    )
    conn.executemany(
        """
        INSERT OR IGNORE INTO index_build_items
            (index_build_id, chunk_id, vector_id)
        VALUES (?, ?, ?)
        """,
        [
            (build["index_build_id"], chunk["chunk_id"], vector_id)
            for chunk, vector_id in zip(chunks, vector_ids)
        ],
    )


def _unit_index_target(
    conn: sqlite3.Connection,
    unit_id: int,
) -> tuple[EmbeddingConfig, str]:
    unit = conn.execute(
        "SELECT unit_code, year, semester FROM units WHERE unit_id = ?",
        (unit_id,),
    ).fetchone()
    if unit is None:
        raise ValueError(f"No unit found with unit_id={unit_id}")

    embedding_config = require_writable_unit_embedding_config(conn, unit_id)
    collection_name = build_collection_name(
        unit["unit_code"],
        unit["year"],
        unit["semester"],
        embedding_config,
    )
    return embedding_config, collection_name


def _store_material_chunks(
    conn: sqlite3.Connection,
    file_path: Path,
    unit_id: int,
    material_type: str,
    title: str,
    raw_text: str,
    cleaned_text: str,
    chunks: list[dict],
    chunking_strategy: str,
    embedding_config: EmbeddingConfig,
    collection_name: str,
    week_number: int | None = None,
    assignment_id: int | None = None,
) -> int:
    if not cleaned_text.strip():
        raise ValueError("The material has no instructional text to index.")
    if not chunks:
        raise ValueError("The material produced no chunks to index.")

    # Model loading and encoding can take many seconds. Do that work before
    # the first SQLite write so other web and worker connections are not
    # blocked for the duration of embedding.
    embeddings = encode_chunks(chunks, embedding_config)

    cur = conn.execute(
        """
        INSERT INTO unit_materials
            (unit_id, assignment_id, material_type, title,
             week_number, source_file_path, source_content_hash,
             raw_text, cleaned_text)
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
    material_id = int(cur.lastrowid)
    print(f"Inserted unit_materials record: material_id={material_id}")

    for index, chunk in enumerate(chunks):
        cur = conn.execute(
            """
            INSERT INTO material_chunks
                (material_id, chunk_index, chunk_text, section_title,
                 page_number_start, page_number_end,
                 token_count, chunking_strategy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                material_id,
                index,
                chunk["text"],
                chunk.get("section_title"),
                chunk["page_start"],
                chunk["page_end"],
                chunk["word_count"],
                chunking_strategy,
            ),
        )
        chunk["chunk_id"] = int(cur.lastrowid)

    print(f"Embedding into ChromaDB collection '{collection_name}'...")
    vector_ids = store_chunk_embeddings(
        chunks,
        embeddings,
        collection_name,
        embedding_config=embedding_config,
    )

    conn.executemany(
        """
        INSERT INTO chunk_embedding_map
            (chunk_id, embedding_model, embedding_version,
             vector_store_name, vector_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                chunk["chunk_id"],
                embedding_config.model_name,
                embedding_config.model_version,
                collection_name,
                vector_id,
            )
            for chunk, vector_id in zip(chunks, vector_ids)
        ],
    )
    record_index_build(
        conn,
        unit_id,
        collection_name,
        embedding_config,
        chunks,
        vector_ids,
        chunking_strategy,
    )
    conn.commit()
    print(
        f"Ingestion complete. material_id={material_id}, "
        f"{len(chunks)} chunks in '{collection_name}'."
    )
    return material_id


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

    embedding_config, collection_name = _unit_index_target(conn, unit_id)

    # 2. Extract text from the source file.
    print(f"Extracting text from '{file_path.name}'...")
    pages = read_document_pages(file_path)
    raw_text = "\n".join(p["text"] for p in pages)
    cleaned_text = clean_text(raw_text)

    chunks = chunk_pages(pages)
    print(f"Produced {len(chunks)} chunk(s).")
    return _store_material_chunks(
        conn,
        file_path,
        unit_id,
        material_type,
        title,
        raw_text,
        cleaned_text,
        chunks,
        DEFAULT_CHUNKING_STRATEGY,
        embedding_config,
        collection_name,
        week_number,
        assignment_id,
    )


def ingest_processed_slides(
    conn: sqlite3.Connection,
    file_path: str | Path,
    unit_id: int,
    title: str,
    week_number: int | None = None,
) -> int:
    """Validate, chunk, and index an AI-assisted slide JSON document."""
    path = Path(file_path)
    embedding_config, collection_name = _unit_index_target(conn, unit_id)
    source_hash = hash_file(path)
    duplicate = conn.execute(
        """
        SELECT material_id
        FROM unit_materials
        WHERE unit_id = ?
          AND source_content_hash = ?
          AND is_active = 1
        LIMIT 1
        """,
        (unit_id, source_hash),
    ).fetchone()
    if duplicate is not None:
        raise ValueError(
            "This processed slide file has already been imported "
            f"as material_id={duplicate['material_id']}."
        )

    print(f"Reading processed slides from '{path.name}'...")
    document = load_processed_slides(path)
    raw_text = path.read_text(encoding="utf-8-sig")
    cleaned_text = render_processed_slide_document(document)
    chunks = chunk_processed_slides(document)
    print(f"Produced {len(chunks)} slide chunk(s).")
    return _store_material_chunks(
        conn,
        path,
        unit_id,
        "lecture_slide",
        title,
        raw_text,
        cleaned_text,
        chunks,
        SLIDE_CHUNKING_STRATEGY,
        embedding_config,
        collection_name,
        week_number,
    )


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
