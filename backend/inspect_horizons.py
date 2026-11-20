"""Read-only projection of the deployed curve beyond its trained horizons."""
import argparse
import math

import pandas as pd
from database import connect
from inference import forecast, load_model


def project(row, model):
    months = [12, 18, 24, 30, 36]
    inputs = pd.DataFrame([row], columns=model.features, dtype=float)
    rates = model.predict(inputs, months=months)[0]
    standard = forecast(model, row)
    reference = standard["reference_price_manwon"]
    prices = [reference * math.exp(rate) for rate in rates]
    if not math.isclose(prices[0], standard["forecasts"][-1]["price_manwon"]):
        raise ValueError("12개월 기준값 불일치")
    print(f"{row['complex_id']} · {row['area_group']}㎡군 · 기준월 {row['month']} · 기준가 {reference/10000:.2f}억 원")
    print("기간    예상가격(억 원)    기준가 대비    구분")
    for month, price, rate in zip(months, prices, rates):
        label = "운영 전망" if month == 12 else "곡선 외삽"
        print(f"{month:2}개월    {price/10000:10.2f}    {100*math.expm1(rate):+9.2f}%    {label}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("complex_id")
    parser.add_argument("area_group", type=int)
    args = parser.parse_args()
    with connect() as db:
        row = db.execute("""
            SELECT m.*, a.representative_area FROM model_inputs m
            JOIN area_groups a USING(complex_id, area_group)
            JOIN district_input_months d ON d.district=split_part(m.complex_id,' ',2) AND d.month=m.month
            WHERE m.complex_id=%s AND m.area_group=%s
        """, (args.complex_id, args.area_group)).fetchone()
    if not row:
        parser.exit(1, "해당 단지·면적군의 최신 예측 입력이 없습니다.\n")
    project(row, load_model())
