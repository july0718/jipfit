import fs from "node:fs/promises";
import path from "node:path";
import {createHash} from "node:crypto";

const source = process.argv[2];
const target = process.argv[3];
if (!source || !target) throw new Error("Expected snapshot directory or HTTPS origin, then output directory");
const remote = source.startsWith("https://");
async function api(route) {
  const response = await fetch(new URL(route, source), {signal: AbortSignal.timeout(90000)});
  if (!response.ok) throw new Error(route + ": HTTP " + response.status);
  return response.json();
}
const districts = remote ? await api("/api/districts")
  : JSON.parse(await fs.readFile(path.join(source, "manifest.json"))).districts;
if (districts.length !== 25 || new Set(districts.map(d => d.code)).size !== 25) throw new Error("Incomplete district catalog");
const files = new Map();
let count = 0;
for (const {code, name, complexes} of districts) {
  if (!/^\d{5}$/.test(code)) throw new Error("Invalid district code");
  let rows;
  if (remote) rows = await api("/api/complexes?" + new URLSearchParams({district: name}));
  else {
    const bytes = await fs.readFile(path.join(source, "objects", complexes + ".json"));
    if (createHash("sha256").update(bytes).digest("hex") !== complexes) throw new Error("Invalid snapshot checksum");
    rows = JSON.parse(bytes);
  }
  const records = new Map();
  for (const row of rows) {
    const {complex_id, name: title, address, built, latitude, longitude} = row;
    if (complex_id.split(" ")[1] !== name || !title || !address ||
        [built, latitude, longitude].includes(undefined)) throw new Error("Incomplete metadata for " + complex_id);
    records.set(complex_id, {complex_id, name: title, address, built, latitude, longitude});
  }
  if (!records.size) throw new Error("Empty district: " + name);
  files.set(code, JSON.stringify([...records.values()].sort((a,b) => a.complex_id < b.complex_id ? -1 : 1)) + "\n");
  count += records.size;
}
await fs.mkdir(target, {recursive:true});
for (const [code, data] of files) await fs.writeFile(path.join(target, code + ".json"), data);
console.log(files.size + " districts, " + count + " complexes");
