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
