import test from "node:test";
import assert from "node:assert/strict";
import worker from "./_worker.js";

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
  const list = readHomes(controller.signal);
  const trades = readTrades(id, 60);
  controller.abort();
  loadManifest(response(manifest));
  await assert.rejects(list, {name: "AbortError"});
  assert.deepEqual(await trades, [row]);
  assert.equal((await readHomes()).length, 25);
  assert.equal(manifestRequests, 1);
});

test("a missing manifest falls back to the live list and selected history", async ctx => {
  const seen = [];
  ctx.mock.method(globalThis, "fetch", async url => {
    seen.push(url);
    return url === "/data/manifest.json" ? missing() : response([]);
  });
  const {readHomes, readTrades} = await freshModule("missing");
  await readHomes();
  await readTrades(id, 60);
  assert.equal(seen[1], "/api/complexes?include_sort=true");
  assert.ok(seen[3].startsWith("/api/trades?"));
});

test("a district with the right count and wrong keys fails explicitly", async ctx => {
  ctx.mock.method(globalThis, "fetch", async url => {
    if (url.endsWith("manifest.json")) return response(manifest);
    if (url.endsWith("latest-trades/11000.json")) return response([{complex_id: id, area_group: 85}]);
    return staticRead(url);
  });
  const {readHomes} = await freshModule("wrong-keys");
  await assert.rejects(readHomes(), /11000/);
});

test("Pages converts its HTML fallback for missing data into a 404", async () => {
  const env = {ASSETS: {fetch: () => new Response("<html>", {headers: {"Content-Type": "text/html"}})}};
  assert.equal((await worker.fetch(new Request("https://example.com/data/manifest.json"), env)).status, 404);
  assert.equal((await worker.fetch(new Request("https://example.com/"), env)).status, 200);
});

