const homesCache = new Map();
let catalogPromise;
let liveHomesPromise;

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
  return (await getCatalog()).districts;
}

function districtHomes(name) {
  if (!homesCache.has(name)) {
    const query = new URLSearchParams({include_sort: "true", district: name});
    homesCache.set(name, readJSON("/api/complexes?" + query).catch(error => {
      homesCache.delete(name);
      throw error;
    }));
  }
  return homesCache.get(name);
}

export async function readHomes(names, signal) {
  signal?.throwIfAborted();
  if (!names.length) return [];
  const {scoped, districts} = await getCatalog();
  signal?.throwIfAborted();
  const scope = new Set(names);
  const useDistricts = scoped && !liveHomesPromise && scope.size < districts.length;
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
