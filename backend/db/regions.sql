CREATE TABLE IF NOT EXISTS district_refresh_runs (
    district text NOT NULL, day date NOT NULL, finished_at timestamptz NOT NULL,
    added integer NOT NULL, removed integer NOT NULL, forecast_groups integer NOT NULL,
    PRIMARY KEY (district,day)
);
CREATE OR REPLACE VIEW district_input_months AS
SELECT split_part(complex_id,' ',2) AS district, max(month) AS month
FROM model_inputs GROUP BY 1;
