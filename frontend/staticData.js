// Public snapshot transport. All predictions and account operations remain live API calls.
const base = "/data/";
let manifestPromise;

async function readJSON(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) { const error = new Error("JSON 조회 실패 (HTTP " + response.status + "): " + url); error.status = response.status; throw error; }
  return response.json();
}

async function getManifest() {
  if (!manifestPromise) {
    manifestPromise = readJSON(base + "manifest.json", { cache: "no-cache" }).then(manifest => {
      if (manifest.schema_version !== 1 || !/^releases\/\d{8}T\d{12}Z$/.test(manifest.release) ||
          manifest.districts.length !== 25 ||
          new Set(manifest.districts.map(d => d.code)).size !== 25 ||
          manifest.districts.some(d => !/^\d{5}$/.test(d.code))) {
        throw new Error("지원하지 않는 단지 JSON 매니페스트입니다.");
      }
      return manifest;
    }).catch(error => {
      manifestPromise = null;
      throw error;
    });
  }
  return manifestPromise;
}

async function liveAPI(url, signal) {
  const response = await fetch(url, { signal, cache: "no-store" });
  if (!response.ok) throw new Error("API 조회 실패 (HTTP " + response.status + ")");
  return response.json();
}

export async function readHomes(signal) {
  let manifest;
  try { manifest = await getManifest(); }
  catch (error) {
    if (error.status === 404) return liveAPI("/api/complexes?include_sort=true", signal);
    throw error;
  }
  const path = base + manifest.release + "/";
  const key = item => JSON.stringify([item.complex_id, item.area_group]);
  const chunks = await Promise.all(manifest.districts.map(async ({code}) => {
    const [complexes, latest] = await Promise.all([
      readJSON(path + "complexes/" + code + ".json", { signal }),
      readJSON(path + "latest-trades/" + code + ".json", { signal })
    ]);
    const index = new Map(latest.map(item => [key(item), item]));
    if (complexes.length !== latest.length || index.size !== latest.length ||
        new Set(complexes.map(key)).size !== complexes.length) {
      throw new Error("단지 JSON 건수 불일치: " + code);
    }
    return complexes.map(item => {
      const price = index.get(key(item));
      if (!price) throw new Error("단지 JSON 면적군 불일치: " + code);
      return {...item, ...price};
    });
  }));
  const result = chunks.flat();
  if (result.length !== manifest.totals.area_groups) throw new Error("단지 JSON 건수 불일치");
  return result;
}

async function encodedID(id) {
  const bytes = new TextEncoder().encode(id);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, "0")).join("");
}

export async function readTrades(complexId, areaGroup, signal) {
  let manifest;
  try { manifest = await getManifest(); }
  catch (error) {
    if (error.status === 404) return liveAPI("/api/trades?" + new URLSearchParams({complex_id: complexId, area_group: areaGroup}), signal);
    throw error;
  }
  const district = complexId.split(" ")[1];
  const code = manifest.districts.find(entry => entry.name === district)?.code;
  if (!code) throw new Error("단지 소재 자치구를 확인할 수 없습니다.");
  const hash = await encodedID(complexId);
  const path = base + manifest.release + "/transactions/" + code + "/" + hash + ".json";
  const document = await readJSON(path, { signal });
  if (document.complex_id !== complexId) throw new Error("거래 JSON 단지 식별 오류");
  return document.area_groups[String(areaGroup)] || [];
}
