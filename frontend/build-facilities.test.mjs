import test from "node:test";
import assert from "node:assert/strict";
import {enrich,distance} from "./build-facilities.mjs";

const sample = {complex_id:"서울특별시 광진구 구의동|1|현대",name:"현대",address:"서울특별시 광진구 구의동 1",latitude:37.5,longitude:127.1};
const apt = ["광진구","구의동","구의현대아파트",37.5,127.1,200,250];
const catalog = {apartments:{rows:[apt]},stations:{rows:[["A","1호선",37.5,127.1]]},
  schools:{rows:[["S","초등학교","도로 1",37.501,127.1]]}};
test("matches unique identities and uses straight-line distances",()=>{
  const f=enrich([sample],catalog)[0].facilities;
  assert.equal(f.households,200);assert.equal(f.parking,1.25);
  assert.equal(f.station[2],0);assert.equal(f.schools[0][2],111);
  assert.ok(distance(37.5,127,37.51,127)>1100);
});
test("ambiguous identities, distant matches and missing coordinates stay unknown",()=>{
  const both=enrich([sample,{...sample,complex_id:"other"}],catalog);
  assert.ok(both.every(h=>h.facilities.households===null));
  assert.equal(enrich([sample],{...catalog,apartments:{rows:[apt,apt]}})[0].facilities.parking,null);
  assert.equal(enrich([{...sample,latitude:37.6}],catalog)[0].facilities.households,null);
  assert.equal(enrich([{...sample,latitude:null}],catalog)[0].facilities.station,undefined);
});
test("verified zero parking is distinct from unknown",()=>{
  for(const [value,expected] of [[0,0],[null,null]]){
    const f=enrich([sample],{...catalog,apartments:{rows:[[...apt.slice(0,6),value]]}})[0].facilities;
    assert.equal(f.parking,expected);
  }
});