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
