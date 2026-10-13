'use strict';

const list = document.querySelector('#episode-list');
const reader = document.querySelector('#reader');
const refresh = document.querySelector('#refresh');
const session = window.ReaderSession;
const states = { summarized: ['模型初稿 · 待复核', 'pending'], summarizing: ['正在生成摘要', 'pending'], transcribed: ['文字稿就绪 · 待摘要', 'pending'], audio_ready: ['音频就绪 · 待转写', 'pending'], discovered: ['仅节目信息', ''], failed: ['处理失败', 'failed'] };
let episodes = [];
let selectedId = new URL(location.href).searchParams.get('episode');
let automation=null, csrfToken='', pollTimer=null, inFlight=null, selectedSignature='', currentEpisode=null, listSignature='';
let sources=[], discovery=null, groupFilter='primary', sourceFilter='all', filtersInitialized=false, filterSignature='', sourceSignature='';
const readingStates=new Map(), submitting=new Set(), requestErrors=new Map();
const groupLabels={all:'全部',primary:'英文重点',chinese:'中文',additional:'英文补充'};
const sourceBoundary='订阅更新会发现新节目。读取节目列表不代表已有全文或摘要；取得文字稿后才能整理中文要点。';

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function array(value) { return Array.isArray(value) ? value : []; }
function hasTranscript(ep) { return ['full','sample','partial'].includes(ep.coverage?.scope) && array(ep.segments).some(s=>s&&typeof s.text==='string'&&s.text.trim()); }
function thirdParty(ep) { return ep.source_kind === 'third_party_transcript'; }
function summaryReady(ep) { return hasTranscript(ep)&&ep.status==='summarized'&&Boolean(ep.summary?.overview||array(ep.summary?.points).length); }
function jobFor(ep) { return array(automation?.jobs).find(j=>j.article_id===ep.id); }
function jobState(ep) { return submitting.has(ep.id)?'submitting':jobFor(ep)?.state||(ep.status==='summarizing'?'running':ep.status==='failed'?'failed':''); }
function isReviewed(ep) { return summaryReady(ep) && ep.quality_review?.status === 'assistant_reviewed'; }
function beijingDate(value) { const parsed = value && new Date(value);return parsed && !Number.isNaN(parsed.getTime()) ? parsed.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '时间待确认'; }
function deferred(job) { return job?.state === 'queued' && (job.deferred_reason === 'off_peak_only' || automation?.off_peak_only === true && automation?.execution_allowed === false); }
function deferredNote(job) { return `等待谷时处理，预计最早 ${beijingDate(job?.not_before || automation?.next_run_at)}（北京时间）。可以先阅读文字稿，后台恢复后依次处理。`; }
function acquisitionInfo(ep) {
  if(hasTranscript(ep))return null;
  const acquisition=ep.transcript_acquisition,waiting=automation?.off_peak_only===true&&automation?.execution_allowed===false;
  if(!acquisition||typeof acquisition!=='object')return null;
  const retry=acquisition.next_retry_at?`最早 ${beijingDate(acquisition.next_retry_at)}（北京时间）再次尝试；这不是摘要完成时间。`:'等待后续检查，暂时没有确定的重试时间。';
  const pending=waiting?`高峰暂不取稿。${automation.next_run_at?`最早 ${beijingDate(automation.next_run_at)}（北京时间）恢复检查；这不是摘要完成时间。`:''}`:'等待下一轮官方文字稿或字幕检查。';
  return ({
    pending:{label:waiting?'等待谷时取稿':'待取官方文字稿',kind:'pending',note:`${pending}取得正文后才会安排中文要点和段落证据，仍需复核。`},
    fetching:{label:'正在读取文字稿',kind:'pending',note:'正在读取官方文字稿或字幕。取得正文后才会安排中文要点与段落证据；尚未开始内容分析。'},
    unavailable:{label:'文字稿暂不可读',kind:'pending',note:`本次未能取得可读的官方文字稿或字幕。${retry}`},
    unsupported:{label:'待接入转写',kind:'pending',note:'这集暂无可自动读取的官方文字稿，整集音频转写尚未启用，当前不会自动生成摘要。可打开原节目收听。'},
    archive:{label:'历史节目 · 未自动处理',kind:'',note:'这集属于历史节目。当前只自动处理支持来源的最新一集，历史内容不自动回填。可打开原节目收听。'},
    ready:{label:'文字稿待就绪',kind:'pending',note:'来源已返回文字稿状态，当前还没有可读取的正文。请稍后刷新查看，尚不能生成内容摘要。'}
  })[acquisition.state]||null;
}
function status(ep) { if(!hasTranscript(ep)){const acquisition=acquisitionInfo(ep);return acquisition?[acquisition.label,acquisition.kind]:states[ep.status==='audio_ready'?'audio_ready':ep.status==='failed'?'failed':'discovered'];}return isReviewed(ep) ? [thirdParty(ep)?'摘要已对照转写复核':'文字已复核', 'done'] : !summaryReady(ep)&&(deferred(jobFor(ep))?['等待谷时','pending']:({queued:['摘要已排队','pending'],running:['正在生成摘要','pending'],failed:['摘要失败 · 可重试','failed'],completed:['摘要已完成','done']})[jobState(ep)]) || states[ep.status] || ['状态待确认', 'pending']; }
function schedulePoll() { clearTimeout(pollTimer);if(session.authenticated())pollTimer=setTimeout(load,array(automation?.jobs).some(j=>j.state==='running'||j.state==='queued'&&!deferred(j))||episodes.some(ep=>!hasTranscript(ep)&&ep.transcript_acquisition?.state==='fetching')?5000:30000); }
function updateSchedule() {
  const node=document.querySelector('#podcast-schedule'),boundary=document.querySelector('.podcast-source-boundary');
  if(automation?.transcript_policy==='official_latest_only'){
    const names={dwarkesh:'Dwarkesh',lex:'Lex Fridman',diary:'The Diary Of A CEO'},supported=array(automation.automatic_transcript_sources).map(id=>names[id]).filter(Boolean).join('、')||'已接入官方文字稿的来源';
    const minutes=Number.isFinite(automation.off_peak_minutes)?automation.off_peak_minutes:60,limit=Number.isFinite(automation.podcast_daily_limit)?automation.podcast_daily_limit:3;
    const pause=automation.enabled===false?'自动处理未开启。':automation.provider_blocked?'自动分析暂停中。':automation.worker_alive===false?'后台暂未运行。':'';
    const policy=`自动取稿仅覆盖 ${supported}：谷时每 ${minutes} 分钟检查，每个来源只处理最新 1 集，每天最多新增 ${limit} 集；历史节目不自动回填。取得正文后，由 DeepSeek 生成中文要点与段落证据，仍需复核。其他来源暂未接入自动取稿，整集音频转写尚未启用。`;
    if(node)node.textContent=`${pause}自动取稿：${supported}。谷时每 ${minutes} 分钟检查，每源最新 1 集，每天最多新增 ${limit} 集；其他来源及历史节目暂不自动取稿。${automation.execution_allowed===false?' 高峰先排队，具体取稿状态见各集。':''}`;
    if(boundary)boundary.textContent=`${sourceBoundary}${pause}${policy}`;
    return;
  }
  if(boundary)boundary.textContent=sourceBoundary;
  if(node)node.textContent=automation?.off_peak_only ? `阅读任务仅在谷时执行；谷时每 ${Number.isFinite(automation.off_peak_minutes)?automation.off_peak_minutes:60} 分钟检查，高峰先排队。${automation.execution_allowed===false?` 最早 ${beijingDate(automation.next_run_at)}（北京时间）恢复。`:''}` : '摘要按后台队列安排；音频仅在点击后加载。';
}
function updateGeneration() {
  const ep=currentEpisode;if(!ep)return;
  const badge=reader.querySelector('.reader-meta .status'),[label,kind]=status(ep);if(badge){badge.textContent=label;badge.className=`status ${kind}`;}
  const acquisition=acquisitionInfo(ep),coverageState=reader.querySelector('.transcript-acquisition-status'),acquisitionNote=reader.querySelector('.transcript-acquisition-note');
  if(acquisition){if(coverageState)coverageState.textContent=acquisition.label;if(acquisitionNote)acquisitionNote.textContent=acquisition.note;}
  const box=reader.querySelector('#podcast-generation');if(!box)return;
  const state=jobState(ep),job=jobFor(ep),waiting=deferred(job),busy=['submitting','queued','running'].includes(state),button=box.querySelector('button'),note=box.querySelector('.podcast-job-state'),error=box.querySelector('.error-box');
  note.textContent=waiting?deferredNote(job):({submitting:'正在提交请求…',queued:'摘要已排队，可继续阅读文字稿。',running:'正在整理文字稿与出处，完成后自动更新。',completed:'摘要已完成，正在等待最新结果。',failed:'摘要未能完成，可重试。'})[state]||`只总结当前取得的文字稿，不会自动下载音频或开始转写。${automation?.off_peak_only?'高峰提交后等待谷时执行。':''}`;
  button.textContent=waiting?'等待谷时':({submitting:'正在提交…',queued:'已排队',running:'正在生成…'})[state]||(state==='failed'||requestErrors.has(ep.id)?'重新安排摘要':'安排摘要');button.disabled=busy||(!session.isKol&&!csrfToken);button.hidden=state==='completed';
  error.textContent=requestErrors.get(ep.id)||(state==='failed'?jobFor(ep)?.error||ep.error||'':'');error.hidden=!error.textContent;
}
async function generateSummary(id) {
  const generation=session.revision(),ep=episodes.find(e=>e.id===id);
  if(!session.valid(generation)||!ep||!hasTranscript(ep)||summaryReady(ep)||['submitting','queued','running','completed'].includes(jobState(ep)))return;
  submitting.add(id);requestErrors.delete(id);updateGeneration();
  try {const response=await session.request(session.api('summary'),{method:'POST',headers:session.summaryHeaders(csrfToken),body:JSON.stringify({id})});const result=await response.json();if(!session.valid(generation))return;if(!response.ok)throw new Error(session.errorMessage(result,response.status));if(result.job?.article_id===id)automation={...automation,jobs:[...array(automation?.jobs).filter(j=>j.article_id!==id),result.job]};if(inFlight)await inFlight;await load();}
  catch(error){if(session.valid(generation)&&!error.authExpired)requestErrors.set(id,error.message||'请求失败，请稍后重试。');}
  finally{submitting.delete(id);if(session.valid(generation)){updateGeneration();schedulePoll();}}
}
function collectionStatus(items) {
  return `${items.length} 集节目；已生成初稿 ${items.filter(summaryReady).length} 集，摘要已复核 ${items.filter(isReviewed).length} 集`;
}
function mediaTime(absoluteTime, offset = 0) {
  if (!Number.isFinite(absoluteTime) || absoluteTime < 0) return 0;
  const origin = Number.isFinite(offset) && offset >= 0 ? offset : 0;
  return Math.max(0, absoluteTime - origin);
}
function summaryPlaceholder(ep) {
  const acquisition=acquisitionInfo(ep);if(acquisition)return acquisition.note;
  if (ep.status === 'summarizing') return '正在逐块处理文字稿，中文摘要尚未完成。你可以先阅读下方原文，稍后刷新查看进度。';
  if (ep.status === 'transcribed') return '文字稿已准备好，中文摘要尚未生成。你可以先阅读下方原文。';
  if (ep.status === 'audio_ready') return '音频样本已准备好，尚未取得文字稿。此页面不会自动开始转写。';
  if (ep.status === 'failed') return '这集尚未完成摘要，具体原因见上方提示。';
  if (hasTranscript(ep)) return '已有文字稿，中文摘要尚未完成。你可以先阅读下方原文。';
  return '还未获取完整文字稿，暂不根据节目简介生成内容摘要。';
}
function time(value) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return null;
  const seconds = Math.floor(value);
  const h = Math.floor(seconds / 3600);
  return [h ? String(h).padStart(2, '0') : null, String(Math.floor(seconds / 60) % 60).padStart(2, '0'), String(seconds % 60).padStart(2, '0')].filter(x => x !== null).join(':');
}
function date(value) {
  if (!value) return '日期未提供';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? '日期未提供' : d.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' });
}
function safeURL(value, media = false) {
  if (typeof value !== 'string' || !value.trim()) return null;
  try {
    if (media && /^\/media\/[a-zA-Z0-9_./-]+$/.test(value) && !value.includes('..')) return new URL(value, location.origin).href;
    const parsed = new URL(value);
    return ['https:', 'http:'].includes(parsed.protocol) && !parsed.username && !parsed.password ? parsed.href : null;
  } catch { return null; }
}
function externalLink(url, label) {
  const clean = safeURL(url);
  if (!clean) return null;
  const a = el('a', 'source-link', label);
  a.href = clean; a.target = '_blank'; a.rel = 'noopener noreferrer';
  return a;
}
function sourceFor(ep) { return sources.find(source=>source.id===ep.source_id) || sources.find(source=>source.name===ep.show); }
function inGroup(item, group) {
  if(group==='all')return true;
  const source=sourceFor(item),language=item.language||source?.language||'',priority=item.source_priority||item.priority||source?.priority;
  if(group==='chinese')return language.startsWith('zh');
  return language==='en'&&(group==='primary'?priority==='primary':priority!=='primary');
}
function visibleEpisodes() { return episodes.filter(ep=>inGroup(ep,groupFilter)&&(sourceFilter==='all'||(ep.source_id||sourceFor(ep)?.id)===sourceFilter)); }
function count(value) { return Number.isFinite(value)&&value>=0?Math.floor(value):0; }
function renderFilters() {
  const container=document.querySelector('#podcast-filters'),groups=document.querySelector('#podcast-groups'),selectSource=document.querySelector('#podcast-source');
  container.hidden=false;
  const available=sources.filter(source=>inGroup(source,groupFilter));
  if(sourceFilter!=='all'&&!available.some(source=>source.id===sourceFilter))sourceFilter='all';
  const signature=JSON.stringify([sources.length>0,groupFilter,sourceFilter,available.map(source=>[source.id,source.name])]);
  if(signature===filterSignature)return;filterSignature=signature;groups.replaceChildren();
  for(const group of sources.length?Object.keys(groupLabels):['all']){
    const button=el('button','podcast-group',groupLabels[group]);button.type='button';button.dataset.group=group;button.setAttribute('aria-pressed',String(group===groupFilter));
    button.addEventListener('click',()=>{if(groupFilter===group)return;groupFilter=group;sourceFilter='all';selectedId=null;renderCollection();});groups.append(button);
  }
  selectSource.replaceChildren();const all=el('option','','全部节目来源');all.value='all';selectSource.append(all);
  for(const source of available){const option=el('option','',source.name||'未命名来源');option.value=source.id;selectSource.append(option);}
  selectSource.value=sourceFilter;selectSource.hidden=!sources.length;document.querySelector('.podcast-source-label').hidden=!sources.length;
}
function renderSources() {
  const panel=document.querySelector('#podcast-sources'),content=document.querySelector('#podcast-source-list');panel.hidden=!sources.length;
  const signature=JSON.stringify([sources,discovery]);if(signature===sourceSignature)return;sourceSignature=signature;content.replaceChildren();
  const primaryGroups=Number.isFinite(discovery?.primary_group_count)?count(discovery.primary_group_count):new Set(sources.filter(source=>source.priority==='primary').map(source=>source.group||source.id)).size;
  document.querySelector('#podcast-sources-heading').textContent=`订阅来源与更新 · ${primaryGroups} 个英文重点频道`;
  const labels={pending:['等待首次检查','pending'],ok:['节目列表已读取','done'],empty:['暂无节目','pending'],failed:['暂时无法读取','failed']};
  for(const source of sources){
    const row=el('section','podcast-source-card'),heading=el('div','podcast-source-heading');
    heading.append(el('h3','',source.name||'未命名来源'));const [label,kind]=labels[source.status]||labels.pending;heading.append(el('span',`status ${kind}`,label));row.append(heading);
    if(typeof source.focus==='string'&&source.focus)row.append(el('p','podcast-source-focus',source.focus));
    row.append(el('p','podcast-source-counts',`已收录 ${count(source.discovered_count)} 集 · 文字稿 ${count(source.transcript_count)} 集 · 摘要 ${count(source.summary_count)} 集`));
    row.append(el('p','podcast-source-check',source.last_checked_at?`最近检查：${beijingDate(source.last_checked_at)}（北京时间）`:'尚未检查，等待后台更新。'));
    if(source.latest_published_at)row.append(el('p','podcast-source-latest',`最新节目：${beijingDate(source.latest_published_at)}（北京时间）`));
    if(source.status==='failed')row.append(el('p','podcast-source-failure',source.last_success_at?`保留历史节目。上次读取成功：${beijingDate(source.last_success_at)}（北京时间）。`:'稍后将再次检查；已有阅读内容不受影响。'));
    const homepage=externalLink(source.homepage_url,'节目主页');if(homepage)row.append(homepage);content.append(row);
  }
}
function renderCollection() {
  renderFilters();renderSources();
  const items=visibleEpisodes();
  document.querySelector('#collection-status').textContent=`${groupLabels[groupFilter]} · ${collectionStatus(items)}`;
  const selected=items.find(ep=>ep.id===selectedId)||items.find(isReviewed)||items.find(summaryReady)||items[0];
  if(selected){if(selected.id!==selectedId||selectedSignature!==JSON.stringify(selected))select(selected,{preserve:selected.id===selectedId});else{currentEpisode=selected;updateGeneration();showList();}}
  else {
    const audio=reader.querySelector('audio');if(audio){audio.pause();audio.removeAttribute('src');audio.load();}
    selectedId=null;currentEpisode=null;selectedSignature='';showList();
    reader.replaceChildren(el('h2','','这个范围暂时没有节目'),el('p','empty',sources.length?'可以切换节目范围或来源；后台检查后会收录可获取的新节目。':'导入或订阅节目后刷新列表，即可开始阅读。'));
  }
}
function showList() {
  const items=visibleEpisodes();
  const signature=JSON.stringify([selectedId,groupFilter,sourceFilter,episodes.length,items.map(ep=>[ep.id,ep.title,ep.show,ep.published_at,status(ep)])]);if(signature===listSignature)return;listSignature=signature;
  const scroll=list.scrollTop;
  list.replaceChildren();
  document.querySelector('#episode-count').textContent = `${items.length} / ${episodes.length} 集`;
  for (const ep of items) {
    const button = el('button', 'episode');
    button.type = 'button'; button.dataset.episodeId=ep.id;button.setAttribute('aria-current', String(ep.id === selectedId));
    button.append(el('span', 'episode-show', ep.show || '未命名节目'), el('span', 'episode-title', ep.title || '未命名单集'));
    const meta = el('span', 'episode-meta');
    const [label, kind] = status(ep);
    meta.append(el('span', '', date(ep.published_at)), el('span', `status ${kind}`, ep.coverage?.scope === 'sample' ? `${label} / 节选` : ep.coverage?.scope === 'partial' ? `${label} / 部分转写` : label));
    button.append(meta);
    button.addEventListener('click', () => select(episodes.find(item=>item.id===ep.id)||ep));
    list.append(button);
  }
  if (!items.length) list.append(el('p', 'empty', '当前范围暂无节目，可以切换筛选或稍后刷新。'));
  list.scrollTop=scroll;
}
function section(title, meta) {
  const result = el('section', 'reader-section');
  const heading = el('div', 'section-heading');
  heading.append(el('h3', '', title));
  if (meta) heading.append(el('span', '', meta));
  result.append(heading);
  return result;
}
function select(ep,{preserve=false}={}) {
  if(currentEpisode){const old=readingStates.get(currentEpisode.id)||{};old.scroll=window.scrollY;old.details=[...reader.querySelectorAll('details')].map(n=>n.open);old.search=reader.querySelector('#segment-search')?.value||'';readingStates.set(currentEpisode.id,old);}
  const saved=readingStates.get(ep.id)||{};
  const previous = reader.querySelector('audio');
  if (previous) { previous.pause(); previous.removeAttribute('src'); previous.load(); }
  selectedId = ep.id;
  const selectedURL=new URL(location.href);selectedURL.searchParams.set('episode',ep.id);history.replaceState(null,'',selectedURL.pathname+selectedURL.search+selectedURL.hash);
  currentEpisode=ep;selectedSignature=JSON.stringify(ep);
  showList(); reader.replaceChildren();
  const heading = el('header', 'reader-heading');
  heading.append(el('p', 'reader-show', ep.show || '播客'), el('h2', '', ep.title || '未命名单集'));
  const metadata = el('div', 'reader-meta');
  metadata.append(el('span', '', date(ep.published_at)), el('span', '', ep.language === 'en' ? '英语原文' : ep.language?.startsWith('zh') ? '中文原文' : '语言待确认'));
  if (time(ep.duration_seconds)) metadata.append(el('span', '', `${thirdParty(ep)?'来源标注时长':'节目时长'} ${time(ep.duration_seconds)}`));
  const source = externalLink(ep.source_url, '打开原节目');
  if (source) metadata.append(source);
  const [stateText, stateClass] = status(ep);
  metadata.append(el('span', `status ${stateClass}`, stateText));
  if(thirdParty(ep))metadata.append(el('span','transcript-source-kind','第三方自动转写'));
  heading.append(metadata); reader.append(heading);

  const coverage = ep.coverage || {};
  const box = el('div', `coverage ${['sample','partial'].includes(coverage.scope) ? 'sample' : ''} ${ep.status === 'failed' ? 'failed' : ''}`);
  const start = time(coverage.start_seconds), end = time(coverage.end_seconds);
  const officialTranscript = typeof ep.source_kind === 'string' && ep.source_kind.startsWith('official_');
  const acquisition=acquisitionInfo(ep);
  let scopeText = thirdParty(ep) ? `第三方自动转写${coverage.scope==='partial'?' · 部分覆盖':''} · 未逐句音频核验` : coverage.scope === 'partial' ? '已取得部分文字稿' : coverage.scope === 'sample' ? '仅节选内容' : coverage.scope === 'full' ? officialTranscript ? '已取得官方文字稿' : '已取得文字稿' : '目前只有节目信息';
  if(acquisition)scopeText=acquisition.label;
  if (coverage.scope === 'sample' && start && end) scopeText += `：${start}–${end}`;
  box.append(el('strong', acquisition?'transcript-acquisition-status':'', scopeText));
  if(officialTranscript&&hasTranscript(ep)){const officialSource=externalLink(ep.transcript_source_url,'官方文字稿 / 字幕');if(officialSource){officialSource.classList.add('official-transcript-link');box.append(officialSource);}}
  if(thirdParty(ep)){
    box.append(el('p','transcript-boundary','非官方稿，摘要只覆盖已取得的文字；重要信息请回听原节目。'));
    const provenance=ep.transcript_provenance||{};
    const transcriptSource=externalLink(ep.transcript_source_url||provenance.source_url,'查看第三方转写来源');if(transcriptSource){transcriptSource.classList.add('transcript-provenance-link');box.append(transcriptSource);}
    if(array(provenance.excluded_regions).length)box.append(el('p','transcript-exclusions','已排除来源作者标为不可靠的片段；其余内容仍可能存在遗漏或重叠。'));
    const details=el('details','transcript-provenance-details');details.append(el('summary','','转写来源与覆盖说明'));
    if(typeof ep.duration_basis==='string'&&ep.duration_basis)details.append(el('p','transcript-duration-basis',`时长依据：${ep.duration_basis}`));
    if(typeof ep.published_at_note==='string'&&ep.published_at_note)details.append(el('p','transcript-publication-note',ep.published_at_note));
    if(coverage.note)details.append(el('p','',coverage.note));
    for(const warning of array(provenance.warnings).filter(value=>typeof value==='string').slice(0,8))details.append(el('p','transcript-warning',warning));
    box.append(details);
  }
  if (coverage.scope === 'full') box.append(el('p', '', '文字稿已获取；摘要处理进度见下方。文字稿不一定涵盖音频的每一秒。'));
  if (coverage.note && !thirdParty(ep)) box.append(el('p', '', coverage.note));
  if (Number.isFinite(coverage.chunks_total) && Number.isFinite(coverage.chunks_processed)) box.append(el('p', '', `摘要进度：已处理 ${coverage.chunks_processed} / ${coverage.chunks_total} 块${ep.status === 'summarizing' ? '，整集摘要尚未完成' : ''}`));
  if (Number.isFinite(coverage.segments_total) && coverage.segments_total > 0 && Number.isFinite(coverage.segments_processed) && (!Number.isFinite(coverage.chunks_total) || ep.status === 'summarized')) box.append(el('p', '', `摘要段落进度：${coverage.segments_processed} / ${coverage.segments_total} 段`));
  if (ep.status === 'failed') box.append(el('p', 'error-box', typeof ep.error === 'string' ? ep.error : '此集处理失败，请查看处理记录。'));
  reader.append(box);

  const mediaURL = session.isKol?session.mediaURL(ep):safeURL(ep.audio_url, true);
  const mediaRow = el('div', 'media-row');
  let audio = null;
  let pendingTime = null;
  let playbackRequested = false;
  const audioOffset = Number.isFinite(ep.audio_offset_seconds) && ep.audio_offset_seconds >= 0 ? ep.audio_offset_seconds : 0;
  const mediaNotice = el('p', '', mediaURL ? audioOffset > 0 ? `音频从原节目 ${time(audioOffset)} 开始。原文保留节目时间，播放器从样本开头计时。` : '点击后读取音频，可从原文时间点收听。' : '当前没有可用音频链接，可打开原节目收听。');
  function loadAudio(at, play = false) {
    if (!mediaURL || !session.authenticated()) return;
    pendingTime = mediaTime(at, audioOffset);
    playbackRequested = play;
    if (!audio) {
      audio = el('audio'); audio.controls = true; audio.preload = 'none';
      const container = el('div', 'audio-container'); container.append(audio); mediaRow.append(container);
      audio.addEventListener('loadedmetadata', () => {
        if(!audio.isConnected||!audio.hasAttribute('src')||!session.authenticated())return;
        if (pendingTime !== null) audio.currentTime = Number.isFinite(audio.duration) ? Math.min(pendingTime, audio.duration) : pendingTime;
        if (playbackRequested) audio.play().catch(() => { mediaNotice.textContent = '音频已就绪，点击播放器的播放按钮开始。'; });
      });
      audio.addEventListener('error', () => { if(!audio.isConnected||!audio.hasAttribute('src')||!session.authenticated())return;mediaNotice.className = 'audio-note'; mediaNotice.textContent = '音频暂时无法读取。可打开原节目收听，文字内容仍可阅读。';if(session.isKol){session.lock('音频暂不可用，正在重新核验会话…');void session.check();} });
      audio.src = mediaURL; audio.load();
      loadButton.classList.add('hidden');
    } else if (audio.readyState >= 1) {
      if (pendingTime !== null) audio.currentTime = Number.isFinite(audio.duration) ? Math.min(pendingTime, audio.duration) : pendingTime;
      if (play) audio.play().catch(() => { mediaNotice.textContent = '点击播放器的播放按钮开始。'; });
    }
  }
  const loadButton = el('button', 'primary-button', '加载音频'); loadButton.type = 'button';
  loadButton.addEventListener('click', () => loadAudio(null));
  if (mediaURL) mediaRow.append(loadButton);
  mediaRow.append(mediaNotice); reader.append(mediaRow);

  const segments = hasTranscript(ep)?array(ep.segments):[];
  const segmentMap = new Map(segments.map(s => [String(s.id), s]));
  function segmentNode(seg, evidence = false) {
    const node = el(evidence ? 'blockquote' : 'div', evidence ? 'evidence' : 'transcript-segment');
    const meta = el('div', 'segment-heading');
    const stamp = time(seg.start_seconds);
    if (stamp && mediaURL) {
      const seek = el('button', 'time-button', `▶ ${stamp}`); seek.type = 'button'; seek.setAttribute('aria-label', `从 ${stamp} 播放`);
      seek.addEventListener('click', () => loadAudio(seg.start_seconds, true)); meta.append(seek);
    } else meta.append(el('span', '', stamp || '未提供时间戳'));
    if (seg.speaker) meta.append(el('span', '', seg.speaker));
    node.append(meta, el('p', 'segment-text', seg.text || '此段没有正文。'));
    for(const note of (Array.isArray(seg.source_notes)?seg.source_notes:typeof seg.source_notes==='string'?[seg.source_notes]:[]).filter(value=>typeof value==='string'))node.append(el('p','review-meta',`来源说明：${note}`));
    return node;
  }
  const summarySection = section('中文摘要', '保留关键观点与原文出处');
  if (summaryReady(ep)) {
    const reviewed = isReviewed(ep);
    const reviewNote = el('div', `review-note ${reviewed ? 'reviewed' : ''}`);
    reviewNote.append(el('strong', '', reviewed ? thirdParty(ep)?'摘要已对照转写复核':'文字已复核' : '模型初稿 · 待复核'));
    reviewNote.append(el('p', '', reviewed ? `助手于 ${date(ep.quality_review.reviewed_at)} 对照文字稿修订；未逐秒核对音频，也不代表内容已独立证实。` : `以下是${ep.processing?.inference==='deepseek'?' DeepSeek ':ep.processing?.inference==='local_mlx'?'本地 MLX 模型':'模型'}自动生成的初稿，尚未对照文字稿复核，可能误读语义。请展开引用核对。`));
    summarySection.append(reviewNote);
    if (ep.summary?.overview) summarySection.append(el('p', 'overview', ep.summary.overview));
    const points = array(ep.summary?.points);
    for (const point of points) {
      const details = el('details', 'point');
      const cited = array(point.segment_ids).map(id => segmentMap.get(String(id))).filter(Boolean);
      const summary = el('summary', '', point.text || '未提供观点文字');
      summary.append(el('span', 'point-note', cited.length ? `查看 ${cited.length} 处原文` : '暂无可核对的原文引用'));
      details.append(summary);
      if (cited.length) cited.forEach(seg => details.append(segmentNode(seg, true)));
      else details.append(el('p', 'empty', '这条观点未关联可用原文，请结合原节目核对。'));
      summarySection.append(details);
    }
    if (!ep.summary?.overview && !points.length) summarySection.append(el('p', 'empty', '处理状态为已生成，但没有收到摘要正文。请查看处理记录。'));
    if (reviewed) {
      const audit = el('details', 'review-audit');
      audit.append(el('summary', '', '查看复核说明与原模型草稿'));
      const review = ep.quality_review;
      audit.append(el('p', 'review-meta', `${review.reviewer || '本轮助手复核'}；${date(review.reviewed_at)}`));
      if (review.note) audit.append(el('p', 'review-meta', review.note));
      if (array(review.issues).length) {
        audit.append(el('h4', '', '复核发现的问题'));
        const issues = el('ul', 'review-issues');
        review.issues.forEach(issue => issues.append(el('li', '', issue)));
        audit.append(issues);
      }
      audit.append(el('h4', '', '原模型草稿（未修订）'), el('p', 'draft-warning', '保留初始输出供对照，其中可能包含上述错误。阅读时请以展开框外的复核稿及原文为准。'));
      const draft = ep.model_draft_summary;
      if (draft && typeof draft === 'object') {
        if (draft.overview) audit.append(el('p', 'draft-overview', draft.overview));
        const draftPoints = el('ul', 'draft-points');
        array(draft.points).forEach(point => draftPoints.append(el('li', '', point.text || '未提供观点文字')));
        if (draftPoints.childElementCount) audit.append(draftPoints);
      } else audit.append(el('p', 'empty', '未提供原模型草稿。'));
      summarySection.append(audit);
    }
  } else {
    summarySection.append(el('p',`empty${acquisition?' transcript-acquisition-note':''}`,summaryPlaceholder(ep)));
    if(hasTranscript(ep)){const generation=el('div','podcast-generation');generation.id='podcast-generation';const state=el('p','podcast-job-state');state.setAttribute('role','status');const error=el('p','error-box');error.setAttribute('role','alert');const button=el('button','','安排摘要');button.id='generate-podcast-summary';button.type='button';button.addEventListener('click',()=>generateSummary(ep.id));generation.append(state,error,button);summarySection.append(generation);}
  }
  reader.append(summarySection);

  const transcript = section('原文阅读', segments.length ? `${segments.length} 段` : '等待文字稿');
  if (segments.length) {
    const label = el('label', 'search-label', '搜索原文或说话人'); label.htmlFor = 'segment-search';
    const search = el('input', 'segment-search'); search.id = 'segment-search'; search.type = 'search'; search.placeholder = '例如 Agent、business、Bret'; search.autocomplete = 'off';
    const count = el('p', 'search-count'); count.setAttribute('aria-live', 'polite');
    const results = el('div');
    const more = el('button', 'load-more', '继续阅读'); more.type = 'button';
    search.value=saved.search||'';let limit=saved.limit||30;
    function renderSegments() {
      const term = search.value.trim().toLocaleLowerCase();
      const matches = term ? segments.filter(s => `${s.text || ''} ${s.speaker || ''}`.toLocaleLowerCase().includes(term)) : segments;
      results.replaceChildren(); matches.slice(0, limit).forEach(s => results.append(segmentNode(s)));
      count.textContent = term ? `找到 ${matches.length} 段，显示 ${Math.min(limit, matches.length)} 段` : `显示 ${Math.min(limit, matches.length)} / ${matches.length} 段`;
      if (!matches.length) results.append(el('p', 'empty', '没有找到匹配内容，试试其他关键词。'));
      more.classList.toggle('hidden', limit >= matches.length);
    }
    search.addEventListener('input', () => { limit = 30;readingStates.set(ep.id,{...readingStates.get(ep.id),limit}); renderSegments(); });
    more.addEventListener('click', () => { limit += 50;readingStates.set(ep.id,{...readingStates.get(ep.id),limit}); renderSegments(); });
    transcript.append(label, search, count, results, more); renderSegments();
  } else transcript.append(el('p', 'empty', '暂无可阅读的文字稿。'));
  reader.append(transcript);
  const processing = ep.processing || {};
  const record = el('details', 'processing'); record.append(el('summary', '', '查看处理记录'));
  const dl = el('dl');
  const originLabels = { official_transcript: '官方文字稿', official_edited_transcript: '官方整理稿', third_party_transcript: '第三方自动转写（未逐句音频核验）', local_asr: '本地语音转写', local_asr_sample: '本地语音转写节选', rss: 'RSS 节目元信息' };
  const fields = [['文字来源', originLabels[ep.source_kind] || ep.source_kind || '未提供'], ['摘要模型', processing.summary_model || '尚未生成'], ['转写模型', processing.asr_model || '未使用 / 未提供'], ['处理用时', Number.isFinite(processing.elapsed_seconds) ? `${processing.elapsed_seconds.toFixed(1)} 秒` : '未提供'], ['API请求', Number.isFinite(processing.paid_api_calls) ? `${processing.paid_api_calls} 次` : '未提供']];
  fields.forEach(([key, value]) => dl.append(el('dt', '', key), el('dd', '', value))); record.append(dl); reader.append(record);
  reader.querySelectorAll('details').forEach((n,i)=>{n.open=Boolean(saved.details?.[i]);});updateGeneration();
  if(preserve)requestAnimationFrame(()=>{if(currentEpisode?.id===ep.id&&session.authenticated())window.scrollTo({top:saved.scroll||0,behavior:'instant'});});
}
async function fetchEpisodes() {
  const generation=session.revision();if(!session.valid(generation))return;
  refresh.disabled = true;
  try {
    const response = await session.request(session.api('podcasts'), { cache: 'no-store', headers: { Accept: 'application/json' } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if(!session.valid(generation))return;
    if (!Array.isArray(data.episodes)) throw new Error('接口缺少节目列表');
    episodes = data.episodes.filter(ep => ep && typeof ep === 'object' && typeof ep.id === 'string');
    sources=array(data.sources).filter(source=>source&&typeof source==='object'&&typeof source.id==='string');discovery=data.discovery||null;
    automation=data.automation||null;csrfToken=!session.isKol&&typeof data.csrf_token==='string'?data.csrf_token:'';
    updateSchedule();if(!selectedId)selectedId=new URL(location.href).searchParams.get('episode');
    if(!sources.length)groupFilter='all';
    if(!filtersInitialized){groupFilter=sources.length?'primary':'all';const linked=episodes.find(ep=>ep.id===selectedId);if(linked&&!inGroup(linked,groupFilter))groupFilter='all';filtersInitialized=true;}
    renderCollection();
  } catch (error) {
    if(!session.valid(generation)||error.authExpired)return;
    document.querySelector('#collection-status').textContent = '节目列表读取失败';
    if (!episodes.length) {
      list.replaceChildren(el('p', 'error-box', '暂时无法连接节目服务。'));
      reader.replaceChildren(el('h2', '', '节目暂时无法加载'), el('p', 'empty', `请稍后点击“刷新列表”重试。${error.message ? `（${error.message}）` : ''}`));
    }
  } finally { refresh.disabled = false; }
}
function load(){if(!session.authenticated())return Promise.resolve();if(inFlight)return inFlight;clearTimeout(pollTimer);const pending=fetchEpisodes().finally(()=>{if(inFlight===pending){inFlight=null;schedulePoll();}});inFlight=pending;return pending;}
refresh.addEventListener('click',()=>load());
document.querySelector('#podcast-source').addEventListener('change',event=>{sourceFilter=event.target.value;selectedId=null;renderCollection();});
session.on('lock',()=>{clearTimeout(pollTimer);inFlight=null;pollTimer=null;const audio=reader.querySelector('audio');if(audio){audio.pause();audio.removeAttribute('src');audio.load();}episodes=[];selectedId=null;currentEpisode=null;selectedSignature='';listSignature='';automation=null;csrfToken='';readingStates.clear();requestErrors.clear();submitting.clear();reader.replaceChildren();list.replaceChildren();document.querySelector('#episode-count').textContent='—';document.querySelector('#collection-status').textContent='请先解锁阅读';refresh.disabled=false;});
session.on('lock',()=>{document.querySelector('#podcast-schedule').textContent='';document.querySelector('.podcast-source-boundary').textContent=sourceBoundary;});
session.on('lock',()=>{sources=[];discovery=null;groupFilter='primary';sourceFilter='all';filtersInitialized=false;filterSignature='';sourceSignature='';document.querySelector('#podcast-filters').hidden=true;document.querySelector('#podcast-groups').replaceChildren();document.querySelector('#podcast-source').replaceChildren();const panel=document.querySelector('#podcast-sources');panel.hidden=true;panel.open=false;document.querySelector('#podcast-source-list').replaceChildren();document.querySelector('#podcast-sources-heading').textContent='订阅来源与更新';});
session.on('unlock',()=>load());session.init().then(ready=>{if(ready)load();});
