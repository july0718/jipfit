import React, { useMemo } from "react";
import { referenceBandPoints } from "./forecastReference.mjs";

function PriceChart({ title, points, forecast = false, rate = false, band = [] }) {
  if (!points.length) return <p>{title}에 표시할 유효 거래가 없습니다.</p>;
  const first = points[0].time;
  const last = points[points.length - 1].time;
  const prices = points.map(p => rate ? p.rate : p.price / 10000);
  const bounds = band.flatMap(p => [p.lower / 10000, p.upper / 10000]);
  const bottom = Math.min(...prices, ...bounds);
  const top = Math.max(...prices, ...bounds);
  const padding = Math.max((top - bottom) * 0.1, 0.02);
  const low = rate ? bottom - padding : Math.max(0, bottom - padding);
  const high = top + padding;
  const x = time => first === last ? 250 : 54 + (time - first) / (last - first) * 384;
  const y = price => 145 - (price - low) / (high - low) * 122;
  const envelope = band.length ? [
    {time: points[0].time, lower: points[0].price, upper: points[0].price}, ...band
  ] : [];
  const polygon = envelope.length > 1 ? [
    ...envelope.map(p => x(p.time) + "," + y(p.upper / 10000)),
    ...[...envelope].reverse().map(p => x(p.time) + "," + y(p.lower / 10000))
  ].join(" ") : null;
  return <figure className="price-chart"><figcaption>{title} ({rate ? "%" : "억 원"})</figcaption>
    <svg viewBox="0 0 480 178" role="img" aria-label={title}>
      {[low, (low + high) / 2, high].map(v => <g key={v}>
        <line x1="54" x2="438" y1={y(v)} y2={y(v)} stroke="#d5dfdd"/>
        <text x="48" y={y(v)+4} textAnchor="end">{v.toFixed(2)}</text>
      </g>)}
      {polygon && <polygon points={polygon} fill="#a6c8dc" fillOpacity="0.5">
        <title>광진구 과거 모델 잔차의 10~90백분위 참고범위. 서울 통합 모델의 예측 신뢰구간 아님.</title>
      </polygon>}
      {points.map((p, i) => <g key={p.time}>
        {i > 0 && (forecast || p.time - points[i-1].time <= 3) &&
          <line x1={x(points[i-1].time)} y1={y(prices[i-1])} x2={x(p.time)} y2={y(prices[i])}
            stroke="#087e74" strokeWidth="2" strokeDasharray={forecast ? "5 4" : undefined}/>}
        <circle cx={x(p.time)} cy={y(prices[i])} r={forecast ? 4 : 2.8} fill="#087e74">
          <title>{p.label} · {rate ? p.rate.toFixed(2) + "%" : p.price.toLocaleString("ko-KR") + "만 원"}</title>
        </circle>
        {(forecast || i === 0 || i === points.length - 1) &&
          <text x={x(p.time)} y="168" textAnchor="middle">{p.label}</text>}
      </g>)}
    </svg></figure>;
}

export function ForecastChart({ data }) {
  const [showReference, setShowReference] = React.useState(false);
  const points = [{time: 0, price: Number(data.reference_price_manwon), label: "기준가"},
    ...data.forecasts.map(f => ({time: f.months, price: Number(f.price_manwon), label: f.months + "개월"}))];
  const reference = referenceBandPoints(data);
  const available = reference.length === data.forecasts.length;
  return <section className="forecast-reference">
    <PriceChart title="예측 매매가" points={points} forecast
      band={showReference && available ? reference : []}/>
    {available ? <>
      <label><input type="checkbox" checked={showReference}
        onChange={e => setShowReference(e.target.checked)}/>
        광진구 과거 검증 오차 참고범위 표시</label>
      {showReference && <table>
        <caption>광진구 과거 모델 오차 분포 · 실제 상승률 − 예측 상승률 · 단위 %p</caption>
        <thead><tr><th>기간</th><th>표본</th><th>10백분위</th><th>90백분위</th></tr></thead>
        <tbody>{reference.map(row => <tr key={row.time}>
          <th>{row.time}개월</th><td>{row.sample_count.toLocaleString("ko-KR")}건</td>
          <td>{row.p10_pp.toFixed(2)}%p</td><td>{row.p90_pp.toFixed(2)}%p</td>
        </tr>)}</tbody>
      </table>}
      <p role="note">참고용: 음영은 광진구 과거 모델 평가의 오차 분포를 현재 예측에 단순 적용한 값입니다.
        현재 서울 통합 HGB 모델의 검증된 신뢰구간이나 개별 단지 미래 가격의 확률 범위가 아닙니다.</p>
    </> : <p role="note">현재 모델과 동일한 조건에서 검증한 예측 오차 범위는 아직 제공되지 않습니다.
      예측선은 단일 추정값입니다.</p>}
  </section>;
}

