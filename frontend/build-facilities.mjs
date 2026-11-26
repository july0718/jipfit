import {readFile, readdir, mkdir, writeFile} from "node:fs/promises";
import {pathToFileURL} from "node:url";
import path from "node:path";

const earth = 6371008.8;
const radians = Math.PI / 180;
export function distance(lat, lon, y, x) {
  const a = Math.sin((y-lat)*radians/2)**2 + Math.cos(lat*radians)*Math.cos(y*radians)*Math.sin((x-lon)*radians/2)**2;
  return 2*earth*Math.asin(Math.sqrt(Math.min(1,a)));
}

function valid(lat, lon) { return lat >= 37 && lat <= 38.5 && lon >= 126 && lon <= 128; }
function simple(name, dong) {
  let s = name.normalize("NFKC").toLowerCase().replace(/[\s·._-]/g,"").replace(/아파트$/,"");
  const prefix = dong.replace(/동$/,"");
  if (prefix.length >= 2 && s.startsWith(prefix)) s = s.slice(prefix.length);
  return s;
}

function nearest(lat, lon, records) {
  let best, meters = Infinity;
  for (const [name, kind, y, x] of records) {
    const d = distance(lat, lon, y, x);
    if (d < meters) { best = [name, kind, y, x]; meters = d; }
  }
  return best ? [best[0],best[1],Math.round(meters),best[2],best[3]] : null;
}

export function enrich(homes, catalog) {
  const byName = new Map();
  for (const apt of catalog.apartments.rows) {
    const [gu, dong, name] = apt;
    const key = [gu,dong,simple(name,dong)].join("|");
    if (!byName.has(key)) byName.set(key,[]);
    byName.get(key).push(apt);
  }
  const levels = ["초등학교","중학교","고등학교"];
  const schools = levels.map(level => catalog.schools.rows.filter(s=>s[1]===level).map(([n,k,,y,x])=>[n,k,y,x]));
  const candidates = new Map(), uses = new Map();
  for (const h of homes) {
    const [,gu,dong] = h.address.split(/\s+/);
    if (!dong || !valid(h.latitude,h.longitude)) continue;
    const key = [gu,dong,simple(h.name,dong)].join("|");
    const hits = (byName.get(key)||[]).filter(a=>distance(h.latitude,h.longitude,a[3],a[4])<=350);
    for (const apt of hits) uses.set(apt,(uses.get(apt)||0)+1);
    if (hits.length===1) candidates.set(h.complex_id,hits[0]);
  }
  const nearby = new Map();
  return homes.map(h=>{
    const apt = candidates.get(h.complex_id);
    const matched = apt && uses.get(apt)===1;
    const households = matched && apt[5]>0 ? apt[5] : null;
    const parking = households && Number.isInteger(apt[6]) && apt[6]>=0 ? apt[6]/households : null;
    const key = [h.latitude,h.longitude].join(",");
    if (!nearby.has(key) && valid(h.latitude,h.longitude)) nearby.set(key,{
      station: nearest(h.latitude,h.longitude,catalog.stations.rows),
      schools: schools.map(rows=>nearest(h.latitude,h.longitude,rows)).filter(Boolean)
    });
    return {...h, facilities:{households,parking,...nearby.get(key)}};
  });
}

export async function build(input, output) {
  const catalog = {};
  for (const key of ["apartments","stations","schools"]) catalog[key]=JSON.parse(await readFile(new URL("./facilities/"+key+".json",import.meta.url),"utf8"));
  await mkdir(output,{recursive:true});
  await writeFile(path.join(output,"facility-sources.txt"), Object.values(catalog).map(c => c.source).join("\n") + "\n거리 기준: 수록된 좌표 간 직선거리. 학교 배정·도보 경로는 별도입니다.\n");
  let total=0, matched=0, located=0;
  for (const file of (await readdir(input)).filter(f=>/^\d{5}\.json$/.test(f))) {
    const rows=enrich(JSON.parse(await readFile(path.join(input,file),"utf8")),catalog);
    total+=rows.length; matched+=rows.filter(h=>h.facilities.households!=null).length; located+=rows.filter(h=>h.facilities.station).length;
    await writeFile(path.join(output,file),JSON.stringify(rows));
  }
  if (!total || !matched || !located) throw Error("시설 자료 연결 결과를 확인하세요.");
  try {
    const revision = await readFile(path.join(input, "verified-revision.txt"), "utf8");
    await writeFile(path.join(output, "verified-revision.txt"), revision);
  } catch (err) {
    if (err?.code !== "ENOENT") throw err;
  }
  console.log({total,matched,located});
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href===import.meta.url)
  await build(process.argv[2] || "public/metadata", process.argv[3] || "dist/metadata");
