'use strict';

const list = document.querySelector('#episode-list');
const reader = document.querySelector('#reader');
const refresh = document.querySelector('#refresh');
const session = window.ReaderSession;
const states = { summarized: ['模型初稿 · 待复核', 'pending'], summarizing: ['正在生成摘要', 'pending'], transcribed: ['文字稿就绪 · 待摘要', 'pending'], audio_ready: ['音频就绪 · 待转写', 'pending'], discovered: ['仅节目信息', ''], failed: ['处理失败', 'failed'] };
let episodes = [];
let selectedId = null;
let automation=null, csrfToken='', pollTimer=null, inFlight=null, selectedSignature='', currentEpisode=null, listSignature='';
const readingStates=new Map(), submitting=new Set(), requestErrors=new Map();

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function array(value) { return Array.isArray(value) ? value : []; }
function hasTranscript(ep) { return ['full','sample'].includes(ep.coverage?.scope) && array(ep.segments).some(s=>s&&typeof s.text==='string'&&s.text.trim()); }
function summaryReady(ep) { return hasTranscript(ep)&&ep.status==='summarized'&&Boolean(ep.summary?.overview||array(ep.summary?.points).length); }
function jobFor(ep) { return array(automation?.jobs).find(j=>j.article_id===ep.id); }
function jobState(ep) { return submitting.has(ep.id)?'submitting':jobFor(ep)?.state||(ep.status==='summarizing'?'running':ep.status==='failed'?'failed':''); }
function isReviewed(ep) { return summaryReady(ep) && ep.quality_review?.status === 'assistant_reviewed'; }
function status(ep) { if(!hasTranscript(ep))return states[ep.status==='audio_ready'?'audio_ready':ep.status==='failed'?'failed':'discovered'];return isReviewed(ep) ? ['文字已复核', 'done'] : !summaryReady(ep)&&({queued:['摘要已排队','pending'],running:['正在生成摘要','pending'],failed:['摘要失败 · 可重试','failed'],completed:['摘要已完成','done']})[jobState(ep)] || states[ep.status] || ['状态待确认', 'pending']; }
function schedulePoll() { clearTimeout(pollTimer);if(session.authenticated())pollTimer=setTimeout(load,array(automation?.jobs).some(j=>['queued','running'].includes(j.state))?5000:30000); }
function updateGeneration() {
  const ep=currentEpisode,box=reader.querySelector('#podcast-generation');if(!ep||!box)return;
  const state=jobState(ep),busy=['submitting','queued','running'].includes(state),button=box.querySelector('button'),note=box.querySelector('.podcast-job-state'),error=box.querySelector('.error-box');
  note.textContent=({submitting:'正在提交请求…',queued:'摘要已排队，可继续阅读文字稿。',running:'正在整理文字稿与出处，完成后自动更新。',completed:'摘要已完成，正在等待最新结果。',failed:'摘要未能完成，可重试。'})[state]||'只总结当前取得的文字稿，不会自动下载音频或开始转写。';
  button.textContent=({submitting:'正在提交…',queued:'已排队',running:'正在生成…'})[state]||(state==='failed'||requestErrors.has(ep.id)?'重试生成摘要':'立即生成摘要');button.disabled=busy||(!session.isKol&&!csrfToken);button.hidden=state==='completed';
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
  return `${items.length} 集节目；已生成初稿 ${items.filter(summaryReady).length} 集，文字已复核 ${items.filter(isReviewed).length} 集`;
}
function mediaTime(absoluteTime, offset = 0) {
  if (!Number.isFinite(absoluteTime) || absoluteTime < 0) return 0;
  const origin = Number.isFinite(offset) && offset >= 0 ? offset : 0;
  return Math.max(0, absoluteTime - origin);
}
function summaryPlaceholder(ep) {
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
function showList() {
  const signature=JSON.stringify([selectedId,episodes.map(ep=>[ep.id,ep.title,ep.show,ep.published_at,status(ep)])]);if(signature===listSignature)return;listSignature=signature;
  const scroll=list.scrollTop;
  list.replaceChildren();
  document.querySelector('#episode-count').textContent = `${episodes.length} 集`;
  for (const ep of episodes) {
    const button = el('button', 'episode');
    button.type = 'button'; button.dataset.episodeId=ep.id;button.setAttribute('aria-current', String(ep.id === selectedId));
    button.append(el('span', 'episode-show', ep.show || '未命名节目'), el('span', 'episode-title', ep.title || '未命名单集'));
    const meta = el('span', 'episode-meta');
    const [label, kind] = status(ep);
    meta.append(el('span', '', date(ep.published_at)), el('span', `status ${kind}`, ep.coverage?.scope === 'sample' ? `${label} / 节选` : label));
    button.append(meta);
    button.addEventListener('click', () => select(episodes.find(item=>item.id===ep.id)||ep));
    list.append(button);
  }
  if (!episodes.length) list.append(el('p', 'empty', '还没有节目。导入样本后点击“刷新列表”。'));
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
  currentEpisode=ep;selectedSignature=JSON.stringify(ep);
  showList(); reader.replaceChildren();
  const heading = el('header', 'reader-heading');
  heading.append(el('p', 'reader-show', ep.show || '播客'), el('h2', '', ep.title || '未命名单集'));
  const metadata = el('div', 'reader-meta');
  metadata.append(el('span', '', date(ep.published_at)), el('span', '', ep.language === 'en' ? '英语原文' : ep.language?.startsWith('zh') ? '中文原文' : '语言待确认'));
  if (time(ep.duration_seconds)) metadata.append(el('span', '', `节目时长 ${time(ep.duration_seconds)}`));
  const source = externalLink(ep.source_url, '打开原节目');
  if (source) metadata.append(source);
  const [stateText, stateClass] = status(ep);
  metadata.append(el('span', `status ${stateClass}`, stateText));
  heading.append(metadata); reader.append(heading);

  const coverage = ep.coverage || {};
  const box = el('div', `coverage ${coverage.scope === 'sample' ? 'sample' : ''} ${ep.status === 'failed' ? 'failed' : ''}`);
  const start = time(coverage.start_seconds), end = time(coverage.end_seconds);
  const officialTranscript = typeof ep.source_kind === 'string' && ep.source_kind.startsWith('official_');
  let scopeText = coverage.scope === 'sample' ? '仅节选内容' : coverage.scope === 'full' ? officialTranscript ? '已取得官方文字稿' : '已取得文字稿' : '目前只有节目信息';
  if (coverage.scope === 'sample' && start && end) scopeText += `：${start}–${end}`;
  box.append(el('strong', '', scopeText));
  if (coverage.scope === 'full') box.append(el('p', '', '文字稿已获取；摘要处理进度见下方。文字稿不一定涵盖音频的每一秒。'));
  if (coverage.note) box.append(el('p', '', coverage.note));
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
    return node;
  }
  const summarySection = section('中文摘要', '保留关键观点与原文出处');
  if (summaryReady(ep)) {
    const reviewed = isReviewed(ep);
    const reviewNote = el('div', `review-note ${reviewed ? 'reviewed' : ''}`);
    reviewNote.append(el('strong', '', reviewed ? '文字已复核' : '模型初稿 · 待复核'));
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
    summarySection.append(el('p','empty',summaryPlaceholder(ep)));
    if(hasTranscript(ep)){const generation=el('div','podcast-generation');generation.id='podcast-generation';const state=el('p','podcast-job-state');state.setAttribute('role','status');const error=el('p','error-box');error.setAttribute('role','alert');const button=el('button','','立即生成摘要');button.id='generate-podcast-summary';button.type='button';button.addEventListener('click',()=>generateSummary(ep.id));generation.append(state,error,button);summarySection.append(generation);}
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
  const originLabels = { official_transcript: '官方文字稿', official_edited_transcript: '官方整理稿', local_asr: '本地语音转写', local_asr_sample: '本地语音转写节选', rss: 'RSS 节目元信息' };
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
    automation=data.automation||null;csrfToken=!session.isKol&&typeof data.csrf_token==='string'?data.csrf_token:'';
    document.querySelector('#collection-status').textContent = collectionStatus(episodes);
    const selected = episodes.find(ep => ep.id === selectedId) || episodes.find(isReviewed) || episodes.find(ep => ep.status === 'summarized') || episodes[0];
    if (selected) {if(selected.id!==selectedId||selectedSignature!==JSON.stringify(selected))select(selected,{preserve:selected.id===selectedId});else{currentEpisode=selected;updateGeneration();showList();}}
    else { showList(); reader.replaceChildren(el('p', 'empty', '尚无节目样本。导入后刷新列表，即可开始阅读。')); }
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
session.on('lock',()=>{clearTimeout(pollTimer);inFlight=null;pollTimer=null;const audio=reader.querySelector('audio');if(audio){audio.pause();audio.removeAttribute('src');audio.load();}episodes=[];selectedId=null;currentEpisode=null;selectedSignature='';listSignature='';automation=null;csrfToken='';readingStates.clear();requestErrors.clear();submitting.clear();reader.replaceChildren();list.replaceChildren();document.querySelector('#episode-count').textContent='—';document.querySelector('#collection-status').textContent='请先解锁阅读';refresh.disabled=false;});
session.on('unlock',()=>load());session.init().then(ready=>{if(ready)load();});
