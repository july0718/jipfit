import {test} from "node:test";
import assert from "node:assert/strict";
import {referenceBandPoints} from "./forecastReference.mjs";

function fixture() {
  return {
    reference_price_manwon: 10000,
    forecasts: [{months:3,return_percent:10,price_manwon:11000},
                {months:6,return_percent:-5,price_manwon:9500},
                {months:12,return_percent:25,price_manwon:12500}],
    historical_reference: {
      kind: "gwangjin_historical_model",
      calibrated_for_current_model: false,
      horizons: [{months:3,sample_count:100,p10_pp:-20,p90_pp:15},
                 {months:6,sample_count:80,p10_pp:-30,p90_pp:5},
                 {months:12,sample_count:50,p10_pp:-100,p90_pp:10}]
    }
  };
}
test("applies historical residual percent points to return rather than price percent", () => {
  const bands = referenceBandPoints(fixture());
  assert.deepEqual(bands.map(x=>x.time), [3,6,12]);
  assert.equal(bands[0].lower, 9000);
  assert.equal(bands[0].upper, 12500);
  assert.equal(bands[1].lower, 6500);
  assert.equal(bands[1].upper, 10000);
  assert.equal(bands[2].lower, 2500);
});
test("missing or unspecified audit never invents a confidence interval", () => {
  const sample = fixture();
  delete sample.historical_reference;
  assert.deepEqual(referenceBandPoints(sample), []);
  sample.historical_reference = fixture().historical_reference;
  sample.historical_reference.calibrated_for_current_model = true;
  assert.deepEqual(referenceBandPoints(sample), []);
});
test("null percentile cannot silently become zero", () => {
  const sample = fixture();
  sample.historical_reference.horizons[0].p10_pp = null;
  assert.deepEqual(referenceBandPoints(sample).map(x=>x.time), [6,12]);
});
test("rejects invalid sample sizes and reversed percentiles", () => {
  const sample = fixture();
  sample.historical_reference.horizons[0].sample_count = 4;
  sample.historical_reference.horizons[1].p10_pp = 100;
  assert.deepEqual(referenceBandPoints(sample).map(x=>x.time), [12]);
});
