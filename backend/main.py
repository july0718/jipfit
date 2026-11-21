from contextlib import asynccontextmanager
from datetime import datetime, timezone

from pathlib import Path
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from fastapi import FastAPI, HTTPException, Query
from account_api import router
from database import connect
from districts import DISTRICTS

from math import expm1
from inference import HORIZONS, forecast, load_model, predict_growth


@asynccontextmanager
async def lifespan(app):
    with connect() as db:
        db.execute("SELECT pg_advisory_xact_lock(1121506)")
        db.execute((Path(__file__).parent / "db/trade_status.sql").read_text(encoding="utf-8"))
        db.execute((Path(__file__).parent / "db/regions.sql").read_text(encoding="utf-8"))
        db.execute((Path(__file__).parent / "db/metadata_refresh.sql").read_text(encoding="utf-8"))
        db.execute((Path(__file__).parent / "db/user_accounts.sql").read_text(encoding="utf-8"))
    app.state.model = load_model()
    yield


app = FastAPI(title="집핏 예측 API", lifespan=lifespan)
app.include_router(router)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    if request.url.path.startswith("/api/account"):
        return JSONResponse({"detail": "아이디·비밀번호 또는 선택 항목의 입력 형식을 확인해 주세요."}, status_code=422)
    return await request_validation_exception_handler(request, exc)


@app.get("/api/health")
def health():
    with connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/complexes")
def list_complexes(include_sort: bool = False):
    with connect() as db:
        rows = db.execute("""
            SELECT c.complex_id, c.name, c.address, c.latitude, c.longitude, c.existence_status,
                   a.area_group, a.representative_area,
                   (m.complex_id IS NOT NULL) AS predictable
            FROM complexes c JOIN area_groups a USING (complex_id)
            LEFT JOIN district_input_months d ON d.district=split_part(c.complex_id,' ',2)
            LEFT JOIN model_inputs m ON m.complex_id=c.complex_id AND m.area_group=a.area_group AND m.month=d.month
            ORDER BY c.name, c.complex_id, a.area_group
        """).fetchall()
        if not include_sort:
            return rows
        stats = {r["complex_id"]: r for r in db.execute("""
            SELECT complex_id, count(*) AS trade_count, max(contract_date) AS last_trade
            FROM trades WHERE NOT is_cancelled GROUP BY complex_id
        """)}
        last_prices = {(r["complex_id"], r["area_group"]): r["price_manwon"] for r in db.execute("""
            SELECT DISTINCT ON (complex_id, area_group) complex_id, area_group, price_manwon
            FROM trades WHERE NOT is_cancelled
            ORDER BY complex_id, area_group, contract_date DESC, trade_id DESC
        """)}
        inputs = db.execute("""
            SELECT DISTINCT ON (complex_id) m.* FROM model_inputs m
            JOIN district_input_months d ON d.district=split_part(m.complex_id,' ',2) AND m.month=d.month
            ORDER BY complex_id, area_group
        """).fetchall()
    returns = {}
    if inputs:
        for row, rates in zip(inputs, predict_growth(app.state.model, inputs), strict=True):
            returns[row["complex_id"]] = dict(zip(HORIZONS, (100 * expm1(r) for r in rates), strict=True))
    for row in rows:
        cid = row["complex_id"]
        key = cid, row["area_group"]
        row["marker_price_manwon"] = last_prices.get(key)
        stat = stats.get(cid, {})
        row.update(trade_count=stat.get("trade_count", 0), last_trade=stat.get("last_trade"),
                   sort_returns=returns.get(cid, {}))
    return rows


@app.get("/api/complex")
def complex_detail(complex_id: str = Query(min_length=1)):
    with connect() as db:
        detail = db.execute("""
            SELECT complex_id, name, address, built, latitude, longitude
            FROM complexes WHERE complex_id = %s
        """, (complex_id,)).fetchone()
        if detail is None:
            raise HTTPException(404, "단지를 찾을 수 없습니다.")
        detail["area_groups"] = db.execute("""
            SELECT area_group, representative_area FROM area_groups
            WHERE complex_id = %s ORDER BY area_group
        """, (complex_id,)).fetchall()
        exists = db.execute("SELECT to_regclass('refresh_runs') AS runs").fetchone()["runs"]
        detail["last_sync_at"] = (db.execute(
            "SELECT max(finished_at) AS ts FROM refresh_runs"
        ).fetchone()["ts"] if exists else None)
        district = complex_id.split(" ")[1]
        if district != "광진구":
            detail["last_sync_at"] = db.execute("SELECT max(finished_at) AS ts FROM district_refresh_runs WHERE district=%s",
                                                (next((k for k,v in DISTRICTS.items() if v == district), None),)).fetchone()["ts"]
        return detail


@app.get("/api/trades")
def list_trades(complex_id: str = Query(min_length=1), area_group: int = Query(gt=0)):
    with connect() as db:
        return db.execute("""
            SELECT contract_date, area, floor, price_manwon, is_cancelled
            FROM trades
            WHERE complex_id = %s AND area_group = %s
            ORDER BY contract_date DESC, trade_id DESC
        """, (complex_id, area_group)).fetchall()


@app.get("/api/predict")
def predict(complex_id: str = Query(min_length=1), area_group: int = Query(gt=0),
            as_of: str | None = Query(default=None, pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")):
    with connect() as db:
        row = db.execute("""
            SELECT m.*, a.representative_area
            FROM model_inputs m JOIN area_groups a USING (complex_id, area_group)
            WHERE m.complex_id = %s AND m.area_group = %s
              AND m.month = COALESCE(%s, (SELECT d.month FROM district_input_months d WHERE d.district=split_part(m.complex_id,' ',2)))
            ORDER BY m.month DESC LIMIT 1
        """, (complex_id, area_group, as_of)).fetchone()
    if row is None:
        raise HTTPException(404, "선택한 단지와 면적군의 모델 입력이 없습니다.")
    result = forecast(app.state.model, row)
    result["pilot"] = complex_id.split(" ")[1] != "광진구"
    result["calculated_at"] = datetime.now(timezone.utc)
    return result




