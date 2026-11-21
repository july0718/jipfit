// Historical Seoul-wide HGB holdout residuals. Not a confidence interval for
// the current deployed model, which may have a different training cutoff.
let staticReferencePromise;

export function isSeoulHoldoutReference(ref) {
  return ref?.schema_version === 1 && ref?.kind === "seoul_hgb_holdout" &&
    ref.training_cutoff === "2025-04" &&
    ref.holdout_start === "2025-04" && ref.holdout_end === "2025-09" &&
    ref.as_of === "2026-11" && ref.calibrated_for_current_model === false &&
    /^[a-f0-9]{64}$/.test(ref.model_sha256 || "") &&
    Array.isArray(ref.horizons);
}

export function readStaticReference() {
  if (!staticReferencePromise) {
    staticReferencePromise = fetch("/metadata/seoul-forecast-reference.json", {cache: "no-store"})
      .then(response => {
        if (!response.ok) throw new Error("Historical Seoul reference unavailable");
        return response.json();
      })
      .then(document => isSeoulHoldoutReference(document) ? document : null)
      .catch(() => null);
  }
  return staticReferencePromise;
}

export function referenceBandPoints(data) {
  const ref = data?.historical_reference;
  const baseline = Number(data?.reference_price_manwon);
  if (!isSeoulHoldoutReference(ref) ||
      !Number.isFinite(baseline) || baseline <= 0 ||
      !Array.isArray(data.forecasts)) return [];

  const referenceByHorizon = new Map(ref.horizons.map(row => [Number(row.months), row]));
  return data.forecasts.map(forecast => {
    const h = Number(forecast.months);
    const observed = referenceByHorizon.get(h);
    const rate = Number(forecast.return_percent);
    const lo = Number(observed?.p10_pp), hi = Number(observed?.p90_pp);
    if (!observed || !Number.isInteger(observed.sample_count) ||
        observed.sample_count < 30 || observed.p10_pp == null ||
        observed.p90_pp == null || !Number.isFinite(rate) ||
        !Number.isFinite(lo) || !Number.isFinite(hi) || lo > hi) return null;
    return {
      time: h,
      lower: Math.max(0, baseline * (1 + (rate + lo) / 100)),
      upper: Math.max(0, baseline * (1 + (rate + hi) / 100)),
      sample_count: observed.sample_count,
      p10_pp: lo,
      p90_pp: hi,
      mae_pp: observed.mae_pp,
      bias_pp: observed.bias_pp
    };
  }).filter(Boolean);
}
