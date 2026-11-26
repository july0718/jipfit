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

CREATE TABLE IF NOT EXISTS kapt_metadata_cache (
    batch TEXT NOT NULL,
    kapt_code TEXT NOT NULL,
    payload JSONB NOT NULL,
    saved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (batch, kapt_code)
);

CREATE TABLE IF NOT EXISTS kapt_metadata_pending (
    batch TEXT NOT NULL,
    district TEXT NOT NULL,
    kapt_code TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT NOT NULL DEFAULT 'EMPTY_DETAIL',
    attempts INTEGER NOT NULL DEFAULT 1,
    last_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    next_retry_at TIMESTAMPTZ,
    PRIMARY KEY (batch, kapt_code)
);

-- Source ID decisions are explicit. A self mapping confirms a separate complex.
CREATE TABLE IF NOT EXISTS refresh_identity_decisions (
    district text NOT NULL, source_id text NOT NULL, target_id text NOT NULL,
    PRIMARY KEY (district, source_id)
);
CREATE TABLE IF NOT EXISTS refresh_identity_pending (
    district text NOT NULL, month date NOT NULL, source_id text NOT NULL,
    candidates text[] NOT NULL, source_rows jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    PRIMARY KEY (district, month, source_id)
);
CREATE TABLE IF NOT EXISTS refresh_missing_trades (
    trade_id bigint PRIMARY KEY REFERENCES trades(trade_id) ON DELETE CASCADE,
    district text NOT NULL, month date NOT NULL,
    first_seen date NOT NULL, last_seen date NOT NULL,
    approved boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS refresh_revision_pending (
    trade_id bigint PRIMARY KEY REFERENCES refresh_missing_trades(trade_id) ON DELETE CASCADE,
    district text NOT NULL,
    month date NOT NULL,
    complex_id text NOT NULL,
    candidate_row jsonb NOT NULL,
    first_seen date NOT NULL,
    last_seen date NOT NULL
);
CREATE TABLE IF NOT EXISTS refresh_runs (
    day date PRIMARY KEY,
    finished_at timestamptz NOT NULL,
    added integer NOT NULL,
    removed integer NOT NULL,
    forecast_groups integer NOT NULL,
    slot_start timestamptz
);
CREATE TABLE IF NOT EXISTS daily_trade_checkpoints (
    district text NOT NULL, day date NOT NULL, added integer NOT NULL, removed integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    slot_start timestamptz,
    PRIMARY KEY (district, day)
);
CREATE TABLE IF NOT EXISTS refresh_worker_attempts (
    worker text NOT NULL,
    slot_start timestamptz NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'deferred',
    next_attempt_at timestamptz,
    lease_until timestamptz,
    claim_token text,
    PRIMARY KEY (worker, slot_start)
);
CREATE TABLE IF NOT EXISTS refresh_publish_pending (
    district text NOT NULL, day date NOT NULL, added integer NOT NULL, removed integer NOT NULL,
    PRIMARY KEY (district, day)
);
CREATE TABLE IF NOT EXISTS historical_rechecks (
    district text NOT NULL, month date NOT NULL, checked_on date NOT NULL,
    status text NOT NULL DEFAULT 'ok', last_error text,
    PRIMARY KEY (district, month)
);
-- Approved months whose replay failed; skipped until next_retry_on so one bad month cannot block a district.
CREATE TABLE IF NOT EXISTS refresh_approval_failures (
    district text NOT NULL, month date NOT NULL,
    attempts integer NOT NULL DEFAULT 1,
    last_error text NOT NULL,
    last_attempt_on date NOT NULL,
    next_retry_on date NOT NULL,
    PRIMARY KEY (district, month)
);
CREATE TABLE IF NOT EXISTS kapt_verified_links (
    district text NOT NULL, kapt_code text NOT NULL,
    complex_id text NOT NULL REFERENCES complexes(complex_id),
    PRIMARY KEY (district, kapt_code), UNIQUE (district, complex_id)
);

CREATE TABLE IF NOT EXISTS complex_metadata_candidates (
    candidate_id text PRIMARY KEY,
    district text NOT NULL,
    kapt_code text NOT NULL DEFAULT '',
    complex_id text REFERENCES complexes(complex_id),
    field text NOT NULL,
    expected_value text,
    proposed_value text,
    source text NOT NULL,
    first_month text NOT NULL,
    last_month text NOT NULL,
    first_observed_on date NOT NULL DEFAULT CURRENT_DATE,
    last_observed_on date NOT NULL DEFAULT CURRENT_DATE,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'held', 'superseded', 'resolved', 'rejected')),
    reason text NOT NULL,
    candidate_ids text[] NOT NULL DEFAULT '{}',
    evidence text
);

