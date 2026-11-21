import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";

import { ForecastChart, TradeChart, ComparisonChart } from "./PriceCharts";
import { districtOf, readDistricts, readHomes, readTrades } from "./staticData";
import { districtNames } from "./mapData";
import Account from "./Account";
import DistrictMap from "./DistrictMap";
import Coordinates, { cachedPoint } from "./Coordinates";

import { EMPTY_RANGES, matchingComplex, changePercent, pricePerPyeong } from "./housingAnalysis.mjs";

const MAX_COMPARED = 4;
const money = value => Math.round(value).toLocaleString("ko-KR") + "만 원";
const percent = new Intl.NumberFormat("ko-KR", {
  signDisplay: "exceptZero", minimumFractionDigits: 2, maximumFractionDigits: 2
});

async function readApi(url, signal) {
  const response = await fetch(url, { signal, cache: "no-store" });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const failure = new Error(typeof error.detail === "string" ? error.detail : "서버에 연결할 수 없습니다.");
    failure.status = response.status;
    throw failure;
  }
  return response.json();
}

async function readForecast(params, signal) {
  try {
    return await readApi("/api/predict?" + params, signal);
  } catch (error) {
    if (error.status === 404 && error.message === "선택한 단지와 면적군의 모델 입력이 없습니다.") return null;
    throw error;
  }
}

const seoulTime = value => new Date(value).toLocaleString("ko-KR", { timeZone: "Asia/Seoul" });

const schoolLevels = ["초등학교","중학교","고등학교"];
const emptyFacilityFilters = {households:"",parking:"",station:"",school:"",level:"초등학교"};
const schoolAt = (f, level) => f?.schools?.find(row => row[1] === level);
const nearbyLabel = row => row ? row[0] + " · " + row[1] + " · " + row[2].toLocaleString("ko-KR") + " m" : "정보 없음";
const facilityFields = [
  ["총세대수", f => f?.households != null ? f.households.toLocaleString("ko-KR") + "세대" : "정보 없음"],
  ["세대당 주차대수", f => f?.parking != null ? f.parking.toFixed(2) + "대/세대" : "정보 없음"],
  ["가까운 지하철역", f => nearbyLabel(f?.station)],
  ...schoolLevels.map(level => ["가까운 " + level, f => nearbyLabel(schoolAt(f,level))])
];
function matchesFacilities(f, filters) {
  const min = (value, bound) => bound === "" || (value != null && value >= Number(bound));
  const max = (value, bound) => bound === "" || (value != null && value <= Number(bound));
  return min(f?.households,filters.households) && min(f?.parking,filters.parking) &&
    max(f?.station?.[2],filters.station) && max(schoolAt(f,filters.level)?.[2],filters.school);
}
function FacilityPanel({ facilities }) {
  return <section><h3>단지 규모와 주변 시설</h3><dl className="facility-details">
    {facilityFields.map(([label,show])=><React.Fragment key={label}><dt>{label}</dt><dd>{show(facilities)}</dd></React.Fragment>)}
  </dl><small>거리 기준은 수록된 시설까지의 직선거리입니다. 학교 배정은 별도 확인이 필요합니다.{" "}
    <a href="/metadata/facility-sources.txt" target="_blank" rel="noreferrer">자료 기준 확인</a></small></section>;
}

function ComplexDetail({ id, built }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    readApi("/api/complex?" + new URLSearchParams({ complex_id: id }), controller.signal)
      .then(result => { if (!controller.signal.aborted) setDetail(result); })
      .catch(err => { if (!controller.signal.aborted) setError(err.message); });
    return () => controller.abort();
  }, [id]);
  if (error) return <p role="alert">단지 상세 조회 실패 · {error}</p>;
  if (!detail) return <p>단지 상세 조회 중…</p>;
  const year = built ?? detail.built;
  return <section><h2>{detail.name}</h2><p>{detail.address}</p>
    <p>{year > 0 ? year + "년 준공 · 경과 " + (new Date().getFullYear() - year) + "년" : "준공연도 미상"}
    {" · "}전용면적군 {detail.area_groups.map(g => g.area_group + "㎡").join(", ")}</p><p>거래 동기화 {detail.last_sync_at ? seoulTime(detail.last_sync_at) + " (한국시간)" : "기록 없음"}</p></section>;
}

