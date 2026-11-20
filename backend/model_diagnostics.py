"""Print the stage 3 paired historical evaluation as a Markdown report."""
import csv
import gzip
from collections import defaultdict
from math import isfinite
from pathlib import Path
from statistics import fmean

ROOT = Path(__file__).resolve().parent


def summarize(rows):
    actual = [float(r["actual_return_percent"]) for r in rows]
    values = []
    for predictions in ([float(r["predicted_return_percent"]) for r in rows],
                        [0.0] * len(rows),
                        [float(r["macro18_return_percent"]) for r in rows]):
        errors = [p - a for p, a in zip(predictions, actual, strict=True)]
        if not all(map(isfinite, errors)):
            raise ValueError("Missing or invalid paired prediction")
        values.extend([fmean(map(abs, errors)), fmean(errors)])
    return values


def report():
    with gzip.open(ROOT / "audit.csv.gz", "rt", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    keys = {(r["month"], r["complex_id"], r["area_group"], r["horizon_months"]) for r in rows}
    if len(keys) != len(rows) or any(r["training_cutoff"] > r["month"] for r in rows):
        raise ValueError("Invalid audit keys or training dates")
    print("# 광진구 매매가 전망 검증\n")
    print("오차 단위는 %p이며 편향은 예측 상승률에서 실제 상승률을 뺀 평균입니다. "
          "후보는 보관된 18개 변수 대조 모델(control_macro18)입니다. "
          "단지·면적군·기준월·기간을 일치시키고 실제 상승률이 같은 행에서 비교했습니다.\n")
    print("모든 방법이 평가 표본 전부에 값을 제공합니다. 실제 서비스 제공 범위는 별도로 집계합니다.")
    for title, field in [
        ("기간별", lambda r: ""),
        ("기준월별", lambda r: r["month"]),
        ("최근 12개월 거래량별", lambda r: "1~5건" if int(r["count12"]) <= 5 else
         "6~12건" if int(r["count12"]) <= 12 else "13건 이상"),
    ]:
        groups = defaultdict(list)
        for row in rows:
            groups[int(row["horizon_months"]), field(row)].append(row)
        print(f"\n## {title}\n")
        print("| 기간 | 구간 | 표본 | 기존 MAE | 기존 편향 | 가격 유지 MAE | 가격 유지 편향 | 후보 MAE | 후보 편향 |")
        print("|---:|---|---:|---:|---:|---:|---:|---:|---:|")
        for (months, group), records in sorted(groups.items()):
            numbers = " | ".join(f"{n:.2f}" for n in summarize(records))
            print(f"| {months}개월 | {group or '전체'} | {len(records)} | {numbers} |")
    district = ROOT / "db/gwangjin"
    with (district / "groups.csv").open(encoding="utf-8-sig") as source:
        total = sum(1 for _ in csv.DictReader(source))
    with (district / "inputs.csv").open(encoding="utf-8-sig") as source:
        inputs = list(csv.DictReader(source))
    month = max(r["month"] for r in inputs)
    count = sum(r["month"] == month for r in inputs)
    print(f"\n{month} 저장 자료 기준 기존 모델 제공 범위는 {count}/{total}개 단지·면적군({count/total:.1%})입니다.")


if __name__ == "__main__":
    report()
