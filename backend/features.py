"""Build the current forecast inputs from PostgreSQL transaction history."""
import numpy as np
import pandas as pd

from macro_features import MACRO_FEATURES, load_monthly_macro


def input_month_range(anchor):
    """Return (first_period, last_period) = (anchor - 19, anchor - 2) for model inputs."""
    last = anchor - 2
    return last - 17, last


def is_in_input_window(month, anchor):
    first, last = input_month_range(anchor)
    return first <= month <= last


def build_inputs(trades, anchor, macro_path):
    macro, _ = load_monthly_macro(macro_path, [anchor])
    if not macro.iloc[0]["macro_complete"]:
        raise ValueError("Update macro_raw.csv before publishing this forecast month")
    trades = trades.copy()
    trades["month"] = pd.to_datetime(trades.contract_date).dt.to_period("M")
    trades["ppm"] = trades.price_manwon.astype(float) / trades.area.astype(float)
    trades["area"] = trades.area.astype(float)
    first_period, last = input_month_range(anchor)
    periods = pd.period_range(first_period, last, freq="M")
    recent = trades[trades.month.between(periods[0], last)]
    monthly = recent.groupby(["complex_id", "area_group", "month"]).agg(
        ppm=("ppm", "median"), count=("ppm", "size"), area=("area", "median"),
        floor=("floor", "median"), built=("built", "first"))
    result = []
    for (cid, group), frame in monthly.groupby(level=[0, 1]):
        frame = frame.droplevel([0, 1]).reindex(periods)
        prices = frame.ppm
        current = prices.iloc[-6:].median()
        last_year = frame.iloc[-12:]
        count12 = last_year["count"].sum()
        if pd.isna(current) or count12 < 3:
            continue
        def momentum(lag):
            historic = prices.iloc[-6-lag:-lag].dropna()
            return np.log(current / historic.median()) if len(historic) else None
        result.append(dict(
            complex_id=cid, area_group=int(group), month=str(anchor),
            representative_area=last_year.area.median(), built=frame.built.dropna().iloc[-1],
            age=anchor.year-frame.built.dropna().iloc[-1], log_current=np.log(current),
            count6=frame["count"].iloc[-6:].sum(), count12=count12,
            momentum6=momentum(6), momentum12=momentum(12),
            volatility=np.log(last_year.ppm).std(),
            months_since_trade=last.ordinal-prices.last_valid_index().ordinal,
            floor_known=last_year.floor.median(),
            time_index=anchor.ordinal-pd.Period("2005-08", freq="M").ordinal,
            month_sin=np.sin(2*np.pi*anchor.month/12), month_cos=np.cos(2*np.pi*anchor.month/12),
            **macro.iloc[0][MACRO_FEATURES].to_dict()))
    if not result:
        raise ValueError("No eligible model inputs; previous release retained")
    return pd.DataFrame(result)
