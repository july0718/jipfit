CREATE TABLE IF NOT EXISTS app_users (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username text NOT NULL UNIQUE,
    salt bytea NOT NULL,
    password_hash bytea NOT NULL,
    failed_attempts integer NOT NULL DEFAULT 0,
    last_failure timestamptz
);
CREATE TABLE IF NOT EXISTS app_sessions (
    token_hash bytea PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS favorites (
    user_id bigint NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    complex_id text NOT NULL,
    area_group integer NOT NULL,
    PRIMARY KEY (user_id, complex_id, area_group),
    FOREIGN KEY (complex_id, area_group) REFERENCES area_groups(complex_id, area_group)
);

CREATE TABLE IF NOT EXISTS saved_comparisons (
    user_id bigint REFERENCES app_users ON DELETE CASCADE,
    left_id text NOT NULL, left_area integer NOT NULL,
    right_id text NOT NULL, right_area integer NOT NULL,
    months integer NOT NULL CHECK (months IN (3,6,12)),
    note text NOT NULL DEFAULT '',
    PRIMARY KEY (user_id,left_id,left_area,right_id,right_area,months),
    CHECK (left_id <> right_id),
    FOREIGN KEY (left_id,left_area) REFERENCES area_groups,
    FOREIGN KEY (right_id,right_area) REFERENCES area_groups
);
CREATE TABLE IF NOT EXISTS complex_notes (
    user_id bigint REFERENCES app_users ON DELETE CASCADE,
    complex_id text REFERENCES complexes,
    note text NOT NULL,
    PRIMARY KEY (user_id,complex_id)
);
ALTER TABLE favorites ADD COLUMN IF NOT EXISTS seen_trade bigint;
ALTER TABLE favorites ADD COLUMN IF NOT EXISTS seen_month text;
CREATE INDEX IF NOT EXISTS trades_group_id_idx ON trades(complex_id,area_group,trade_id DESC);
UPDATE favorites f SET
    seen_trade=COALESCE((SELECT max(trade_id) FROM trades t WHERE NOT t.is_cancelled AND
        (t.complex_id,t.area_group)=(f.complex_id,f.area_group)),0),
    seen_month=COALESCE((SELECT max(month) FROM model_inputs m WHERE
        (m.complex_id,m.area_group)=(f.complex_id,f.area_group)),'')
WHERE seen_trade IS NULL;
