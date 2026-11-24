"""Evaluate publication-aware price anchors against matched apartment forecasts."""
import argparse
from pathlib import Path
import sys
import numpy as np
import pandas as pd
from macro_features import load_monthly_macro

ROOT = Path(__file__).resolve().parents[1]
KEYS = ['complex_id', 'area_group', 'month']


def price_anchors(trades, macro_path):
    months = pd.period_range(trades.month.min(), trades.month.max(), freq='M')
    macro, _ = load_monthly_macro(macro_path, months)
    releases = pd.read_csv(macro_path).set_index('reference_month')
    index = releases.gwangjin_apt_index.reindex(months.astype(str)).to_numpy()
    public = pd.to_datetime(releases.gwangjin_apt_index_available_at.reindex(months.astype(str))).to_numpy()
    anchor_values = macro.gwangjin_apt_index.to_numpy()
    observed = months.to_timestamp().to_numpy()
    monthly = trades.groupby(KEYS).ppm.median()
    out = []
    for (cid, area), group in monthly.groupby(level=[0,1]):
        prices = group.droplevel([0,1]).reindex(months.astype(str))
        known = prices.shift(2).rolling(6,min_periods=1).median().to_numpy()
        last_trade = np.maximum.accumulate(np.where(prices.shift(2).notna(), np.arange(len(months))-2, -1000))
        for i in np.flatnonzero(known>0):
            past = np.arange(max(0,i-7),i-1)
            values = prices.to_numpy()[past]
            has_index = np.isfinite(index[past]) & (public[past]<=observed[i])
            adjusted = values.copy()
            if np.isfinite(anchor_values[i]):
                adjusted[has_index] *= anchor_values[i]/index[past][has_index]
            out.append((cid,area,str(months[i]),known[i],np.nanmedian(adjusted),i-last_trade[i]))
    return pd.DataFrame(out,columns=KEYS+['reference_old','reference_new','trade_age'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, default=ROOT/'indexed_anchor')
    args = parser.parse_args()
    root = args.project
    args.output.mkdir(parents=True, exist_ok=True)
    original = root/'realestate_model/results'
    trades = pd.read_csv(original/'clean_transactions.csv',usecols=KEYS+['ppm'])
    # Bootstrap from the previous app release, which carries the deployed baseline.
    previous = root.parent/'jipfit_update'
    sys.path.insert(0, str(previous/'model'))
    from calibrate import prepare, corrections
    existing = pd.read_csv(previous/'input/backtest.csv').query("district=='광진구' and horizon==3")
    growth = pd.read_csv(previous/'input/regime_inputs.csv')
    existing = prepare(existing,growth)
    existing['residual'] = existing.actual-existing.selected
    existing['baseline_return'] = existing.selected+corrections(existing,existing)
    anchors = price_anchors(trades,root/'external_data/macro_raw.csv')
    raw = pd.read_csv(original/'backtest_predictions.csv').query('horizon_months==3')
    rows = raw.merge(existing[KEYS+['baseline_return']],on=KEYS,validate='one_to_one').merge(anchors,on=KEYS,validate='one_to_one')
    assert len(rows)==len(raw) and np.allclose(rows.current,rows.reference_old)
    # Both predictions retain the same learned growth multiplier and use the same actual future price.
    rows['base_ppm'] = rows.current*(1+rows.baseline_return/100)
    rows['indexed_ppm'] = rows.reference_new*(1+rows.baseline_return/100)
    # Use the nominal area group to express both predictions and the target in manwon.
    area = pd.read_csv(original/'forecast_3m.csv').representative_area
    assert area.gt(0).all()
    rows['base_error_pp'] = 100 * (rows.future - rows.base_ppm).abs() / rows.current
    rows['indexed_error_pp'] = 100 * (rows.future - rows.indexed_ppm).abs() / rows.current
    rows['trade_band'] = np.where(rows.trade_age<=2,'2 months','3-7 months')
    rows['actual_manwon'] = rows.future*rows.area_group
    rows['base_manwon'] = rows.base_ppm*rows.area_group
    rows['indexed_manwon'] = rows.indexed_ppm*rows.area_group
    rows['base_error'] = (rows.actual_manwon-rows.base_manwon).abs()
    rows['indexed_error'] = (rows.actual_manwon-rows.indexed_manwon).abs()
    summary=[]
    for label,group in [('all',rows),*rows.groupby('trade_band')]:
        summary.append(dict(group=label,n=len(group),base_mae=group.base_error.mean(),indexed_mae=group.indexed_error.mean(),
                            base_mape=(group.base_error/group.actual_manwon).mean()*100,
                            indexed_mape=(group.indexed_error/group.actual_manwon).mean()*100,
                            base_return_mae=group.base_error_pp.mean(), indexed_return_mae=group.indexed_error_pp.mean()))
    stats=pd.DataFrame(summary)
    rows.to_csv(args.output/'backtest.csv',index=False,encoding='utf-8-sig')
    stats.to_csv(args.output/'metrics.csv',index=False,encoding='utf-8-sig')
    latest=pd.read_csv(previous/'results/forecasts.csv').query("district=='광진구' and horizon==3")
    latest=latest.merge(anchors,on=KEYS,validate='one_to_one')
    assert len(latest)==len(area)
    latest['indexed_reference_manwon']=latest.current_reference_manwon*latest.reference_new/latest.reference_old
    latest['indexed_price_manwon']=latest.final_price_manwon*latest.reference_new/latest.reference_old
    assert np.isfinite(latest.indexed_price_manwon).all() and latest.indexed_price_manwon.gt(0).all()
    latest.to_csv(args.output/'forecast_3m_candidate.csv',index=False,encoding='utf-8-sig')
    print(stats.to_string(index=False))
    print(rows.groupby('month')[['base_error','indexed_error']].mean().round(1).to_string())
    print('latest',len(latest),'changed',(~np.isclose(latest.reference_old,latest.reference_new)).sum())


if __name__=='__main__':
    main()


