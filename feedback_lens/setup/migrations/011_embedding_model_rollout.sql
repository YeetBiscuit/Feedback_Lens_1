-- Preserve the model identity of every existing vector collection. This makes
-- the MiniLM read-only boundary durable even if all of a Unit's material
-- mappings are later removed.
INSERT INTO index_builds
    (unit_offering_id, vector_store_name, embedding_model,
     embedding_version, chunking_strategy, status, completed_at)
SELECT
    offering.unit_offering_id,
    mapping.vector_store_name,
    mapping.embedding_model,
    mapping.embedding_version,
    MIN(chunk.chunking_strategy),
    'active',
    CURRENT_TIMESTAMP
FROM chunk_embedding_map AS mapping
JOIN material_chunks AS chunk ON chunk.chunk_id = mapping.chunk_id
JOIN unit_materials AS material ON material.material_id = chunk.material_id
JOIN unit_offerings AS offering ON offering.legacy_unit_id = material.unit_id
WHERE NOT EXISTS (
    SELECT 1
    FROM index_builds AS existing
    WHERE existing.unit_offering_id = offering.unit_offering_id
      AND existing.vector_store_name = mapping.vector_store_name
)
GROUP BY
    offering.unit_offering_id,
    mapping.vector_store_name,
    mapping.embedding_model,
    mapping.embedding_version;

INSERT OR IGNORE INTO index_build_items
    (index_build_id, chunk_id, vector_id)
SELECT
    build.index_build_id,
    mapping.chunk_id,
    mapping.vector_id
FROM chunk_embedding_map AS mapping
JOIN material_chunks AS chunk ON chunk.chunk_id = mapping.chunk_id
JOIN unit_materials AS material ON material.material_id = chunk.material_id
JOIN unit_offerings AS offering ON offering.legacy_unit_id = material.unit_id
JOIN index_builds AS build
  ON build.unit_offering_id = offering.unit_offering_id
 AND build.vector_store_name = mapping.vector_store_name
 AND build.embedding_model = mapping.embedding_model
 AND build.embedding_version IS mapping.embedding_version;
