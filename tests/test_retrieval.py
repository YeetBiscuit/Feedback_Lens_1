import sqlite3
import unittest
from unittest.mock import patch

from feedback_lens.db.connection import ensure_schema_updates
from feedback_lens.feedback.retrieval import retrieve_relevant_chunks
from feedback_lens.paths import SCHEMA_PATH


def _connect_retrieval_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    ensure_schema_updates(conn)

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
            VALUES (?, 'test-model', 'v1', 'comp1001_2026_s1', ?)
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


if __name__ == "__main__":
    unittest.main()
