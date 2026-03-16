import json
import chromadb
from sentence_transformers import SentenceTransformer

CHUNKS_PATH = "./sample_spec/sample1_chunks.json"
CHROMA_DIR = "./chromadb"
COLLECTION_NAME = "samplespec1"
MODEL_NAME = "all-MiniLM-L6-v2"


def embed_and_store(chunks, collection_name, chroma_dir=CHROMA_DIR):
    """
    Embed a list of chunks and store them in a ChromaDB collection.

    Each chunk must have: chunk_id (SQLite PK, used as vector ID), text,
    page_start, page_end.

    The collection is created if it does not already exist, so multiple
    materials belonging to the same unit accumulate in one collection.

    Returns a list of vector IDs in the same order as the input chunks.
    """
    print(f"Loading embedding model: {MODEL_NAME}")
    model = SentenceTransformer(MODEL_NAME)

    texts = [chunk["text"] for chunk in chunks]
    print(f"Embedding {len(chunks)} chunk(s)...")
    embeddings = model.encode(texts, show_progress_bar=True)

    client = chromadb.PersistentClient(path=chroma_dir)

    existing_names = [col.name for col in client.list_collections()]
    if collection_name in existing_names:
        collection = client.get_collection(collection_name)
    else:
        collection = client.create_collection(collection_name)

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


def build_index(chunks_path=CHUNKS_PATH):
    """Legacy demo entry point — loads chunks from a JSON file and indexes them."""
    print(f"Loading chunks from {chunks_path}...")
    with open(chunks_path, encoding="utf-8") as f:
        chunks = json.load(f)

    client = chromadb.PersistentClient(path=CHROMA_DIR)

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
