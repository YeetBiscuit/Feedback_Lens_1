import copy
import unittest

from feedback_lens.file_management.processed_slides import (
    SLIDE_CHUNKING_STRATEGY,
    ProcessedSlidesValidationError,
    chunk_processed_slides,
    render_processed_slide,
    validate_processed_slides,
)


def _slide(number: int, *, instructional: bool = True) -> dict:
    return {
        "slide_number": number,
        "title": f"Topic {number}" if instructional else None,
        "text_content": [f"Teaching content {number}"] if instructional else [],
        "visual_elements": [],
        "unclear_content": [],
        "has_instructional_content": instructional,
    }


def _document() -> dict:
    return {
        "lecture": "Lecture 9",
        "source_file": "lecture9.pdf",
        "slides": [
            _slide(1),
            _slide(2),
            _slide(3, instructional=False),
            _slide(4),
            _slide(5),
            _slide(6),
            _slide(7),
        ],
    }


class ProcessedSlidesTests(unittest.TestCase):
    def test_chunking_excludes_non_instructional_slides_and_overlaps_one(self):
        chunks = chunk_processed_slides(_document())

        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["slide_numbers"], [1, 2, 4, 5, 6])
        self.assertEqual(chunks[1]["slide_numbers"], [6, 7])
        self.assertEqual(chunks[0]["page_start"], 1)
        self.assertEqual(chunks[0]["page_end"], 6)
        self.assertEqual(
            chunks[0]["chunking_strategy"],
            SLIDE_CHUNKING_STRATEGY,
        )
        self.assertNotIn("Slide 3", chunks[0]["text"])
        self.assertIn("Slide 6", chunks[0]["text"])
        self.assertIn("Slide 6", chunks[1]["text"])

    def test_rendering_keeps_text_visuals_and_uncertainty_separate(self):
        slide = _slide(1)
        slide["visual_elements"] = [
            {
                "type": "graph",
                "title": "Learning outcomes",
                "description": "The plotted line rises from left to right.",
            }
        ]
        slide["unclear_content"] = ["The final axis label is unclear."]

        rendered = render_processed_slide(slide)

        self.assertIn("Text:\nTeaching content 1", rendered)
        self.assertIn("Visual element 1 (graph):", rendered)
        self.assertIn("Preprocessing uncertainty:", rendered)

    def test_validation_rejects_non_sequential_slide_numbers(self):
        document = _document()
        document["slides"][3]["slide_number"] = 5

        with self.assertRaisesRegex(
            ProcessedSlidesValidationError,
            "slide_number must be the integer 4",
        ):
            validate_processed_slides(document)

    def test_validation_rejects_derived_slide_fields(self):
        document = copy.deepcopy(_document())
        document["slides"][0]["embedding_text"] = "Derived summary"

        with self.assertRaisesRegex(
            ProcessedSlidesValidationError,
            "unexpected field",
        ):
            validate_processed_slides(document)


if __name__ == "__main__":
    unittest.main()
