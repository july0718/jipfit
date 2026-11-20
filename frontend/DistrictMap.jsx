import React, { useEffect, useRef, useState } from "react";

let sdk;
function loadMaps() {
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
        encodeURIComponent(window.NAVER_MAP_CLIENT_ID);
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

export default function DistrictMap({ complexes, onSelect, selectedId }) {
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
      center: new maps.LatLng(37.546, 127.086), zoom: 13, zoomControl: true
    });
    instance.current = map;
    return () => { instance.current = null; map.destroy?.(); };
  }, [maps]);
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
      const inside = complexes.filter(c => c.latitude != null && c.longitude != null &&
        bounds.hasLatLng(new maps.LatLng(c.latitude, c.longitude)));
      markers = capped(inside, limit, selectedId).map(c => {
        const marker = new maps.Marker({
          map: instance.current, position: new maps.LatLng(c.latitude, c.longitude),
          title: c.name, zIndex: selectedId === c.complex_id ? 100 : 1,
          icon: { content: '<span class="pin ' +
            (selectedId === c.complex_id ? 'selected' : c.groups.some(g => g.predictable) ? 'ready' : '') +
            '"></span>', anchor: new maps.Point(8, 8) }
        });
        const listener = maps.Event.addListener(marker, "click", () => selectRef.current(c));
        return { marker, listener };
      });
    }
    const idle = maps.Event.addListener(map, "idle", redraw);
    redraw();
    return () => { maps.Event.removeListener(idle); clear(); };
  }, [maps, complexes, selectedId, limit]);
  useEffect(() => {
    const c = complexes.find(item => item.complex_id === selectedId);
    if (instance.current && c?.latitude != null && c.longitude != null)
      instance.current.panTo(new maps.LatLng(c.latitude, c.longitude));
  }, [selectedId, maps, complexes]);
  const positioned = complexes.filter(c => c.latitude != null && c.longitude != null);
  return <><label>현재 지도 범위의 마커<select value={limit} onChange={e => setLimit(Number(e.target.value))}>
    <option value={20}>20개</option><option value={100}>100개</option><option value={Infinity}>전체</option>
  </select></label><div className="map-box">
    <div ref={container} className="district-map" aria-label="광진구 단지 지도"/>
    {failure ? <div className="coordinate-map" aria-label="광진구 좌표 지도">
      <p>단지 위치도 · {failure}</p>
      {capped(positioned.filter(c => c.longitude >= 127.05 && c.longitude <= 127.125 && c.latitude >= 37.521 && c.latitude <= 37.581), limit, selectedId).map(c => <button key={c.complex_id} title={c.name} aria-label={c.name}
        className={"pin " + (selectedId === c.complex_id ? "selected" : c.groups.some(g => g.predictable) ? "ready" : "")}
        style={{left: ((c.longitude - 127.05) / 0.075 * 100) + "%", top: ((37.581 - c.latitude) / 0.06 * 100) + "%"}}
        onClick={() => onSelect(c)}/>)}
    </div>
      : !maps && <p className="map-status">지도를 불러오는 중입니다.</p>}
  </div></>;
}


