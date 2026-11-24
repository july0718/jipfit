'use strict';
(function(root){
  const normalize=s=>String(s||'').trim().replace(/\s+/g,' ');
  function exactAddressCandidates(query,addresses){
    const q=normalize(query);
    if(!q)return[];
    return (addresses||[]).filter(a=>{
      const v=normalize(a.jibunAddress);
      const lat=Number(a.y),lng=Number(a.x);
      return (v===q||v.startsWith(q+' '))&&Number.isFinite(lat)&&Number.isFinite(lng)&&lat>=33&&lat<=39&&lng>=124&&lng<=132;
    });
  }
  const api={normalize,exactAddressCandidates};
  if(typeof module==='object'&&module.exports)module.exports=api;
  else root.GwangjinGeocode=api;
})(typeof window!=='undefined'?window:globalThis);
