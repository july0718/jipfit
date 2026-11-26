import { test } from "node:test";
import assert from "node:assert/strict";
import worker from "./_worker.js";
import {
  clusterMarkers,
  districtNames,
  districtCenter,
  eligibleForMap,
  hasValidCoordinates,
  isDemolitionConfirmed,
  isMapPositioned,
  positionedComplexes,
  pricedGroup,
} from "./mapData.js";
import { mergeHomeRow } from "./staticData.js";

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
    if (url.startsWith("/metadata/")) {
      const name = districts.find(d => url.includes(d.code)).name;
      return reflect([{...row(name), built: 2000}]);
    }
    const query = new URL(url, "http://localhost").searchParams;
    return reflect([row(query.get("district"))]);
  });
  const {readDistricts, readHomes} = await fresh("scoped");
  const [catalog, initial] = await Promise.all([readDistricts(), readHomes(["광진구"])]);
  assert.deepEqual(catalog, districts);
  assert.deepEqual(initial, [{...row("광진구"), built: 2000}]);
  assert.equal(seen.length, 3);
  assert.deepEqual(await readHomes(["광진구"]), initial);
  assert.equal(seen.length, 3);
  assert.deepEqual(await readHomes(["강서구"]), [{...row("강서구"), built: 2000}]);
  assert.equal(seen.length, 5);
  assert.ok(seen.some(url => url.includes("live_only=true")));
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
  t.mock.method(globalThis, "fetch", url => url === "/api/districts" ? reflect(districts) : url.startsWith("/metadata/") ? reflect([{complex_id: row("광진구").complex_id}]) : started);
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

test("district navigation never falls back to City Hall while another ward is loading", () => {
  const gwangjin = {complex_id: "서울특별시 광진구 화양동|1|단지", latitude: 37.55, longitude: 127.07};
  const gangnam = {complex_id: "서울특별시 강남구 역삼동|1|단지", latitude: 37.50, longitude: 127.04};
  const bounds = [37.46, 127.02, 37.54, 127.12];

  // The old ward's homes can remain on screen while readHomes resolves.
  assert.equal(districtCenter("강남구", [gwangjin], null), null);
  const fallback = districtCenter("강남구", [gwangjin], bounds);
  assert.ok(Math.abs(fallback[0] - 37.5) < 1e-9);
  assert.ok(Math.abs(fallback[1] - 127.07) < 1e-9);
  assert.deepEqual(districtCenter("강남구", [gwangjin, gangnam], bounds), [gangnam.latitude, gangnam.longitude]);

  // The whole-Seoul view alone is allowed to use the City Hall center.
  assert.deepEqual(districtCenter("", [], null), [37.5665, 126.978]);
});

test("fresh DB coordinates replace the monthly copy and newly listed complexes load full data", async t => {
  const existing = row("광진구");
  const added = {...existing, complex_id: existing.complex_id + " 신규"};
  const seen = [];
  t.mock.method(globalThis, "fetch", async url => {
    seen.push(url);
    if (url === "/api/districts") return reflect(districts);
    if (url.startsWith("/metadata/")) return reflect([{...existing, name: "이전 이름", latitude: null, longitude: null}]);
    const current = {...existing, name: "현재 이름", latitude: 37.55, longitude: 127.07};
    if (url.includes("live_only=true")) return reflect([current, added]);
    return reflect([current, {...added, address: "서울 광진구 신규", latitude: 37.56, longitude: 127.08, marker_price_manwon: 100000}]);
  });
  const {readHomes} = await fresh("daily-coordinates");
  const [existingHome, newHome] = await readHomes(["광진구"]);
  assert.equal(existingHome.latitude, 37.55);
  assert.equal(existingHome.name, "현재 이름");
  assert.equal(newHome.longitude, 127.08);
  assert.equal(newHome.marker_price_manwon, 100000);
  assert.ok(seen.some(url => url.startsWith("/api/complexes?") && !url.includes("live_only")));
});

