'use strict';
window.ReaderSession = (() => {
  const isKol = document.documentElement.dataset.readingMode === 'kol';
  const configuredBase = document.documentElement.dataset.kolBase;
  const base = isKol ? (configuredBase === '' ? '' : /^\/[a-zA-Z0-9/_-]*$/.test(configuredBase || '') ? configuredBase.replace(/\/$/,'') : '/kol') : '';
  let authenticated = !isKol, revision = 0, configured = false, checking = null, gate, notice, form, input, submit, sessionButton, channel;
  const callbacks = {lock:new Set(),unlock:new Set()}, controllers = new Set();
  const emit = kind => callbacks[kind].forEach(callback=>callback());
  function node(tag,cls,text) { const n=document.createElement(tag);if(cls)n.className=cls;if(text)n.textContent=text;return n; }
  function expired() { const error=new Error('私人会话需要重新验证');error.authExpired=true;return error; }
  function valid(value) { return !isKol || authenticated && value === revision; }
  function lock(message='私人会话已结束，请重新解锁。',broadcast=false) {
    if (!isKol) return;
    revision++; authenticated=false; checking=null; controllers.forEach(c=>c.abort());controllers.clear();
    document.documentElement.classList.add('reader-locked');
    if (input) input.value=''; if(notice)notice.textContent=message;if(gate)gate.hidden=false;
    if(sessionButton)sessionButton.textContent='解锁阅读';
    emit('lock');
    if (broadcast) { try { channel?.postMessage({type:'private-session-locked',reason:'reader-session'}); } catch {} }
  }
  async function request(url,options={}) {
    if(!isKol)return fetch(url,{...options,cache:'no-store'});
    if(!authenticated)throw expired();
    const requestedRevision=revision, controller=new AbortController();controllers.add(controller);
    const cancel=()=>controller.abort();options.signal?.addEventListener('abort',cancel,{once:true});
    if(options.signal?.aborted)controller.abort();
    try {
      const response=await fetch(url,{...options,credentials:'same-origin',cache:'no-store',signal:controller.signal});
      if(!valid(requestedRevision))throw expired();
      if(response.status===401||response.status===403){lock('私人会话已失效，请重新解锁。',true);throw expired();}
      const originalJSON=response.json.bind(response);
      response.json=async()=>{const result=await originalJSON();if(!valid(requestedRevision))throw expired();return result;};
      return response;
    } finally {controllers.delete(controller);options.signal?.removeEventListener('abort',cancel);}
  }
  async function check({notify=true}={}) {
    if(!isKol)return true;
    if(checking)return checking;
    const generation=revision;
    const promise=(async()=>{
      try {
        const response=await fetch(`${base}/api/auth/status`,{cache:'no-store',credentials:'same-origin',headers:{Accept:'application/json'}});
        const result=await response.json();if(generation!==revision)return false;
        configured=Boolean(result.configured);submit.disabled=!configured;input.disabled=!configured;
        if(!response.ok||!result.authenticated){lock(configured?'输入 KOL 页面口令，解锁播客与公众号阅读。':'服务端尚未配置私人阅读口令。');return false;}
        const changed=!authenticated;authenticated=true;gate.hidden=true;notice.textContent='';document.documentElement.classList.remove('reader-locked');sessionButton.textContent='退出阅读';
        if(notify&&changed)emit('unlock');return true;
      } catch {if(generation===revision)lock('暂时无法核验会话，请稍后重试。');return false;}
      finally {if(checking===promise)checking=null;}
    })();checking=promise;return promise;
  }
  async function login(event) {
    event.preventDefault();submit.disabled=true;notice.textContent='正在验证…';
    let passcode=input.value;input.value='';const generation=revision;
    try {
      const response=await fetch(`${base}/api/auth/login`,{method:'POST',cache:'no-store',credentials:'same-origin',headers:{'Content-Type':'application/json',Accept:'application/json'},body:JSON.stringify({passcode})});passcode='';
      if(generation!==revision)return;
      if(!response.ok)throw new Error(response.status===401?'口令不正确，请重新输入。':response.status===429?'尝试次数过多，请稍后再试。':'解锁失败，请稍后重试。');
      if(checking)await checking;await check();
    } catch(error) {if(generation===revision)notice.textContent=error.message||'暂时无法连接服务。';}
    finally {passcode='';input.value='';submit.disabled=!configured;}
  }
  async function logout() {
    lock('正在退出私人阅读…',true);
    try {const response=await fetch(`${base}/api/auth/logout`,{method:'POST',cache:'no-store',credentials:'same-origin'});if(!response.ok)throw new Error();notice.textContent='已退出私人阅读。';}
    catch {notice.textContent='内容已清除，但服务端退出未确认；请重试退出。';sessionButton.textContent='重试退出';sessionButton.dataset.logoutPending='true';}
  }
  function initializeUI() {
    if(!isKol)return;
    document.documentElement.classList.add('reader-locked');
    const links=document.querySelectorAll('.content-nav a');if(links[0])links[0].href=`${base}/podcasts`;if(links[1])links[1].href=`${base}/wechat`;
    const local=document.querySelector('.local-label');if(local)local.textContent='私人阅读';
    const bar=node('div','reader-session-bar');const back=node('a','reader-home-link','返回 KOL');back.href=`${base}/`;
    sessionButton=node('button','reader-session-button','解锁阅读');sessionButton.type='button';sessionButton.id='reader-session-button';
    sessionButton.addEventListener('click',()=>{if(authenticated||sessionButton.dataset.logoutPending){delete sessionButton.dataset.logoutPending;void logout();}else{gate.hidden=false;input.focus();}});bar.append(back,sessionButton);document.querySelector('.masthead').after(bar);
    gate=node('section','reader-auth-gate');gate.id='reader-auth-gate';gate.setAttribute('aria-labelledby','reader-auth-title');const title=node('h2','','解锁私人阅读');title.id='reader-auth-title';notice=node('p','reader-auth-notice','正在核验 KOL 会话…');notice.id='reader-auth-notice';notice.setAttribute('role','status');
    form=node('form','reader-auth-form');form.id='reader-auth-form';const label=node('label','','KOL 页面口令');label.htmlFor='reader-passcode';input=node('input');input.id='reader-passcode';input.type='password';input.name='passcode';input.autocomplete='current-password';input.required=true;input.disabled=true;submit=node('button','','解锁阅读');submit.type='submit';submit.disabled=true;form.append(label,input,submit);form.addEventListener('submit',login);const retry=node('button','reader-auth-recheck','重新检查会话');retry.type='button';retry.addEventListener('click',()=>check());gate.append(title,notice,form,retry);bar.after(gate);
    document.querySelectorAll('[data-local-copy]').forEach(n=>{n.textContent=n.dataset.kolCopy||n.textContent;});
    try{channel=new BroadcastChannel('finance-radar-private-session');channel.onmessage=e=>{if(e.data?.type==='private-session-locked')lock('另一页面已退出私人模式，请重新解锁。');};}catch{}
    document.addEventListener('visibilitychange',()=>{if(document.hidden)lock('返回页面后会重新核验会话。');else{lock('正在重新核验会话…');void check();}});
    window.addEventListener('pagehide',()=>lock('正在重新核验会话…'));
    window.addEventListener('pageshow',e=>{if(e.persisted){lock('正在重新核验会话…');void check();}});
  }
  initializeUI();
  return {isKol,base,on:(kind,callback)=>callbacks[kind].add(callback),revision:()=>revision,valid,authenticated:()=>authenticated,lock,check,init:()=>check({notify:false}),request,
    errorMessage:(result,status)=>typeof result?.error==='string'?result.error:({reading_body_unavailable:'尚未取得可用于摘要的正文。',reading_item_not_found:'这篇内容已不存在，请刷新列表。',item_not_found:'这篇内容已不存在，请刷新列表。',reading_service_unavailable:'摘要服务暂不可用，请稍后重试。',service_unavailable:'摘要服务暂不可用，请稍后重试。'})[result?.detail]||`请求失败（HTTP ${status}）`,
    api:kind=>isKol?`${base}/api/private/reading/${kind}`:kind==='summary'?'/api/wechat/summarize':`/api/${kind==='podcasts'?'podcasts':'wechat'}`,
    summaryHeaders:token=>isKol?{'Content-Type':'application/json','X-Finance-Radar-Action':'request-reading-summary'}:{'Content-Type':'application/json','X-Reader-Token':token},
    mediaURL:ep=>isKol&&ep.audio_url&&/^[a-zA-Z0-9_-]+$/.test(ep.id)?`${base}/api/private/reading/media/${encodeURIComponent(ep.id)}.wav`:null};
})();
