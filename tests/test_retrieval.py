import sqlite3
import unittest
from unittest.mock import patch

from feedback_lens.db.migrations import migrate_database
from feedback_lens.feedback.retrieval import retrieve_relevant_chunks
from feedback_lens.file_management.indexing.embedding import (
    DEFAULT_MODEL_NAME,
    DEFAULT_MODEL_REVISION,
    LegacyEmbeddingUnitReadOnlyError,
    build_collection_name,
    require_writable_unit_embedding_config,
    resolve_unit_embedding_config,
)
from feedback_lens.paths import SCHEMA_PATH


def _connect_retrieval_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    migrate_database(conn)

    conn.execute(
        """
        INSERT INTO units (unit_id, unit_code, unit_name, semester, year)
        VALUES (1, 'COMP1001', 'Computing Foundations', 'S1', 2026)
        """
    )
    conn.execute(
        """
        INSERT INTO unit_materials
            (material_id, unit_id, material_type, title, week_number, cleaned_text)
        VALUES
            (1, 1, 'lecture_transcript', 'Week 1 Transcript', 1, 'Lecture text')
        """
    )
    for chunk_id in range(1, 13):
        conn.execute(
            """
            INSERT INTO material_chunks
                (chunk_id, material_id, chunk_index, chunk_text)
            VALUES (?, 1, ?, ?)
            """,
            (chunk_id, chunk_id, f"Chunk {chunk_id} text"),
        )
        conn.execute(
            """
            INSERT INTO chunk_embedding_map
                (chunk_id, embedding_model, embedding_version, vector_store_name, vector_id)
            VALUES (?, 'all-MiniLM-L6-v2', NULL, 'comp1001_2026_s1', ?)
            """,
            (chunk_id, f"v{chunk_id}"),
        )

    conn.commit()
    return conn


def _query_results(vector_ids: list[int]) -> list[dict]:
    return [
        {
            "vector_id": f"v{vector_id}",
            "distance": vector_id / 100,
            "document": None,
            "metadata": None,
        }
        for vector_id in vector_ids
    ]


class RetrievalTests(unittest.TestCase):
    def test_embedding_policy_keeps_minilm_units_read_only(self) -> None:
        with _connect_retrieval_db() as conn:
            config = resolve_unit_embedding_config(conn, 1)

            self.assertEqual(config.model_name, "all-MiniLM-L6-v2")
            self.assertIsNone(config.model_version)
            with self.assertRaises(LegacyEmbeddingUnitReadOnlyError):
                require_writable_unit_embedding_config(conn, 1)

    def test_unit_without_embeddings_defaults_to_versioned_bge_collection(self) -> None:
        with _connect_retrieval_db() as conn:
            conn.execute(
                """
                INSERT INTO units
                    (unit_id, unit_code, unit_name, semester, year)
                VALUES (2, 'COMP2002', 'New Unit', 'S2', 2027)
                """
            )
            config = require_writable_unit_embedding_config(conn, 2)

            self.assertEqual(config.model_name, DEFAULT_MODEL_NAME)
            self.assertEqual(config.model_version, DEFAULT_MODEL_REVISION)
            self.assertEqual(
                build_collection_name("COMP2002", 2027, "S2", config),
                "comp2002_2027_s2_bge_m3_5617a9f",
            )

    def test_bge_unit_uses_versioned_collection_and_cosine_similarity(self) -> None:
        with _connect_retrieval_db() as conn:
            conn.execute(
                """
                INSERT INTO units
                    (unit_id, unit_code, unit_name, semester, year)
                VALUES (2, 'COMP2002', 'New Unit', 'S2', 2027)
                """
            )
            conn.execute(
                """
                INSERT INTO unit_materials
                    (material_id, unit_id, material_type, title,
                     week_number, cleaned_text)
                VALUES (2, 2, 'lecture_transcript', 'BGE Context', 1,
                        'BGE lecture text')
                """
            )
            conn.execute(
                """
                INSERT INTO material_chunks
                    (chunk_id, material_id, chunk_index, chunk_text)
                VALUES (20, 2, 0, 'BGE lecture text')
                """
            )
            conn.execute(
                """
                INSERT INTO chunk_embedding_map
                    (chunk_id, embedding_model, embedding_version,
                     vector_store_name, vector_id)
                VALUES (20, ?, ?, ?, '20')
                """,
                (
                    DEFAULT_MODEL_NAME,
                    DEFAULT_MODEL_REVISION,
                    "comp2002_2027_s2_bge_m3_5617a9f",
                ),
            )
            conn.commit()
            unit_row = conn.execute(
                "SELECT * FROM units WHERE unit_id = 2"
            ).fetchone()

            with patch(
                "feedback_lens.feedback.retrieval.query_collection",
                return_value=[
                    {
                        "vector_id": "20",
                        "distance": 0.2,
                        "document": None,
                        "metadata": None,
                    }
                ],
            ) as mocked_query:
                collection_name, chunks, _ = retrieve_relevant_chunks(
                    conn,
                    unit_row,
                    [{"order": 1, "label": "Cue", "text": "query"}],
                    per_cue_top_k=1,
                    max_final_chunks=1,
                )

            self.assertEqual(
                collection_name,
                "comp2002_2027_s2_bge_m3_5617a9f",
            )
            self.assertEqual(chunks[0]["similarity_score"], 0.8)
            self.assertEqual(
                mocked_query.call_args.kwargs["embedding_config"].model_name,
                DEFAULT_MODEL_NAME,
            )

    def test_per_cue_top_k_and_final_chunk_limit_are_separate(self) -> None:
        retrieval_cues = [
            {"order": 1, "label": "Cue 1", "text": "first"},
            {"order": 2, "label": "Cue 2", "text": "second"},
            {"order": 3, "label": "Cue 3", "text": "third"},
        ]

        with (
            _connect_retrieval_db() as conn,
            patch("feedback_lens.feedback.retrieval.query_collection") as mock_query,
        ):
            unit_row = conn.execute("SELECT * FROM units WHERE unit_id = 1").fetchone()
            mock_query.side_effect = [
                _query_results([1, 2, 3, 4, 5]),
                _query_results([4, 5, 6, 7, 8]),
                _query_results([8, 9, 10, 11, 12]),
            ]

            collection_name, final_chunks, raw_hits = retrieve_relevant_chunks(
                conn,
                unit_row,
                retrieval_cues,
                per_cue_top_k=5,
                max_final_chunks=10,
            )

        self.assertEqual(collection_name, "comp1001_2026_s1")
        self.assertEqual(len(raw_hits), 15)
        self.assertEqual(len({hit["chunk_id"] for hit in raw_hits}), 12)
        self.assertEqual(len(final_chunks), 10)
        self.assertEqual([chunk["rank_position"] for chunk in final_chunks], list(range(1, 11)))
        self.assertTrue(all(call.kwargs["n_results"] == 5 for call in mock_query.call_args_list))
        self.assertTrue(
            all(
                call.kwargs["embedding_config"].model_name
                == "all-MiniLM-L6-v2"
                for call in mock_query.call_args_list
            )
        )


if __name__ == "__main__":
    unittest.main()
