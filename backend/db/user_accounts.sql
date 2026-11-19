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
