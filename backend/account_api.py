from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

import accounts
from database import connect

router = APIRouter(prefix="/api/account")


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(min_length=10, max_length=128)


class Favorite(BaseModel):
    complex_id: str = Field(min_length=1, max_length=512)
    area_group: int = Field(gt=0)
    save: bool


def require_action(request: Request):
    if request.headers.get("X-Jipfit-Action") != "1":
        raise HTTPException(403, "집핏 화면에서 다시 시도해 주세요.")


def token(request):
    return request.cookies.get(accounts.COOKIE, "")


def sign_in(db, user, request, response):
    accounts.end_session(db, token(request))
    session = accounts.start_session(db, user["id"])
    response.set_cookie(accounts.COOKIE, session, max_age=accounts.LIFETIME,
                        httponly=True, secure=True, samesite="lax", path="/api/account")
    return accounts.profile(db,user)


@router.get("")
def get_profile(request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    with connect() as db:
        return accounts.profile(db, accounts.current_user(db, token(request)))


@router.post("/register", dependencies=[Depends(require_action)], status_code=201)
def register(body: Credentials, request: Request, response: Response):
    with connect() as db:
        user = accounts.create_user(db, body.username.lower(), body.password)
        if not user:
            raise HTTPException(409, "이미 사용 중인 아이디입니다.")
        return sign_in(db, user, request, response)


@router.post("/login", dependencies=[Depends(require_action)])
def login(body: Credentials, request: Request, response: Response):
    with connect() as db:
        user = accounts.authenticate(db, body.username.lower(), body.password)
        if user:
            return sign_in(db, user, request, response)
    raise HTTPException(401, "로그인할 수 없습니다. 입력 정보를 확인하거나 잠시 후 다시 시도해 주세요.")


@router.post("/logout", dependencies=[Depends(require_action)])
def logout(request: Request, response: Response):
    with connect() as db:
        accounts.end_session(db, token(request))
    response.delete_cookie(accounts.COOKIE, path="/api/account", secure=True, httponly=True, samesite="lax")
    return {"username": None, "favorites": []}


@router.post("/favorite", dependencies=[Depends(require_action)])
def favorite(body: Favorite, request: Request):
    with connect() as db:
        user = accounts.current_user(db, token(request))
        if not user:
            raise HTTPException(401, "다시 로그인해 주세요.")
        if not accounts.set_favorite(db, user["id"], body.complex_id, body.area_group, body.save):
            raise HTTPException(404, "등록되지 않은 단지·면적군입니다.")
        return accounts.profile(db, user)


class Comparison(BaseModel):
    left_id: str = Field(min_length=1, max_length=512)
    left_area: int = Field(gt=0)
    right_id: str = Field(min_length=1, max_length=512)
    right_area: int = Field(gt=0)
    months: int
    save: bool = True
    note: str | None = Field(default=None, max_length=2000)


class Note(BaseModel):
    complex_id: str = Field(min_length=1, max_length=512)
    note: str = Field(max_length=2000)


class Seen(BaseModel):
    complex_id: str = Field(min_length=1, max_length=512)
    area_group: int = Field(gt=0)
    latest_trade_id: int = Field(ge=0)
    new_month: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")


def member(db, request):
    user = accounts.current_user(db, token(request))
    if not user:
        raise HTTPException(401, "다시 로그인해 주세요.")
    return user


@router.post("/comparison", dependencies=[Depends(require_action)])
def comparison(body: Comparison, request: Request):
    with connect() as db:
        user = member(db, request)
        if body.left_id == body.right_id or body.months not in (3, 6, 12):
            raise HTTPException(422, "서로 다른 두 단지와 전망 기간을 선택해 주세요.")
        params = (user["id"], body.left_id, body.left_area, body.right_id, body.right_area, body.months)
        if body.save:
            valid = db.execute("""
                SELECT count(*) AS n FROM area_groups WHERE (complex_id,area_group) IN ((%s,%s),(%s,%s))
            """, params[1:5]).fetchone()
            if valid["n"] != 2:
                raise HTTPException(404, "등록되지 않은 면적군입니다.")
            db.execute("""
                INSERT INTO saved_comparisons VALUES (%s,%s,%s,%s,%s,%s,COALESCE(%s,''))
                ON CONFLICT (user_id,left_id,left_area,right_id,right_area,months)
                DO UPDATE SET note=COALESCE(%s,saved_comparisons.note)
            """, (*params, body.note, body.note))
        else:
            db.execute("""
                DELETE FROM saved_comparisons WHERE
                (user_id,left_id,left_area,right_id,right_area,months)=(%s,%s,%s,%s,%s,%s)
            """, params)
        return accounts.profile(db, user)


@router.post("/note", dependencies=[Depends(require_action)])
def save_note(body: Note, request: Request):
    with connect() as db:
        user = member(db, request)
        key = (user["id"], body.complex_id)
        if body.note.strip():
            if not db.execute("SELECT 1 FROM complexes WHERE complex_id=%s", (body.complex_id,)).fetchone():
                raise HTTPException(404, "등록되지 않은 단지입니다.")
            db.execute("""
                INSERT INTO complex_notes VALUES (%s,%s,%s) ON CONFLICT (user_id,complex_id)
                DO UPDATE SET note=excluded.note
            """, (*key, body.note.strip()))
        else:
            db.execute("DELETE FROM complex_notes WHERE user_id=%s AND complex_id=%s", key)
        return accounts.profile(db, user)


@router.post("/seen", dependencies=[Depends(require_action)])
def mark_seen(body: Seen, request: Request):
    with connect() as db:
        user = member(db, request)
        db.execute("""
            UPDATE favorites SET seen_trade=greatest(seen_trade,%s), seen_month=greatest(seen_month,%s)
            WHERE user_id=%s AND complex_id=%s AND area_group=%s
        """, (body.latest_trade_id, body.new_month or "", user["id"], body.complex_id, body.area_group))
        return accounts.profile(db, user)
