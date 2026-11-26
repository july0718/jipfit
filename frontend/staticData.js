const homesCache = new Map();
let catalogPromise;
let liveHomesPromise;
const cacheLifetime = 5 * 60 * 1000;
let expiresAt = 0;

function expireCachedHomes() {
  if (Date.now() < expiresAt) return;
  homesCache.clear();
  liveHomesPromise = undefined;
  catalogPromise = undefined;
  expiresAt = Date.now() + cacheLifetime;
}

export function invalidateHomesCache() {
  homesCache.clear();
  liveHomesPromise = undefined;
  catalogPromise = undefined;
  expiresAt = 0;
}

async function readJSON(url, options) {
  const response = await fetch(url, {cache: "no-store", ...options});
  if (!response.ok) {
    const error = new Error("데이터 조회 실패 (HTTP " + response.status + "): " + url);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

export const districtOf = value => value.split(/\s+/)[1];

function getLiveHomes() {
  if (!liveHomesPromise) {
    liveHomesPromise = readJSON("/api/complexes?include_sort=true").catch(error => {
      liveHomesPromise = null;
      throw error;
    });
  }
  return liveHomesPromise;
}

function getCatalog() {
  if (!catalogPromise) {
    catalogPromise = readJSON("/api/districts").then(districts => ({
      districts, scoped: true
    })).catch(async error => {
      // Older API containers can continue serving the front end during deployment.
      if (error.status !== 404) throw error;
      const rows = await getLiveHomes();
      const districts = [...new Set(rows.map(row => districtOf(row.complex_id)))].map(name => ({name}));
      return {districts, scoped: false};
    }).catch(error => {
      catalogPromise = null;
      throw error;
    });
  }
  return catalogPromise;
}

export async function readDistricts() {
  expireCachedHomes();
  return (await getCatalog()).districts;
}

export function mergeHomeRow(staticItem, liveRow) {
  if (!staticItem) return {...liveRow};
  const addressChanged = Boolean(
    liveRow.address && staticItem.address && liveRow.address !== staticItem.address
  );
  const hasLiveCoords = "latitude" in liveRow || "longitude" in liveRow;
  const livePairValid = hasLiveCoords && liveRow.latitude != null && liveRow.longitude != null;
  const merged = {...staticItem, ...liveRow};
  if (hasLiveCoords) {
    merged.latitude = livePairValid ? liveRow.latitude : null;
    merged.longitude = livePairValid ? liveRow.longitude : null;
  } else if (addressChanged) {
    merged.latitude = null;
    merged.longitude = null;
  }
  if (typeof liveRow.demolition_confirmed === "boolean") {
    merged.demolition_confirmed = liveRow.demolition_confirmed;
  } else if (typeof staticItem.demolition_confirmed === "boolean") {
    merged.demolition_confirmed = staticItem.demolition_confirmed;
  }
  return merged;
}

async function hybridDistrict(name, code) {
  const query = new URLSearchParams({include_sort: "true", district: name});
  const full = () => readJSON("/api/complexes?" + query);
  if (!code) return full();
  const metadata = await readJSON("/metadata/" + code + ".json", {cache: "no-cache"}).catch(() => null);
  if (!Array.isArray(metadata)) return full();
  const byId = new Map(metadata.map(item => [item.complex_id, item]));
  const rows = await readJSON("/api/complexes?" + query + "&live_only=true");
  // Live responses now include address and built; fall back to full() only for older API servers.
  const missing = rows.some(row => !byId.has(row.complex_id) && (!row.address || row.built === undefined));
  const source = missing ? await full() : rows;
  return source.map(row => mergeHomeRow(byId.get(row.complex_id), row));
}

function districtHomes(name, code) {
  if (!homesCache.has(name)) {
    homesCache.set(name, hybridDistrict(name, code).catch(error => {
      homesCache.delete(name);
      throw error;
    }));
  }
  return homesCache.get(name);
}

export async function readHomes(names, signal) {
  signal?.throwIfAborted();
  if (!names.length) return [];
  expireCachedHomes();
  const {scoped, districts} = await getCatalog();
  signal?.throwIfAborted();
  const scope = new Set(names);
  const useDistricts = scoped && !liveHomesPromise;
  const result = useDistricts
    ? (await Promise.all([...scope].map(name => districtHomes(name, districts.find(d => d.name === name)?.code)))).flat()
    : (await getLiveHomes()).filter(row => scope.has(districtOf(row.complex_id)));
  signal?.throwIfAborted();
  return result;
}

export async function readTrades(complexId, areaGroup, signal) {
  const query = new URLSearchParams({complex_id: complexId, area_group: areaGroup});
  return readJSON("/api/trades?" + query, {signal});
}
