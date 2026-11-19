BEGIN;
CREATE TABLE complexes (
    complex_id text PRIMARY KEY,
    name text NOT NULL,
    address text NOT NULL,
    built integer NOT NULL,
    latitude double precision,
    longitude double precision
);
CREATE TABLE area_groups (
    complex_id text REFERENCES complexes,
    area_group integer,
    representative_area numeric NOT NULL,
    PRIMARY KEY (complex_id, area_group)
);
CREATE TABLE trades (
    trade_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    complex_id text NOT NULL,
    area_group integer NOT NULL,
    contract_date date NOT NULL,
    area numeric NOT NULL,
    floor integer,
    price_manwon numeric NOT NULL,
    FOREIGN KEY (complex_id, area_group) REFERENCES area_groups
);
CREATE TABLE model_inputs (
    complex_id text,
    area_group integer,
    month text,
    built double precision NOT NULL,
    age double precision NOT NULL,
    log_current double precision NOT NULL,
    count6 double precision NOT NULL,
    count12 double precision NOT NULL,
    momentum6 double precision NOT NULL,
    momentum12 double precision NOT NULL,
    volatility double precision NOT NULL,
    months_since_trade double precision NOT NULL,
    floor_known double precision NOT NULL,
    time_index double precision NOT NULL,
    month_sin double precision NOT NULL,
    month_cos double precision NOT NULL,
    policy_rate double precision NOT NULL,
    policy_rate_change_3m double precision NOT NULL,
    gwangjin_apt_index_change_3m double precision NOT NULL,
    gwangjin_apt_index_change_12m double precision NOT NULL,
    PRIMARY KEY (complex_id, area_group, month),
    FOREIGN KEY (complex_id, area_group) REFERENCES area_groups
);
INSERT INTO complexes VALUES ('서울특별시 광진구 구의동|631-1|현대프라임','현대프라임','서울특별시 광진구 구의동 631-1',1997,37.5374027,127.0976329);
INSERT INTO area_groups VALUES ('서울특별시 광진구 구의동|631-1|현대프라임',60,59.82);
\copy trades(complex_id, area_group, contract_date, area, floor, price_manwon) FROM 'sample/trades.csv' WITH (FORMAT csv, HEADER true)
\copy model_inputs FROM 'sample/model_inputs.csv' WITH (FORMAT csv, HEADER true)
COMMIT;
