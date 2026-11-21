import test from "node:test";
import assert from "node:assert/strict";
import worker from "./_worker.js";
import { clusterMarkers, districtNames } from "./mapData.js";
import { createHash } from "node:crypto";

const districts = Array.from({ length: 25 }, (_, i) => ({
  code: String(11000 + i), name: i === 0 ? "광진구" : "구" + i
}));
const release = "releases/20261121T000000000000Z";
const id = "서울 광진구 테스트";
const manifest = {schema_version: 1, release, districts, totals: {area_groups: 25}};
const response = value => Response.json(value);
const missing = () => new Response(null, { status: 404 });
const freshModule = name => import("./staticData.js?" + name);
const staticRead = url => {
  const code = url.split("/").at(-1).slice(0, -5);
  const location = districts.find(entry => entry.code === code).name;
  const row = {complex_id: "서울 " + location + " 테스트", area_group: 60};
  return response([{...row, ...(url.includes("latest-trades") ? {trade_count: 1} : {name: "테스트"})}]);
};

test("a cancelled list doesn't cancel a concurrent whole-history lookup", async ctx => {
  const controller = new AbortController();
  let loadManifest;
  let manifestRequests = 0;
  const row = {contract_date: "2026-09-01", is_cancelled: true};
  ctx.mock.method(globalThis, "fetch", async (url, {signal} = {}) => {
    if (url === "/data/manifest.json") {
      ++manifestRequests;
      return new Promise(resolve => { loadManifest = resolve; });
    }
    signal?.throwIfAborted();
    if (url.includes("/transactions/11000/") && /[a-f0-9]{64}\.json$/.test(url)) {
      return response({complex_id: id, area_groups: {60: [row]}});
    }
    return staticRead(url);
  });
  const {readHomes, readTrades} = await freshModule("cancel");
  const list = readHomes(["광진구"], controller.signal);
  const trades = readTrades(id, 60);
  controller.abort();
  loadManifest(response(manifest));
  await assert.rejects(list, {name: "AbortError"});
  assert.deepEqual(await trades, [row]);
  assert.equal((await readHomes(["광진구"])).length, 1);
  assert.equal(manifestRequests, 1);
});

test("a missing manifest falls back to the live list and selected history", async ctx => {
  const seen = [];
  ctx.mock.method(globalThis, "fetch", async url => {
    seen.push(url);
    return url === "/data/manifest.json" ? missing() : response([]);
  });
  const {readHomes, readTrades} = await freshModule("missing");
  await readHomes(["광진구"]);
  await readTrades(id, 60);
  assert.equal(seen[1], "/api/complexes?include_sort=true");
  assert.ok(seen[2].startsWith("/api/trades?"));
});

test("a district with the right count and wrong keys fails explicitly", async ctx => {
  ctx.mock.method(globalThis, "fetch", async url => {
    if (url.endsWith("manifest.json")) return response(manifest);
    if (url.endsWith("latest-trades/11000.json")) return response([{complex_id: id, area_group: 85}]);
    return staticRead(url);
  });
  const {readHomes} = await freshModule("wrong-keys");
  await assert.rejects(readHomes(["광진구"]), /11000/);
});

test("Pages converts its HTML fallback for missing data into a 404", async () => {
  const env = {ASSETS: {fetch: () => new Response("<html>", {headers: {"Content-Type": "text/html"}})}};
  assert.equal((await worker.fetch(new Request("https://example.com/data/manifest.json"), env)).status, 404);
  assert.equal((await worker.fetch(new Request("https://example.com/"), env)).status, 200);
});


test("startup fetches 3 files and district revisits reuse cached rows", async ctx => {
  const seen = [];
  ctx.mock.method(globalThis, "fetch", async url => {
    seen.push(url);
    return url.endsWith("manifest.json") ? response(manifest) : staticRead(url);
  });
  const {readHomes, readDistricts} = await freshModule("cold");
  const [catalog, rows] = await Promise.all([readDistricts(), readHomes(["광진구"])]);
  assert.equal(catalog.length, 25);
  assert.equal(rows.length, 1);
  assert.equal(seen.length, 3);
  assert.deepEqual(await readHomes(["광진구"]), rows);
  assert.equal(seen.length, 3);
  await readHomes(["구1"]);
  assert.equal(seen.length, 5);
  assert.equal(seen.filter(url => url.endsWith("11000.json")).length, 2);
});

test("cluster grouping preserves count and selected marker; zoom separates all", () => {
  const items = Array.from({length: 4}, (_, i) => ({
    complex_id: String(i), latitude: 37.55 + i / 1000000, longitude: 127.1
  }));
  assert.deepEqual(clusterMarkers(items, 11, "0").map(x => x.count || 1), [1, 3]);
  assert.equal(clusterMarkers(items, 11, "0")[0], items[0]);
  assert.deepEqual(clusterMarkers(items, 15), items);
  assert.deepEqual(districtNames([
    {name: "광진구", bounds: [37.5, 127.05, 37.6, 127.15]},
    {name: "강서구", bounds: [37.5, 126.75, 37.6, 126.85]}
  ], [37.52, 127.06, 37.56, 127.09]), ["광진구"]);
});

test("content-addressed trade history uses the district index once", async ctx => {
  const {readTrades} = await freshModule("objects");
  const hash = createHash("sha256").update(id).digest("hex");
  const history = "a".repeat(64);
  const trades = "b".repeat(64);
  const seen = [];
  ctx.mock.method(globalThis, "fetch", async url => {
    seen.push(url);
    if (url.endsWith("manifest.json")) return response({...manifest, schema_version: 2,
      districts: districts.map(entry => ({...entry, history}))});
    if (url.endsWith(history + ".json")) return response({[hash]: trades});
    if (url.endsWith(trades + ".json")) return response({
      complex_id: id, area_groups: {60: [{is_cancelled: true}], 85: [{is_cancelled: false}]}
    });
    throw new Error("Unexpected URL");
  });
  assert.deepEqual(await readTrades(id, 60), [{is_cancelled: true}]);
  assert.deepEqual(await readTrades(id, 85), [{is_cancelled: false}]);
  assert.equal(seen.length, 3);
});

test("Pages cache policy and private bucket routing are scoped to snapshots", async () => {
  const key = "c".repeat(64);
  const seen = [];
  const env = {
    PUBLIC_SNAPSHOTS: {get: async path => {
      seen.push(path);
      return {body: "{}", httpEtag: '"sample"'};
    }},
    ASSETS: {fetch: async () => new Response("asset")}
  };
  const get = path => worker.fetch(new Request("https://example.com" + path), env);
  assert.equal((await get("/data/manifest.json")).headers.get("Cache-Control"), "no-store");
  assert.match((await get("/data/objects/" + key + ".json")).headers.get("Cache-Control"), /immutable/);
  assert.equal(await (await get("/data/releases/test/manifest.json")).text(), "asset");
  assert.deepEqual(seen, ["manifest.json", "objects/" + key + ".json"]);
});
