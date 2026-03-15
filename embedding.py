import json
import chromadb
from sentence_transformers import SentenceTransformer

CHUNKS_PATH = "./sample_spec/sample1_chunks.json"
CHROMA_DIR = "./chromadb"
COLLECTION_NAME = "samplespec1"
MODEL_NAME = "all-MiniLM-L6-v2"


def build_index(chunks_path=CHUNKS_PATH):
    print(f"Loading chunks from {chunks_path}...")
    with open(chunks_path, encoding="utf-8") as f:
        chunks = json.load(f)

    print(f"Loading embedding model: {MODEL_NAME}")
    model = SentenceTransformer(MODEL_NAME)

    print(f"Embedding {len(chunks)} chunks...")
    texts = [chunk["text"] for chunk in chunks]
    embeddings = model.encode(texts, show_progress_bar=True)

    print(f"Storing in ChromaDB at {CHROMA_DIR}...")
    client = chromadb.PersistentClient(path=CHROMA_DIR)

    # Drop and recreate collection for demo run.
    client.delete_collection(COLLECTION_NAME) if COLLECTION_NAME in [c.name for c in client.list_collections()] else None
    collection = client.create_collection(COLLECTION_NAME)

    collection.add(
        ids=[str(chunk["chunk_id"]) for chunk in chunks],
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

    print(f"Done. {collection.count()} chunks indexed.")
    return collection


if __name__ == "__main__":
    build_index()
