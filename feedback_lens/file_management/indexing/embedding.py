import json
import re
from functools import lru_cache
from pathlib import Path

import chromadb
from sentence_transformers import SentenceTransformer

from feedback_lens.paths import CHROMA_DIR as DEFAULT_CHROMA_DIR

CHUNKS_PATH = "./sample_spec/sample1_chunks.json"
COLLECTION_NAME = "samplespec1"
MODEL_NAME = "all-MiniLM-L6-v2"


@lru_cache(maxsize=1)
def get_embedding_model() -> SentenceTransformer:
    print(f"Loading embedding model: {MODEL_NAME}")
    return SentenceTransformer(MODEL_NAME)


def build_collection_name(unit_code: str, year: int | None, semester: str | None) -> str:
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


def get_chroma_client(chroma_dir: str | Path = DEFAULT_CHROMA_DIR):
    return chromadb.PersistentClient(path=str(chroma_dir))


def get_or_create_collection(client, collection_name: str):
    existing_names = [col.name for col in client.list_collections()]
    if collection_name in existing_names:
        return client.get_collection(collection_name)
    return client.create_collection(collection_name)


def get_collection(client, collection_name: str):
    existing_names = [col.name for col in client.list_collections()]
    if collection_name not in existing_names:
        raise ValueError(
            f"Collection '{collection_name}' does not exist in '{DEFAULT_CHROMA_DIR}'."
        )
    return client.get_collection(collection_name)


def embed_and_store(chunks, collection_name, chroma_dir: str | Path = DEFAULT_CHROMA_DIR):
    """
    Embed a list of chunks and store them in a ChromaDB collection.

    Each chunk must have: chunk_id (SQLite PK, used as vector ID), text,
    page_start, page_end.

    The collection is created if it does not already exist, so multiple
    materials belonging to the same unit accumulate in one collection.

    Returns a list of vector IDs in the same order as the input chunks.
    """
    model = get_embedding_model()

    texts = [chunk["text"] for chunk in chunks]
    print(f"Embedding {len(chunks)} chunk(s)...")
    embeddings = model.encode(texts, show_progress_bar=True)

    client = get_chroma_client(chroma_dir)
    collection = get_or_create_collection(client, collection_name)

    vector_ids = [str(chunk["chunk_id"]) for chunk in chunks]

    collection.add(
        ids=vector_ids,
        embeddings=[e.tolist() for e in embeddings],
        documents=texts,
        metadatas=[
            {
                "chunk_id": chunk["chunk_id"],
                "page_start": chunk["page_start"],
                "page_end": chunk["page_end"],
            }
            for chunk in chunks
        ],
    )

    print(f"Stored {len(vector_ids)} chunk(s) in collection '{collection_name}'.")
    return vector_ids


def query_collection(
    query_text: str,
    collection_name: str,
    n_results: int = 5,
    chroma_dir: str | Path = DEFAULT_CHROMA_DIR,
) -> list[dict]:
    model = get_embedding_model()
    query_embedding = model.encode([query_text], show_progress_bar=False)[0].tolist()

    client = get_chroma_client(chroma_dir)
    collection = get_collection(client, collection_name)
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=n_results,
        include=["documents", "distances", "metadatas"],
    )

    ids = results.get("ids", [[]])[0]
    documents = results.get("documents", [[]])[0]
    distances = results.get("distances", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]

    query_results = []
    for index, vector_id in enumerate(ids):
        query_results.append(
            {
                "vector_id": vector_id,
                "document": documents[index] if index < len(documents) else None,
                "distance": distances[index] if index < len(distances) else None,
                "metadata": metadatas[index] if index < len(metadatas) else None,
            }
        )

    return query_results


def build_index(chunks_path=CHUNKS_PATH):
    """Legacy demo entry point — loads chunks from a JSON file and indexes them."""
    print(f"Loading chunks from {chunks_path}...")
    with open(chunks_path, encoding="utf-8") as f:
        chunks = json.load(f)

    client = get_chroma_client(DEFAULT_CHROMA_DIR)

    # Drop and recreate for a clean demo run.
    existing_names = [col.name for col in client.list_collections()]
    if COLLECTION_NAME in existing_names:
        client.delete_collection(COLLECTION_NAME)

    embed_and_store(chunks, COLLECTION_NAME)
    collection = client.get_collection(COLLECTION_NAME)
    print(f"Done. {collection.count()} chunks indexed.")
    return collection


if __name__ == "__main__":
    build_index()
