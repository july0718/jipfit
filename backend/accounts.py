"""Account storage. Callers own the database transaction."""
import hashlib
import hmac
import secrets

COOKIE = "jipfit_session"
LIFETIME = 7 * 24 * 3600


def hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)


def create_user(db, username, password):
    salt = secrets.token_bytes(16)
    return db.execute("""
        INSERT INTO app_users(username,salt,password_hash) VALUES (%s,%s,%s)
        ON CONFLICT (username) DO NOTHING RETURNING id,username
    """, (username, salt, hash_password(password, salt))).fetchone()


def authenticate(db, username, password):
    row = db.execute("""
        SELECT *, coalesce(last_failure > now()-interval '15 minutes',false) AS recent
        FROM app_users WHERE username=%s FOR UPDATE
    """, (username,)).fetchone()
    if row and row["recent"] and row["failed_attempts"] >= 8:
        return None
    digest = hash_password(password, row["salt"] if row else bytes(16))
    if not row or not hmac.compare_digest(row["password_hash"], digest):
        if row:
            db.execute("UPDATE app_users SET failed_attempts=%s,last_failure=now() WHERE id=%s",
                       ((row["failed_attempts"] if row["recent"] else 0)+1, row["id"]))
        return None
    db.execute("UPDATE app_users SET failed_attempts=0,last_failure=NULL WHERE id=%s", (row["id"],))
    return row


def token_hash(token):
    return hashlib.sha256(token.encode()).digest()


def start_session(db, user_id):
    token = secrets.token_urlsafe(32)
    db.execute("DELETE FROM app_sessions WHERE expires_at <= now()")
    db.execute("""INSERT INTO app_sessions VALUES (%s,%s,now()+interval '7 days')""",
               (token_hash(token), user_id))
    return token


def end_session(db, token):
    db.execute("DELETE FROM app_sessions WHERE token_hash=%s", (token_hash(token),))


def current_user(db, token):
    return db.execute("""
        SELECT u.id,u.username FROM app_sessions s JOIN app_users u ON u.id=s.user_id
        WHERE s.token_hash=%s AND s.expires_at>now()
    """, (token_hash(token),)).fetchone()


def profile(db, user):
    if not user:
        return {"username": None, "favorites": []}
    rows = db.execute("""
        SELECT f.complex_id,f.area_group,c.name,c.address,
               (SELECT count(*) FROM trades t WHERE (t.complex_id,t.area_group)=(f.complex_id,f.area_group)
                 AND NOT t.is_cancelled AND t.trade_id>f.seen_trade
                 AND t.contract_date >= (date_trunc('month', now() AT TIME ZONE 'Asia/Seoul')::date - interval '3 months')) AS new_trades,
               (SELECT max(month) FROM model_inputs m WHERE (m.complex_id,m.area_group)=(f.complex_id,f.area_group)
                 AND m.month>f.seen_month) AS new_month,
               COALESCE((SELECT max(trade_id) FROM trades t WHERE (t.complex_id,t.area_group)=(f.complex_id,f.area_group)
                 AND NOT t.is_cancelled),0) AS latest_trade_id
        FROM favorites f
        JOIN complexes c USING(complex_id) WHERE user_id=%s ORDER BY c.name,c.address,f.area_group
    """, (user["id"],)).fetchall()
    comparisons = db.execute("""
        SELECT left_id,left_area,right_id,right_area,months,note FROM saved_comparisons
        WHERE user_id=%s ORDER BY left_id,right_id,left_area,right_area,months
    """, (user["id"],)).fetchall()
    notes = db.execute("SELECT complex_id,note FROM complex_notes WHERE user_id=%s ORDER BY complex_id",
                       (user["id"],)).fetchall()
    return {"username": user["username"], "favorites": rows, "comparisons": comparisons, "notes": notes}


def set_favorite(db, user_id, cid, group, save):
    if save:
        exists = db.execute("SELECT 1 FROM area_groups WHERE complex_id=%s AND area_group=%s",
                            (cid, group)).fetchone()
        if not exists:
            return False
        db.execute("""
            INSERT INTO favorites(user_id,complex_id,area_group,seen_trade,seen_month)
            SELECT %s,%s,%s,
              COALESCE((SELECT max(trade_id) FROM trades WHERE complex_id=%s AND area_group=%s AND NOT is_cancelled),0),
              COALESCE((SELECT max(month) FROM model_inputs WHERE complex_id=%s AND area_group=%s),'')
            ON CONFLICT DO NOTHING
        """, (user_id,cid,group,cid,group,cid,group))
    else:
        db.execute("DELETE FROM favorites WHERE user_id=%s AND complex_id=%s AND area_group=%s",
                   (user_id,cid,group))
    return True
