"""Price anchor for the Seoul forecast service."""
from pathlib import Path
import numpy as np
import pandas as pd


def published_index(path: Path, anchor: pd.Period) -> pd.Series:
    source = pd.read_csv(path)
    months = pd.PeriodIndex(source["reference_month"], freq="M")
    levels = pd.to_numeric(source["gwangjin_apt_index"], errors="raise")
    date_col = "gwangjin_apt_index_available_at"
    if date_col in source:
        released = pd.to_datetime(source[date_col], errors="raise")
        if (levels.notna() & released.isna()).any():
            raise ValueError("Price index observation missing its publication date")
    else:
        released = (months + 1).to_timestamp() + pd.Timedelta(days=14)
    if months.has_duplicates:
        raise ValueError("Duplicate price index reference month")
    known = (released <= anchor.start_time) & (months <= anchor)
    known &= np.isfinite(levels) & levels.gt(0)
    return pd.Series(levels[known].to_numpy(), index=months[known]).sort_index()


def indexed_log_current(prices: pd.Series, index: pd.Series) -> float | None:
    """Reprice the six monthly medians with the latest public district index."""
    if index.empty:
        return None
    source = index.reindex(prices.index)
    ratios = (index.iloc[-1] / source).fillna(1.0)
    adjusted = (prices * ratios).median()
    return float(np.log(adjusted)) if np.isfinite(adjusted) and adjusted > 0 else None
