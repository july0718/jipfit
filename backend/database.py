import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row


def connect():
    try:
        return psycopg.connect(row_factory=dict_row, connect_timeout=5)
    except psycopg.OperationalError as exc:
        raise HTTPException(503, "PostgreSQL 연결을 확인해 주세요.") from exc
