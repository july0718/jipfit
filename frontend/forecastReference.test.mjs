import {test} from "node:test";
import assert from "node:assert/strict";
import {referenceBandPoints} from "./forecastReference.mjs";

const SHA = "a".repeat(64);
function fixture() {
  return {
    reference_price_manwon: 10000,
    forecasts: [{months:3,return_percent:10,price_manwon:11000},
                {months:6,return_percent:-5,price_manwon:9500},
                {months:12,return_percent:25,price_manwon:12500}],
    historical_reference: {
      schema_version: 1,
      kind: "seoul_hgb_holdout",
      model_sha256: SHA,
      training_cutoff: "2025-04", holdout_start: "2025-04",
      holdout_end: "2025-09", as_of: "2026-11",
      calibrated_for_current_model: false,
      horizons: [{months:3,sample_count:100,p10_pp:-20,p90_pp:15},
                 {months:6,sample_count:80,p10_pp:-30,p90_pp:5},
                 {months:12,sample_count:50,p10_pp:-100,p90_pp:10}]
    }
  };
}
test("Seoul holdout residual percent points apply to forecast returns, not prices", () => {
  const bands = referenceBandPoints(fixture());
  assert.deepEqual(bands.map(x=>x.time), [3,6,12]);
  assert.equal(bands[0].lower, 9000);
  assert.equal(bands[0].upper, 12500);
  assert.equal(bands[1].lower, 6500);
  assert.equal(bands[1].upper, 10000);
  assert.equal(bands[2].lower, 2500);
});
test("rejects historical Gwangjin reference and model provenance mismatch", () => {
  const sample = fixture();
  sample.historical_reference.kind = "gwangjin_historical_model";
  assert.deepEqual(referenceBandPoints(sample), []);
  sample.historical_reference = fixture().historical_reference;
  sample.historical_reference.training_cutoff = "2024-03";
  assert.deepEqual(referenceBandPoints(sample), []);
});
test("missing or unverified holdout cannot invent intervals", () => {
  const sample = fixture();
  delete sample.historical_reference;
  assert.deepEqual(referenceBandPoints(sample), []);
  sample.historical_reference = fixture().historical_reference;
  sample.historical_reference.calibrated_for_current_model = true;
  assert.deepEqual(referenceBandPoints(sample), []);
});
test("rejects invalid, missing and reversed percentile values", () => {
  const sample = fixture();
  sample.historical_reference.horizons[0].p10_pp = null;
  sample.historical_reference.horizons[1].p10_pp = 100;
  sample.historical_reference.horizons[2].sample_count = 4;
  assert.deepEqual(referenceBandPoints(sample), []);
});
test("static loader accepts Seoul data, never the old Gwangjin data", async t => {
  const sample = fixture();
  t.mock.method(globalThis, "fetch", async (url, opts) => {
    assert.equal(url, "/metadata/seoul-forecast-reference.json");
    assert.equal(opts.cache, "no-store");
    return Response.json(sample.historical_reference);
  });
  const mod = await import("./forecastReference.mjs?seoul-fetch-test");
  const got = await mod.readStaticReference();
  assert.equal(got.kind, "seoul_hgb_holdout");
  assert.equal(mod.referenceBandPoints({...sample, historical_reference: got}).length, 3);
  await mod.readStaticReference();
  assert.equal(globalThis.fetch.mock.callCount(), 1);
});
