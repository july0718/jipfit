'use strict';
(function(root){
  function defaultGroup(c){return c.groups.find(g=>g.forecasts['12'])||[...c.groups].sort((a,b)=>b.trades.length-a.trades.length)[0];}
  function latestDate(c){return c.groups.map(g=>g.trades.at(-1)?.date||'').sort().at(-1)||'';}
  function forecastState(group,horizon){
    const forecast=group?.forecasts?.[horizon];
    if(!forecast)return{kind:'missing',forecast:null,label:'예측 없음'};
    if(forecast.status==='baseline_only_not_a_learned_forecast')return{kind:'withheld',forecast,label:'학습모델 채택 보류'};
    if(!Number.isFinite(forecast.returnPercent)||!Number.isFinite(forecast.future))return{kind:'missing',forecast:null,label:'예측 없음'};
    return{kind:'experimental',forecast,label:'실험용 예측'};
  }
  function sortComplexes(complexes,mode,horizon){
    return [...complexes].sort((a,b)=>{
      if(mode==='recent'){const cmp=latestDate(b).localeCompare(latestDate(a));if(cmp)return cmp;}
      if(mode==='return'){
        const sa=forecastState(defaultGroup(a),horizon),sb=forecastState(defaultGroup(b),horizon);
        const fa=sa.kind==='experimental'?sa.forecast:null,fb=sb.kind==='experimental'?sb.forecast:null;
        const ra=fa?.returnPercent,rb=fb?.returnPercent;
        const va=Number.isFinite(ra),vb=Number.isFinite(rb);
        if(va!==vb)return va?-1:1;
        if(va&&rb!==ra)return rb-ra;
      }
      return b.count-a.count||a.id.localeCompare(b.id);
    });
  }
  const api={defaultGroup,latestDate,sortComplexes,forecastState};if(typeof module==='object'&&module.exports)module.exports=api;else root.GwangjinView=api;
})(typeof window!=='undefined'?window:globalThis);
