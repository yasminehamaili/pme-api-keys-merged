-- Idempotent: safe to run on every deploy (python migrate.py).

CREATE TABLE IF NOT EXISTS customers (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email         text        NOT NULL UNIQUE,            -- stored lower-case
    password_hash text        NOT NULL,                   -- scrypt, never plain text
    daily_limit   integer     NOT NULL DEFAULT 1000 CHECK (daily_limit >= 0),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS api_keys (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id  uuid        NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    name         text        NOT NULL CHECK (char_length(name) BETWEEN 1 AND 60),
    key_prefix   text        NOT NULL,                    -- e.g. "pme_a1b2c3d4" (safe to show)
    key_hash     text        NOT NULL,                    -- HMAC-SHA256(pepper, key); never sent out
    is_active    boolean     NOT NULL DEFAULT true,
    last_used_at timestamptz,
    revoked_at   timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- Every API request looks a key up by its hash: must be indexed and unique.
CREATE UNIQUE INDEX IF NOT EXISTS api_keys_key_hash_key ON api_keys (key_hash);
CREATE INDEX IF NOT EXISTS api_keys_customer_idx ON api_keys (customer_id, created_at DESC);

-- One row per key per UTC day.
CREATE TABLE IF NOT EXISTS usage_log (
    api_key_id    uuid   NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    day           date   NOT NULL,
    request_count bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (api_key_id, day)
);
