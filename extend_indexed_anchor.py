"""Evaluate and export indexed 6- and 12-month forecasts."""
from pathlib import Path
import numpy as np
import pandas as pd
from indexed_anchor import price_anchors, KEYS

ROOT = Path(__file__).resolve().parents[1]


def main():
    original = ROOT/'realestate_model/results'
    previous = ROOT.parent/'jipfit_update'
    output = ROOT/'indexed_anchor'
    anchors = price_anchors(pd.read_csv(original/'clean_transactions.csv', usecols=KEYS+['ppm']),
                            ROOT/'external_data/macro_raw.csv')
    raw = pd.read_csv(original/'backtest_predictions.csv')
    linked = pd.read_csv(previous/'input/backtest.csv').query("district=='광진구' and horizon in (6,12)")
    raw = raw.rename(columns={'horizon_months':'horizon'})
    rows = linked.merge(raw[KEYS+['horizon','future','current']], on=KEYS+['horizon'],
                        validate='one_to_one').merge(anchors, on=KEYS, validate='many_to_one')
    if len(rows)!=len(linked) or not np.allclose(rows.current,rows.reference_old):
        raise ValueError('Missing or inconsistent comparison rows')
    rows['actual_price'] = rows.future*rows.area_group
    for model, reference in [('before','reference_old'),('after','reference_new')]:
        price = rows[reference]*(1+rows.selected/100)*rows.area_group
        rows[model+'_error'] = (price-rows.actual_price).abs()
        rows[model+'_return_error'] = 100*rows[model+'_error']/(rows.reference_old*rows.area_group)
    scores = []
    for h,g in rows.groupby('horizon'):
        month = g.groupby('month')[['before_error','after_error']].mean()
        scores.append(dict(horizon=h,n=len(g),start=g.month.min(),end=g.month.max(),
                           before_mae=g.before_error.mean(),after_mae=g.after_error.mean(),
                           before_return_mae=g.before_return_error.mean(),after_return_mae=g.after_return_error.mean(),
                           improved_months=(month.after_error<month.before_error).sum(),months=len(month)))
    latest = pd.read_csv(previous/'results/forecasts.csv').query("district=='광진구' and horizon in (6,12)")
    ready = latest.merge(anchors,on=KEYS,validate='many_to_one')
    if len(ready)!=len(latest):
        raise ValueError('Missing latest anchors')
    ready['indexed_reference_manwon'] = ready.current_reference_manwon*ready.reference_new/ready.reference_old
    ready['indexed_price_manwon'] = ready.indexed_reference_manwon*(1+ready.final_return/100)
    if not np.isfinite(ready.indexed_price_manwon).all() or not ready.indexed_price_manwon.gt(0).all():
        raise ValueError('Invalid price forecast')
    for name,data in [('long_backtest',rows),('long_metrics',pd.DataFrame(scores)),('long_forecasts',ready)]:
        data.to_csv(output/f'{name}.csv',index=False,encoding='utf-8-sig')
    print(pd.DataFrame(scores).to_string(index=False))


if __name__=='__main__':
    main()