test("invalidateHomesCache forces a fresh fetch even before the 5-minute TTL expires", async t => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async url => {
    if (url === "/api/districts") return reflect(districts);
    if (url.startsWith("/metadata/")) return reflect([{...row("광진구"), built: 2000}]);
    calls += 1;
    return reflect([{...row("광진구"), marker_price_manwon: 100000 + calls}]);
  });
  const {readHomes, invalidateHomesCache} = await fresh("invalidate");
  const first = await readHomes(["광진구"]);
  const cached = await readHomes(["광진구"]);
  assert.equal(calls, 1);
  assert.deepEqual(cached, first);
  invalidateHomesCache();
  const refreshed = await readHomes(["광진구"]);
  assert.equal(calls, 2);
  assert.equal(refreshed[0].marker_price_manwon, 100002);
});

test("Pages worker forwards /api/metadata-revision to the API origin", async t => {
  t.mock.method(globalThis, "fetch", async url => {
    assert.equal(url.toString(), "https://api.example.com/api/metadata-revision");
    return reflect({revision: "0123456789abcdef0123456789abcdef"});
  });
  const response = await worker.fetch(new Request("https://jipfit.example/api/metadata-revision"), {
    API_ORIGIN: "https://api.example.com",
  });
  assert.deepEqual(await response.json(), {revision: "0123456789abcdef0123456789abcdef"});
});

test("fresh live address and coordinates override stale monthly metadata without redundant full fetch", async t => {
  const existing = row("광진구");
  const added = {
    ...existing,
    complex_id: existing.complex_id + " 신규",
    address: "서울특별시 광진구 구의동 99",
    built: 2024,
    latitude: 37.56,
    longitude: 127.08,
  };
  const seen = [];
  t.mock.method(globalThis, "fetch", async url => {
    seen.push(url);
    if (url === "/api/districts") return reflect(districts);
    if (url.startsWith("/metadata/")) {
      return reflect([{
        ...existing,
        address: "서울특별시 광진구 구의동 1",
        built: 1995,
        latitude: 37.50,
        longitude: 127.00,
        facilities: {households: 500},
      }]);
    }
    return reflect([
      {...existing, address: "서울특별시 광진구 구의동 2", built: 1997, latitude: 37.55, longitude: 127.07},
      added,
    ]);
  });
  const {readHomes} = await fresh("live-address-consistent");
  const [updated, newlyAdded] = await readHomes(["광진구"]);
  assert.equal(updated.address, "서울특별시 광진구 구의동 2");
  assert.equal(updated.built, 1997);
  assert.equal(updated.latitude, 37.55);
  assert.equal(updated.longitude, 127.07);
  assert.deepEqual(updated.facilities, {households: 500});
  assert.equal(newlyAdded.address, "서울특별시 광진구 구의동 99");
  assert.equal(newlyAdded.built, 2024);
  assert.ok(!seen.some(url => url.startsWith("/api/complexes?") && !url.includes("live_only")));
});

