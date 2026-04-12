def read_transcript(path):
    """
    Read a plain-text transcript file.
    Returns the same [{page, text}] structure as pdf_reader.extract_pages so
    that the rest of the pipeline (chunking, embedding) is unaffected.
    Transcripts have no page boundaries, so the entire content is returned as
    a single entry with page=1.
    """
    with open(path, encoding="utf-8") as f:
        text = f.read().strip()
    return [{"page": 1, "text": text}]
