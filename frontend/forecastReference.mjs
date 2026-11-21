// A descriptive, opt-in chart overlay from the historical Gwangjin audit.
// It is NOT a calibrated interval for the live Seoul-wide model.
export function referenceBandPoints(data) {
  const ref = data?.historical_reference;
  const baseline = Number(data?.reference_price_manwon);
  if (ref?.kind !== "gwangjin_historical_model" ||
      ref.calibrated_for_current_model !== false ||
      !Number.isFinite(baseline) || baseline <= 0 ||
      !Array.isArray(data.forecasts) || !Array.isArray(ref.horizons)) return [];

  const referenceByHorizon = new Map(ref.horizons.map(row => [Number(row.months), row]));
  return data.forecasts.map(forecast => {
    const h = Number(forecast.months);
    const observed = referenceByHorizon.get(h);
    const rate = Number(forecast.return_percent);
    const lo = Number(observed?.p10_pp), hi = Number(observed?.p90_pp);
    if (!observed || observed.sample_count < 30 ||
        !Number.isFinite(rate) || !Number.isFinite(lo) ||
        !Number.isFinite(hi) || lo > hi) return null;
    return {
      time: h,
      lower: Math.max(0, baseline * (1 + (rate + lo) / 100)),
      upper: Math.max(0, baseline * (1 + (rate + hi) / 100)),
      sample_count: observed.sample_count,
      p10_pp: lo,
      p90_pp: hi
    };
  }).filter(Boolean);
}
