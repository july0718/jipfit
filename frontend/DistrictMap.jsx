import React, { useEffect, useRef, useState } from "react";
import pendingCoordinateReviewIds from "./map-unverified-complexes.json";

// Coordinates can be archived without marking a complex as currently existing.
// Hide unverified complexes from map markers until existence status is adjudicated.
const unverifiedIds = new Set(pendingCoordinateReviewIds);
const eligibleForMap = complex => !unverifiedIds.has(complex.complex_id);

let sdk;
export function loadMaps() {
  if (window.naver?.maps?.Map) return Promise.resolve(window.naver.maps);
  if (!sdk) {
    sdk = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      let poll;
      const deadline = setTimeout(() => fail("지도 SDK 준비 시간 초과"), 12000);
      function clearTimers() {
        clearInterval(poll);
        clearTimeout(deadline);
      }
      function fail(message) {
        clearTimers();
        reject(new Error(message));
      }
      function checkReady() {
        const maps = window.naver?.maps;
        if (maps?.Map && maps.Marker && maps.LatLng && maps.Point && maps.Event) {
          clearTimers();
          resolve(maps);
        }
      }
      script.src = "https://oapi.map.naver.com/openapi/v3/maps.js?ncpKeyId=" +
        encodeURIComponent(window.NAVER_MAP_CLIENT_ID) + "&submodules=geocoder";
      script.onload = checkReady;
      script.onerror = () => fail("지도 스크립트 연결 실패");
      poll = setInterval(checkReady, 50);
      document.head.appendChild(script);
    });
  }
  return sdk;
}

function capped(items, limit, selectedId) {
  if (items.length <= limit) return items;
  const first = items.slice(0, limit);
  const selected = items.find(c => c.complex_id === selectedId);
  if (selected && !first.includes(selected)) first[first.length - 1] = selected;
  return first;
}

function priceLabel(c) {
  const group = c.groups.find(g => g.predictable) || c.groups[0];
  const value = group.marker_price_manwon;
  const price = value == null ? "가격 없음" :
    (Number(value) / 10000).toLocaleString("ko-KR", { maximumFractionDigits: 2 }) + "억";
  return { price, detail: group.area_group + "㎡군 · " + (group.predictable ? "기준가격" : "최근 실거래"),
    ready: group.predictable };
}

function markerClass(label, selected) {
  return "price-pin" + (label.ready ? " ready" : "") + (selected ? " selected" : "");
}

function markerContent(c, selected) {
  const label = priceLabel(c);
  const card = document.createElement("div");
  card.className = markerClass(label, selected);
  for (const [tag, text] of [["span", c.name], ["b", label.price], ["small", label.detail]]) {
    const line = document.createElement(tag);
    line.textContent = text;
    card.appendChild(line);
  }
  return card.outerHTML;
}

export default function DistrictMap({ district, allComplexes, complexes, onSelect, selectedId }) {
  const center = React.useMemo(() => {
    const points = allComplexes.filter(c => eligibleForMap(c) && c.latitude != null && c.longitude != null && c.address.split(" ")[1] === district);
    if (!points.length) return [37.5665, 126.978];
    return [points.reduce((s,c) => s + Number(c.latitude), 0) / points.length,
            points.reduce((s,c) => s + Number(c.longitude), 0) / points.length];
  }, [district, allComplexes]);
  const [lat, lng] = center;
  const width = district ? 0.12 : 0.65;
  const height = district ? 0.09 : 0.42;
  const [limit, setLimit] = useState(20);
  const container = useRef(null);
  const instance = useRef(null);
  const selectRef = useRef(onSelect);
  selectRef.current = onSelect;
  const [maps, setMaps] = useState(null);
  const [failure, setFailure] = useState("");
  useEffect(() => {
    let active = true;
    window.navermap_authFailure = () => {
      if (active) setFailure("네이버 지도 인증 실패");
    };
    loadMaps().then(api => {
      if (active) setMaps(api);
    }).catch(error => { if (active) setFailure(previous => previous || error.message); });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    if (!maps) return;
    const map = new maps.Map(container.current, {
      center: new maps.LatLng(lat, lng), zoom: district ? 13 : 11, zoomControl: true
    });
    instance.current = map;
    return () => { instance.current = null; map.destroy?.(); };
  }, [maps, district, lat, lng]);
  useEffect(() => {
    if (!instance.current) return;
    const map = instance.current;
    let markers = [];
    function clear() {
      markers.forEach(({ marker, listener }) => {
        maps.Event.removeListener(listener);
        marker.setMap(null);
      });
      markers = [];
    }
    function redraw() {
      const bounds = map.getBounds();
      if (!bounds) return;
      clear();
      const inside = complexes.filter(c => eligibleForMap(c) && c.latitude != null && c.longitude != null &&
        bounds.hasLatLng(new maps.LatLng(c.latitude, c.longitude)));
      markers = capped(inside, limit, selectedId).map(c => {
        const marker = new maps.Marker({
          map: instance.current, position: new maps.LatLng(c.latitude, c.longitude),
          title: c.name, zIndex: selectedId === c.complex_id ? 100 : 1,
          icon: { content: markerContent(c, selectedId === c.complex_id), anchor: new maps.Point(60, 58) }
        });
        const listener = maps.Event.addListener(marker, "click", () => selectRef.current(c));
        return { marker, listener };
      });
    }
    const idle = maps.Event.addListener(map, "idle", redraw);
    redraw();
    return () => { maps.Event.removeListener(idle); clear(); };
  }, [maps, complexes, selectedId, limit, district, lat, lng]);
  useEffect(() => {
    const c = complexes.find(item => item.complex_id === selectedId);
    if (instance.current && c && eligibleForMap(c) && c.latitude != null && c.longitude != null)
      instance.current.panTo(new maps.LatLng(c.latitude, c.longitude));
  }, [selectedId, maps, complexes]);
  const positioned = complexes.filter(c => eligibleForMap(c) && c.latitude != null && c.longitude != null);
  return <><label>현재 지도 범위의 마커<select value={limit} onChange={e => setLimit(Number(e.target.value))}>
    <option value={20}>20개</option><option value={100}>100개</option><option value={Infinity}>전체</option>
  </select></label><div className="map-box">
    <div ref={container} className="district-map" aria-label="단지 지도"/>
    {failure ? <div className="coordinate-map" aria-label="단지 좌표 지도">
      <p>단지 위치도 · {failure}</p>
      {capped(positioned.filter(c => Math.abs(c.longitude-lng) <= width/2 && Math.abs(c.latitude-lat) <= height/2), limit, selectedId).map(c => {
        const label = priceLabel(c);
        return <button key={c.complex_id} title={c.name} aria-label={c.name}
        className={markerClass(label, selectedId === c.complex_id)}
        style={{left: ((c.longitude - lng + width/2) / width * 100) + "%", top: ((lat + height/2 - c.latitude) / height * 100) + "%"}}
        onClick={() => onSelect(c)}><span>{c.name}</span><b>{label.price}</b><small>{label.detail}</small></button>; })}
    </div>
      : !maps && <p className="map-status">지도를 불러오는 중입니다.</p>}
  </div></>;
}


