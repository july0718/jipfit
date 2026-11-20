\pset pager off
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;

WITH districts(code, name) AS (VALUES
    ('11110', '종로구'), ('11140', '중구'), ('11170', '용산구'), ('11200', '성동구'),
    ('11215', '광진구'), ('11230', '동대문구'), ('11260', '중랑구'), ('11290', '성북구'),
    ('11305', '강북구'), ('11320', '도봉구'), ('11350', '노원구'), ('11380', '은평구'),
    ('11410', '서대문구'), ('11440', '마포구'), ('11470', '양천구'), ('11500', '강서구'),
    ('11530', '구로구'), ('11545', '금천구'), ('11560', '영등포구'), ('11590', '동작구'),
    ('11620', '관악구'), ('11650', '서초구'), ('11680', '강남구'), ('11710', '송파구'),
    ('11740', '강동구')
), sites AS (
    SELECT split_part(address, ' ', 2) AS district, count(*) AS complexes,
           count(*) FILTER (WHERE latitude IS NOT NULL AND longitude IS NOT NULL) AS positioned
    FROM complexes GROUP BY 1
), transactions AS (
    SELECT split_part(complex_id, ' ', 2) AS district, count(*) AS trade_rows,
           count(*) FILTER (WHERE NOT is_cancelled) AS active_trades
    FROM trades GROUP BY 1
), latest_input AS (
    SELECT m.district, m.month, count(*) AS forecast_groups
    FROM district_input_months m JOIN model_inputs i
      ON i.month = m.month AND split_part(i.complex_id, ' ', 2) = m.district
    GROUP BY m.district, m.month
), latest_run AS (
    SELECT DISTINCT ON (district) district, day, finished_at, forecast_groups
    FROM district_refresh_runs ORDER BY district, day DESC
)
SELECT d.name AS district, coalesce(s.complexes, 0) AS complexes,
       coalesce(t.trade_rows, 0) AS trade_rows, coalesce(t.active_trades, 0) AS active_trades,
       coalesce(i.forecast_groups, 0) AS forecast_groups, i.month AS input_month,
       coalesce(s.positioned, 0) AS positioned,
       coalesce(s.complexes - s.positioned, 0) AS unpositioned,
       r.day AS refresh_day, r.finished_at AT TIME ZONE 'Asia/Seoul' AS refreshed_kst,
       r.forecast_groups AS refreshed_groups,
       coalesce(r.day = (now() AT TIME ZONE 'Asia/Seoul')::date, false) AS refreshed_today
FROM districts d
LEFT JOIN sites s ON s.district = d.name
LEFT JOIN transactions t ON t.district = d.name
LEFT JOIN latest_input i ON i.district = d.name
LEFT JOIN latest_run r ON r.district = d.code
ORDER BY d.name;

SELECT address, string_agg(DISTINCT name, ', ' ORDER BY name) AS complexes,
       count(*) AS unpositioned_complexes
FROM complexes
WHERE latitude IS NULL OR longitude IS NULL
GROUP BY address
ORDER BY address;

COMMIT;
