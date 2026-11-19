import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";

import Account from "./Account";
import DistrictMap from "./DistrictMap";

const money = value => Math.round(value).toLocaleString("ko-KR") + "만 원";
const percent = new Intl.NumberFormat("ko-KR", {
  signDisplay: "exceptZero", minimumFractionDigits: 2, maximumFractionDigits: 2
});

async function readApi(url, signal) {
  const response = await fetch(url, { signal, cache: "no-store" });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === "string" ? error.detail : "서버에 연결할 수 없습니다.");
  }
  return response.json();
}

function Results({ home, data }) {
  return <>
    <h2>{home.name} <small>{home.representative_area}㎡</small></h2>
    <p>{home.address}</p>
    <div className="reference"><span>{data.as_of} 기준가</span><strong>{money(data.reference_price_manwon)}</strong></div>
    <table><caption>기간별 예측</caption><thead><tr>
      <th>예측 기간</th><th>예측 매매가</th><th>기준가 대비</th>
    </tr></thead><tbody>{data.forecasts.map(f =>
      <tr key={f.months}><th scope="row">{f.months}개월 후</th><td>{money(f.price_manwon)}</td>
        <td className={f.return_percent >= 0 ? "positive" : "negative"}>{percent.format(f.return_percent)}%</td></tr>
    )}</tbody></table>
  </>;
}

function App() {
  const [homes, setHomes] = useState(null);
  const [listError, setListError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [home, setHome] = useState(null);
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const pending = useRef(null);

  const [query, setQuery] = useState("");
  const [onlyReady, setOnlyReady] = useState(false);
  const complexes = useMemo(() => {
    const index = new Map();
    for (const item of homes || []) {
      if (!index.has(item.complex_id)) index.set(item.complex_id, { ...item, groups: [] });
      index.get(item.complex_id).groups.push(item);
    }
    return Array.from(index.values());
  }, [homes]);
  const visible = useMemo(() => complexes.filter(c =>
    (c.name + " " + c.address).includes(query.trim()) &&
    (!onlyReady || c.groups.some(g => g.predictable))
  ), [complexes, query, onlyReady]);
  const selected = complexes.find(c => c.complex_id === home?.complex_id);
  function selectComplex(c) {
    select(c.groups.find(g => g.predictable) || c.groups[0]);
  }

  useEffect(() => {
    const controller = new AbortController();
    setListError("");
    readApi("/api/complexes", controller.signal).then(setHomes).catch(err => {
      if (!controller.signal.aborted) setListError(err.message);
    });
    return () => controller.abort();
  }, [attempt]);
  useEffect(() => () => pending.current?.abort(), []);

  async function select(item) {
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setHome(item);
    setData(null);
    setError("");
    if (!item.predictable) return;
    const params = new URLSearchParams({ complex_id: item.complex_id, area_group: item.area_group });
    try {
      const result = await readApi("/api/predict?" + params, controller.signal);
      if (!controller.signal.aborted) setData(result);
    } catch (err) {
      if (!controller.signal.aborted) setError(err.message);
    }
  }

  return <>
    <header><div className="brand">집핏</div><span>광진구 아파트</span></header>
    <main><h1>아파트 매매가 예측</h1><Account home={home} homes={homes} onSelect={select}/><DistrictMap complexes={visible} onSelect={selectComplex} selectedId={home?.complex_id}/>
      <p className="map-key">초록색은 예측 가능한 단지, 회색은 실거래만 있는 단지입니다.</p>
      <div className="layout">
      <section className="panel"><h2>단지·면적 선택</h2>
        {listError ? <p role="alert">{listError} <button onClick={() => setAttempt(attempt + 1)}>다시 불러오기</button></p>
          : homes === null ? <p>단지를 불러오는 중입니다.</p>
          : homes.length === 0 ? <p>등록된 단지가 없습니다.</p>
          : <><label>단지명 또는 주소<input type="search" value={query} onChange={e => setQuery(e.target.value)}/></label>
              <label><input type="checkbox" checked={onlyReady} onChange={e => setOnlyReady(e.target.checked)}/> 예측 가능한 단지만</label>
              <p>{visible.length}개 단지</p><div className="home-list">{visible.map(c =>
                <button className="home" key={c.complex_id} aria-pressed={selected === c}
                  onClick={() => selectComplex(c)}><b>{c.name}</b><span>{c.address}</span></button>
              )}</div></>}
      </section>
      <section className="panel result" aria-label="예측 결과" aria-live="polite">
        {selected && <label>면적군
          <select value={home.area_group} onChange={e => select(selected.groups.find(g => g.area_group === Number(e.target.value)))}>
            {selected.groups.map(g => <option key={g.area_group} value={g.area_group}>
              {g.area_group}㎡ · 대표 {g.representative_area}㎡{g.predictable ? "" : " · 예측 입력 없음"}
            </option>)}
          </select></label>}
        {home && !home.predictable ? <><h2>{home.name}</h2><p>이 면적군에는 예측에 필요한 월별 입력이 없습니다.</p></>
          : data ? <Results home={home} data={data}/>
          : error ? <><h2>예측 요청 실패</h2><p>{error}</p><button onClick={() => select(home)}>다시 예측</button></>
          : home ? <p>{home.name}의 예측값을 계산하고 있습니다.</p>
          : <><h2>예측 결과</h2><p>단지와 면적을 선택하면 기간별 예측 매매가가 표시됩니다.</p></>}
      </section>
    </div></main>
  </>;
}

createRoot(document.getElementById("root")).render(<App/>);

