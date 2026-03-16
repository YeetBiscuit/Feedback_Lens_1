import fitz  # PyMuPDF
import json


def extract_pages(path):
    """Extract text per page. Returns list of {page, text}."""
    doc = fitz.open(path)
    pages = []
    for i in range(doc.page_count):
        text = doc.load_page(i).get_text()
        pages.append({"page": i + 1, "text": text.strip()})
    doc.close()
    return pages


if __name__ == "__main__":
    pages = extract_pages("./sample_spec/sample1.pdf")
    print(f"Extracted {len(pages)} page(s).")

    output_path = "./sample_spec/sample1_pages.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(pages, f, ensure_ascii=False, indent=2)
    print(f"Saved to {output_path}")
