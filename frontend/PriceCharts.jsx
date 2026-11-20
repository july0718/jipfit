import React, { useMemo } from "react";

function PriceChart({ title, points, forecast = false }) {
  if (!points.length) return <p>{title}에 표시할 유효 거래가 없습니다.</p>;
  const first = points[0].time;
  const last = points[points.length - 1].time;
  const prices = points.map(p => p.price / 10000);
  const bottom = Math.min(...prices);
  const top = Math.max(...prices);
  const padding = Math.max((top - bottom) * 0.1, 0.02);
  const low = Math.max(0, bottom - padding);
  const high = top + padding;
  const x = time => first === last ? 250 : 54 + (time - first) / (last - first) * 384;
  const y = price => 145 - (price - low) / (high - low) * 122;
  return <figure className="price-chart"><figcaption>{title} (억 원)</figcaption>
    <svg viewBox="0 0 480 178" role="img" aria-label={title}>
      {[low, (low + high) / 2, high].map(v => <g key={v}>
        <line x1="54" x2="438" y1={y(v)} y2={y(v)} stroke="#d5dfdd"/>
        <text x="48" y={y(v)+4} textAnchor="end">{v.toFixed(2)}</text>
      </g>)}
      {points.map((p, i) => <g key={p.time}>
        {i > 0 && (forecast || p.time - points[i-1].time <= 3) &&
          <line x1={x(points[i-1].time)} y1={y(prices[i-1])} x2={x(p.time)} y2={y(prices[i])}
            stroke="#087e74" strokeWidth="2" strokeDasharray={forecast ? "5 4" : undefined}/>}
        <circle cx={x(p.time)} cy={y(prices[i])} r={forecast ? 4 : 2.8} fill="#087e74">
          <title>{p.label} · {p.price.toLocaleString("ko-KR", {maximumFractionDigits: 0})}만 원</title>
        </circle>
        {(forecast || i === 0 || i === points.length - 1) &&
          <text x={x(p.time)} y="168" textAnchor="middle">{p.label}</text>}
      </g>)}
    </svg></figure>;
}

export function ForecastChart({ data }) {
  const points = [{time: 0, price: Number(data.reference_price_manwon), label: "기준가"},
    ...data.forecasts.map(f => ({time: f.months, price: Number(f.price_manwon), label: f.months + "개월"}))];
  return <PriceChart title="예측 매매가" points={points} forecast/>;
}

export function TradeChart({ rows }) {
  const points = useMemo(() => {
    const buckets = new Map();
    for (const row of rows) {
      if (row.is_cancelled) continue;
      const month = row.contract_date.slice(0, 7);
      if (!buckets.has(month)) buckets.set(month, []);
      buckets.get(month).push(Number(row.price_manwon));
    }
    return Array.from(buckets.keys()).sort().map(label => {
      const values = buckets.get(label).sort((a,b) => a-b);
      const middle = Math.floor(values.length / 2);
      const price = values.length % 2 ? values[middle] : (values[middle-1] + values[middle]) / 2;
      const [year, month] = label.split("-").map(Number);
      return {time: year * 12 + month, price, label};
    });
  }, [rows]);
  return <PriceChart title="월별 실거래가 중앙값" points={points}/>;
}
