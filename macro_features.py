"""Publication-aware monthly policy-rate and regional-index features.

The CSV uses reference_month, policy_rate, gwangjin_apt_index. Optional
<variable>_available_at columns override conservative default release dates.
Values must be observations from an identified source, not future-filled data.
"""
from pathlib import Path
import hashlib
import json
import numpy as np
import pandas as pd

MACRO_FEATURES = [
    "policy_rate", "policy_rate_change_3m",
    "gwangjin_apt_index_change_3m", "gwangjin_apt_index_change_12m",
]


def _ordered_releases(source, value_name):
    """Ignore delayed older releases once a newer reference month is known."""
    date_col = value_name + "_available_at"
    month_col = value_name + "_source_month"
    source = source.sort_values([date_col, month_col]).drop_duplicates(date_col, keep="last")
    if len(source):
        ordinals = pd.PeriodIndex(source[month_col], freq="M").asi8
        source = source.loc[ordinals >= np.maximum.accumulate(ordinals)]
    return source


def _release_dates(df, name, fallback):
    column = name + "_available_at"
    if column not in df:
        return pd.Series(fallback, index=df.index), True
    dates = pd.to_datetime(df[column], errors="coerce")
    missing = df[name].notna() & dates.isna()
    if missing.any():
        raise ValueError(f"{column}: {int(missing.sum())} observed values lack a valid release date")
    return dates, False