function Results({ home, data }) {
  return <>
    <h2>{home.name} <small>{home.representative_area}㎡</small></h2>
    <p>{home.address}</p>
    <div className="reference"><span>입력 기준월 {data.as_of} · 기준가</span><strong>{money(data.reference_price_manwon)}</strong></div>
    <table><caption>기간별 예측</caption><thead><tr>
      <th>예측 기간</th><th>예측 매매가</th><th>기준가 대비</th>
    </tr></thead><tbody>{data.forecasts.map(f =>
      <tr key={f.months}><th scope="row">{f.months}개월 후</th><td>{money(f.price_manwon)}</td>
        <td className={f.return_percent >= 0 ? "positive" : "negative"}>{percent.format(f.return_percent)}%</td></tr>
    )}</tbody></table>
    <p>서울 통합 HGB 모델</p>
    <ForecastChart data={data}/>
    {data.calculated_at && <p>예측 계산 {seoulTime(data.calculated_at)} (한국시간)</p>}
  </>;
}

function TradeHistory({ complexId, areaGroup, forecast }) {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [page, setPage] = useState(0);
  const pageSize = 20;
  const [years, setYears] = useState(0);
  const visibleRows = useMemo(() => {
    if (!years || !rows) return rows;
    const today = new Date();
    const cutoff = new Date(Date.UTC(today.getUTCFullYear() - years, today.getUTCMonth(), today.getUTCDate()));
    return rows.filter(row => row.contract_date >= cutoff.toISOString().slice(0, 10));
  }, [rows, years]);

  useEffect(() => {
    const controller = new AbortController();
    setError("");
    readTrades(complexId, areaGroup, controller.signal)
      .then(result => { if (!controller.signal.aborted) setRows(result); })
      .catch(err => { if (!controller.signal.aborted) setError(err.message); });
    return () => controller.abort();
  }, [complexId, areaGroup, attempt]);

  const latest = rows?.find(row => !row.is_cancelled && Number(row.price_manwon) > 0);
  return <section className="trade-history"><h2>실거래 이력</h2>
    {error ? <p role="alert">{error} <button onClick={() => setAttempt(a => a + 1)}>다시 조회</button></p>
      : rows === null ? <p>실거래 이력을 불러오는 중입니다.</p>
      : rows.length === 0 ? <p>이 면적군에 등록된 실거래가 없습니다.</p>
      : <>{latest && <section><h3>최근 유효 실거래와 예측가</h3>
          <p>{latest.contract_date} · {latest.area}㎡ · {money(latest.price_manwon)}
            {pricePerPyeong(latest) != null && <> · 전용면적 1평당 {money(pricePerPyeong(latest))}</>}</p>
          {forecast && <table><caption>입력 기준월 {forecast.as_of} · 대표면적 {forecast.representative_area}㎡</caption>
            <thead><tr><th>기간</th><th>예측가</th><th>최근 실거래 대비 차액</th><th>변화율</th></tr></thead>
            <tbody>{forecast.forecasts.map(f => <tr key={f.months}><th>{f.months}개월</th><td>{money(f.price_manwon)}</td>
              <td>{money(f.price_manwon - latest.price_manwon)}</td><td>{percent.format(changePercent(f.price_manwon, latest.price_manwon))}%</td></tr>)}</tbody>
          </table>}</section>}<label>거래 조회 기간<select value={years} onChange={e => { setYears(Number(e.target.value)); setPage(0); }}>
            {[1, 3, 5, 0].map(n => <option key={n} value={n}>{n ? "최근 " + n + "년" : "전체 기간"}</option>)}
          </select></label><TradeChart rows={visibleRows}/><table><caption>최근 계약순 · 총 {visibleRows.length.toLocaleString("ko-KR")}건</caption>
          <thead><tr><th>계약일</th><th>전용면적 (㎡)</th><th>층</th><th>거래금액 (만 원)</th><th>전용평당가 (만 원/평)</th></tr></thead>
          <tbody>{visibleRows.slice(page * pageSize, (page + 1) * pageSize).map((row, index) =>
            <tr key={index}><td>{row.contract_date}{row.is_cancelled && <strong> · 계약 해제</strong>}</td><td>{Number(row.area).toLocaleString("ko-KR", { maximumFractionDigits: 6 })}</td>
              <td>{row.floor ?? "미상"}</td><td>{Number(row.price_manwon).toLocaleString("ko-KR")}</td><td>{pricePerPyeong(row)?.toLocaleString("ko-KR", {maximumFractionDigits: 0}) ?? "—"}</td></tr>
          )}</tbody></table>
          {visibleRows.length > pageSize && <div>
            <button disabled={page === 0} onClick={() => setPage(n => n - 1)}>앞 페이지</button>
            {" "}{page + 1} / {Math.ceil(visibleRows.length / pageSize)}{" "}
            <button disabled={(page + 1) * pageSize >= visibleRows.length} onClick={() => setPage(n => n + 1)}>이전 거래</button>
          </div>}</>}
  </section>;
}

