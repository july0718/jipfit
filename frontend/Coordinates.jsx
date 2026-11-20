import React, { useRef, useState } from "react";
import { loadMaps } from "./DistrictMap";
import { exactAddressCandidates } from "./geocode-utils.cjs";

// 추가 코드
import allowedComplexIds from "./geocoding-allowlist.json";

const allowedIds = new Set(allowedComplexIds);
// 주소 검색 결과 후보 중에서 정확히 일치하는 주소만 반환

const prefix = "jipfit-geocode-v1:";
export function cachedPoint(address) {
  try {
    const p = JSON.parse(localStorage.getItem(prefix + address));
    if (Number.isFinite(p?.latitude) && Number.isFinite(p?.longitude)) return p;
  } catch {}
  return null;
}

function lookup(service, address) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("주소 검색 응답 시간 초과")), 12000);
    service.geocode({ query: address }, (status, response) => {
      clearTimeout(timer);
      if (status !== service.Status.OK) return reject(new Error("네이버 Geocoding 설정과 호출 한도를 확인해 주세요."));
      const candidates = exactAddressCandidates(address, response?.v2?.addresses);
      resolve(candidates.length === 1 ? { latitude: Number(candidates[0].y), longitude: Number(candidates[0].x) } : null);
    });
  });
}

export default function Coordinates({ complexes, district, onChange }) {
  const running = useRef(false);
  const stop = useRef(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const scoped = complexes.filter(c =>
    allowedIds.has(c.complex_id) &&
    (!district || c.address.split(" ")[1] === district)
  );
  async function collect() {
    if (running.current) return;
    running.current = true;
    stop.current = false;
    setBusy(true);
    let found = 0, skipped = 0;
    try {
      const service = (await loadMaps()).Service;
      if (!service?.geocode) throw new Error("네이버 지도 Geocoding을 사용할 수 없습니다.");
      const known = new Map(scoped.filter(c => c.latitude != null && c.longitude != null)
        .map(c => [c.address, { latitude: Number(c.latitude), longitude: Number(c.longitude) }]));
      const addresses = new Set(
        scoped
          .filter(c => c.latitude == null || c.longitude == null)
          .map(c => c.address)
      );
      for (const address of addresses) {
        if (stop.current) break;
        const point = cachedPoint(address) || await lookup(service, address);
        if (point) {
          localStorage.setItem(prefix + address, JSON.stringify(point));
          found++;
        } else skipped++;
        setMessage(`${district || "전체 지역"} · ${found + skipped}/${addresses.size}개 주소 확인 · ${found}개 저장 · ${skipped}개 미확정`);
        await new Promise(resolve => setTimeout(resolve, 220));
      }
      if (!addresses.size) setMessage("선택한 지역의 모든 단지에 좌표가 있습니다.");
    } catch (error) { setMessage(`${error.message} · 이번에 저장한 주소 ${found}개`); }
    finally { running.current = false; setBusy(false); onChange(); }
  }
  function download() {
    const coordinates = {};
    for (const c of scoped) {
      const point = cachedPoint(c.address);
      if (point) coordinates[c.complex_id] = { address: c.address, lat: point.latitude, lng: point.longitude };
    }
    if (!Object.keys(coordinates).length) return setMessage("내보낼 수집 좌표가 없습니다.");
    const url = URL.createObjectURL(new Blob([JSON.stringify({ schemaVersion: 1, coordinates })], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url; link.download = "jipfit-coordinates.json"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return <div><button type="button" disabled={busy || !scoped.length} onClick={collect}>좌표 수집하기</button>
    {busy && <button type="button" onClick={() => { stop.current = true; }}>수집 중지</button>}
    <button type="button" disabled={busy} onClick={download}>수집 좌표 내보내기</button>
    <span role="status">{message}</span></div>;
}
