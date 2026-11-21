export const EMPTY_RANGES = {priceMin: "", priceMax: "", areaMin: "", areaMax: "", yearMin: "", yearMax: ""};

export function inRange(value, min, max) {
  if (min === "" && max === "") return true;
  if (!(Number(value) > 0)) return false;
  return (min === "" || value >= Number(min)) && (max === "" || value <= Number(max));
}

export function matchingComplex(complex, ranges, onlyReady) {
  if (!inRange(complex.built, ranges.yearMin, ranges.yearMax)) return null;
  const groups = complex.groups.filter(group =>
    (!onlyReady || group.predictable) &&
    inRange(group.representative_area, ranges.areaMin, ranges.areaMax) &&
    inRange(Number(group.marker_price_manwon) / 10000, ranges.priceMin, ranges.priceMax));
  if (!groups.length) return null;
  if (groups.length === complex.groups.length) return complex;
  const firstPredicted = complex.groups.find(g => g.predictable);
  return {...complex, groups, sort_returns: groups.includes(firstPredicted) ? complex.sort_returns : {}};
}

export function changePercent(value, base) {
  return Number(base) > 0 && Number(value) > 0 ? (Number(value) / Number(base) - 1) * 100 : null;
}

export function pricePerPyeong(trade) {
  return Number(trade.area) > 0 && Number(trade.price_manwon) > 0
    ? Number(trade.price_manwon) * 3.305785 / Number(trade.area) : null;
}
