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

export default function Account({home, homes, onSelect}) {
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

  async function submit(action, body) {
    setBusy(true);
    setMessage("");
    try {
      setProfile(await accountRequest(action, body));
      setMode("");
    } catch (err) {
      if (action === "/favorite" && err.status === 401) setProfile(guest);
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
          const item = homes?.find(g => g.complex_id === f.complex_id && g.area_group === f.area_group);
          return <li key={JSON.stringify([f.complex_id,f.area_group])}><button disabled={!item || busy}
            onClick={() => onSelect(item)}>{f.name} · {f.area_group}㎡</button> <small>{f.address}</small></li>;
        })}</ul>}
    </>}
    {message && <p role="alert">{message}</p>}
  </section>;
}
