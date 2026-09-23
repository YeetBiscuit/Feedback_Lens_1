import json
import re
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import chromadb
from sentence_transformers import SentenceTransformer

from feedback_lens.paths import CHROMA_DIR as DEFAULT_CHROMA_DIR

CHUNKS_PATH = "./sample_spec/sample1_chunks.json"

LEGACY_MODEL_NAME = "all-MiniLM-L6-v2"
DEFAULT_MODEL_NAME = "BAAI/bge-m3"
DEFAULT_MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"

# Backwards-compatible name for callers that mean "the model used for new data".
MODEL_NAME = DEFAULT_MODEL_NAME
MODEL_VERSION = DEFAULT_MODEL_REVISION


@dataclass(frozen=True)
class EmbeddingConfig:
    model_name: str
    model_version: str | None
    dimension: int
    distance_metric: str
    normalize_embeddings: bool
    collection_suffix: str
    batch_size: int


LEGACY_EMBEDDING_CONFIG = EmbeddingConfig(
    model_name=LEGACY_MODEL_NAME,
    model_version=None,
    dimension=384,
    distance_metric="l2",
    normalize_embeddings=False,
    collection_suffix="",
    batch_size=32,
)

DEFAULT_EMBEDDING_CONFIG = EmbeddingConfig(
    model_name=DEFAULT_MODEL_NAME,
    model_version=DEFAULT_MODEL_REVISION,
    dimension=1024,
    distance_metric="cosine",
    normalize_embeddings=True,
    collection_suffix="_bge_m3_5617a9f",
    batch_size=8,
)

COLLECTION_NAME = f"samplespec1{DEFAULT_EMBEDDING_CONFIG.collection_suffix}"


class LegacyEmbeddingUnitReadOnlyError(ValueError):
    """Raised when new retrieval material is added to a MiniLM unit."""


def embedding_config_for(
    model_name: str,
    model_version: str | None = None,
) -> EmbeddingConfig:
    if model_name == LEGACY_MODEL_NAME:
        return LEGACY_EMBEDDING_CONFIG
    if model_name == DEFAULT_MODEL_NAME:
        return EmbeddingConfig(
            model_name=DEFAULT_MODEL_NAME,
            model_version=model_version or DEFAULT_MODEL_REVISION,
            dimension=DEFAULT_EMBEDDING_CONFIG.dimension,
            distance_metric=DEFAULT_EMBEDDING_CONFIG.distance_metric,
            normalize_embeddings=DEFAULT_EMBEDDING_CONFIG.normalize_embeddings,
            collection_suffix=(
                f"_bge_m3_{(model_version or DEFAULT_MODEL_REVISION)[:7]}"
            ),
            batch_size=DEFAULT_EMBEDDING_CONFIG.batch_size,
        )
    raise ValueError(f"Unsupported embedding model: {model_name}")


@lru_cache(maxsize=4)
def get_embedding_model(
    model_name: str = DEFAULT_MODEL_NAME,
    model_version: str | None = DEFAULT_MODEL_REVISION,
) -> SentenceTransformer:
    version_label = f" at revision {model_version}" if model_version else ""
    print(f"Loading embedding model: {model_name}{version_label}")
    kwargs = {"revision": model_version} if model_version else {}
    return SentenceTransformer(model_name, **kwargs)


def _base_collection_name(
    unit_code: str,
    year: int | None,
    semester: str | None,
) -> str:
    parts = [unit_code or "unit"]
    if year is not None:
        parts.append(str(year))
    if semester:
        parts.append(semester)

    raw = "_".join(parts)
    normalised = raw.lower()
    normalised = re.sub(r"[^a-z0-9_-]", "_", normalised)
    normalised = re.sub(r"_+", "_", normalised)
    return normalised.strip("_-")


def build_collection_name(
    unit_code: str,
    year: int | None,
    semester: str | None,
    embedding_config: EmbeddingConfig = DEFAULT_EMBEDDING_CONFIG,
) -> str:
    suffix = embedding_config.collection_suffix
    max_base_length = 63 - len(suffix)
    base = _base_collection_name(unit_code, year, semester)[:max_base_length]
    base = base.rstrip("_-") or "unit"
    return f"{base}{suffix}"


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _unit_embedding_identities(
    conn: sqlite3.Connection,
    unit_id: int,
) -> set[tuple[str, str | None]]:
    identities = {
        (str(row["embedding_model"]), row["embedding_version"])
        for row in conn.execute(
            """
            SELECT DISTINCT cem.embedding_model, cem.embedding_version
            FROM chunk_embedding_map AS cem
            JOIN material_chunks AS chunk ON chunk.chunk_id = cem.chunk_id
            JOIN unit_materials AS material
              ON material.material_id = chunk.material_id
            WHERE material.unit_id = ?
            """,
            (unit_id,),
        ).fetchall()
    }

    # index_builds preserves the model assignment after all material mappings
    # have been deleted, so a legacy unit cannot silently become writable.
    if _table_exists(conn, "index_builds") and _table_exists(conn, "unit_offerings"):
        identities.update(
            (
                str(row["embedding_model"]),
                row["embedding_version"],
            )
            for row in conn.execute(
                """
                SELECT DISTINCT build.embedding_model, build.embedding_version
                FROM index_builds AS build
                JOIN unit_offerings AS offering
                  ON offering.unit_offering_id = build.unit_offering_id
                WHERE offering.legacy_unit_id = ?
                  AND build.status = 'active'
                """,
                (unit_id,),
            ).fetchall()
        )
    return identities


