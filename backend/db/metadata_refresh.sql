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

CREATE TABLE IF NOT EXISTS complex_geocode_attempts (
    complex_id text PRIMARY KEY REFERENCES complexes(complex_id) ON DELETE CASCADE,
    address text NOT NULL,
    attempted_on date NOT NULL
);

CREATE TABLE IF NOT EXISTS complex_registrations (
    complex_id text PRIMARY KEY REFERENCES complexes(complex_id),
    refresh_day date NOT NULL,
    district text NOT NULL
);

CREATE TABLE IF NOT EXISTS complex_geocode_pending (
    complex_id text PRIMARY KEY REFERENCES complexes(complex_id) ON DELETE CASCADE
);
