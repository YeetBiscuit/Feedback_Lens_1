import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from feedback_lens.db.migrations import migrate_database
from feedback_lens.file_management.indexing.embedding import (
    DEFAULT_EMBEDDING_CONFIG,
    DEFAULT_MODEL_NAME,
    DEFAULT_MODEL_REVISION,
    LEGACY_MODEL_NAME,
    LegacyEmbeddingUnitReadOnlyError,
    embed_and_store,
    get_embedding_model,
)
from feedback_lens.file_management.ingestion import (
    ingest_material,
    ingest_processed_slides,
)
from feedback_lens.file_management.processed_slides import (
    SLIDE_CHUNKING_STRATEGY,
)
from feedback_lens.paths import SCHEMA_PATH


def _database_with_unit() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute(
        """
        INSERT INTO units (unit_id, unit_code, unit_name, semester, year)
        VALUES (1, 'COMP3001', 'New Retrieval Unit', 'S1', 2027)
        """
    )
    migrate_database(conn)
    conn.commit()
    return conn


class EmbeddingModelTests(unittest.TestCase):
    def test_default_model_loader_pins_the_bge_revision(self) -> None:
        get_embedding_model.cache_clear()
        with patch(
            "feedback_lens.file_management.indexing.embedding.SentenceTransformer"
        ) as sentence_transformer:
            get_embedding_model()

        sentence_transformer.assert_called_once_with(
            DEFAULT_MODEL_NAME,
            revision=DEFAULT_MODEL_REVISION,
        )
        get_embedding_model.cache_clear()

    def test_bge_embeddings_are_normalized_and_collection_uses_cosine(self) -> None:
        model = Mock()
        model.encode.return_value = np.zeros((1, 1024), dtype=np.float32)
        collection = Mock()
        client = Mock()
        client.list_collections.return_value = []
        client.create_collection.return_value = collection

        with (
            patch(
                "feedback_lens.file_management.indexing.embedding.get_embedding_model",
                return_value=model,
            ),
            patch(
                "feedback_lens.file_management.indexing.embedding.get_chroma_client",
                return_value=client,
            ),
        ):
            vector_ids = embed_and_store(
                [
                    {
                        "chunk_id": 7,
                        "text": "Multilingual retrieval text",
                        "page_start": 1,
                        "page_end": 1,
                    }
                ],
                "comp3001_2027_s1_bge_m3_5617a9f",
            )

        self.assertEqual(vector_ids, ["7"])
        model.encode.assert_called_once_with(
            ["Multilingual retrieval text"],
            batch_size=8,
            normalize_embeddings=True,
            show_progress_bar=True,
        )
        client.create_collection.assert_called_once_with(
            "comp3001_2027_s1_bge_m3_5617a9f",
            embedding_function=None,
            configuration={"hnsw": {"space": "cosine"}},
            metadata={
                "embedding_model": DEFAULT_MODEL_NAME,
                "embedding_version": DEFAULT_MODEL_REVISION,
                "dimension": 1024,
            },
        )

    def test_new_material_is_recorded_with_pinned_bge_version(self) -> None:
        with _database_with_unit() as conn:
            def encode_without_sqlite_write_lock(chunks, embedding_config):
                self.assertFalse(conn.in_transaction)
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM unit_materials"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(embedding_config, DEFAULT_EMBEDDING_CONFIG)
                return [[0.0] * 1024 for _ in chunks]

            with (
                patch(
                    "feedback_lens.file_management.ingestion.read_document_pages",
                    return_value=[{"page": 1, "text": "Course context"}],
                ),
                patch(
                    "feedback_lens.file_management.ingestion.hash_file",
                    return_value="hash",
                ),
                patch(
                    "feedback_lens.file_management.ingestion.normalise_source_path",
                    return_value="context.txt",
                ),
                patch(
                    "feedback_lens.file_management.ingestion.encode_chunks",
                    side_effect=encode_without_sqlite_write_lock,
                ),
                patch(
                    "feedback_lens.file_management.ingestion.store_chunk_embeddings",
                    side_effect=lambda chunks, embeddings, collection_name, **kwargs: [
                        str(chunk["chunk_id"]) for chunk in chunks
                    ],
                ) as mocked_store,
            ):
                ingest_material(
                    conn,
                    "context.txt",
                    1,
                    "lecture_transcript",
                    "Course context",
                )

            mapping = conn.execute(
                "SELECT * FROM chunk_embedding_map"
            ).fetchone()
            self.assertEqual(mapping["embedding_model"], DEFAULT_MODEL_NAME)
            self.assertEqual(mapping["embedding_version"], DEFAULT_MODEL_REVISION)
            self.assertEqual(
                mapping["vector_store_name"],
                "comp3001_2027_s1_bge_m3_5617a9f",
            )
            config = mocked_store.call_args.kwargs["embedding_config"]
            self.assertEqual(config, DEFAULT_EMBEDDING_CONFIG)

    def test_minilm_unit_rejects_new_material_before_extraction(self) -> None:
        with _database_with_unit() as conn:
            material_id = conn.execute(
                """
                INSERT INTO unit_materials
                    (unit_id, material_type, title, cleaned_text)
                VALUES (1, 'lecture_transcript', 'Legacy', 'Legacy text')
                """
            ).lastrowid
            chunk_id = conn.execute(
                """
                INSERT INTO material_chunks
                    (material_id, chunk_index, chunk_text)
                VALUES (?, 0, 'Legacy text')
                """,
                (material_id,),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO chunk_embedding_map
                    (chunk_id, embedding_model, vector_store_name, vector_id)
                VALUES (?, ?, 'comp3001_2027_s1', ?)
                """,
                (chunk_id, LEGACY_MODEL_NAME, str(chunk_id)),
            )
            conn.commit()

            with patch(
                "feedback_lens.file_management.ingestion.read_document_pages"
            ) as mocked_read:
                with self.assertRaises(LegacyEmbeddingUnitReadOnlyError):
                    ingest_material(
                        conn,
                        "new.txt",
                        1,
                        "lecture_transcript",
                        "New material",
                    )

            mocked_read.assert_not_called()
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM unit_materials").fetchone()[0],
                1,
            )

    def test_processed_slide_json_uses_slide_windows_and_bge(self) -> None:
        slides = []
        for number in range(1, 8):
            instructional = number != 3
            slides.append(
                {
                    "slide_number": number,
                    "title": f"Topic {number}" if instructional else None,
                    "text_content": (
                        [f"Teaching content {number}"] if instructional else []
                    ),
                    "visual_elements": [],
                    "unclear_content": [],
                    "has_instructional_content": instructional,
                }
            )
        document = {
            "lecture": "Lecture 9",
            "source_file": "lecture9.pdf",
            "slides": slides,
        }

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            source = Path(tmp) / "lecture9.json"
            source.write_text(
                json.dumps(document, ensure_ascii=False),
                encoding="utf-8",
            )
            with _database_with_unit() as conn:
                with (
                    patch(
                        "feedback_lens.file_management.ingestion.encode_chunks",
                        side_effect=lambda chunks, config: [
                            [0.0] * 1024 for _ in chunks
                        ],
                    ),
                    patch(
                        "feedback_lens.file_management.ingestion.store_chunk_embeddings",
                        side_effect=lambda chunks, embeddings, collection_name, **kwargs: [
                            str(chunk["chunk_id"]) for chunk in chunks
                        ],
                    ) as mocked_store,
                ):
                    material_id = ingest_processed_slides(
                        conn,
                        source,
                        1,
                        "Lecture 9 slides",
                    )

                material = conn.execute(
                    "SELECT * FROM unit_materials WHERE material_id = ?",
                    (material_id,),
                ).fetchone()
                chunks = conn.execute(
                    """
                    SELECT * FROM material_chunks
                    WHERE material_id = ?
                    ORDER BY chunk_index
                    """,
                    (material_id,),
                ).fetchall()
                mappings = conn.execute(
                    """
                    SELECT map.*
                    FROM chunk_embedding_map AS map
                    JOIN material_chunks AS chunk
                      ON chunk.chunk_id = map.chunk_id
                    WHERE chunk.material_id = ?
                    """,
                    (material_id,),
                ).fetchall()

                self.assertEqual(material["material_type"], "lecture_slide")
                self.assertEqual(len(chunks), 2)
                self.assertEqual(chunks[0]["page_number_start"], 1)
                self.assertEqual(chunks[0]["page_number_end"], 6)
                self.assertEqual(
                    chunks[0]["chunking_strategy"],
                    SLIDE_CHUNKING_STRATEGY,
                )
                self.assertNotIn("Slide 3", chunks[0]["chunk_text"])
                self.assertEqual(len(mappings), 2)
                self.assertTrue(
                    all(
                        row["embedding_model"] == DEFAULT_MODEL_NAME
                        for row in mappings
                    )
                )
                self.assertEqual(
                    mocked_store.call_args.kwargs["embedding_config"],
                    DEFAULT_EMBEDDING_CONFIG,
                )


if __name__ == "__main__":
    unittest.main()
