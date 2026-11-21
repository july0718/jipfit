ALTER TABLE complexes ADD COLUMN IF NOT EXISTS existence_status text;
ALTER TABLE complexes ALTER COLUMN existence_status SET DEFAULT 'unconfirmed';

CREATE TABLE IF NOT EXISTS complex_metadata_runs (
    batch text PRIMARY KEY,
    checksum text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS complex_metadata_evidence (
    batch text NOT NULL REFERENCES complex_metadata_runs(batch),
    complex_id text NOT NULL REFERENCES complexes(complex_id),
    evidence text NOT NULL,
    changes jsonb NOT NULL,
    PRIMARY KEY (batch, complex_id)
);