def load_monthly_macro(path, months, max_staleness_months=3):
    """Return one macro row per model anchor month, known on its first day.

    Derived changes are computed over reference months before the publication
    join. Availability is the latest release date of every observation used.
    Missing reference months remain missing and are never backfilled.
    """
    path = Path(path)
    raw = pd.read_csv(path, encoding="utf-8-sig")
    required = {"reference_month", "policy_rate", "gwangjin_apt_index"}
    if not required.issubset(raw.columns):
        raise ValueError(f"Macro CSV missing columns: {sorted(required-set(raw.columns))}")
    raw["reference_month"] = pd.to_datetime(raw["reference_month"], errors="raise").dt.to_period("M")
    if raw["reference_month"].duplicated().any():
        raise ValueError("Macro CSV must contain at most one row per reference_month")
    raw = raw.sort_values("reference_month").reset_index(drop=True)
    for name in ("policy_rate", "gwangjin_apt_index"):
        raw[name] = pd.to_numeric(raw[name], errors="raise")
    if raw[["policy_rate", "gwangjin_apt_index"]].isin([np.inf, -np.inf]).any().any():
        raise ValueError("Macro values must be finite or missing")
    if (raw["gwangjin_apt_index"].dropna() <= 0).any():
        raise ValueError("Regional price index must be positive")
    next_month = (pd.PeriodIndex(raw["reference_month"]) + 1).to_timestamp()
    policy_available, policy_assumed = _release_dates(raw, "policy_rate", next_month)
    index_available, index_assumed = _release_dates(raw, "gwangjin_apt_index", next_month + pd.Timedelta(days=14))
    raw["policy_rate_available_at"] = policy_available
    raw["gwangjin_apt_index_available_at"] = index_available
    for name in ("policy_rate", "gwangjin_apt_index"):
        # A monthly statistic cannot legitimately be known before its month.
        early = raw[name].notna() & (raw[name+"_available_at"] < raw["reference_month"].dt.to_timestamp())
        if early.any():
            raise ValueError(f"{name}: release date precedes its reference month")
    full_index = pd.period_range(raw["reference_month"].min(), raw["reference_month"].max(), freq="M")
    monthly = raw.set_index("reference_month").reindex(full_index)
    monthly["policy_rate_change_3m"] = monthly["policy_rate"] - monthly["policy_rate"].shift(3)
    for lag in (3, 12):
        monthly[f"gwangjin_apt_index_change_{lag}m"] = np.log(monthly["gwangjin_apt_index"] / monthly["gwangjin_apt_index"].shift(lag))
    availability = {"policy_rate": monthly["policy_rate_available_at"]}
    availability["policy_rate_change_3m"] = pd.concat(
        [monthly["policy_rate_available_at"], monthly["policy_rate_available_at"].shift(3)], axis=1
    ).max(axis=1)
    for lag in (3, 12):
        availability[f"gwangjin_apt_index_change_{lag}m"] = pd.concat(
            [monthly["gwangjin_apt_index_available_at"], monthly["gwangjin_apt_index_available_at"].shift(lag)], axis=1
        ).max(axis=1)
    anchor_months = pd.PeriodIndex(months, freq="M")
    anchors = pd.DataFrame({"month": anchor_months.astype(str), "macro_anchor_date": anchor_months.to_timestamp()})
    anchors = anchors.sort_values("macro_anchor_date")
    for feature in MACRO_FEATURES:
        source = pd.DataFrame({
            feature: monthly[feature].to_numpy(),
            feature+"_available_at": availability[feature].to_numpy(),
            feature+"_source_month": monthly.index.astype(str),
        }).dropna(subset=[feature, feature+"_available_at"])
        source = _ordered_releases(source, feature)
        anchors = pd.merge_asof(
            anchors.sort_values("macro_anchor_date"), source,
            left_on="macro_anchor_date", right_on=feature+"_available_at",
            direction="backward", allow_exact_matches=True,
        )
        valid = anchors[feature].notna()
        if (anchors.loc[valid, feature+"_available_at"] > anchors.loc[valid, "macro_anchor_date"]).any():
            raise AssertionError("Future publication entered an anchor")
        source_periods = pd.PeriodIndex(anchors[feature+"_source_month"], freq="M")
        age = np.array([a.ordinal-b.ordinal if not pd.isna(b) else np.nan for a,b in zip(anchor_months, source_periods)])
        anchors[feature+"_staleness_months"] = age
        stale = age > max_staleness_months
        anchors.loc[stale, feature] = np.nan
    # Keep the underlying index in provenance even though the model uses changes.
    index_source = pd.DataFrame({
        "gwangjin_apt_index": monthly["gwangjin_apt_index"].to_numpy(),
        "gwangjin_apt_index_available_at": monthly["gwangjin_apt_index_available_at"].to_numpy(),
        "gwangjin_apt_index_source_month": monthly.index.astype(str),
    }).dropna(subset=["gwangjin_apt_index", "gwangjin_apt_index_available_at"])
    index_source = _ordered_releases(index_source, "gwangjin_apt_index")
    anchors = pd.merge_asof(anchors.sort_values("macro_anchor_date"), index_source,
                            left_on="macro_anchor_date", right_on="gwangjin_apt_index_available_at", direction="backward")
    anchors["macro_complete"] = anchors[MACRO_FEATURES].notna().all(axis=1)
    assumptions = []
    if policy_assumed:
        assumptions.append("Monthly policy-rate observations assumed usable from next-month day 1; source CSV did not provide exact release dates.")
    if index_assumed:
        assumptions.append("Regional monthly index assumed usable from next-month day 15; exact release dates were not supplied.")
    assumptions.append("Current downloaded historical values may include revisions; this is not a complete vintage-data backtest.")
    coverage = {}
    for name in ("policy_rate", "gwangjin_apt_index"):
        observed = raw.loc[raw[name].notna(), "reference_month"]
        coverage[name] = {"first_reference_month": str(observed.min()) if len(observed) else None,
                          "last_reference_month": str(observed.max()) if len(observed) else None,
                          "observations": len(observed)}
    latest = anchors.iloc[-1].to_dict()
    latest = {k: (v.isoformat() if isinstance(v,pd.Timestamp) else None if pd.isna(v) else v.item() if isinstance(v,np.generic) else v) for k,v in latest.items()}
    latest["asof"] = str(anchor_months[-1].to_timestamp().date())
    summary = {"csv": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
               "added_features": MACRO_FEATURES, "asof_rule": "reference_month_first_day",
               "max_staleness_months": max_staleness_months, "coverage": coverage,
               "complete_anchor_months": int(anchors["macro_complete"].sum()), "latest": latest,
               "warnings": assumptions}
    return anchors, summary


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
