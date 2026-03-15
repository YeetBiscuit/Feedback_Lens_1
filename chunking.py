import json


def naive_chunking(pages, chunk_size=500, overlap=100):
    """
    Naive sliding window chunker with overlap.
    Flattens all pages into a word list, tracks page per word for citations.
    chunk_size and overlap are in words.
    """
    # Build a flat list of (word, page_number) pairs.
    words = []
    for page in pages:
        for word in page["text"].split():
            words.append((word, page["page"]))

    chunks = []
    chunk_id = 1
    start = 0

    while start < len(words):
        end = min(start + chunk_size, len(words))
        window = words[start:end]

        text = " ".join(w for w, _ in window)
        page_start = window[0][1]
        page_end = window[-1][1]

        chunks.append({
            "chunk_id": chunk_id,
            "page_start": page_start,
            "page_end": page_end,
            "word_count": len(window),
            "text": text,
        })

        chunk_id += 1
        start += chunk_size - overlap  # slide forward by (chunk_size - overlap)

    return chunks


def chunk_pages(pages, chunk_size=500, overlap=100):
    """
    Default chunking entry point.
    Currently delegates to naive sliding-window chunking.
    """
    return naive_chunking(pages, chunk_size=chunk_size, overlap=overlap)


if __name__ == "__main__":
    with open("./sample_spec/sample1_pages.json", encoding="utf-8") as f:
        pages = json.load(f)

    chunks = chunk_pages(pages, chunk_size=500, overlap=100)
    print(f"Total chunks: {len(chunks)}")

    for chunk in chunks[:3]:
        print(f"\nChunk {chunk['chunk_id']} | p.{chunk['page_start']}-{chunk['page_end']} | {chunk['word_count']} words")
        preview = chunk["text"][:200].encode("ascii", errors="replace").decode("ascii")
        print(preview, "...")

    output_path = "./sample_spec/sample1_chunks.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(chunks, f, ensure_ascii=False, indent=2)
    print(f"\nSaved {len(chunks)} chunks to {output_path}")
