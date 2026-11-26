from math import expm1, isfinite

from inference import HORIZONS, load_model, predict_growth


def load_home_rows(db, *, district=None, include_sort=True, live_only=False, model=None):
    metadata = "c.name, c.address, c.built, c.latitude, c.longitude,"
    scope = (district, district)
    rows = db.execute(f"""
        SELECT c.complex_id, {metadata}
               a.area_group, a.representative_area,
               (m.complex_id IS NOT NULL) AS predictable,
               COALESCE(ls.existence = 'demolished', FALSE) AS demolition_confirmed
        FROM complexes c JOIN area_groups a USING (complex_id)
        LEFT JOIN complex_lifecycle_state ls USING (complex_id)
        LEFT JOIN district_input_months d ON d.district=split_part(c.complex_id,' ',2)
        LEFT JOIN model_inputs m ON m.complex_id=c.complex_id AND m.area_group=a.area_group AND m.month=d.month
        WHERE (%s::text IS NULL OR split_part(c.complex_id, ' ', 2) = %s)
        ORDER BY c.name, c.complex_id, a.area_group
    """, scope).fetchall()
    for row in rows:
        if isinstance(row, dict):
            row["demolition_confirmed"] = bool(row.get("demolition_confirmed", False))
    if not include_sort:
        return rows
    stats = {r["complex_id"]: r for r in db.execute("""
        SELECT complex_id, count(*) AS trade_count, max(contract_date) AS last_trade
        FROM trades
        WHERE NOT is_cancelled AND (%s::text IS NULL OR split_part(complex_id, ' ', 2) = %s)
        GROUP BY complex_id
    """, scope)}
    last_prices = {(r["complex_id"], r["area_group"]): r["price_manwon"] for r in db.execute("""
        SELECT DISTINCT ON (complex_id, area_group) complex_id, area_group, price_manwon
        FROM trades
        WHERE NOT is_cancelled AND (%s::text IS NULL OR split_part(complex_id, ' ', 2) = %s)
        ORDER BY complex_id, area_group, contract_date DESC, trade_id DESC
    """, scope)}
    inputs = db.execute("""
        SELECT DISTINCT ON (complex_id) m.* FROM model_inputs m
        JOIN district_input_months d ON d.district=split_part(m.complex_id,' ',2) AND m.month=d.month
        WHERE (%s::text IS NULL OR d.district = %s)
        ORDER BY complex_id, area_group
    """, scope).fetchall()
    returns = {}
    if inputs:
        for row, rates in zip(inputs, predict_growth(model if model is not None else load_model(), inputs), strict=True):
            returns[row["complex_id"]] = {
                horizon: 100 * expm1(float(rate)) for horizon, rate in zip(HORIZONS, rates, strict=True)
                if isfinite(float(rate))
            }
    for row in rows:
        cid = row["complex_id"]
        key = cid, row["area_group"]
        price = last_prices.get(key)
        row["marker_price_manwon"] = price if price is not None and isfinite(float(price)) else None
        stat = stats.get(cid, {})
        row.update(trade_count=stat.get("trade_count", 0), last_trade=stat.get("last_trade"),
                   sort_returns=returns.get(cid, {}))
    return rows
