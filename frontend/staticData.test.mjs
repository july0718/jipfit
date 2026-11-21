import { test } from "node:test";
import assert from "node:assert/strict";
import worker from "./_worker.js";
import { clusterMarkers, districtNames } from "./mapData.js";

const districts = [
  {code: "11215", name: "광진구", bounds: [37.52, 127.06, 37.58, 127.12]},
  {code: "11500", name: "강서구", bounds: [37.5, 126.75, 37.6, 126.85]},
];
const row = name => ({ complex_id: "서울 " + name + " 테스트", area_group: 60 });
const fresh = name => import("./staticData.js?" + name);
const reflect = data => Response.json(data);

test("first view requests only the selected district and revisits reuse its response", async t => {
  const seen = [];
  t.mock.method(globalThis, "fetch", async url => {
    seen.push(url);
    if (url === "/api/districts") return reflect(districts);
    const query = new URL(url, "http://localhost").searchParams;
    return reflect([row(query.get("district"))]);
  });
  const {readDistricts, readHomes} = await fresh("scoped");
  const [catalog, initial] = await Promise.all([readDistricts(), readHomes(["광진구"])]);
  assert.deepEqual(catalog, districts);
  assert.deepEqual(initial, [row("광진구")]);
  assert.equal(seen.length, 2);
  assert.deepEqual(await readHomes(["광진구"]), initial);
  assert.equal(seen.length, 2);
  assert.deepEqual(await readHomes(["강서구"]), [row("강서구")]);
  assert.equal(seen.length, 3);
  assert.ok(seen.every(url => url.startsWith("/api/")));
});

test("a missing district endpoint uses the existing DB endpoint only once", async t => {
  const seen = [];
  t.mock.method(globalThis, "fetch", async url => {
    seen.push(url);
    return url === "/api/districts" ? new Response(null, {status: 404})
      : reflect([row("광진구"), row("강서구")]);
  });
  const {readDistricts, readHomes} = await fresh("compatibility");
  assert.deepEqual(await readDistricts(), [{name: "광진구"}, {name: "강서구"}]);
  assert.deepEqual(await readHomes(["강서구"]), [row("강서구")]);
  assert.deepEqual(await readHomes(["광진구"]), [row("광진구")]);
  assert.deepEqual(seen, ["/api/districts", "/api/complexes?include_sort=true"]);
});

test("an aborted request does not invalidate the shared district response", async t => {
  const controller = new AbortController();
  let resolveRows;
  const started = new Promise(resolve => { resolveRows = resolve; });
  t.mock.method(globalThis, "fetch", url => url === "/api/districts" ? reflect(districts) : started);
  const {readDistricts, readHomes} = await fresh("abort");
  await readDistricts();
  const cancelled = readHomes(["광진구"], controller.signal);
  const other = readHomes(["광진구"]);
  await Promise.resolve();
  controller.abort();
  resolveRows(reflect([row("광진구")]));
  await assert.rejects(cancelled, {name: "AbortError"});
  assert.deepEqual(await other, [row("광진구")]);
});

test("whole trade history comes directly from the existing DB API", async t => {
  const trades = [{contract_date: "2026-10-01", is_cancelled: true}];
  const id = row("광진구").complex_id;
  t.mock.method(globalThis, "fetch", async raw => {
    const path = new URL(raw, "http://localhost");
    assert.equal(path.pathname, "/api/trades");
    assert.equal(path.searchParams.get("complex_id"), id);
    assert.equal(path.searchParams.get("area_group"), "60");
    return reflect(trades);
  });
  const {readTrades} = await fresh("trades");
  assert.deepEqual(await readTrades(id, 60), trades);
});

test("Pages forwards the district endpoint to the existing API origin", async t => {
  t.mock.method(globalThis, "fetch", async url => {
    assert.equal(url.toString(), "https://api.example.com/api/districts");
    return reflect(districts);
  });
  const response = await worker.fetch(new Request("https://jipfit.example/api/districts"), {
    API_ORIGIN: "https://api.example.com",
  });
  assert.deepEqual(await response.json(), districts);
});

test("viewport and zoom cluster behavior stay the same", () => {
  const points = [0, 1, 2].map(i => ({complex_id: String(i), latitude: 37.55, longitude: 127.1}));
  assert.deepEqual(clusterMarkers(points, 11, "0").map(p => p.count || 1), [1, 2]);
  assert.deepEqual(clusterMarkers(points, 15), points);
  assert.deepEqual(districtNames(districts, [37.52, 127.06, 37.56, 127.09]), ["광진구"]);
});