function useComparedHome(item) {
  const [result, setResult] = useState(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!item) return;
    const controller = new AbortController();
    const params = new URLSearchParams({ complex_id: item.complex_id, area_group: item.area_group });
    const read = path => readApi(path + "?" + params, controller.signal);
    Promise.all([readTrades(item.complex_id, item.area_group, controller.signal), read("/api/complex"), readForecast(params, controller.signal)])
      .then(([rows, detail, forecast]) => {
        const trades = rows.filter(r => !r.is_cancelled);
        if (!controller.signal.aborted) setResult({ item, attempt, detail, forecast, trades, count: trades.length, latest: trades[0] });
      }).catch(err => {
        if (!controller.signal.aborted) setResult({ item, attempt, error: err.message });
      });
    return () => controller.abort();
  }, [item, attempt]);
  return { item, data: result?.item === item && result?.attempt === attempt ? result : null,
    retry: () => setAttempt(n => n + 1) };
}

function Comparison({ items, months, setMonths, remove, close }) {
  const dialog = useRef(null);
  const columns = [useComparedHome(items[0]), useComparedHome(items[1]),
    useComparedHome(items[2]), useComparedHome(items[3])].filter(column => column.item);
  useEffect(() => { dialog.current.showModal(); }, []);
  const fields = [
    ["예측 기준월", d => d.forecast ? d.forecast.as_of + (d.forecast.pilot ? " · 시범" : "") : "—"],
    ["전용면적군", d => d.item.area_group + "㎡군"],
    ["모델 대표면적", d => d.forecast ? d.forecast.representative_area + "㎡" : "—"],
    ["준공연도", d => (d.item.built === undefined ? d.detail.built : d.item.built) + "년"],
    ...facilityFields.map(([label, show]) => [label, d => show(d.item.facilities)]),
    ["현재 기준가격", d => d.forecast ? money(d.forecast.reference_price_manwon) : "산정값 없음"],
    [months + "개월 후 예상가격", (_, f) => f ? money(f.price_manwon) : "예측 없음"],
    ["예상 가격상승률", (_, f) => f ? percent.format(f.return_percent) + "%" : "—"],
    ["예상 차익", (d, f) => f ? money(f.price_manwon - d.forecast.reference_price_manwon) : "—"],
    ["이 면적군 유효 거래", d => d.count.toLocaleString("ko-KR") + "건"],
    ["최근 실거래", d => d.latest ? <>{money(d.latest.price_manwon)}<small>{d.latest.contract_date} · {d.latest.area}㎡ · {d.latest.floor ?? "미상"}층</small></> : "거래 없음"]
  ];
  return <dialog ref={dialog} className="compare-dialog" aria-labelledby="compare-title" onClose={close}>
    <div className="compare-heading"><h2 id="compare-title">단지 비교</h2><button onClick={() => dialog.current.close()}>닫기</button></div>
    {items.length === 0 ? <p>지도나 목록에서 단지와 면적군을 선택한 뒤 ‘이 면적군으로 비교에 담기’를 누르세요.</p> : <>
      <label>같은 예측 기간으로 보기<select value={months} onChange={e => setMonths(Number(e.target.value))}>
        {[3,6,12].map(m => <option key={m} value={m}>{m}개월</option>)}</select></label>
      <div className="compare-scroll"><table><thead><tr><th>비교 항목</th>{columns.map(({item,data,retry}, i) =>
        <th key={i}>{item ? <>{item.name}<small>{item.address}</small><button onClick={() => remove(item)}>비교에서 빼기</button>
          {data?.error && <small role="alert">{data.error} <button onClick={retry}>다시 조회</button></small>}</> : ""}</th>
      )}</tr></thead><tbody>{fields.map(([label, value]) => <tr key={label}><th scope="row">{label}</th>
        {columns.map(({item,data}, i) => <td key={i}>{!item || data?.error ? "—" : !data ? "조회 중…" :
          value(data, data.forecast?.forecasts.find(f => f.months === months))}</td>)}</tr>)}</tbody></table></div>
      <ComparisonChart columns={columns}/>
      <small>예상 차익은 기준가격 대비 변화이며 거래 비용은 포함하지 않습니다.</small></>}
  </dialog>;
}