test("common map positioning excludes confirmed demolished or unpositioned complexes across markers and clusters", () => {
  const presentComplex = {
    complex_id: "서울특별시 광진구 구의동|631-1|현대프라임",
    name: "현대프라임",
    latitude: 37.5374,
    longitude: 127.0976,
    trade_count: 15,
    demolition_confirmed: false,
    groups: [{area_group: 85, predictable: true, marker_price_manwon: 169000, demolition_confirmed: false}],
  };
  const demolishedComplex = {
    ...presentComplex,
    complex_id: "서울특별시 광진구 구의동|100|멸실확정단지",
    name: "멸실확정단지",
    demolition_confirmed: true,
    groups: [{area_group: 85, predictable: true, marker_price_manwon: 150000, demolition_confirmed: true}],
  };
  const pendingComplex = {
    ...presentComplex,
    complex_id: "서울특별시 광진구 구의동|200|판정보류단지",
    name: "판정보류단지",
    demolition_confirmed: false,
    groups: [{area_group: 85, predictable: false, marker_price_manwon: 130000, demolition_confirmed: false}],
  };
  const nullCoordComplex = {
    ...presentComplex,
    complex_id: "서울특별시 광진구 구의동|300|주소정정대기단지",
    name: "주소정정대기단지",
    latitude: null,
    longitude: null,
  };

  assert.equal(isDemolitionConfirmed(presentComplex), false);
  assert.equal(isDemolitionConfirmed(demolishedComplex), true);
  assert.equal(hasValidCoordinates(presentComplex), true);
  assert.equal(hasValidCoordinates(nullCoordComplex), false);
  assert.equal(eligibleForMap(presentComplex), true);
  assert.equal(eligibleForMap(pendingComplex), true);
  assert.equal(eligibleForMap(demolishedComplex), false);
  assert.equal(isMapPositioned(demolishedComplex), false);
  assert.equal(isMapPositioned(nullCoordComplex), false);

  const positioned = positionedComplexes([
    presentComplex,
    demolishedComplex,
    pendingComplex,
    nullCoordComplex,
  ]);
  assert.deepEqual(
    positioned.map(c => c.complex_id),
    [presentComplex.complex_id, pendingComplex.complex_id]
  );

  // Even when the demolished complex is selectedId, clusterMarkers(positioned, ...) never includes it.
  const clustered = clusterMarkers(positioned, 11, demolishedComplex.complex_id);
  assert.equal(clustered.some(pin => pin.complex_id === demolishedComplex.complex_id), false);
});

test("live NULL coordinates and demolition_confirmed override stale static JSON metadata", async t => {
  const addrCleared = {
    complex_id: "서울특별시 광진구 구의동|631-1|주소변경",
    area_group: 85,
  };
  const demolished = {
    complex_id: "서울특별시 광진구 구의동|100|멸실단지",
    area_group: 85,
  };

  // Direct unit check on mergeHomeRow
  const mergedCleared = mergeHomeRow(
    {...addrCleared, address: "서울특별시 광진구 구의동 631-1", latitude: 37.53, longitude: 127.09, demolition_confirmed: false},
    {...addrCleared, address: "서울특별시 광진구 구의동 631-2", latitude: null, longitude: null, demolition_confirmed: false},
  );
  assert.equal(mergedCleared.latitude, null);
  assert.equal(mergedCleared.longitude, null);

  const mergedDemolished = mergeHomeRow(
    {...demolished, address: "서울특별시 광진구 구의동 100", latitude: 37.54, longitude: 127.08},
    {...demolished, address: "서울특별시 광진구 구의동 100", latitude: 37.54, longitude: 127.08, demolition_confirmed: true},
  );
  assert.equal(mergedDemolished.demolition_confirmed, true);

  t.mock.method(globalThis, "fetch", async url => {
    if (url === "/api/districts") return reflect(districts);
    if (url.startsWith("/metadata/")) {
      return reflect([
        {...addrCleared, address: "서울특별시 광진구 구의동 631-1", built: 1997, latitude: 37.53, longitude: 127.09},
        {...demolished, address: "서울특별시 광진구 구의동 100", built: 1988, latitude: 37.54, longitude: 127.08, demolition_confirmed: false},
      ]);
    }
    return reflect([
      {...addrCleared, address: "서울특별시 광진구 구의동 631-2", built: 1997, latitude: null, longitude: null, marker_price_manwon: 150000, trade_count: 10, demolition_confirmed: false},
      {...demolished, address: "서울특별시 광진구 구의동 100", built: 1988, latitude: 37.54, longitude: 127.08, marker_price_manwon: 140000, trade_count: 8, demolition_confirmed: true},
    ]);
  });

  const {readHomes} = await fresh("stale-static-override");
  const rows = await readHomes(["광진구"]);
  const complexes = rows.map(r => ({...r, groups: [r]}));
  assert.equal(rows[0].latitude, null);
  assert.equal(rows[0].longitude, null);
  assert.equal(rows[1].demolition_confirmed, true);
  assert.deepEqual(positionedComplexes(complexes), []);
});
