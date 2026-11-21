const base = "/data/";
const digestPattern = /^[a-f0-9]{64}$/;
let manifestPromise;
let liveHomesPromise;
const homesCache = new Map();
const cache = new Map();

async function readJSON(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const error = new Error("데이터 조회 실패 (HTTP " + response.status + "): " + url);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function getManifest() {
  if (!manifestPromise) {
    manifestPromise = readJSON(base + "manifest.json", { cache: "no-cache" }).then(manifest => {
      const {schema_version, release, districts} = manifest;
      if (![1, 2].includes(schema_version) || !/^releases\/\d{8}T\d{12}Z$/.test(release) ||
          !Array.isArray(districts) || districts.length !== 25 ||
          new Set(districts.map(d => d.code)).size !== 25 ||
          new Set(districts.map(d => d.name)).size !== 25 ||
          districts.some(d => !/^\d{5}$/.test(d.code) || typeof d.name !== "string")) {
        throw new Error("지원하지 않는 단지 JSON 매니페스트입니다.");
      }
      return manifest;
    }).catch(error => {
      if (error.status === 404) return null;
      manifestPromise = null;
      throw error;
    });
  }
  return manifestPromise;
}

function getLiveHomes() {
  if (!liveHomesPromise) {
    liveHomesPromise = readJSON("/api/complexes?include_sort=true", {cache: "no-store"}).catch(error => {
      liveHomesPromise = null;
      throw error;
    });
  }
  return liveHomesPromise;
}

export const districtOf = value => value.split(/\s+/)[1];

export async function readDistricts() {
  const manifest = await getManifest();
  if (manifest) return manifest.districts;
  const rows = await getLiveHomes();
  return Array.from(new Set(rows.map(row => districtOf(row.complex_id))), name => ({name}));
}

function objectPath(digest) {
  if (typeof digest !== "string" || !digestPattern.test(digest)) throw new Error("JSON 데이터 참조가 잘못되었습니다.");
  return base + "objects/" + digest + ".json";
}

function cachedJSON(path) {
  if (!cache.has(path)) {
    cache.set(path, readJSON(path).catch(error => {
      cache.delete(path);
      throw error;
    }));
    if (cache.size > 32) cache.delete(cache.keys().next().value);
  }
  return cache.get(path);
}

function districtHomes(manifest, district) {
  if (!homesCache.has(district.code)) {
    homesCache.set(district.code, readDistrictHomes(manifest, district).catch(error => {
      homesCache.delete(district.code);
      throw error;
    }));
  }
  return homesCache.get(district.code);
}

async function readDistrictHomes(manifest, district) {
  const {release, schema_version: version} = manifest;
  const {code, complexes, latest} = district;
  const root = base + release + "/";
  const [homes, prices] = await Promise.all([
    readJSON(version === 2 ? objectPath(complexes) : root + "complexes/" + code + ".json"),
    readJSON(version === 2 ? objectPath(latest) : root + "latest-trades/" + code + ".json")
  ]);
  const key = item => JSON.stringify([item.complex_id, item.area_group]);
  const index = new Map(prices.map(item => [key(item), item]));
  if (homes.length !== prices.length || index.size !== prices.length ||
      new Set(homes.map(key)).size !== homes.length) throw new Error("단지 JSON 건수 불일치: " + code);
  return homes.map(item => {
    const price = index.get(key(item));
    if (!price) throw new Error("단지 JSON 면적군 불일치: " + code);
    return {...item, ...price};
  });
}

export async function readHomes(names, signal) {
  const manifest = await getManifest();
  signal?.throwIfAborted();
  const scope = new Set(names);
  const rows = manifest
    ? (await Promise.all(manifest.districts.filter(d => scope.has(d.name)).map(d => districtHomes(manifest, d)))).flat()
    : (await getLiveHomes()).filter(row => scope.has(districtOf(row.complex_id)));
  signal?.throwIfAborted();
  return rows;
}

async function encodedID(id) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(id));
  return Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, "0")).join("");
}

export async function readTrades(complexId, areaGroup, signal) {
  const manifest = await getManifest();
  signal?.throwIfAborted();
  if (!manifest) {
    return readJSON("/api/trades?" + new URLSearchParams({complex_id: complexId, area_group: areaGroup}),
      { signal, cache: "no-store" });
  }
  const district = manifest.districts.find(entry => entry.name === districtOf(complexId));
  if (!district) throw new Error("단지 소재 자치구를 확인할 수 없습니다.");
  const hash = await encodedID(complexId);
  const path = manifest.schema_version === 2
    ? objectPath((await cachedJSON(objectPath(district.history)))[hash])
    : base + manifest.release + "/transactions/" + district.code + "/" + hash + ".json";
  signal?.throwIfAborted();
  const document = await cachedJSON(path);
  signal?.throwIfAborted();
  if (document.complex_id !== complexId) throw new Error("거래 JSON 단지 식별 오류");
  return document.area_groups[String(areaGroup)] || [];
}
