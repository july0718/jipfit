"""Replay the two districts' live inputs against trades, a pinned REB snapshot and the API."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
from urllib.parse import urlencode
from urllib.request import urlopen

import numpy as np
import pandas as pd
from database import connect
from districts import DISTRICTS
from features import build_inputs
from inference import load_model, predict_growth

ROOT = Path(__file__).resolve().parent


def verify(code, model, indexes, rates):
    name = DISTRICTS[code]
    with connect() as db:
        rows = db.execute("""
            SELECT m.*, a.representative_area FROM model_inputs m JOIN area_groups a USING(complex_id, area_group)
            JOIN district_input_months d ON d.district=split_part(m.complex_id,' ',2) AND d.month=m.month
            WHERE d.district=%s ORDER BY complex_id, area_group
        """, (name,)).fetchall()
        if not rows:
            raise ValueError(f"No published inputs for {name}")
        stored = pd.DataFrame(rows)
        anchor = pd.Period(stored.month.unique().item(), freq="M")
        trades = db.execute("""
            SELECT t.*,c.built FROM trades t JOIN complexes c USING(complex_id)
            WHERE split_part(t.complex_id,' ',2)=%s AND NOT t.is_cancelled
              AND contract_date >= %s AND contract_date < %s
        """, (name, (anchor-19).start_time.date(), (anchor-1).start_time.date())).fetchall()
    if str(anchor) != "2026-11":
        raise ValueError(f"{anchor}: this historical index snapshot verifies the November 2026 release only")
    with TemporaryDirectory() as tmp:
        macro = rates.copy()
        macro["gwangjin_apt_index"] = indexes[code]
        path = Path(tmp)/"macro.csv"
        macro.to_csv(path)
        rebuilt = build_inputs(pd.DataFrame(trades), anchor, path)
    keys = ["complex_id", "area_group"]
    left, right = (f.set_index(keys).sort_index() for f in (stored, rebuilt))
    pd.testing.assert_index_equal(left.index, right.index)
    for col in [name for name in model.features if name not in keys] + ["representative_area"]:
        np.testing.assert_allclose(left[col].astype(float), right[col].astype(float), rtol=1e-8, atol=1e-8,
                                   equal_nan=True, err_msg=f"{name} {anchor}: input mismatch for {col}")
    growth = predict_growth(model, left.reset_index().to_dict("records"))
    if not np.isfinite(growth).all():
        raise ValueError("Non-finite model output")
    print(f"{name} {anchor}: {len(left)} reconstructed inputs matched")
    for j,h in enumerate((3,6,12)):
        values = 100*np.expm1(growth[:,j])
        print(f"{h}m: negative={sum(values<0)}/{len(values)}, median={np.median(values):+.2f}%")
    # Check examples across the distribution against the independently rebuilt reference price.
    for i in np.unique(np.argsort(growth[:,2])[[0,len(left)//2,-1]]):
        cid, group = left.index[i]
        query = urlencode(dict(complex_id=cid, area_group=int(group)))
        with urlopen("http://api:8000/api/predict?"+query, timeout=30) as response:
            api = json.load(response)
        ref = np.exp(right.iloc[i].log_current) * right.iloc[i].representative_area
        assert api["as_of"] == str(anchor) and [f["months"] for f in api["forecasts"]] == [3,6,12]
        np.testing.assert_allclose(api["reference_price_manwon"], ref)
        np.testing.assert_allclose([f["return_percent"] for f in api["forecasts"]],100*np.expm1(growth[i]))
        np.testing.assert_allclose([f["price_manwon"] for f in api["forecasts"]],ref*np.exp(growth[i]))
        print("PASS", cid, group, "returns %", np.round(100*np.expm1(growth[i]),2))


if __name__ == "__main__":
    model = load_model()
    indexes = pd.read_csv(ROOT/"seoul_audit_indexes.csv",index_col="reference_month")
    rates = pd.read_csv(ROOT/"macro_raw.csv",index_col="reference_month")
    for code in ("11680","11650"):
        verify(code, model, indexes, rates)
