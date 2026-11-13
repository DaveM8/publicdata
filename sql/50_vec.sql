-- Embeddings: one "card" per dataset and per map layer, plus chunks of
-- document text. Filled by `dgi-explore embed`.
--
-- Vectors are stored as real[] so this works before pgvector is installed.
-- Once it is (see ~/org/dgi-explore.org), `dgi-explore embed --index` adds a
-- vector(1024) column with an HNSW index and the search functions.

CREATE TABLE IF NOT EXISTS dgi_vec.item (
    item_id     text PRIMARY KEY,     -- 'ds:<dataset id>', 'layer:<table or sha/part/layer>', 'doc:<sha256>:<n>'
    kind        text NOT NULL,        -- dataset, layer, doc
    dataset_id  text,                 -- the dataset it belongs to (for docs and layers: one of them)
    sha256      text,
    title       text,
    body        text NOT NULL,        -- exactly the text that was embedded
    body_hash   text NOT NULL,        -- re-embed only when this changes
    tokens      int,
    model       text,
    emb         real[],               -- the embedding; NULL until fetched
    embedded_at timestamptz
);
CREATE INDEX IF NOT EXISTS item_dataset ON dgi_vec.item (dataset_id);
CREATE INDEX IF NOT EXISTS item_pending ON dgi_vec.item (kind) WHERE emb IS NULL;

-- The 20 nearest datasets by meaning (cosine similarity of the dataset cards).
CREATE TABLE IF NOT EXISTS dgi_vec.similar (
    dataset_id  text NOT NULL,
    rank        int NOT NULL,
    other_id    text NOT NULL,
    score       real,
    PRIMARY KEY (dataset_id, rank)
);
