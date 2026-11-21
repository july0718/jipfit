import React, { useEffect, useState } from "react";

const guest = {username: null, favorites: []};

async function accountRequest(action, body) {
  const response = await fetch("/api/account" + action, {
    method: body ? "POST" : "GET", credentials: "same-origin", cache: "no-store",
    headers: body ? {"Content-Type": "application/json", "X-Jipfit-Action": "1"} : {},
    body: body ? JSON.stringify(body) : undefined
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(typeof result.detail === "string" ? result.detail : "입력 형식을 확인해 주세요.");
    error.status = response.status;
    throw error;
  }
  return result;
}

function NoteEditor({ initial, busy, save, label }) {
  return <form onSubmit={e => {
    e.preventDefault();
    save(new FormData(e.currentTarget).get("note"));
  }}><label>{label}<textarea name="note" defaultValue={initial} maxLength={2000} rows={2}/></label>
    <button disabled={busy}>메모 저장</button></form>;
}

function Account({home, homes, resolveHome, onSelect, compared, horizon, onOpenComparison}) {
  const [profile, setProfile] = useState(guest);
  const [mode, setMode] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(true);
  useEffect(() => {
    let active = true;
    accountRequest("").then(data => { if (active) setProfile(data); })
      .catch(() => { if (active) setMessage("계정 정보를 불러올 수 없습니다."); })
      .finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!profile.username || busy) return;
    let active = true;
    let pending = false;
    async function refresh() {
      if (pending || document.hidden) return;
      pending = true;
      try {
        const next = await accountRequest("");
        if (active) setProfile(next);
      } catch { /* Keep the last profile until the next refresh. */ }
      finally { pending = false; }
    }
    const timer = setInterval(refresh, 60000);
    window.addEventListener("focus", refresh);
    return () => { active = false; clearInterval(timer); window.removeEventListener("focus", refresh); };
  }, [profile.username, busy]);

  async function submit(action, body) {
    setBusy(true);
    setMessage("");
    try {
      setProfile(await accountRequest(action, body));
      setMode("");
    } catch (err) {
      if (action !== "/login" && err.status === 401) setProfile(guest);
      setMessage(err.message);
    } finally {
      setBusy(false);
    }
  }

  function authenticate(event) {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    submit("/" + mode, {username: values.get("username"), password: values.get("password")});
  }

  const saved = home && profile.favorites.some(f => f.complex_id === home.complex_id && f.area_group === home.area_group);
  const lookup = (id, area) => homes?.find(g => g.complex_id === id && (area == null || g.area_group === area));
  async function openSaved(keys, action) {
    setBusy(true);
    setMessage("");
    try {
      const items = await Promise.all(keys.map(([id, area]) => lookup(id, area) || resolveHome(id, area)));
      action(items);
    } catch (error) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }
  const notes = profile.notes || [];
  const note = notes.find(n => n.complex_id === home?.complex_id)?.note || "";
  const notifications = profile.favorites.filter(f => f.new_trades > 0 || f.new_month);
  return <section className="account panel" aria-label="내 관심 단지">
    <div className="account-row"><h2>{profile.username ? profile.username + "님의 관심 단지" : "내 관심 단지"}</h2>
      {profile.username ? <button disabled={busy} onClick={() => submit("/logout", {})}>로그아웃</button>
        : ["login", "register"].map(key => <button key={key} disabled={busy} aria-pressed={mode === key}
          onClick={() => { setMode(mode === key ? "" : key); setMessage(""); }}>{key === "login" ? "로그인" : "회원가입"}</button>)}
    </div>
    {mode && <form key={mode} onSubmit={authenticate}>
      <label>아이디<input name="username" required minLength={3} maxLength={32} pattern="[A-Za-z0-9_]+" autoComplete="username" placeholder="영문·숫자·밑줄 3~32자"/></label>
      <label>비밀번호<input name="password" required type="password" minLength={10} maxLength={128}
        autoComplete={mode === "login" ? "current-password" : "new-password"} placeholder="10자 이상"/></label>
      <button disabled={busy}>{mode === "register" ? "가입하고 로그인" : "로그인하기"}</button>
    </form>}
    {profile.username && <>
      {home && <button disabled={busy} onClick={() => submit("/favorite", {
        complex_id: home.complex_id, area_group: home.area_group, save: !saved
      })}>{home.name} {home.area_group}㎡ {saved ? "관심 해제" : "관심 등록"}</button>}
      {profile.favorites.length === 0 ? <p>단지와 면적군을 선택해 관심 목록에 저장하세요.</p>
        : <ul>{profile.favorites.map(f => {
          return <li key={JSON.stringify([f.complex_id,f.area_group])}><button disabled={busy}
            onClick={() => openSaved([[f.complex_id, f.area_group]], ([item]) => onSelect(item))}>{f.name} · {f.area_group}㎡</button> <small>{f.address}</small>
            <button disabled={busy || (compared.length >= 4 && !compared.some(c => c.complex_id === f.complex_id))}
              onClick={() => openSaved([[f.complex_id, f.area_group]], ([item]) => {
                const included = compared.some(c => c.complex_id === item.complex_id);
                onOpenComparison(included ? compared.map(c => c.complex_id === item.complex_id ? item : c) : [...compared, item], horizon);
              })}>비교에 담기</button>
            <button disabled={busy} onClick={() => submit("/favorite", {complex_id:f.complex_id, area_group:f.area_group, save:false})}>관심 해제</button></li>;
        })}</ul>}
      <h3>단지 메모</h3>
      {home && <NoteEditor key={JSON.stringify([profile.username,home.complex_id,note])} initial={note} busy={busy}
        label={home.name + " 메모"} save={text => submit("/note", {complex_id:home.complex_id, note:text})}/>}
      {notes.map(n => <p key={n.complex_id}><button disabled={busy}
        onClick={() => openSaved([[n.complex_id]], ([item]) => onSelect(item))}>{lookup(n.complex_id)?.name || n.complex_id}</button> {n.note}</p>)}
      <h3>저장한 비교</h3>
      {compared.length === 2 && <button disabled={busy} onClick={() => submit("/comparison", {
        left_id:compared[0].complex_id, left_area:compared[0].area_group,
        right_id:compared[1].complex_id, right_area:compared[1].area_group, months:horizon
      })}>현재 비교 저장 · {horizon}개월</button>}
      {!profile.comparisons?.length && <p>두 단지를 비교에 담으면 현재 조합을 저장할 수 있습니다.</p>}
      {(profile.comparisons || []).map(c => {
        const left = lookup(c.left_id,c.left_area), right = lookup(c.right_id,c.right_area);
        return <div className="saved-comparison" key={JSON.stringify([profile.username,c.left_id,c.left_area,c.right_id,c.right_area,c.months])}>
          <button disabled={busy} onClick={() => openSaved([[c.left_id,c.left_area], [c.right_id,c.right_area]], items => onOpenComparison(items, c.months))}>
            {left?.name || c.left_id} {c.left_area}㎡군 · {right?.name || c.right_id} {c.right_area}㎡군 · {c.months}개월 열기</button>
          <button disabled={busy} onClick={() => submit("/comparison", {...c,save:false})}>비교 삭제</button>
          <NoteEditor key={c.note} label="비교 메모" initial={c.note} busy={busy}
            save={text => submit("/comparison", {...c,note:text})}/>
        </div>;
      })}
      <h3>관심 단지 갱신 알림</h3>
      {notifications.length === 0 && <p>새 알림이 없습니다.</p>}
      {notifications.map(f => <p key={JSON.stringify([f.complex_id,f.area_group])}>
        {f.name} {f.area_group}㎡군 · {[f.new_trades > 0 && "새 거래 " + f.new_trades + "건",
          f.new_month && "새 전망 기준월 " + f.new_month].filter(Boolean).join(" · ")}{" "}
        <button disabled={busy} onClick={() => submit("/seen", {complex_id:f.complex_id,area_group:f.area_group,
          latest_trade_id:f.latest_trade_id,new_month:f.new_month})}>읽음</button></p>)}
    </>}
    {message && <p role="alert">{message}</p>}
  </section>;
}

export default React.memo(Account);
