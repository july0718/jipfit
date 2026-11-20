from contextlib import asynccontextmanager
from datetime import datetime, timezone

from pathlib import Path
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from fastapi import FastAPI, HTTPException, Query
from account_api import router
from database import connect

from inference import forecast, load_model


@asynccontextmanager
async def lifespan(app):
    with connect() as db:
        db.execute("SELECT pg_advisory_xact_lock(1121506)")
        db.execute((Path(__file__).parent / "db/trade_status.sql").read_text(encoding="utf-8"))
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


@app.get("/api/complexes")
def list_complexes():
    with connect() as db:
        return db.execute("""
            SELECT c.complex_id, c.name, c.address, c.latitude, c.longitude,
                   a.area_group, a.representative_area,
                   EXISTS (SELECT 1 FROM model_inputs m
                           WHERE m.month = (SELECT max(month) FROM model_inputs)
                             AND (m.complex_id, m.area_group) = (a.complex_id, a.area_group)) AS predictable
            FROM complexes c JOIN area_groups a USING (complex_id)
            ORDER BY c.name, c.complex_id, a.area_group
        """).fetchall()


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
              AND m.month = COALESCE(%s, (SELECT max(month) FROM model_inputs))
            ORDER BY m.month DESC LIMIT 1
        """, (complex_id, area_group, as_of)).fetchone()
    if row is None:
        raise HTTPException(404, "선택한 단지와 면적군의 모델 입력이 없습니다.")
    result = forecast(app.state.model, row)
    result["calculated_at"] = datetime.now(timezone.utc)
    return result