CREATE TABLE IF NOT EXISTS complex_metadata_candidate_observations (
    candidate_id text NOT NULL REFERENCES complex_metadata_candidates(candidate_id) ON DELETE CASCADE,
    batch text NOT NULL,
    observed_on date NOT NULL DEFAULT CURRENT_DATE,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (candidate_id, batch)
);

CREATE TABLE IF NOT EXISTS complex_lifecycle_results (
    result_key text PRIMARY KEY,
    complex_id text NOT NULL REFERENCES complexes(complex_id),
    existence text NOT NULL CHECK (existence IN ('present', 'demolished', 'pending')),
    as_of date NOT NULL,
    verifier_version text NOT NULL,
    evidence text NOT NULL,
    payload_digest text NOT NULL,
    hold_code text,
    correction_of text,
    successor_id text,
    successor_status text,
    effective_date date,
    effective_precision text CHECK (effective_precision IS NULL OR effective_precision IN ('day', 'month', 'year')),
    applied_status text NOT NULL DEFAULT 'applied'
        CHECK (applied_status IN ('applied', 'pending_recorded', 'held_conflict', 'stale_ignored')),
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (
        (effective_date IS NULL AND effective_precision IS NULL)
        OR (effective_date IS NOT NULL AND effective_precision IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS complex_lifecycle_state (
    complex_id text PRIMARY KEY REFERENCES complexes(complex_id),
    result_key text REFERENCES complex_lifecycle_results(result_key),
    existence text CHECK (existence IS NULL OR existence IN ('present', 'demolished')),
    as_of date,
    verifier_version text,
    evidence text,
    effective_date date,
    effective_precision text CHECK (effective_precision IS NULL OR effective_precision IN ('day', 'month', 'year')),
    pending_result_key text REFERENCES complex_lifecycle_results(result_key),
    pending_hold_code text,
    pending_as_of date,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS complex_lifecycle_quarantine (
    quarantine_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    result_key text NOT NULL DEFAULT '',
    complex_id text NOT NULL DEFAULT '',
    existence text,
    reason text NOT NULL,
    raw_payload jsonb NOT NULL,
    quarantined_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

DO $$
DECLARE constraint_name text;
BEGIN
    SELECT conname INTO constraint_name FROM pg_constraint
    WHERE conrelid = 'complex_lifecycle_quarantine'::regclass
      AND pg_get_constraintdef(oid) = 'UNIQUE (result_key, complex_id, reason, raw_payload)';
    IF constraint_name IS NOT NULL THEN
        EXECUTE 'ALTER TABLE complex_lifecycle_quarantine DROP CONSTRAINT ' || quote_ident(constraint_name);
    END IF;
END $$;
ALTER TABLE complex_lifecycle_quarantine ADD COLUMN IF NOT EXISTS payload_digest text;
UPDATE complex_lifecycle_quarantine
SET payload_digest = encode(sha256(convert_to(jsonb_build_array(result_key, complex_id, reason, raw_payload)::text, 'UTF8')), 'hex')
WHERE payload_digest IS NULL;
ALTER TABLE complex_lifecycle_quarantine ALTER COLUMN payload_digest SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS complex_lifecycle_quarantine_digest_idx
ON complex_lifecycle_quarantine (payload_digest);

-- Contracts on or after exclude_from stay in trades but are left out of model training and forecast inputs.
CREATE TABLE IF NOT EXISTS complex_training_exclusions (
    complex_id text PRIMARY KEY REFERENCES complexes(complex_id),
    exclude_from date NOT NULL,
    reason text NOT NULL
);
-- Removed complex IDs that daily registration must not create again (no FK: the complex row is gone).
CREATE TABLE IF NOT EXISTS complex_exclusions (
    complex_id text PRIMARY KEY,
    reason text NOT NULL,
    excluded_on date NOT NULL
);
-- Read-only convenience copy of complex_lifecycle_state; restored backups already have the column.
-- The catalog check avoids taking an exclusive lock on complexes at every startup.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'complexes' AND column_name = 'existence_status'
    ) THEN
        ALTER TABLE complexes ADD COLUMN IF NOT EXISTS existence_status text;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS refresh_identity_period_decisions (
    decision_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    district text NOT NULL,
    source_id text NOT NULL,
    built integer,
    valid_from date,
    valid_to date,
    target_id text NOT NULL,
    target_name text,
    target_address text,
    target_built integer,
    evidence text NOT NULL DEFAULT 'verified_split',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK (valid_from IS NULL OR valid_to IS NULL OR valid_from <= valid_to)
);

DROP TRIGGER IF EXISTS trg_identity_period_decision_overlap ON refresh_identity_period_decisions;
DROP FUNCTION IF EXISTS check_identity_period_decision_overlap();
CREATE EXTENSION IF NOT EXISTS btree_gist;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'refresh_identity_period_decisions'::regclass
                     AND conname = 'refresh_identity_period_no_overlap') THEN
        ALTER TABLE refresh_identity_period_decisions ADD CONSTRAINT refresh_identity_period_no_overlap
        EXCLUDE USING gist (
            district WITH =, source_id WITH =, (COALESCE(built, -1)) WITH =,
            (daterange(valid_from, valid_to, '[]')) WITH &&
        );
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS daily_trade_month_checkpoints (
    district text NOT NULL,
    slot_start timestamptz NOT NULL,
    month date NOT NULL,
    day date NOT NULL,
    added integer NOT NULL,
    removed integer NOT NULL,
    reassigned integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (district, slot_start, month)
);

CREATE TABLE IF NOT EXISTS monthly_refresh_stages (
    month text NOT NULL,
    district text NOT NULL,
    stage text NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'completed', 'awaiting_review', 'skipped_unconfigured', 'deferred', 'exhausted')),
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    slot_start timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    next_retry_at timestamptz,
    PRIMARY KEY (month, district, stage)
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'complex_geocode_attempts' AND column_name = 'last_error_at'
    ) THEN
        ALTER TABLE complex_geocode_attempts ADD COLUMN last_error_at timestamptz;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'historical_rechecks' AND column_name = 'status'
    ) THEN
        ALTER TABLE historical_rechecks
            ADD COLUMN status text NOT NULL DEFAULT 'ok',
            ADD COLUMN last_error text;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'daily_trade_checkpoints' AND column_name = 'created_at'
    ) THEN
        ALTER TABLE daily_trade_checkpoints
            ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT clock_timestamp();
    END IF;
    ALTER TABLE IF EXISTS district_refresh_runs ADD COLUMN IF NOT EXISTS slot_start timestamptz;
    ALTER TABLE IF EXISTS refresh_runs ADD COLUMN IF NOT EXISTS slot_start timestamptz;
    ALTER TABLE IF EXISTS daily_trade_checkpoints ADD COLUMN IF NOT EXISTS slot_start timestamptz;
    ALTER TABLE IF EXISTS refresh_worker_attempts ADD COLUMN IF NOT EXISTS lease_until timestamptz;
    ALTER TABLE IF EXISTS refresh_worker_attempts ADD COLUMN IF NOT EXISTS claim_token text;
    ALTER TABLE IF EXISTS trade_revisions ADD COLUMN IF NOT EXISTS reason text NOT NULL DEFAULT 'unspecified';
    ALTER TABLE IF EXISTS complex_metadata_evidence ADD COLUMN IF NOT EXISTS candidate_id text;
    ALTER TABLE IF EXISTS complex_metadata_evidence ADD COLUMN IF NOT EXISTS before_values jsonb;
    ALTER TABLE complex_metadata_evidence ADD COLUMN IF NOT EXISTS candidate_ids text[];
    ALTER TABLE complex_metadata_evidence ADD COLUMN IF NOT EXISTS field_evidence jsonb;
END $$;

UPDATE refresh_worker_attempts
SET status = 'deferred', lease_until = NULL, claim_token = NULL
WHERE status = 'running' AND next_attempt_at IS NOT NULL;

WITH migrated_checkpoints AS (
    UPDATE daily_trade_checkpoints c
    SET slot_start = c.created_at
    WHERE c.slot_start IS NULL
    RETURNING c.district, c.day, c.added, c.removed, c.created_at
)
INSERT INTO refresh_publish_pending(district, day, added, removed)
SELECT m.district, m.day, m.added, m.removed
FROM migrated_checkpoints m
WHERE (m.added <> 0 OR m.removed <> 0)
  AND NOT EXISTS (
      SELECT 1 FROM district_refresh_runs r
      WHERE r.district = m.district AND r.day = m.day AND r.finished_at >= m.created_at
  )
ON CONFLICT (district, day) DO UPDATE
SET added = refresh_publish_pending.added + EXCLUDED.added,
    removed = refresh_publish_pending.removed + EXCLUDED.removed;

UPDATE complexes c SET latitude=NULL, longitude=NULL
FROM complex_geocode_pending p
WHERE c.complex_id=p.complex_id
  AND (c.latitude IS NOT NULL OR c.longitude IS NOT NULL);
