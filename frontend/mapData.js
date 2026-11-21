// Bounds order: south, west, north, east.
export function districtNames(districts, bounds) {
  if (!bounds) return districts.filter(d => d.name === "광진구").map(d => d.name);
  const [south, west, north, east] = bounds;
  return districts.filter(({bounds: box}) => !box ||
    (box[0] <= north && box[2] >= south && box[1] <= east && box[3] >= west)).map(d => d.name);
}

export function clusterMarkers(items, zoom, selectedId) {
  if (zoom >= 15) return items;
  const scale = 256 * 2 ** zoom / 90;
  const groups = new Map();
  for (const item of items) {
    const {latitude, longitude, complex_id: id} = item;
    const sin = Math.sin(latitude * Math.PI / 180);
    const x = Math.floor((longitude + 180) / 360 * scale);
    const y = Math.floor((0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * scale);
    const key = id === selectedId ? "selected:" + id : x + ":" + y;
    const group = groups.get(key) || [];
    group.push(item);
    groups.set(key, group);
  }
  return Array.from(groups, ([key, group]) => group.length === 1 ? group[0] : ({
    complex_id: "cluster:" + key,
    count: group.length,
    latitude: group.reduce((sum, item) => sum + Number(item.latitude), 0) / group.length,
    longitude: group.reduce((sum, item) => sum + Number(item.longitude), 0) / group.length
  }));
}


/**
 * Resolve the selected ward's map center without mistaking stale homes from the
 * previously selected ward for an empty result. A null center means "wait for
 * district metadata or homes", never "navigate to Seoul City Hall".
 */
export function districtCenter(district, complexes, bounds) {
  if (!district) return [37.5665, 126.978];
  const points = (complexes || []).filter(c => {
    const ward = c.complex_id?.trim().split(/\s+/)[1] || c.address?.trim().split(/\s+/)[1];
    const lat = Number(c.latitude), lng = Number(c.longitude);
    return ward === district && c.latitude != null && c.longitude != null &&
      Number.isFinite(lat) && Number.isFinite(lng) &&
      lat >= 37 && lat <= 38 && lng >= 126 && lng <= 128;
  });
  if (points.length) {
    return [
      points.reduce((sum, c) => sum + Number(c.latitude), 0) / points.length,
      points.reduce((sum, c) => sum + Number(c.longitude), 0) / points.length
    ];
  }
  // The district catalog is available before its on-demand apartment data.
  // Use its geographic bounds while the next district's homes are loading.
  if (Array.isArray(bounds) && bounds.length === 4) {
    const [south, west, north, east] = bounds.map(Number);
    if ([south, west, north, east].every(Number.isFinite) &&
        south <= north && west <= east &&
        south >= 37 && north <= 38 && west >= 126 && east <= 128) {
      return [(south + north) / 2, (west + east) / 2];
    }
  }
  return null;
}
