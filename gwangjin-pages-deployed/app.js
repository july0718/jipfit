'use strict';
const $=id=>document.getElementById(id);
let dataset,selected=null,group=null,horizon='12',map=null,pickingId=null,markers=new Map(),coordinates=new Map();
let batchRunning=false,batchStopRequested=false;
const addressCache=new Map();
let markerMode='price';
const compared=[];
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=v=>Number.isFinite(Number(v))?`${(Number(v)/10000).toFixed(2)}억`:'—';
const message=t=>{$('mapStatus').textContent=t;};
const periodLabel=()=>horizon==='12'?'1년':horizon+'개월';
const errorLabel=v=>Number.isFinite(v)?v.toFixed(2)+'%p':'자료 없음';
const methodLabel=value=>value==='price_unchanged'?'가격 유지 기준':'학습모델';
const featureLabel=()=>dataset?.meta.metrics.find(m=>m.horizon_months===Number(horizon))?.feature_label||'추가 변수 모델';
const sourceMonth=value=>String(value??'').match(/^\d{4}-\d{2}/)?.[0]||'확인 필요';
const sourceDate=value=>String(value??'').match(/^\d{4}-\d{2}-\d{2}/)?.[0]||'확인 필요';
function renderMacroSummary(){
  const meta=dataset.meta,macro=meta.macro||{},latest=macro.latest||{};
  const report=meta.metrics.find(m=>m.horizon_months===Number(horizon));
  const usesIndex=(report?.added_features||macro.added_features||[]).some(name=>name.startsWith('gwangjin_apt_index'));
  $('modelAsOf').textContent=String(meta.asof).slice(0,7).replace('-','.')+' 기준';
  let html=usesIndex?'<p>이 기간은 기존 거래 특성에 한국은행 기준금리와 한국부동산원의 광진구 아파트 조사형 매매가격지수 변화를 추가했습니다.</p>':'<p>1년 모델은 기존 거래 특성에 한국은행 기준금리와 3개월 금리 변화만 추가했습니다. 지역 가격지수는 입력에 사용하지 않습니다.</p><p>과거 개발 비교를 바탕으로 선택한 단순 설정이며, 새로운 기간에서 추가 검증이 필요합니다.</p>';
  if(report?.linked_model)html='<p>면적과 시장 국면을 함께 보정하며, 과거 검증 결과에 따라 구 지수 예측을 추가합니다.</p>';
  if(report?.indexed_anchor)html='<p>이 예측은 과거 거래가격을 기준월에 공개된 광진구 지수 수준으로 환산합니다.</p>';
  html+='<p>각 기준월 1일까지 이용 가능한 값만 연결합니다. 지역지수는 대상월과 공표일이 다르며, 미래 금리·지수의 실제값을 입력하지 않습니다.</p>';
  if(Number.isFinite(latest.policy_rate))html+=`<p>입력 기준금리 <b>${latest.policy_rate.toFixed(2)}%</b>${latest.policy_rate_source_month?' · 원자료 '+sourceMonth(latest.policy_rate_source_month):''}${latest.policy_rate_available_at?' · 이용 가능일 '+sourceDate(latest.policy_rate_available_at):''}</p>`;
  if(usesIndex&&latest.gwangjin_apt_index_source_month)html+=`<p>지역지수 원자료 <b>${sourceMonth(latest.gwangjin_apt_index_source_month)}</b>${latest.gwangjin_apt_index_available_at?' · 이용 가능일 '+sourceDate(latest.gwangjin_apt_index_available_at):''}</p>`;
  html+='<p>외부 통계의 수정 이력을 모두 복원한 검증은 아닙니다. 과거 평가와 재학습 모델의 실제 미래 성능은 다를 수 있습니다.</p>';
  $('macroBody').innerHTML=html;
}
function defaultGroup(c){return GwangjinView.defaultGroup(c);}
function renderList(){
  const q=$('search').value.trim().toLowerCase();
  const filtered=GwangjinView.sortComplexes(dataset.complexes.filter(c=>(!$('forecastOnly').checked||c.hasForecast)&&(`${c.name} ${c.address}`.toLowerCase().includes(q))),$('listSort').value,horizon);
  $('listCount').textContent=`${filtered.length}개 단지 후보 · 위치 연결 ${coordinates.size}곳`;
  $('list').replaceChildren();
  for(const c of filtered){const b=document.createElement('button');b.className=selected?.id===c.id?'selected':'';const latest=GwangjinView.latestDate(c);const coord=coordinates.get(c.id);const g=defaultGroup(c),state=GwangjinView.forecastState(g,horizon),f=state.forecast;const rate=state.kind==='experimental'?`${f.returnPercent>0?'+':''}${f.returnPercent.toFixed(1)}%`:state.label;b.innerHTML=`<strong>${esc(c.name)}</strong><div class="row"><small>${esc(c.address.replace('서울특별시 광진구 ',''))}</small><span class="dot">${coord?(coord.source==='geocode_auto'?'● 자동 위치':'● 위치 확인'):''}</span></div><small>${c.count.toLocaleString()}건 · 최근 ${esc(latest||'—')}</small><div class="preview-rate">${g.areaGroup}㎡ 내외 · ${horizon==='12'?'1년':horizon+'개월'} ${esc(rate)}</div>`;b.onclick=()=>selectComplex(c.id);$('list').append(b);}
}
function selectComplex(id){
  const c=dataset.complexes.find(c=>c.id===id);if(!c)return;
  selected=c;group=defaultGroup(c);pickingId=null;$('detailEmpty').hidden=true;$('detailContent').hidden=false;
  $('name').textContent=c.name;$('address').textContent=c.address;
  $('facts').textContent=`${c.built}년 준공 · ${c.count.toLocaleString()}건 · 전용면적군 ${c.groups.length}개`;
  $('area').replaceChildren();for(const g of c.groups){const o=document.createElement('option');o.value=g.areaGroup;o.textContent=`${g.areaGroup}㎡ 내외 (${g.trades.length}건)`;$('area').append(o);}$('area').value=group.areaGroup;
  $('candidates').replaceChildren();$('locationStatus').textContent=coordinates.has(id)?(coordinates.get(id).source==='geocode_auto'?'주소가 일치하는 자동 검색 위치입니다. 실제 단지 건물·대표점을 확인하세요.':'선택한 위치가 지도에 표시되어 있습니다.'):'아직 좌표가 없습니다. 주소로 찾거나 지도에서 지정하세요.';
  renderDetails();renderList();const pos=coordinates.get(id);if(map&&pos){map.setCenter(new naver.maps.LatLng(pos.lat,pos.lng));map.setZoom(16);}
}
function renderDetails(){
  if(!selected||!group)return;
  const state=GwangjinView.forecastState(group,horizon),f=state.forecast;
  if(state.kind==='withheld'){$('forecast').innerHTML=`<div class="forecast-card withheld"><div class="label">${periodLabel()} 예측</div><h3>학습모델 채택 보류</h3><p>검증에서 학습모델이 가격 유지 기준보다 개선되지 않아 미래가격·상승률을 제공하지 않습니다.</p><div class="prices"><div><small>현재 기준가격</small>${money(f.current)}</div><div><small>미래 예측가격</small>제공 보류</div></div></div>`;}
  else if(state.kind==='experimental'){const pct=f.returnPercent;$('forecast').innerHTML=`<div class="forecast-card"><div class="label">실험용 예상 가격상승률</div><div class="number">${pct>0?'+':''}${pct.toFixed(1)}<span style="font-size:18px">%</span></div><div class="prices"><div><small>현재 기준가격</small>${money(f.current)}</div><div><small>${horizon==='12'?'1년':horizon+'개월'} 후 계산값</small>${money(f.future)}</div></div></div>`;
  }else $('forecast').innerHTML='<div class="empty">이 단지·면적군은 현재 모델의 거래량·최근성 조건을 충족하지 않아 예측을 제공하지 않습니다. 아래 실거래는 조회할 수 있습니다.</div>';
  const m=dataset.meta.metrics.find(m=>m.horizon_months===Number(horizon));
  $('metric').textContent=state.kind==='experimental'?`과거 평가 상승률 MAE ${errorLabel(m?.test_model?.MAE_return_percentage_points)}. ${m?.indexed_anchor ? '비교에는 기존 기준가격을 공통 분모로 사용했습니다. ' : ''}거래는 계약월에서 2개월 지연해 입력했습니다.`:state.kind==='withheld'?'저장된 0%는 가격 유지 기준의 계산값이며, 학습모델의 보합 예측으로 표시하지 않습니다.':'';
  const c=dataset.meta.comparisons?.find(row=>row.horizon_months===Number(horizon));
  let quality=m?`<p>사용한 구성: <b>${esc(featureLabel())}</b></p><p>검증 상승률 MAE: 학습모델 <b>${errorLabel(m.validation_model_mae_pp)}</b> / 가격 유지 <b>${errorLabel(m.validation_baseline_mae_pp)}</b></p><p>과거 평가 상승률 MAE: ${methodLabel(m.selected)} <b>${errorLabel(m.test_model?.MAE_return_percentage_points)}</b> / 가격 유지 <b>${errorLabel(m.test_baseline?.MAE_return_percentage_points)}</b></p>`:'<p>이 기간의 평가 자료가 없습니다.</p>';
  if(m?.linked_model)quality=`<p>사용한 구성 · ${esc(featureLabel())}</p><p>과거 평가 상승률 MAE · ${errorLabel(m.test_model.MAE_return_percentage_points)}</p>`;
  if(m?.indexed_anchor)quality+=`<p>가격 MAE · ${Math.round(m.indexed_anchor.price_mae_manwon).toLocaleString()}만원 (면적 그룹 기준)</p><p>전후 상승률 MAE는 기존 기준가격을 공통 분모로 계산했습니다.</p>`;
  if(c){
    quality+=`<p><b>같은 표본의 기존 변수·추가 변수 비교</b></p><p>기존 14개 변수: ${methodLabel(c.baseline_selected)} <b>${errorLabel(c.baseline_test_mae_pp)}</b><br>${esc(featureLabel())}: ${methodLabel(c.enriched_selected)} <b>${errorLabel(c.enriched_test_mae_pp)}</b></p>`;
    quality+='<p>두 엑셀의 2005~2026년 거래를 사용해 같은 조건으로 다시 학습한 비교입니다. 이전 인수인계 문서의 수치와 직접 대조할 수 없습니다. 이미 확인한 과거 평가기간을 사용하는 개발 비교이며, 새 기간의 독립적인 검증은 아닙니다.</p>';
    if(Number.isFinite(c.baseline_hgb_test_mae_pp)&&Number.isFinite(c.enriched_hgb_test_mae_pp))quality+=`<p>가격 유지 기준을 선택하기 전 학습모델끼리의 MAE: 기존 <b>${errorLabel(c.baseline_hgb_test_mae_pp)}</b> / 추가 <b>${errorLabel(c.enriched_hgb_test_mae_pp)}</b></p>`;
  }
  if(m)quality+=`<p>평가 기준월 시작 ${esc(m.test_start)} · ${Number(m.test_n).toLocaleString()}개 월별 표본. 표본 기간이 겹치며 독립 거래 건수가 아닙니다. 기간별 평가 구간이 달라 단순 비교할 수 없습니다.</p>`;
  $('qualityBody').innerHTML=quality+'<p>MAE는 예상 상승률과 실제 상승률의 평균 절대 차이입니다. 정확도나 예측구간을 뜻하지 않으며 단지별 오차도 아닙니다.</p>';
  $('tradeCount').textContent=`전체 ${group.trades.length}건`;
  $('trades').innerHTML=[...group.trades].reverse().slice(0,12).map(t=>`<tr><td>${esc(t.date)}</td><td>${t.area.toFixed(2)}</td><td>${t.floor??'—'}</td><td>${money(t.price)}</td></tr>`).join('');
  renderChart();updateMarkers();syncCompareButton();
}
function renderChart(){
  const buckets=new Map();for(const t of group.trades){const key=t.date.slice(0,7);if(!buckets.has(key))buckets.set(key,[]);buckets.get(key).push(t.price/10000);}
  const points=[...buckets].sort(([a],[b])=>a.localeCompare(b)).map(([date,values])=>{values.sort((a,b)=>a-b);const m=Math.floor(values.length/2);return{date,value:values.length%2?values[m]:(values[m-1]+values[m])/2,t:Date.parse(date+'-15T00:00:00Z')};});
  if(!points.length){$('chart').textContent='거래 자료가 없습니다.';return;}
  const W=340,H=170,L=35,R=12,T=15,B=28,minT=points[0].t,maxT=points.at(-1).t;
  const min=Math.min(...points.map(p=>p.value))*.9,max=Math.max(...points.map(p=>p.value))*1.06;
  const x=p=>L+(p.t-minT)/Math.max(1,maxT-minT)*(W-L-R),y=v=>H-B-(v-min)/Math.max(.01,max-min)*(H-T-B);
  const grid=[0,.5,1].map(a=>{const v=min+(max-min)*a;return`<line x1="${L}" x2="${W-R}" y1="${y(v)}" y2="${y(v)}" stroke="#e9efec"/><text x="${L-5}" y="${y(v)+4}" text-anchor="end" fill="#84928b" font-size="10">${v.toFixed(1)}</text>`;}).join('');
  let lines='';for(let i=1;i<points.length;i++){if(points[i].t-points[i-1].t<100*86400000)lines+=`<line x1="${x(points[i-1])}" y1="${y(points[i-1].value)}" x2="${x(points[i])}" y2="${y(points[i].value)}" stroke="#4da78c" stroke-width="1.5"/>`;}
  $('chart').innerHTML=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="월별 실거래가격 중앙값 그래프">${grid}${lines}${points.map(p=>`<circle cx="${x(p)}" cy="${y(p.value)}" r="2.4" fill="#087e74"><title>${esc(p.date)} · ${p.value.toFixed(2)}억</title></circle>`).join('')}<text x="${L}" y="${H-7}" fill="#84928b" font-size="10">${points[0].date}</text><text x="${W-R}" y="${H-7}" text-anchor="end" fill="#84928b" font-size="10">${points.at(-1).date}</text></svg>`;
}
function updateMarkers(){
  if(!map||!dataset)return;
  for(const [id,pos] of coordinates){const c=dataset.complexes.find(c=>c.id===id);if(!c)continue;const g=selected?.id===id?group:defaultGroup(c),f=g.forecasts[horizon],last=g.trades.at(-1);
    const state=GwangjinView.forecastState(g,horizon);
    const label=markerMode==='return'?(state.kind==='experimental'?`${f.returnPercent>0?'+':''}${f.returnPercent.toFixed(1)}%`:state.label):(f?money(f.current):(last?money(last.price):'가격 없음'));
    const subtitle=markerMode==='return'?`${horizon==='12'?'1년':horizon+'개월'} ${state.kind==='experimental'?'실험용':'예측 미제공'}`:(f?'기준가격':'최근 실거래');
    const classes=markerMode==='return'?` return${state.kind!=='experimental'?' unavailable':f.returnPercent<0?' negative':''}`:'';
    const icon={content:`<div class="price-pin${classes}">${esc(c.name)}<b>${esc(label)}</b><span>${g.areaGroup}㎡ 내외 · ${esc(subtitle)}</span></div>`,anchor:new naver.maps.Point(55,65)};
    let marker=markers.get(id);if(marker){marker.setIcon(icon);marker.setPosition(new naver.maps.LatLng(pos.lat,pos.lng));}
    else{marker=new naver.maps.Marker({map,position:new naver.maps.LatLng(pos.lat,pos.lng),icon,title:c.name});naver.maps.Event.addListener(marker,'click',()=>selectComplex(id));markers.set(id,marker);}
  }
}
function setCoordinate(id,lat,lng,source){if(!Number.isFinite(lat)||!Number.isFinite(lng)||lat<33||lat>39||lng<124||lng>132)throw new Error('한국 내 유효한 위도·경도가 아닙니다.');coordinates.set(id,{lat,lng,source});updateMarkers();renderList();map?.setCenter(new naver.maps.LatLng(lat,lng));map?.setZoom(16);if(selected?.id===id)$('locationStatus').textContent=source==='manual'?'직접 지정한 위치입니다. 주소·지도 건물명을 확인하세요.':'주소 검색 후보를 선택했습니다. 실제 단지 위치를 확인하세요.';}
function loadMap(){
  if(!window.NAVER_MAP_CLIENT_ID||window.NAVER_MAP_CLIENT_ID.includes('CLIENT_ID')){message('config.js에 네이버 지도 Client ID를 입력하세요. 단지 목록과 예측 조회는 사용할 수 있습니다.');return;}
  window.navermap_authFailure=()=>message('지도 인증 실패: Client ID, Dynamic Map, 등록 주소 http://localhost:8000을 확인하세요.');
  window.initGwangjinMap=()=>{map=new naver.maps.Map('map',{center:new naver.maps.LatLng(37.5385,127.0823),zoom:14,zoomControl:true});
    naver.maps.Event.addListener(map,'click',e=>{if(!pickingId)return;const id=pickingId;pickingId=null;setCoordinate(id,e.coord.lat(),e.coord.lng(),'manual');message('위치를 지정했습니다. 마커를 누르면 상세정보가 열립니다.');});
    updateMarkers();message('지도 연결 완료 · 목록에서 단지를 선택해 위치를 확인하세요.');};
  const script=document.createElement('script');script.src='https://oapi.map.naver.com/openapi/v3/maps.js?ncpKeyId='+encodeURIComponent(window.NAVER_MAP_CLIENT_ID)+'&submodules=geocoder&callback=initGwangjinMap';script.onerror=()=>message('지도 스크립트를 불러오지 못했습니다. 인터넷 연결과 Client ID를 확인하세요.');document.head.append(script);
}
async function geocode(){
  if(batchRunning){$('locationStatus').textContent='일괄 검색이 끝난 뒤 개별 검색을 실행하세요.';return;}
  if(!selected)return;if(!map||!window.naver?.maps.Service){$('locationStatus').textContent='지도를 먼저 연결하세요.';return;}
  const id=selected.id,query=selected.address;$('geocode').disabled=true;$('candidates').replaceChildren();$('locationStatus').textContent='주소를 검색하는 중입니다…';
  let expired=false;const timer=setTimeout(()=>{expired=true;$('geocode').disabled=false;if(selected?.id===id)$('locationStatus').textContent='주소 검색 응답이 없습니다. Geocoding 사용 설정을 확인하거나 지도에서 직접 지정하세요.';},12000);
  try{naver.maps.Service.geocode({query},(status,response)=>{if(expired)return;clearTimeout(timer);$('geocode').disabled=false;if(selected?.id!==id)return;
    const addresses=response?.v2?.addresses;
    if(status!==naver.maps.Service.Status.OK||!addresses?.length){$('locationStatus').textContent='주소 검색에 실패했습니다. Geocoding 사용 설정·등록 웹주소를 확인하거나 지도에서 직접 지정하세요.';return;}
    $('locationStatus').textContent='주소가 일치하는 후보를 선택하세요. 선택 전 지도 위치를 확인할 수 있습니다.';
    addresses.slice(0,5).forEach(a=>{const lat=Number(a.y),lng=Number(a.x);const b=document.createElement('button');b.textContent=(a.roadAddress||a.jibunAddress)+' · 이 위치 사용';b.onclick=()=>{setCoordinate(id,lat,lng,'geocode');$('candidates').replaceChildren();};$('candidates').append(b);});
    map.setCenter(new naver.maps.LatLng(Number(addresses[0].y),Number(addresses[0].x)));map.setZoom(17);
  });}catch(e){clearTimeout(timer);$('geocode').disabled=false;$('locationStatus').textContent='주소 검색을 사용할 수 없습니다. 지도에서 직접 지정하세요.';}
}
function requestAddress(query){
  if(addressCache.has(query))return Promise.resolve(addressCache.get(query));
  return new Promise(resolve=>{
    let done=false;
    function finish(result){if(done)return;done=true;clearTimeout(timer);if(result.status==='ok')addressCache.set(query,result);resolve(result);}
    const timer=setTimeout(()=>finish({status:'timeout',addresses:[]}),12000);
    try{naver.maps.Service.geocode({query},(status,response)=>{
      if(status!==naver.maps.Service.Status.OK){finish({status:'error',addresses:[]});return;}
      finish({status:'ok',addresses:response?.v2?.addresses||[]});
    });}catch(e){finish({status:'error',addresses:[]});}
  });
}
function batchControls(running){$('batchLocate').disabled=running;$('batchStop').disabled=!running;$('batchLimit').disabled=running;$('geocode').disabled=running;}
async function batchLocate(){
  if(batchRunning||!dataset)return;
  if(!map||!window.naver?.maps.Service){$('batchStatus').textContent='지도와 Geocoding 연결을 먼저 확인하세요.';return;}
  if($('geocode').disabled){$('batchStatus').textContent='개별 주소 검색이 끝난 뒤 실행하세요.';return;}
  const q=$('search').value.trim().toLowerCase();
  let targets=dataset.complexes.filter(c=>!coordinates.has(c.id)&&(!$('forecastOnly').checked||c.hasForecast)&&(`${c.name} ${c.address}`.toLowerCase().includes(q)));
  const limit=$('batchLimit').value;if(limit!=='all')targets=targets.slice(0,Number(limit));
  if(!targets.length){$('batchStatus').textContent='현재 필터 결과에서 추가로 찾을 단지가 없습니다.';return;}
  batchRunning=true;batchStopRequested=false;batchControls(true);pickingId=null;
  let processed=0,found=0,review=0,failed=0,consecutiveErrors=0;
  $('batchIssueList').replaceChildren();$('batchIssues').hidden=true;$('batchProgress').max=targets.length;$('batchProgress').value=0;
  function issue(c,reason){$('batchIssues').hidden=false;const b=document.createElement('button');b.textContent=`${c.name} · ${reason}`;b.onclick=()=>selectComplex(c.id);$('batchIssueList').append(b);}
  function progress(name=''){$('batchStatus').textContent=`${processed}/${targets.length}곳 · 연결 ${found} · 확인 필요 ${review} · 오류 ${failed}${name?' · '+name:''}`;$('batchProgress').value=processed;}
  try{
    for(const c of targets){
      if(batchStopRequested)break;
      progress(c.name);const response=await requestAddress(c.address);processed++;
      if(coordinates.has(c.id)){progress();continue;} // 검색 중 직접 지정한 좌표를 덮어쓰지 않습니다.
      if(response.status!=='ok'){failed++;consecutiveErrors++;issue(c,response.status==='timeout'?'응답 시간 초과':'API 오류');}
      else{
        consecutiveErrors=0;
        const matches=GwangjinGeocode.exactAddressCandidates(c.address,response.addresses);
        if(matches.length===1){const a=matches[0];coordinates.set(c.id,{lat:Number(a.y),lng:Number(a.x),source:'geocode_auto'});found++;updateMarkers();renderList();}
        else{review++;issue(c,matches.length>1?'주소 일치 후보가 여러 개':'정확히 일치하는 지번 없음');}
      }
      progress();
      if(consecutiveErrors>=3){batchStopRequested=true;message('API 오류가 3회 연속 발생해 검색을 중단했습니다. 사용 설정·한도·네트워크를 확인하세요.');break;}
      if(!batchStopRequested)await new Promise(resolve=>setTimeout(resolve,350));
    }
  }finally{
    batchRunning=false;batchControls(false);progress();
    $('batchStatus').textContent+=(batchStopRequested?' · 중지됨':' · 완료')+'. 위치는 이 페이지를 여는 동안 유지됩니다.';
    if(!batchStopRequested)message(`단지 위치 ${found}곳을 추가했습니다. 자동 위치 마커를 눌러 실제 주소와 단지를 확인하세요.`);
    if(selected&&coordinates.has(selected.id))$('locationStatus').textContent=coordinates.get(selected.id).source==='geocode_auto'?'주소 일치 자동 위치입니다. 단지 건물을 확인하세요.':'선택한 위치가 지도에 표시되어 있습니다.';
    renderList();
  }
}
$('batchLocate').onclick=batchLocate;
$('batchStop').onclick=()=>{batchStopRequested=true;$('batchStop').disabled=true;$('batchStatus').textContent='현재 요청이 끝나면 중지합니다. 최대 12초 걸릴 수 있습니다.';};
function syncCompareButton(){
  $('openCompare').textContent=`단지 비교 (${compared.length}/2)`;
  if(!selected||!group)return;
  const old=compared.find(x=>x.id===selected.id);
  $('addCompare').textContent=old?(old.areaGroup===group.areaGroup?'비교에서 이 단지 빼기':'비교 면적군을 현재 선택으로 변경'):'이 면적군으로 비교에 담기';
}
function renderComparison(){
  $('compareHorizon').value=horizon;
  if(!compared.length){$('compareBody').innerHTML='<div class="empty">단지를 선택하고 전용면적군을 정한 뒤 ‘비교에 담기’를 누르세요. 두 단지를 나란히 볼 수 있습니다.</div>';return;}
  const items=compared.map(ref=>{const c=dataset.complexes.find(c=>c.id===ref.id);const g=c.groups.find(g=>g.areaGroup===ref.areaGroup);return{c,g,f:g.forecasts[horizon],state:GwangjinView.forecastState(g,horizon)};});
  const row=(title,fn)=>`<tr><td>${esc(title)}</td>${items.map((item,i)=>`<td>${fn(item,i)}</td>`).join('')}${items.length===1?'<td>두 번째 단지를 담아보세요.</td>':''}</tr>`;
  let html=`<table class="comparison-table"><thead><tr><th>비교 항목</th>${items.map(({c},i)=>`<th>${esc(c.name)}<small>${esc(c.address)}</small><button data-remove-compare="${i}">비교에서 빼기</button></th>`).join('')}${items.length===1?'<th>두 번째 후보</th>':''}</tr></thead><tbody>`;
  html+=row('전용면적군',({g})=>`${g.areaGroup}㎡ 내외`);
  html+=row('모델 대표면적',({f})=>f?`${f.representativeArea.toFixed(2)}㎡`:'—');
  html+=row('준공연도',({c})=>`${c.built}년`);
  html+=row('현재 기준가격',({f})=>f?money(f.current):'산정값 없음');
  html+=row(`${horizon==='12'?'1년':horizon+'개월'} 후 계산값`,({f,state})=>state.kind==='experimental'?money(f.future):state.kind==='withheld'?'제공 보류':'예측 없음');
  html+=row('예상 가격상승률',({f,state})=>state.kind==='experimental'?`<strong>${f.returnPercent>0?'+':''}${f.returnPercent.toFixed(1)}%</strong>`:esc(state.label));
  html+=row('예상 차익',({f,state})=>state.kind==='experimental'?`${f.future-f.current>0?'+':''}${money(f.future-f.current)}`:'—');
  html+=row('이 면적군 거래',({g})=>`${g.trades.length.toLocaleString()}건`);
  html+=row('최근 실거래',({g})=>{const t=g.trades.at(-1);return t?`${money(t.price)}<small>${esc(t.date)} · ${t.area.toFixed(2)}㎡ · ${t.floor??'—'}층</small>`:'—';});
  html+=row('결과 성격',({state})=>state.kind==='missing'?'표본 기준 미충족':state.kind==='withheld'?'검증에서 학습모델이 가격 유지 기준보다 개선되지 않아 채택 보류':'실험용 · 단지별 신뢰도 미검증');
  $('compareBody').innerHTML=html+'</tbody></table>';
}
function setHorizon(value){
  if(!['3','6','12'].includes(String(value)))return;horizon=String(value);
  $('markerHorizon').value=horizon;$('compareHorizon').value=horizon;
  const held=dataset.meta.metrics.find(m=>m.horizon_months===Number(horizon))?.selected==='price_unchanged';
  const option=$('listSort').querySelector('option[value="return"]');option.disabled=held;
  if(held&&$('listSort').value==='return')$('listSort').value='count';
  document.querySelectorAll('[data-h]').forEach(b=>b.classList.toggle('active',b.dataset.h===horizon));
  renderMacroSummary();renderDetails();renderList();renderComparison();
}
$('search').addEventListener('input',()=>dataset&&renderList());$('forecastOnly').onchange=()=>dataset&&renderList();
$('listSort').onchange=()=>dataset&&renderList();
$('showPrice').onclick=()=>{markerMode='price';$('showPrice').classList.add('active');$('showReturn').classList.remove('active');$('showPrice').setAttribute('aria-pressed','true');$('showReturn').setAttribute('aria-pressed','false');updateMarkers();};
$('showReturn').onclick=()=>{markerMode='return';$('showReturn').classList.add('active');$('showPrice').classList.remove('active');$('showReturn').setAttribute('aria-pressed','true');$('showPrice').setAttribute('aria-pressed','false');updateMarkers();};
$('markerHorizon').onchange=()=>dataset&&setHorizon($('markerHorizon').value);
$('compareHorizon').onchange=()=>dataset&&setHorizon($('compareHorizon').value);
$('addCompare').onclick=()=>{
  if(!selected||!group)return;const index=compared.findIndex(x=>x.id===selected.id);
  if(index>=0){if(compared[index].areaGroup===group.areaGroup)compared.splice(index,1);else compared[index].areaGroup=group.areaGroup;}
  else{if(compared.length>=2){$('compareStatus').textContent='비교 창에서 한 단지를 뺀 뒤 추가하세요. 최대 두 단지를 비교합니다.';return;}compared.push({id:selected.id,areaGroup:group.areaGroup});}
  $('compareStatus').textContent=`${compared.length}개 단지를 담았습니다. 상단 ‘단지 비교’로 확인하세요.`;syncCompareButton();renderComparison();
};
$('openCompare').onclick=()=>{if(!dataset)return;renderComparison();$('compareDialog').showModal();};
$('closeCompare').onclick=()=>$('compareDialog').close();
$('compareBody').onclick=e=>{const b=e.target.closest('button[data-remove-compare]');if(!b)return;compared.splice(Number(b.dataset.removeCompare),1);syncCompareButton();renderComparison();};
$('area').onchange=()=>{group=selected.groups.find(g=>String(g.areaGroup)===$('area').value);renderDetails();};
$('horizons').onclick=e=>{const b=e.target.closest('button[data-h]');if(b)setHorizon(b.dataset.h);};
$('geocode').onclick=geocode;
$('manual').onclick=()=>{if(!map){$('locationStatus').textContent='지도가 연결되어야 위치를 지정할 수 있습니다.';return;}pickingId=pickingId===selected.id?null:selected.id;message(pickingId?`${selected.name}: 주소와 일치하는 지도 건물을 클릭하세요. 다시 버튼을 누르면 취소됩니다.`:'위치 지정을 취소했습니다.');};
$('exportCoords').onclick=()=>{const entries=[...coordinates].filter(([,v])=>v.source==='manual');const blob=new Blob([JSON.stringify({coordinates:Object.fromEntries(entries)},null,2)],{type:'application/json'});const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download='gwangjin_manual_coordinates.json';link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000);message('직접 지정한 좌표 '+entries.length+'곳을 내보냈습니다. 주소 검색 좌표는 세션 동안만 사용합니다.');};
$('importCoords').onchange=async e=>{try{const file=e.target.files[0];if(!file)return;const data=JSON.parse(await file.text());if(!data.coordinates||typeof data.coordinates!=='object')throw new Error('좌표 파일 형식이 다릅니다.');const ids=new Set(dataset.complexes.map(c=>c.id));const entries=Object.entries(data.coordinates);for(const [id,p] of entries){if(!ids.has(id)||!Number.isFinite(p.lat)||!Number.isFinite(p.lng)||p.lat<33||p.lat>39||p.lng<124||p.lng>132)throw new Error('단지 ID 또는 좌표가 올바르지 않습니다.');}for(const [id,p] of entries)coordinates.set(id,{lat:p.lat,lng:p.lng,source:'manual'});updateMarkers();renderList();message(`좌표 ${entries.length}곳을 불러왔습니다.`);}catch(err){message('좌표 불러오기 실패: '+err.message);}finally{e.target.value='';}};
fetch('data.json').then(r=>{if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}).then(data=>{dataset=data;renderMacroSummary();renderList();selectComplex(data.complexes[0].id);setHorizon(horizon);loadMap();}).catch(e=>message('데이터를 읽지 못했습니다. 파일 더블클릭 대신 http://localhost:8000으로 여세요. '+e.message));




