import React, { useRef, useState } from "react";
import { loadMaps } from "./DistrictMap";
import { exactAddressCandidates } from "./geocode-utils.cjs";

// 추가 코드
import allowedComplexIds from "./geocoding-pending439.json";
import fallback12 from "./geocoding-fallback12.json";

const allowedIds = new Set(allowedComplexIds);
const retryMap = new Map(fallback12.map(item => [item.complex_id, item]));
const retryIds = new Set(retryMap.keys());
// 주소 검색 결과 후보 중에서 정확히 일치하는 주소만 반환

const prefix = "jipfit-geocode-v1:";
export function cachedPoint(address) {
  try {
    const p = JSON.parse(localStorage.getItem(prefix + address));
    if (Number.isFinite(p?.latitude) && Number.isFinite(p?.longitude)) return p;
  } catch {}
  return null;
}

const pendingPrefix = "jipfit-geocode-pending439-v1:";

function pendingPoint(address) {
  try {
    const p = JSON.parse(localStorage.getItem(pendingPrefix + address));
    if (Number.isFinite(p?.latitude) && Number.isFinite(p?.longitude)) {
      return p;
    }
  } catch {}
  return null;
}



const retryPrefix = "jipfit-geocode-retry12-v1:";
function retryPoint(complexId) {
  try {
    const p = JSON.parse(localStorage.getItem(retryPrefix + complexId));
    if (Number.isFinite(p?.latitude) && Number.isFinite(p?.longitude)) return p;
  } catch {}
  return null;
}

const normalize = value => String(value || "").trim().replace(/\s+/g, " ");
function candidatePoint(query, mode, candidate) {
  const expected = normalize(query);
  const actual = normalize(mode === "road" ? candidate.roadAddress : candidate.jibunAddress);
  const latitude = Number(candidate.y);
  const longitude = Number(candidate.x);
  if (!(actual === expected || actual.startsWith(expected + " "))) return null;
  if (!Number.isFinite(latitude) || !Number.isFinite(longitude)) return null;
  if (latitude < 37.3 || latitude > 37.8 || longitude < 126.7 || longitude > 127.3) return null;
  return { latitude, longitude };
}

function lookup(service, address, mode = "jibun") {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("주소 검색 응답 시간 초과")), 12000);
    service.geocode({ query: address }, (status, response) => {
      clearTimeout(timer);
      if (status !== service.Status.OK) return reject(new Error("네이버 Geocoding 설정과 호출 한도를 확인해 주세요."));
      const candidates = mode === "jibun"
        ? exactAddressCandidates(address, response?.v2?.addresses).map(candidate => ({
            latitude: Number(candidate.y), longitude: Number(candidate.x)
          }))
        : (response?.v2?.addresses || []).map(candidate => candidatePoint(address, mode, candidate)).filter(Boolean);
      resolve(candidates.length === 1 ? candidates[0] : null);
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
  const retryScope = scoped.filter(c => retryIds.has(c.complex_id));
  async function collect() {
    if (running.current) return;
    running.current = true;
    stop.current = false;
    setBusy(true);
    let found = 0, skipped = 0;
    try {
      const service = (await loadMaps()).Service;
      if (!service?.geocode) throw new Error("네이버 지도 Geocoding을 사용할 수 없습니다.");
      for (const complex of retryScope) {
        if (stop.current) break;
        const retry = retryMap.get(complex.complex_id);
        if (!retry || retryPoint(complex.complex_id)) {
          if (retryPoint(complex.complex_id)) found++;
          continue;
        }
        const point = await lookup(service, retry.query, retry.mode);
        if (point) {
          localStorage.setItem(retryPrefix + complex.complex_id, JSON.stringify(point));
          found++;
        } else {
          skipped++;
        }
        setMessage(`12개 보정 주소 재조회 · ${found + skipped}/${retryScope.length}개 확인 · ${found}개 저장 · ${skipped}개 미확정`);
        await new Promise(resolve => setTimeout(resolve, 220));
      }
      if (!retryScope.length) setMessage("재조회할 단지가 없습니다.");
    } catch (error) {
      setMessage(`${error.message} · 보정 주소 좌표 확보 ${found}개`);
    } finally {
      running.current = false;
      setBusy(false);
      onChange();
    }
  }
  function download() {
    const coordinates = {};
    for (const c of scoped) {
      const retry = retryMap.get(c.complex_id);
      const point = (retry ? retryPoint(c.complex_id) : null) || pendingPoint(c.address);
      if (!point) continue;
      coordinates[c.complex_id] = {
        address: c.address,
        lat: point.latitude,
        lng: point.longitude,
        ...(retry && retryPoint(c.complex_id) ? {
          geocoded_query: retry.query,
          coordinate_source: retry.historical_site_only ? "current_redeveloped_site" : "corrected_address",
          review_required: true
        } : {})
      };
    }
    if (!Object.keys(coordinates).length) return setMessage("내보낼 수집 좌표가 없습니다.");
    const url = URL.createObjectURL(new Blob([JSON.stringify({ schemaVersion: 1, coordinates })], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url; link.download = "jipfit-coordinates.json"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    setMessage(`수집 좌표 ${Object.keys(coordinates).length}개 내보내기 완료 · 보정 주소는 DB 반영 전 검토 필요`);
  }
  return <div><button type="button" disabled={busy || !retryScope.length} onClick={collect}>좌표 수집하기</button>
    {busy && <button type="button" onClick={() => { stop.current = true; }}>수집 중지</button>}
    <button type="button" disabled={busy} onClick={download}>수집 좌표 내보내기</button>
    <span role="status">{message}</span></div>;
}
