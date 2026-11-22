\set ON_ERROR_STOP on
BEGIN;
LOCK TABLE complexes, area_groups, trades, model_inputs IN SHARE ROW EXCLUSIVE MODE;
ALTER TABLE model_inputs
    ALTER COLUMN momentum6 DROP NOT NULL,
    ALTER COLUMN momentum12 DROP NOT NULL,
    ALTER COLUMN volatility DROP NOT NULL;
CREATE TEMP TABLE district_complexes (LIKE complexes) ON COMMIT DROP;
CREATE TEMP TABLE district_groups (LIKE area_groups) ON COMMIT DROP;
CREATE TEMP TABLE district_inputs (LIKE model_inputs) ON COMMIT DROP;
CREATE TEMP TABLE district_trades ON COMMIT DROP AS
    SELECT complex_id, area_group, contract_date, area, floor, price_manwon FROM trades WITH NO DATA;
\copy district_complexes(complex_id,name,address,built,latitude,longitude) FROM 'gwangjin/complexes.csv' CSV HEADER
\copy district_groups FROM 'gwangjin/groups.csv' CSV HEADER
\copy district_inputs FROM 'gwangjin/inputs.csv' CSV HEADER
\copy district_trades FROM 'gwangjin/trades.csv' CSV HEADER
INSERT INTO complexes(complex_id,name,address,built,latitude,longitude)
SELECT complex_id,name,address,built,latitude,longitude FROM district_complexes
ON CONFLICT (complex_id) DO UPDATE SET latitude=excluded.latitude, longitude=excluded.longitude;
INSERT INTO area_groups SELECT * FROM district_groups ON CONFLICT DO NOTHING;
INSERT INTO model_inputs SELECT * FROM district_inputs ON CONFLICT DO NOTHING;
INSERT INTO trades(complex_id, area_group, contract_date, area, floor, price_manwon)
    SELECT * FROM district_trades
    EXCEPT ALL SELECT complex_id, area_group, contract_date, area, floor, price_manwon FROM trades;
COMMIT;
