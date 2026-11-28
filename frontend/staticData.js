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

// Facilities arrive separately, one record per complex; the list is still shown when that request fails.
async function districtFacilities(name) {
  const body = await readJSON("/api/facilities?" + new URLSearchParams({district: name})).catch(() => null);
  return body?.items && Array.isArray(body.sources) ? body : {sources: [], items: {}};
}

async function loadDistrict(name) {
  const query = new URLSearchParams({include_sort: "true", district: name});
  const [rows, {sources, items}] = await Promise.all([
    readJSON("/api/complexes?" + query), districtFacilities(name)
  ]);
  return rows.map(row => ({
    ...row,
    ...(items[row.complex_id] && {facilities: items[row.complex_id]}),
    ...(sources.length && {facility_sources: sources})
  }));
}

function districtHomes(name) {
  if (!homesCache.has(name)) {
    homesCache.set(name, loadDistrict(name).catch(error => {
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
  const {scoped} = await getCatalog();
  signal?.throwIfAborted();
  const scope = new Set(names);
  const useDistricts = scoped && !liveHomesPromise;
  const result = useDistricts
    ? (await Promise.all([...scope].map(districtHomes))).flat()
    : (await getLiveHomes()).filter(row => scope.has(districtOf(row.complex_id)));
  signal?.throwIfAborted();
  return result;
}

export async function readTrades(complexId, areaGroup, signal) {
  const query = new URLSearchParams({complex_id: complexId, area_group: areaGroup});
  return readJSON("/api/trades?" + query, {signal});
}