function App() {
  const [homes, setHomes] = useState(null);
  const [listError, setListError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [home, setHome] = useState(null);
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [compared, setCompared] = useState([]);
  const [compareOpen, setCompareOpen] = useState(false);
  const [notice, setNotice] = useState("");
  const saved = compared.find(item => item.complex_id === home?.complex_id);
  function removeCompared(item) {
    setCompared(previous => previous.filter(c => c.complex_id !== item.complex_id));
    setNotice(item.name + "을 비교에서 뺐습니다.");
  }
  function toggleCompared() {
    if (saved?.area_group === home.area_group) return removeCompared(home);
    if (saved) setCompared(previous => previous.map(c => c.complex_id === home.complex_id ? home : c));
    else if (compared.length < MAX_COMPARED) setCompared(previous => [...previous, home]);
    else { setNotice("최대 네 단지를 비교합니다. 비교 창에서 한 단지를 빼 주세요."); return; }
    setNotice(home.name + " " + home.area_group + "㎡군을 비교에 담았습니다.");
  }
  const pending = useRef(null);

  const [query, setQuery] = useState("");
  const [facilityFilters,setFacilityFilters] = useState(emptyFacilityFilters);
  const [ranges, setRanges] = useState(EMPTY_RANGES);
  const [sort, setSort] = useState("count");
  const [horizon, setHorizon] = useState(3);
  const [district, setDistrict] = useState("광진구");
  const [districts, setDistricts] = useState([]);
  const [viewport, setViewport] = useState(null);
  const [all, setAll] = useState(false);
  const [listPage, setListPage] = useState(0);
  const onCoordinates = useCallback(() => setCoordinateRevision(n => n + 1), []);
  const onViewport = useCallback(next => {
    if (district) return;
    setViewport(previous => previous && next.every((v, i) => v === previous[i]) ? previous : next);
  }, [district]);
  const scope = JSON.stringify(district ? [district] : all || query.trim()
    ? districts.map(d => d.name) : districtNames(districts, viewport));
  const [onlyReady, setOnlyReady] = useState(false);
  const [coordinateRevision, setCoordinateRevision] = useState(0);
  const complexes = useMemo(() => {
    const index = new Map();
    for (const item of homes || []) {
      if (!index.has(item.complex_id)) index.set(item.complex_id, { ...item, ...(item.latitude == null || item.longitude == null ? cachedPoint(item.address) : {}), groups: [] });
      index.get(item.complex_id).groups.push(item);
    }
    return Array.from(index.values());
  }, [homes, coordinateRevision]);
  const visible = useMemo(() => complexes.filter(c =>
    (!district || c.address.split(" ")[1] === district) &&
    (c.name + " " + c.address).includes(query.trim()) && matchesFacilities(c.facilities,facilityFilters)
  ).map(c => matchingComplex(c, ranges, onlyReady)).filter(Boolean).sort((a, b) => {
    if (sort === "recent") {
      const order = (b.last_trade || "").localeCompare(a.last_trade || "");
      if (order) return order;
    }
    if (sort === "return") {
      const left = a.sort_returns?.[horizon] ?? -Infinity;
      const right = b.sort_returns?.[horizon] ?? -Infinity;
      if (left !== right) return right > left ? 1 : -1;
    }
    return b.trade_count - a.trade_count || a.complex_id.localeCompare(b.complex_id, "ko");
  }), [complexes, query, facilityFilters, ranges, onlyReady, sort, horizon, district]);
  const selected = complexes.find(c => c.complex_id === home?.complex_id);
  

  useEffect(() => {
    const controller = new AbortController();
    setListError("");
    readDistricts().then(rows => { if (!controller.signal.aborted) setDistricts(rows); }, err => {
      if (!controller.signal.aborted) setListError(err.message);
    });
    return () => controller.abort();
  }, [attempt]);

  useEffect(() => {
    const controller = new AbortController();
    setListError("");
    setListPage(0);
    readHomes(JSON.parse(scope), controller.signal).then(rows => {
      if (!controller.signal.aborted) setHomes(rows);
    }).catch(err => {
      if (!controller.signal.aborted) setListError(err.message);
    });
    return () => controller.abort();
  }, [attempt, scope]);
  useEffect(() => () => pending.current?.abort(), []);

  const select = useCallback(async item => {
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setHome(item);
    setData(undefined);
    setError("");
    const params = new URLSearchParams({ complex_id: item.complex_id, area_group: item.area_group });
    try {
      const result = await readForecast(params, controller.signal);
      if (!controller.signal.aborted) setData(result);
    } catch (err) {
      if (!controller.signal.aborted) setError(err.message);
    }
  }, []);
  const selectComplex = useCallback(c => select(c.groups.find(g => g.predictable) || c.groups[0]), [select]);
  const resolveHome = useCallback(async (id, area) => {
    const groups = await readHomes([districtOf(id)]);
    const item = groups.find(g => g.complex_id === id && (area == null || g.area_group === area));
    if (!item) throw new Error("저장한 단지 또는 면적군을 찾을 수 없습니다.");
    return item;
  }, []);
  const selectSaved = useCallback(item => {
    setDistrict(districtOf(item.complex_id));
    select(item);
  }, [select]);
  const openSavedComparison = useCallback((items, months) => {
    setCompared(items);
    setHorizon(months);
    setCompareOpen(true);
  }, []);
  const listSize = 100;
  const pageCount = Math.max(1, Math.ceil(visible.length / listSize));
  const page = Math.min(listPage, pageCount - 1);

  return <>
    <header><div className="brand">집핏</div><span>서울 아파트</span><button className="open-compare" onClick={() => setCompareOpen(true)}>단지 비교 ({compared.length}/{MAX_COMPARED})</button></header>
    {compareOpen && <Comparison items={compared} months={horizon} setMonths={setHorizon} remove={removeCompared} close={() => setCompareOpen(false)}/>}
    <main><h1>아파트 매매가 예측</h1><Account home={home} homes={homes} resolveHome={resolveHome} onSelect={selectSaved} compared={compared} horizon={horizon}
      onOpenComparison={openSavedComparison}/><label>지역<select value={district} onChange={e => { setDistrict(e.target.value); setViewport(null); setAll(false); pending.current?.abort(); setHome(null); setData(null); setError(""); }}>
        {Array.from(new Set(["광진구", ...districts.map(item => item.name)])).sort((a,b) => a.localeCompare(b,"ko")).map(name =>
          <option value={name} key={name}>{name}</option>)}<option value="">전체 지역</option>
      </select></label><Coordinates complexes={complexes} district={district} onChange={onCoordinates}/><DistrictMap district={district} allComplexes={complexes} complexes={visible} onSelect={selectComplex} selectedId={home?.complex_id} onViewport={onViewport}/>
      <p className="map-key">초록색은 예측 가능한 단지, 회색은 실거래만 있는 단지입니다.</p>
      <div className="layout">
      <section className="panel"><h2>단지·면적 선택</h2>
        {listError ? <p role="alert">{listError} <button onClick={() => setAttempt(attempt + 1)}>다시 불러오기</button></p>
          : homes === null ? <p>단지를 불러오는 중입니다.</p>
          : homes.length === 0 ? <p>등록된 단지가 없습니다.</p>
          : <><label>단지명 또는 주소<input type="search" value={query} onChange={e => { setQuery(e.target.value); setListPage(0); }}/></label>
              <label><input type="checkbox" checked={onlyReady} onChange={e => { setOnlyReady(e.target.checked); setListPage(0); }}/> 예측 가능한 단지만</label>
              <fieldset className="range-filters"><legend>상세 검색</legend>
                {[["최근 유효 실거래가 (억 원)", "price", "0.01"], ["면적군 대표 전용면적 (㎡)", "area", "0.01"], ["준공연도 (년)", "year", "1"]].map(([label,key,step]) =>
                  <div key={key}><span>{label}</span>{[["Min","최소"],["Max","최대"]].map(([suffix,hint]) =>
                    <input key={suffix} type="number" min="0" step={step} aria-label={label + " " + hint} placeholder={hint}
                      value={ranges[key + suffix]} onChange={e => { setRanges({...ranges, [key + suffix]: e.target.value}); setListPage(0); }}/>)}
                    {ranges[key+"Min"] && ranges[key+"Max"] && Number(ranges[key+"Min"]) > Number(ranges[key+"Max"]) &&
                      <small>최솟값이 최댓값보다 큽니다.</small>}</div>)}
                {[["households","최소 세대수 (세대)"],["parking","최소 세대당 주차대수 (대)"],["station","역까지 최대 직선거리 (m)"],["school","학교까지 최대 직선거리 (m)"]].map(([key,label]) =>
                  <label key={key}>{label}<input type="number" min="0" step={key==="parking" ? "0.01" : "1"} placeholder="제한 없음"
                    value={facilityFilters[key]} onChange={e=>{setFacilityFilters({...facilityFilters,[key]:e.target.value});setListPage(0);}}/></label>)}
                <label>학교 거리 기준<select value={facilityFilters.level} onChange={e=>{setFacilityFilters({...facilityFilters,level:e.target.value});setListPage(0);}}>
                  {schoolLevels.map(level=><option key={level}>{level}</option>)}</select></label>
                <button onClick={() => { setFacilityFilters(emptyFacilityFilters); setRanges(EMPTY_RANGES); setListPage(0); }}>범위 초기화</button>
              </fieldset><label>목록 정렬<select value={sort} onChange={e => { setSort(e.target.value); setListPage(0); }}>
                <option value="count">거래건수순</option><option value="recent">최근 거래순</option>
                <option value="return">선택 기간 예상 상승률순</option></select></label>
              {sort === "return" && <label>전망 기간<select value={horizon} onChange={e => setHorizon(Number(e.target.value))}>
                {[3, 6, 12].map(m => <option key={m} value={m}>{m}개월</option>)}</select></label>}
              {!district && !query.trim() && <button onClick={() => setAll(value => !value)}>
                {all ? "지도 범위 단지 목록" : "서울 전체 단지 목록"}
              </button>}
              <p>{visible.length}개 단지</p><div className="home-list">{visible.slice(page * listSize, (page + 1) * listSize).map(c =>
                <button className="home" key={c.complex_id} aria-pressed={selected?.complex_id === c.complex_id}
                  onClick={() => selectComplex(c)}><b>{c.name}</b><span>{c.address}</span>
                  {(ranges.priceMin || ranges.priceMax || ranges.areaMin || ranges.areaMax) && <span>{c.groups.map(g =>
                    g.representative_area + "㎡ · " + (g.marker_price_manwon ? money(g.marker_price_manwon) : "거래가 미상")).join(" / ")}</span>}
                  <span>{sort === "count" ? (c.trade_count ?? 0).toLocaleString("ko-KR") + "건"
                    : sort === "recent" ? c.last_trade || "거래 없음"
                    : c.sort_returns?.[horizon] == null ? "예측 없음"
                    : c.groups.find(g => g.predictable)?.area_group + "㎡군 · " + percent.format(c.sort_returns[horizon]) + "%"}</span></button>
              )}</div>{pageCount > 1 && <div>
                <button disabled={page === 0} onClick={() => setListPage(page - 1)}>앞 페이지</button>
                {" "}{page + 1} / {pageCount}{" "}
                <button disabled={page + 1 === pageCount} onClick={() => setListPage(page + 1)}>다음 페이지</button>
              </div>}</>}
      </section>
      <section className="panel result" aria-label="예측 결과" aria-live="polite">
        {selected && <ComplexDetail key={selected.complex_id} id={selected.complex_id} built={selected.built}/>}
        {selected && <FacilityPanel facilities={selected.facilities}/>}
        {selected && <label>면적군
          <select value={home.area_group} onChange={e => select(selected.groups.find(g => g.area_group === Number(e.target.value)))}>
            {selected.groups.map(g => <option key={g.area_group} value={g.area_group}>
              {g.area_group}㎡ · 대표 {g.representative_area}㎡{g.predictable ? "" : " · 예측 입력 없음"}
            </option>)}
          </select></label>}
        {home && <button onClick={toggleCompared}>{!saved ? "이 면적군으로 비교에 담기" :
          saved.area_group === home.area_group ? "비교에서 이 단지 빼기" : "비교 면적군을 현재 선택으로 변경"}</button>}
        {notice && <p role="status">{notice}</p>}
        {home && data === null ? <><h2>{home.name}</h2><p>이 면적군에는 예측에 필요한 월별 입력이 없습니다.</p></>
          : data ? <Results home={home} data={data}/>
          : error ? <><h2>예측 요청 실패</h2><p>{error}</p><button onClick={() => select(home)}>다시 예측</button></>
          : home ? <p>{home.name}의 예측값을 계산하고 있습니다.</p>
          : <><h2>예측 결과</h2><p>단지와 면적을 선택하면 기간별 예측 매매가가 표시됩니다.</p></>}
        {home && <TradeHistory key={JSON.stringify([home.complex_id, home.area_group])}
          complexId={home.complex_id} areaGroup={home.area_group} forecast={data}/>}
      </section>
    </div></main>
  </>;
}

createRoot(document.getElementById("root")).render(<App/>);
