CREATE TABLE IF NOT EXISTS trade_revisions (
    trade_id bigint PRIMARY KEY,
    complex_id text NOT NULL,
    area_group integer NOT NULL,
    contract_date date NOT NULL,
    area numeric NOT NULL,
    floor integer,
    price_manwon numeric NOT NULL,
    is_cancelled boolean NOT NULL,
    superseded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
