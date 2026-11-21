import test from "node:test";
import assert from "node:assert/strict";
import { EMPTY_RANGES, matchingComplex, changePercent, pricePerPyeong } from "./housingAnalysis.mjs";

const small = {area_group: 60, representative_area: 59.92, marker_price_manwon: 110000, predictable: true};
const large = {area_group: 84, representative_area: 84.8, marker_price_manwon: 150000, predictable: false};
const complex = {built: 2006, groups: [small, large], sort_returns: {3: 4.5}};
const match = (ranges, ready = false) => matchingComplex(complex, {...EMPTY_RANGES, ...ranges}, ready);

test("price and area must match the same group, including exact boundaries", () => {
  assert.equal(match({priceMax: "12", areaMin: "84"}), null);
  assert.deepEqual(match({priceMax: "11", areaMin: "59.92"}).groups, [small]);
  assert.deepEqual(match({priceMin: "15", yearMax: "2006"}).groups, [large]);
  assert.deepEqual(match({priceMin: "15"}).sort_returns, {});
  assert.equal(match({priceMin: "15"}, true), null);
  assert.equal(match({yearMin: "2007"}), null);
  assert.equal(match({}), complex);
});

test("missing prices and construction years remain visible only with the corresponding filter open", () => {
  const unknown = {built: null, groups: [{...small, marker_price_manwon: null}]};
  assert.equal(matchingComplex(unknown, EMPTY_RANGES, false), unknown);
  for (const bound of [{priceMin: "0"}, {yearMax: "2026"}]) {
    assert.equal(matchingComplex(unknown, {...EMPTY_RANGES, ...bound}, false), null);
  }
});

test("price per exclusive pyeong and percentage use actual transaction values", () => {
  assert.equal(pricePerPyeong({price_manwon: 2000, area: 3.305785}), 2000);
  assert.equal(pricePerPyeong({price_manwon: 2000, area: 0}), null);
  assert.equal(changePercent(75000, 100000), -25);
  assert.equal(changePercent(100000, 0), null);
});