function monthlyPrices(rows) {
  const buckets = new Map();
  for (const row of rows) {
    if (row.is_cancelled || !(Number(row.price_manwon) > 0)) continue;
    const month = row.contract_date.slice(0, 7);
    if (!buckets.has(month)) buckets.set(month, []);
    buckets.get(month).push(Number(row.price_manwon));
  }
  return Array.from(buckets.keys()).sort().map(label => {
    const values = buckets.get(label).sort((a,b) => a-b);
    const middle = Math.floor(values.length / 2);
    const price = values.length % 2 ? values[middle] : (values[middle-1] + values[middle]) / 2;
    const [year, month] = label.split("-").map(Number);
    return {time: year * 12 + month, price, label, count: values.length};
  });
}

export const TradeChart = React.memo(function TradeChart({ rows }) {
  const points = useMemo(() => monthlyPrices(rows), [rows]);
  const changes = useMemo(() => points.map(p => ({...p, rate: (p.price / points[0].price - 1) * 100})), [points]);
  const first = points[0], last = points[points.length - 1];
  return <><PriceChart title="월별 실거래가 중앙값" points={points}/>
    {points.length >= 2 ? <><PriceChart title="조회 기간 첫 거래월 대비 누적 가격 변동률" points={changes} rate/>
      <p>{first.label} ({first.count}건) → {last.label} ({last.count}건) · {changes.at(-1).rate.toFixed(2)}%
        · 같은 면적군의 월별 중앙값 기준</p></> : <p>가격 변동률은 유효 거래가 있는 달이 2개 이상일 때 표시합니다.</p>}</>;
});

const colors = ["#087e74", "#994b19", "#5952a4", "#9c3457"];

function comparedSeries(column) {
  return {
    label: column.item.name + " " + column.item.area_group + "㎡군",
    points: monthlyPrices(column.data?.trades || [])
  };
}

export function ComparisonChart({ columns }) {
  const series = columns.map(comparedSeries);
  const points = series.flatMap(s => s.points);
  if (!points.length) return null;
  const times = points.map(p => p.time);
  const prices = points.map(p => p.price / 10000);
  const first = Math.min(...times), last = Math.max(...times);
  const min = Math.min(...prices), max = Math.max(...prices);
  const margin = Math.max((max - min) * 0.08, 0.02);
  const low = Math.max(0, min - margin), high = max + margin;
  const x = t => first === last ? 300 : 60 + (t - first) * 480 / (last - first);
  const y = v => 150 - (v - low) * 132 / (high - low);
  const monthLabel = t => Math.floor((t - 1) / 12) + "-" + String((t - 1) % 12 + 1).padStart(2, "0");
  return <figure className="price-chart"><figcaption>단지별 월간 실거래가 중앙값 (억 원)</figcaption>
    <svg viewBox="0 0 594 180" role="img" aria-label="같은 기간과 가격축으로 비교한 월별 실거래가">
      {[low, (low + high) / 2, high].map(v => <g key={v}><line x1="60" x2="540" y1={y(v)} y2={y(v)} stroke="#d5dfdd"/>
        <text x="53" y={y(v)+4} textAnchor="end">{v.toFixed(2)}</text></g>)}
      {Array.from(new Set([first, last])).map(t => <text key={t} x={x(t)} y="173" textAnchor="middle">{monthLabel(t)}</text>)}
      {series.map(({label, points}, i) => <g key={label + i} fill={colors[i]}>{points.map((p, j) => <g key={p.time}>
        {j > 0 && p.time - points[j-1].time <= 3 && <line x1={x(points[j-1].time)} y1={y(points[j-1].price / 10000)}
          x2={x(p.time)} y2={y(p.price / 10000)} stroke={colors[i]}/>}
        <circle cx={x(p.time)} cy={y(p.price / 10000)} r="3"><title>{label} · {p.label} · {p.price.toLocaleString("ko-KR")}만 원</title></circle>
      </g>)}</g>)}
    </svg><ul className="comparison-legend">{series.map(({label, points}, i) => <li key={i}>
      <span style={{background: colors[i]}}/>{label}{!points.length && (columns[i].data ? " · 자료 없음" : " · 조회 중")}
    </li>)}</ul></figure>;
}