def resolve_unit_embedding_config(
    conn: sqlite3.Connection,
    unit_id: int,
) -> EmbeddingConfig:
    identities = _unit_embedding_identities(conn, unit_id)
    if not identities:
        return DEFAULT_EMBEDDING_CONFIG

    model_names = {model_name for model_name, _ in identities}
    if len(model_names) != 1:
        raise ValueError(
            f"Unit {unit_id} has mixed embedding models: "
            f"{', '.join(sorted(model_names))}."
        )

    model_name = next(iter(model_names))
    versions = {version for name, version in identities if name == model_name}
    if len(versions) != 1:
        labels = sorted("NULL" if value is None else value for value in versions)
        raise ValueError(
            f"Unit {unit_id} has mixed versions of {model_name}: "
            f"{', '.join(labels)}."
        )
    return embedding_config_for(model_name, next(iter(versions)))


def unit_has_legacy_embeddings(conn: sqlite3.Connection, unit_id: int) -> bool:
    return any(
        model_name == LEGACY_MODEL_NAME
        for model_name, _ in _unit_embedding_identities(conn, unit_id)
    )


def require_writable_unit_embedding_config(
    conn: sqlite3.Connection,
    unit_id: int,
) -> EmbeddingConfig:
    config = resolve_unit_embedding_config(conn, unit_id)
    if config.model_name == LEGACY_MODEL_NAME:
        raise LegacyEmbeddingUnitReadOnlyError(
            "This legacy Unit uses MiniLM retrieval and its scoping materials "
            "are read-only. New Units use BGE-M3."
        )
    return config


def get_chroma_client(chroma_dir: str | Path = DEFAULT_CHROMA_DIR):
    return chromadb.PersistentClient(path=str(chroma_dir))


def get_or_create_collection(
    client,
    collection_name: str,
    embedding_config: EmbeddingConfig = DEFAULT_EMBEDDING_CONFIG,
):
    existing_names = [col.name for col in client.list_collections()]
    if collection_name in existing_names:
        return client.get_collection(collection_name)
    return client.create_collection(
        collection_name,
        embedding_function=None,
        configuration={"hnsw": {"space": embedding_config.distance_metric}},
        metadata={
            "embedding_model": embedding_config.model_name,
            "embedding_version": embedding_config.model_version or "unversioned",
            "dimension": embedding_config.dimension,
        },
    )


def get_collection(client, collection_name: str):
    existing_names = [col.name for col in client.list_collections()]
    if collection_name not in existing_names:
        raise ValueError(
            f"Collection '{collection_name}' does not exist in '{DEFAULT_CHROMA_DIR}'."
        )
    return client.get_collection(collection_name)


def embed_and_store(
    chunks,
    collection_name,
    chroma_dir: str | Path = DEFAULT_CHROMA_DIR,
    embedding_config: EmbeddingConfig = DEFAULT_EMBEDDING_CONFIG,
):
    """Embed chunks and add them to the selected model-specific collection."""
    embeddings = encode_chunks(chunks, embedding_config)
    return store_chunk_embeddings(
        chunks,
        embeddings,
        collection_name,
        chroma_dir=chroma_dir,
        embedding_config=embedding_config,
    )


def encode_chunks(
    chunks,
    embedding_config: EmbeddingConfig = DEFAULT_EMBEDDING_CONFIG,
):
    """Encode chunk text without opening or mutating the vector store."""
    model = get_embedding_model(
        embedding_config.model_name,
        embedding_config.model_version,
    )
    texts = [chunk["text"] for chunk in chunks]
    print(f"Embedding {len(chunks)} chunk(s)...")
    return model.encode(
        texts,
        batch_size=embedding_config.batch_size,
        normalize_embeddings=embedding_config.normalize_embeddings,
        show_progress_bar=True,
    )


def store_chunk_embeddings(
    chunks,
    embeddings,
    collection_name,
    chroma_dir: str | Path = DEFAULT_CHROMA_DIR,
    embedding_config: EmbeddingConfig = DEFAULT_EMBEDDING_CONFIG,
):
    """Store precomputed chunk embeddings in a model-specific collection."""
    if len(chunks) != len(embeddings):
        raise ValueError("Chunk and embedding counts must match.")

    texts = [chunk["text"] for chunk in chunks]
    client = get_chroma_client(chroma_dir)
    collection = get_or_create_collection(
        client,
        collection_name,
        embedding_config,
    )

    vector_ids = [str(chunk["chunk_id"]) for chunk in chunks]

    collection.add(
        ids=vector_ids,
        embeddings=[
            embedding.tolist()
            if hasattr(embedding, "tolist")
            else list(embedding)
            for embedding in embeddings
        ],
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
    embedding_config: EmbeddingConfig = DEFAULT_EMBEDDING_CONFIG,
) -> list[dict]:
    model = get_embedding_model(
        embedding_config.model_name,
        embedding_config.model_version,
    )
    query_embedding = model.encode(
        [query_text],
        batch_size=1,
        normalize_embeddings=embedding_config.normalize_embeddings,
        show_progress_bar=False,
    )[0].tolist()

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
    """Demo entry point — loads chunks from a JSON file and indexes them."""
    print(f"Loading chunks from {chunks_path}...")
    with open(chunks_path, encoding="utf-8") as f:
        chunks = json.load(f)

    client = get_chroma_client(DEFAULT_CHROMA_DIR)
    existing_names = [col.name for col in client.list_collections()]
    if COLLECTION_NAME in existing_names:
        client.delete_collection(COLLECTION_NAME)

    embed_and_store(chunks, COLLECTION_NAME)
    collection = client.get_collection(COLLECTION_NAME)
    print(f"Done. {collection.count()} chunks indexed.")
    return collection


if __name__ == "__main__":
    build_index()
