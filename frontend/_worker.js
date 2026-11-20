const methods = {
  "/api/complex": "GET", "/api/trades": "GET", "/api/complexes": "GET", "/api/predict": "GET", "/api/account": "GET",
  "/api/account/register": "POST", "/api/account/login": "POST",
  "/api/account/logout": "POST", "/api/account/favorite": "POST"
};

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!url.pathname.startsWith("/api/")) return env.ASSETS.fetch(request);
    const error = (detail, status) => Response.json({detail}, {status, headers: {"Cache-Control": "no-store"}});
    if (!methods[url.pathname]) return error("지원하지 않는 요청입니다.", 404);
    if (request.method !== methods[url.pathname]) return error("지원하지 않는 메서드입니다.", 405);
    const headers = new Headers({Accept:"application/json"});
    const account = url.pathname.startsWith("/api/account");
    if (account) {
      const session = (request.headers.get("Cookie") || "").split(";").map(c => c.trim())
        .find(c => /^jipfit_session=[A-Za-z0-9_-]+$/.test(c));
      if (session) headers.set("Cookie", session);
    }
    const post = request.method === "POST";
    if (post) {
      if (request.headers.get("X-Jipfit-Action") !== "1" ||
          (request.headers.has("Origin") && request.headers.get("Origin") !== url.origin)) return error("다시 시도해 주세요.", 403);
      headers.set("Content-Type","application/json");
      headers.set("X-Jipfit-Action","1");
    }
    try {
      const origin = new URL(env.API_ORIGIN);
      if (origin.protocol !== "https:" || origin.host === url.host) throw new Error();
      const response = await fetch(new URL(url.pathname + url.search, origin.origin), {
        headers, method: request.method, body: post ? request.body : undefined, redirect: "manual", cache: "no-store"
      });
      if (!response.headers.get("content-type")?.includes("application/json")) throw new Error();
      const out = new Headers({"Content-Type":"application/json; charset=utf-8","Cache-Control":"no-store"});
      const cookie = response.headers.get("Set-Cookie");
      if (account && cookie?.startsWith("jipfit_session=")) out.set("Set-Cookie",cookie);
      return new Response(response.body,{status:response.status,headers:out});
    } catch {
      return error("집핏 서버 연결을 확인해 주세요.", 503);
    }
  }
};
