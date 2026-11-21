import React, { useEffect, useRef, useState } from "react";
import { clusterMarkers } from "./mapData";
import pendingCoordinateReviewIds from "./map-unverified-complexes.json";

// Coordinates can be archived without marking a complex as currently existing.
// Hide unverified complexes from map markers until existence status is adjudicated.
const unverifiedIds = new Set(pendingCoordinateReviewIds);
const pricedGroup = complex => complex.groups?.find(group =>
  group.predictable && Number.isFinite(Number(group.marker_price_manwon)) &&
  Number(group.marker_price_manwon) > 0) || complex.groups?.find(group =>
  group.marker_price_manwon != null &&
  Number.isFinite(Number(group.marker_price_manwon)) &&
  Number(group.marker_price_manwon) > 0);
const eligibleForMap = complex => !["demolished", "unconfirmed"].includes(complex.existence_status) &&
  (complex.existence_status === "active" || !unverifiedIds.has(complex.complex_id)) &&
  Number(complex.trade_count) > 0 && Boolean(pricedGroup(complex));

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
  const group = pricedGroup(c);
  const value = group.marker_price_manwon;
  const price = (Number(value) / 10000).toLocaleString("ko-KR", { maximumFractionDigits: 2 }) + "억";
  return { price, detail: group.area_group + "㎡군 · 최근 실거래",
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

function DistrictMap({ district, allComplexes, complexes, onSelect, selectedId, onViewport }) {
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
  const redraw = useRef(null);
  const positioned = React.useMemo(() => complexes.filter(c =>
    eligibleForMap(c) && c.latitude != null && c.longitude != null), [complexes]);
  const props = useRef(null);
  props.current = {positioned, selectedId, limit, onSelect, onViewport};
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
      center: new maps.LatLng(37.5665, 126.978), zoom: 13, zoomControl: true
    });
    instance.current = map;
    const markers = new Map();
    let frame = null;
    function remove({marker, listener}) {
      maps.Event.removeListener(listener);
      marker.setMap(null);
    }
    function render() {
      frame = null;
      const bounds = map.getBounds();
      if (!bounds) return;
      const {positioned, selectedId, limit, onViewport} = props.current;
      const sw = bounds.getSW(), ne = bounds.getNE();
      onViewport([sw.lat(), sw.lng(), ne.lat(), ne.lng()]);
      const inside = positioned.filter(c => bounds.hasLatLng(new maps.LatLng(c.latitude, c.longitude)));
      const pins = capped(clusterMarkers(inside, map.getZoom(), selectedId), limit, selectedId);
      const shown = new Set(pins.map(pin => pin.complex_id));
      for (const [id, entry] of markers) {
        if (!shown.has(id)) {
          remove(entry);
          markers.delete(id);
        }
      }
      for (const pin of pins) {
        const id = pin.complex_id;
        const selected = id === selectedId;
        const label = pin.count ? String(pin.count) : markerContent(pin, selected);
        const signature = JSON.stringify([pin.latitude, pin.longitude, label, selected]);
        const existing = markers.get(id);
        if (existing?.signature === signature) {
          existing.pin = pin;
          continue;
        }
        if (existing) remove(existing);
        const position = new maps.LatLng(pin.latitude, pin.longitude);
        const marker = new maps.Marker({
          map, position, title: pin.count ? pin.count + "개 단지 확대" : pin.name,
          zIndex: selected ? 100 : 1,
          icon: pin.count
            ? {content: '<div class="cluster-pin">' + pin.count + '</div>', anchor: new maps.Point(20, 20)}
            : {content: label, anchor: new maps.Point(60, 58)}
        });
        const entry = {marker, pin, signature};
        entry.listener = maps.Event.addListener(marker, "click", () => {
          if (entry.pin.count) {
            map.setCenter(position);
            map.setZoom(Math.min(15, map.getZoom() + 2));
          } else props.current.onSelect(entry.pin);
        });
        markers.set(id, entry);
      }
    }
    function schedule() {
      if (frame === null) frame = requestAnimationFrame(render);
    }
    redraw.current = schedule;
    const idle = maps.Event.addListener(map, "idle", schedule);
    schedule();
    return () => {
      redraw.current = null;
      instance.current = null;
      centered.current = null;
      if (frame !== null) cancelAnimationFrame(frame);
      maps.Event.removeListener(idle);
      markers.forEach(remove);
      map.destroy?.();
    };
  }, [maps]);
  const centered = useRef(null);
  useEffect(() => {
    const map = instance.current;
    if (!map || (district && !allComplexes.length) || centered.current === district) return;
    centered.current = district;
    map.setCenter(new maps.LatLng(lat, lng));
    map.setZoom(district ? 13 : 11);
  }, [maps, district, allComplexes.length, lat, lng]);
  useEffect(() => { redraw.current?.(); }, [positioned, selectedId, limit]);
  const selectedPoint = positioned.find(item => item.complex_id === selectedId);
  const [showNearby,setShowNearby] = useState(false);
  const facilities = selectedPoint?.facilities;
  useEffect(() => {
    if (!maps || !instance.current || !showNearby || !facilities) return;
    const places = [facilities.station,...(facilities.schools || [])].filter(Boolean);
    const markers = places.map(([name,kind,,lat,lon]) => {
      const label = document.createElement("span");
      label.className = "facility-marker";
      label.textContent = name + " · " + kind;
      return new maps.Marker({map:instance.current,position:new maps.LatLng(lat,lon),title:name,icon:{content:label.outerHTML}});
    });
    return () => markers.forEach(marker => marker.setMap(null));
  },[maps,showNearby,facilities]);
  const selectedLat = selectedPoint?.latitude;
  const selectedLng = selectedPoint?.longitude;
  useEffect(() => {
    if (instance.current && selectedLat != null && selectedLng != null) {
      instance.current.panTo(new maps.LatLng(selectedLat, selectedLng));
    }
  }, [selectedId, maps, selectedLat, selectedLng]);
  useEffect(() => {
    if (failure) onViewport([lat-height/2, lng-width/2, lat+height/2, lng+width/2]);
  }, [failure, lat, lng, height, width, onViewport]);
  return <><label>현재 지도 범위의 마커<select value={limit} onChange={e => setLimit(Number(e.target.value))}>
    <option value={20}>20개</option><option value={100}>100개</option><option value={Infinity}>전체</option>
  </select></label>{maps && facilities && <label><input type="checkbox" checked={showNearby} onChange={e=>setShowNearby(e.target.checked)}/> 선택 단지 주변 역·학교 표시</label>}<div className="map-box">
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



export default React.memo(DistrictMap);
