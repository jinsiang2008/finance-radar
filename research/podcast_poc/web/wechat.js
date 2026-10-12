'use strict';

const reader = document.querySelector('#reader');
const session = window.ReaderSession;
const list = document.querySelector('#article-list');
const inbox = document.querySelector('#article-inbox');
const search = document.querySelector('#article-search');
const refresh = document.querySelector('#refresh');
const workspace = document.querySelector('.wx-workspace');
const wideScreen = window.matchMedia('(min-width:1200px)');
const mobileScreen = window.matchMedia('(max-width:700px)');
const initialParams = new URL(location.href).searchParams;
let articles = [], sources = [], selectedId = initialParams.get('article'), category = 'all', active = null;
let automation = null, csrfToken = '', pollTimer = null, loadInFlight = null;
let lastListSignature = '', lastSourceSignature = '', lastAutomationSignature = '';
const submitting = new Set(), requestErrors = new Map();
const initialView = initialParams.get('view');
const articleStates = new Map();
const array = value => Array.isArray(value) ? value : [];
function el(tag, className, text) { const n = document.createElement(tag); if (className) n.className = className; if (text !== undefined && text !== null) n.textContent = String(text); return n; }
function setText(node,text) { const value=String(text); if (node.textContent !== value) node.textContent=value; }
function button(text, className, action) { const b = el('button',className,text); b.type = 'button'; if (action) b.addEventListener('click',action); return b; }
function safeURL(value) { try { if (typeof value !== 'string' || !value.trim()) return null; const u = new URL(value); return ['http:','https:'].includes(u.protocol) && !u.username && !u.password ? u.href : null; } catch { return null; } }
function link(value,label,className='source-link') { const clean = safeURL(value); if (!clean) return null; const a = el('a',className,label); a.href = clean; a.target = '_blank'; a.rel = 'noopener noreferrer'; return a; }
function date(value,full=false) { if (!value) return '时间未提供'; const d = new Date(value); return Number.isNaN(d.getTime()) ? '时间未提供' : d.toLocaleString('zh-CN',full ? {year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'} : {year:'numeric',month:'2-digit',day:'2-digit'}); }
function categoryId(value) { const c = String(value || '').toLowerCase(); return ({'财经':'finance',finance:'finance',financial:'finance','投资':'finance',ai:'ai','人工智能':'ai','技术':'tech',technology:'tech',tech:'tech'})[c] || c; }
function categoryName(value) { return ({finance:'财经',ai:'AI',tech:'技术'})[categoryId(value)] || value || '未分类'; }
function hasBody(a) { return a.coverage?.scope === 'source_text' && a.source_kind === 'external_rss_body' && array(a.segments).some(s=>s && typeof s.text === 'string' && s.text.trim()); }
function reviewed(a) { return hasBody(a) && a.status === 'summarized' && a.quality_review?.status === 'assistant_reviewed'; }
function summaryReady(a) { return hasBody(a) && a.status === 'summarized' && Boolean(a.summary?.overview || array(a.summary?.points).length); }
function jobFor(a) { return array(automation?.jobs).find(job=>job?.article_id === a.id); }
function jobState(a) { return submitting.has(a.id) ? 'submitting' : jobFor(a)?.state || (a.status === 'summarizing' ? 'running' : a.status === 'failed' && hasBody(a) ? 'failed' : ''); }
function modelLabel() { return automation?.provider === 'local_mlx' ? '本地免费模型' : automation?.provider === 'deepseek' ? 'DeepSeek' : '摘要服务'; }
function recordedModelLabel(a) {
  const inference = a.processing?.inference, model = a.processing?.summary_model;
  if (inference === 'deepseek') return 'DeepSeek';
  if (['local_mlx','mlx'].includes(inference) || (!inference && typeof model === 'string' && model.startsWith('mlx-community/'))) return '本地 MLX 模型';
  return '模型';
}
function status(a) {
  if (!hasBody(a)) return a.status === 'failed' ? ['正文未取得','failed'] : ['仅简介','pending'];
  if (reviewed(a)) return ['文字已复核','done'];
  if (!summaryReady(a)) {
    const current = ({submitting:['正在提交','pending'],queued:['摘要已排队','pending'],running:['摘要处理中','pending'],completed:['摘要已完成','done'],failed:['摘要生成失败','failed']})[jobState(a)];
    if (current) return current;
  }
  return ({summarized:['模型初稿 · 待复核','pending'],summarizing:['摘要处理中','pending'],body_ready:['可读正文',''],failed:['摘要未完成','failed']})[a.status] || ['状态待确认','pending'];
}
function sourceFor(a) { return sources.find(s => s.id === a.source_id) || sources.find(s => s.feed_url && s.feed_url === a.feed_url) || sources.find(s => s.name === a.account); }
function announce(text) { setText(document.querySelector('#reading-announcement'),text); }
function stateFor(a) {
  const params=new URL(location.href).searchParams,view=params.get('view');
  if (!articleStates.has(a.id)) articleStates.set(a.id,{view:a.id === params.get('article') && ['summary','original','compare'].includes(view) ? view : 'summary',openPoints:new Set(),highlightId:null,returnPoint:null,returnView:'summary',viewScroll:{},paneScroll:{summary:0,original:0}});
  return articleStates.get(a.id);
}
function syncURL() {
  const url = new URL(location.href);
  if (selectedId && active) { url.searchParams.set('article',selectedId); url.searchParams.set('view',active.state.view); }
  else { url.searchParams.delete('article'); url.searchParams.delete('view'); }
  history.replaceState(null,'',url.pathname + url.search + url.hash);
}
function matchingArticles() {
  const q = search.value.trim().toLocaleLowerCase();
  return articles.filter(a => (category === 'all' || categoryId(a.category) === category) && (!q || `${a.title || ''} ${a.account || ''} ${a.source_excerpt || ''} ${array(a.segments).map(s => s?.text || '').join(' ')}`.toLocaleLowerCase().includes(q)));
}
function showList() {
  const items = matchingArticles();
  const signature = JSON.stringify([selectedId,category,search.value,articles.length,items.map(a=>[a.id,a.title,a.account,a.published_at,a.category,status(a)])]);
  if (signature === lastListSignature) return items;
  lastListSignature = signature;
  const oldScroll = list.scrollTop, focusedArticle = list.contains(document.activeElement) ? document.activeElement.closest('[data-article-id]')?.dataset.articleId : null; list.replaceChildren();
  document.querySelector('#article-count').textContent = `${items.length} / ${articles.length} 篇`;
  document.querySelector('#clear-search').hidden = !search.value;
  for (const a of items) {
    const b = button('', 'episode',()=>select(articles.find(item=>item.id === a.id) || a,{focus:true,mobileOpen:true})); b.dataset.articleId = a.id; b.setAttribute('aria-current',String(a.id === selectedId));
    b.append(el('span','episode-show',a.account || '未命名公众号'),el('span','episode-title',a.title || '未命名文章'));
    const meta = el('span','episode-meta'), [label,kind] = status(a);
    meta.append(el('span','',`${date(a.published_at)} / ${categoryName(a.category)}`),el('span',`status ${kind}`,label)); b.append(meta); list.append(b);
  }
  if (!items.length) list.append(el('p','empty',articles.length ? '没有匹配的文章。换个关键词，或切换到“全部”。' : '暂时没有文章，请查看订阅来源。'));
  document.querySelector('#show-reader').hidden = !active;
  list.scrollTop = oldScroll;
  if (focusedArticle) [...list.children].find(node=>node.dataset.articleId === focusedArticle)?.focus({preventScroll:true});
  return items;
}
function showEmpty(title,detail) {
  selectedId = null; active = null; workspace.classList.remove('is-comparing');
  reader.className = 'reader wx-reader'; reader.replaceChildren();
  const box = el('div','reading-empty'); box.append(el('h2','',title),el('p','reader-empty-note',detail)); reader.append(box);
  if (mobileScreen.matches) document.body.dataset.mobilePane = 'list';
  document.querySelector('#show-reader').hidden = true;
}
function applyFilters() {
  const items = showList();
  if (!items.some(a => a.id === selectedId)) {
    if (items.length) select(items[0]); else { showEmpty('没有匹配的文章','调整分类或关键词，原有阅读记录会保留。'); syncURL(); }
  }
}
function showSources() {
  const box = document.querySelector('#sources'); box.replaceChildren();
  const bad = sources.filter(s => ['failed','error','unavailable','blocked'].includes(s.status));
  document.querySelector('#source-count').textContent = `${sources.length} 个${bad.length ? `，${bad.length} 个异常` : ''}`;
  const labels = {ok:'读取正常',ready:'读取正常',available:'读取正常',success:'读取正常',empty:'暂未发现文章',partial:'部分文章仅有简介',stale:'最近更新较久',failed:'读取失败',error:'读取失败',unavailable:'暂不可用',blocked:'暂不可用',pending:'等待检查',not_checked:'尚未检查'};
  for (const s of sources) {
    const row = el('div','source-row'); row.append(el('p','source-name',`${s.name || '未命名来源'} / ${categoryName(s.category)}`),el('span',`status ${bad.includes(s) ? 'failed' : ''}`,labels[s.status] || '状态待确认'),el('p','',`来源商：${s.provider || '未提供'}`),el('p','',`最近检查：${date(s.last_checked_at,true)}`),el('p','',`最新发布时间：${date(s.latest_published_at,true)}`));
    if (Number.isFinite(s.article_count)) row.append(el('p','',`${s.article_count} 篇已收录文章`));
    const feed = link(s.feed_url,'来源 RSS'); if (feed) row.append(feed);
    if (typeof s.error === 'string' && s.error) row.append(el('p','source-error',s.error)); box.append(row);
  }
  if (!sources.length) box.append(el('p','empty','没有来源配置。'));
}
function beijingDate(value) {
  if (!value) return '尚未记录';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? '时间未提供' : d.toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
}
function intervalLabel(value) { return Number.isFinite(value) && value > 0 ? value % 60 === 0 ? `${value / 60} 小时` : `${value} 分钟` : '频率未提供'; }
function showAutomation() {
  const signature = JSON.stringify(automation);
  if (signature === lastAutomationSignature) return;
  lastAutomationSignature = signature;
  const box = document.querySelector('#automation-info'); box.replaceChildren();
  const label = document.querySelector('#automation-status');
  if (!automation) { label.textContent='状态未提供'; box.append(el('p','','后台尚未提供自动检查状态。')); return; }
  const pausedReason = typeof automation.automatic_paused_reason === 'string' ? automation.automatic_paused_reason.trim() : '';
  const paused = Boolean(automation.provider_blocked || pausedReason);
  label.textContent = paused ? '自动处理已暂停' : automation.enabled === true ? automation.worker_alive === true ? '已开启' : '已开启 · 后台未运行' : '未开启';
  box.append(el('p','automation-policy',`配置频率（北京时间）：空闲每 ${intervalLabel(automation.off_peak_minutes)}，高峰每 ${intervalLabel(automation.peak_minutes)} 检查一次。`));
  const period = automation.period === 'peak' ? '高峰' : automation.period === 'off_peak' ? '空闲' : '时段待确认';
  box.append(el('p','',`当前：${period}。分时检查采用 DeepSeek 峰谷规则。`),el('p','automation-provider',`实际摘要服务：${modelLabel()}${typeof automation.model === 'string' && automation.model ? `（${automation.model}）` : ''}。`));
  box.append(el('p','',paused ? '已暂停安排新的自动检查与摘要任务。已有内容仍可阅读。' : automation.enabled === true ? '发现新文章后，由后台安排检查与摘要任务。' : '定时检查未开启；有正文的文章可以手动生成摘要。'));
  if (paused) box.append(el('p','automation-warning automation-paused-reason',pausedReason || '摘要服务暂不可用，自动任务等待服务恢复。'));
  box.append(el('p','automation-worker',`后台进程：${automation.worker_alive === true ? '运行中' : automation.worker_alive === false ? '未运行' : '状态未提供'}`));
  box.append(el('p','',`最近检查：${beijingDate(automation.last_check_at)}`),el('p','',`最近成功：${beijingDate(automation.last_success_at)}`),el('p','automation-next-check',`下次检查：${paused ? '暂停中，当前不安排自动检查' : automation.enabled === true ? beijingDate(automation.next_check_at) : '未安排'}`));
  if (automation.calendar_status === 'unknown_year') box.append(el('p','automation-warning','节假日表待更新；工作日白天暂按高峰频率检查。'));
  if (automation.credentials_status === 'authentication_failed' && !paused) box.append(el('p','automation-warning',session.isKol?'DeepSeek 服务验证失败，自动处理尚未开启；请稍后重试。':'DeepSeek 密钥验证失败，定时处理尚未开启；请先在本机终端更新密钥。'));
  if (automation.worker_alive !== true) box.append(el('p','automation-warning','后台当前未运行，已排队任务需等待服务恢复。'));
}
function schedulePoll() {
  clearTimeout(pollTimer);
  if (!session.authenticated()) return;
  const busy = array(automation?.jobs).some(job=>['queued','running'].includes(job?.state));
  pollTimer = setTimeout(()=>load({background:true}),busy ? 5000 : 30000);
}
function updateJobUI() {
  if (!active) return;
  const a = active.article, badge = reader.querySelector('.reader-meta .status'), [label,kind] = status(a);
  if (badge) { setText(badge,label); badge.className=`status ${kind}`; }
  const controls = active.generation;
  if (!controls) return;
  const current = jobState(a), job = jobFor(a), busy = ['submitting','queued','running'].includes(current), complete = current === 'completed';
  const errors = requestErrors.get(a.id) || (current === 'failed' ? job?.error || a.error || '摘要任务未能完成，可以稍后重试。' : '');
  setText(controls.title,({submitting:'正在提交摘要请求',queued:'摘要已排队',running:'正在生成摘要',completed:'摘要已完成',failed:'摘要生成失败'})[current] || '这篇文章还没有摘要');
  setText(controls.note,({submitting:'正在向后台提交，请稍候。',queued:'后台会依次处理。你可以继续阅读正文，完成后此处自动更新。',running:'正在整理正文与出处，完成后此处自动更新。',completed:'后台已完成任务，正在等待最新摘要内容。',failed:'已获取的正文仍可阅读；重试只会提交这篇文章。'})[current] || '可以按需生成要点与出处，也可以直接阅读已获取正文。');
  setText(controls.error,typeof errors === 'string' ? errors : '摘要请求失败，请稍后重试。'); controls.error.hidden = !errors;
  controls.generate.hidden = complete;
  controls.generate.disabled = busy || (!session.isKol && !csrfToken);
  setText(controls.generate,({submitting:'正在提交…',queued:'已排队',running:'正在生成…'})[current] || (current === 'failed' || errors ? '重试生成摘要' : '立即生成摘要'));
  setText(controls.provider,`${modelLabel()}${automation?.model ? ` · ${automation.model}` : ''}${!session.isKol && !csrfToken && !busy ? ' · 服务暂不可提交，请刷新列表后重试' : ''}`);
  controls.root.dataset.state = current || 'ready'; controls.root.setAttribute('aria-busy',String(busy));
}
async function generateSummary(articleId) {
  const generation=session.revision();
  if (!session.valid(generation)) return;
  const a = articles.find(item=>item.id === articleId);
  if (!a || !hasBody(a) || summaryReady(a) || ['submitting','queued','running','completed'].includes(jobState(a))) return;
  if (!session.isKol && !csrfToken) { requestErrors.set(articleId,'缺少本机请求凭据，请刷新列表后重试。'); updateJobUI(); return; }
  submitting.add(articleId); requestErrors.delete(articleId); updateJobUI(); showList();
  const controller = new AbortController(), timeout = setTimeout(()=>controller.abort(),20000);
  try {
    const response = await session.request(session.api('summary'),{method:'POST',headers:{...session.summaryHeaders(csrfToken),Accept:'application/json'},body:JSON.stringify({id:articleId}),signal:controller.signal});
    let result; try { result = await response.json(); } catch { throw new Error(`服务未返回可读结果（HTTP ${response.status}）`); }
    if (!session.valid(generation)) return;
    if (!response.ok) throw new Error(session.errorMessage(result,response.status));
    if (result?.job?.article_id === articleId && ['queued','running','completed','failed'].includes(result.job.state)) {
      automation = {...automation,jobs:[...array(automation?.jobs).filter(job=>job?.article_id !== articleId),result.job]};
    }
    announce('摘要请求已提交，正在读取处理状态。');
    if (loadInFlight) await loadInFlight;
    await load({background:true});
  } catch (error) {
    if (!session.valid(generation) || error.authExpired) return;
    requestErrors.set(articleId,error.name === 'AbortError' ? '请求等待超时，提交结果尚不确定。请刷新列表核对队列后再重试。' : error.message || '无法连接摘要服务，请稍后重试。');
    announce('摘要请求未能确认，请查看错误说明。');
  } finally { clearTimeout(timeout); submitting.delete(articleId); if(session.valid(generation)){updateJobUI(); showList(); schedulePoll();} }
}
function readerVisible() { return !mobileScreen.matches || document.body.dataset.mobilePane !== 'list'; }
function savePosition() {
  if (!active || !readerVisible()) return;
  active.state.viewScroll[active.state.view] = window.scrollY;
  if (active.state.view === 'compare') {
    active.state.paneScroll.summary = active.summaryPane.scrollTop;
    active.state.paneScroll.original = active.originalPane.scrollTop;
  } else if (active.state.view === 'original') {
    const node = visibleNode(active.paragraphNodes);
    active.state.originalAnchor = node ? {id:node.dataset.segmentId,top:node.getBoundingClientRect().top} : null;
  }
}
function captureReaderState() {
  if (!active) return;
  savePosition();
  active.state.openDetails = new Set([...reader.querySelectorAll('details[open]')].map(node=>node.id || node.dataset.stateKey).filter(Boolean));
  active.state.focusId = reader.contains(document.activeElement) ? document.activeElement.id : null;
}
function scrollPaneTo(pane,node,offset=14) {
  pane.scrollTop += node.getBoundingClientRect().top - pane.getBoundingClientRect().top - offset;
}
function visibleNode(nodes) {
  const boundary = active.toolbar.getBoundingClientRect().bottom + 10;
  return [...nodes.values()].find(node=>{const rect=node.getBoundingClientRect();return rect.bottom > boundary && rect.top < boundary + 100;});
}
function restorePosition({fallback=0,compareAnchor=null}={}) {
  const current = active;
  requestAnimationFrame(()=>{
    if (active !== current || !current || !readerVisible()) return;
    if (current.state.view === 'compare') {
      current.summaryPane.scrollTop = current.state.paneScroll.summary;
      current.originalPane.scrollTop = current.state.paneScroll.original;
      if (compareAnchor) scrollPaneTo(compareAnchor.pane,compareAnchor.node);
      const toolbarTop = current.content.getBoundingClientRect().top + window.scrollY - current.toolbar.offsetHeight - parseFloat(getComputedStyle(current.toolbar).marginBottom);
      window.scrollTo({top:Math.max(0,toolbarTop - 12),behavior:'instant'});
    } else {
      window.scrollTo({top:current.state.viewScroll[current.state.view] ?? fallback,behavior:'instant'});
      if (current.state.view === 'original' && current.state.originalAnchor) {
        const anchor = current.paragraphNodes.get(current.state.originalAnchor.id);
        if (anchor) window.scrollBy({top:anchor.getBoundingClientRect().top-current.state.originalAnchor.top,behavior:'instant'});
      }
    }
  });
}
function setView(requested,{restoreScroll=true,writeURL=true}={}) {
  if (!active) return;
  const {state,article} = active;
  let next = ['summary','original','compare'].includes(requested) ? requested : 'summary';
  if (!hasBody(article)) next = 'original';
  if (next === 'compare' && (!wideScreen.matches || !summaryReady(article))) next = 'summary';
  let compareAnchor = null;
  if (restoreScroll) {
    savePosition();
    if (next === 'compare' && state.view !== 'compare') {
      const pane = state.view === 'original' ? active.originalPane : active.summaryPane;
      const preferred = state.view === 'summary' ? active.pointNodes.get(state.lastPoint) : null;
      const preferredRect = preferred?.getBoundingClientRect();
      const node = preferredRect && preferredRect.bottom > active.toolbar.getBoundingClientRect().bottom && preferredRect.top < window.innerHeight ? preferred : visibleNode(state.view === 'original' ? active.paragraphNodes : active.pointNodes);
      if (node) compareAnchor = {pane,node};
    }
  }
  state.view = next;
  reader.classList.remove('mode-summary','mode-original','mode-compare'); reader.classList.add(`mode-${next}`);
  workspace.classList.toggle('is-comparing',next === 'compare');
  active.summaryPane.hidden = next === 'original'; active.originalPane.hidden = next === 'summary';
  active.summaryPane.tabIndex = next === 'compare' ? 0 : -1; active.originalPane.tabIndex = next === 'compare' ? 0 : -1;
  active.modeButtons.forEach(b => b.setAttribute('aria-pressed',String(b.dataset.view === next)));
  active.returnButton.hidden = state.returnPoint === null;
  active.returnButton.textContent = state.returnPoint === null ? '回到刚才的要点' : `回到要点 ${state.returnPoint + 1}`;
  if (writeURL) syncURL();
  if (restoreScroll) restorePosition({fallback:Math.max(0,reader.offsetTop-18),compareAnchor});
}
function openList() {
  savePosition();
  if (active?.state.view === 'compare') setView('summary',{restoreScroll:false});
  document.body.dataset.mobilePane = 'list'; inbox.focus({preventScroll:true}); inbox.scrollIntoView({block:'start',behavior:'auto'});
}
function jumpToSource(segmentId,pointIndex) {
  if (!active) return;
  const target = active.paragraphNodes.get(segmentId);
  if (!target) { announce('这条引用对应的正文段落当前不可用。'); return; }
  savePosition();
  const state = active.state; state.lastPoint = pointIndex; state.returnPoint = pointIndex; state.returnView = state.view; state.highlightId = segmentId;
  active.paragraphNodes.forEach((node,id)=>node.classList.toggle('is-highlighted',id === segmentId));
  setView(state.view === 'compare' ? 'compare' : 'original',{restoreScroll:false});
  document.body.dataset.mobilePane = 'reader';
  requestAnimationFrame(()=>{ target.focus({preventScroll:true}); if (state.view === 'compare') scrollPaneTo(active.originalPane,target); else target.scrollIntoView({block:'start',behavior:'auto'}); });
  announce(`已定位到正文段落 ${segmentId}，可返回要点 ${pointIndex+1}。`);
}
function returnToPoint() {
  if (!active || active.state.returnPoint === null) return;
  savePosition();
  const index = active.state.returnPoint, node = active.pointNodes.get(index);
  if (!node) { active.state.returnPoint = null; active.returnButton.hidden = true; announce('原要点已变更，请重新选择。'); return; }
  const next = active.state.returnView === 'compare' && wideScreen.matches ? 'compare' : 'summary';
  active.state.returnPoint = null; setView(next,{restoreScroll:false});
  requestAnimationFrame(()=>{node.focus({preventScroll:true});if (active.state.view === 'compare') scrollPaneTo(active.summaryPane,node);else node.scrollIntoView({block:'start',behavior:'auto'});});
  announce(`已回到要点 ${index+1}。`);
}
function buildReview(a) {
  const audit = el('details','review-audit'); audit.id = 'review-details';
  audit.append(el('summary','',reviewed(a) ? '复核说明与原模型草稿' : '摘要状态说明'));
  if (!reviewed(a)) { audit.append(el('p','review-meta',`这份摘要由${recordedModelLabel(a)}自动生成，尚未对照来源文字复核。页面可核验出处，不代表文章主张已被证实。`)); return audit; }
  const review = a.quality_review;
  audit.append(el('p','review-meta',`复核日期：${date(review.reviewed_at,true)}。由助手对照 RSS 文字修订；不等于事实核验，也不属于无人值守的自动复核。`));
  if (review.note) audit.append(el('p','review-meta',review.note));
  if (array(review.issues).length) { audit.append(el('h4','','修订记录')); const ul = el('ul','review-issues'); array(review.issues).forEach(issue => ul.append(el('li','',typeof issue === 'string' ? issue : issue?.description || '未提供问题说明'))); audit.append(ul); }
  audit.append(el('h4','','原模型草稿（未经修订）'),el('p','draft-warning','以下保留初始输出，其中可能包含已修订的错误。'));
  const draft = a.model_draft_summary;
  if (draft && typeof draft === 'object') { if (draft.overview) audit.append(el('p','draft-overview',draft.overview)); const ul = el('ul','draft-points'); array(draft.points).forEach(p => ul.append(el('li','',p?.text || '未提供观点文字'))); if (ul.childElementCount) audit.append(ul); }
  else audit.append(el('p','review-meta','未保存原模型草稿。'));
  return audit;
}
function buildProcessing(a,segments) {
  const detail = el('details','processing'); detail.id = 'processing-details'; detail.append(el('summary','','来源与处理记录')); const dl = el('dl'), c = a.coverage || {}, source = sourceFor(a);
  const fields = [['来源商',source?.provider || a.provider || '未提供'],['正文范围',hasBody(a) ? '外部 RSS 已获取文字' : '仅简介 / 元信息'],['摘要模型',a.processing?.summary_model || '尚未生成'],['处理用时',Number.isFinite(a.processing?.elapsed_seconds) ? `${a.processing.elapsed_seconds.toFixed(1)} 秒` : '未提供'],['API请求',Number.isFinite(a.processing?.paid_api_calls) ? `${a.processing.paid_api_calls} 次` : '未提供']];
  if (hasBody(a)) fields.push(['正文处理',`${c.segments_processed || 0} / ${segments.length} 段`],['摘要分块',`${c.chunks_processed || 0} / ${c.chunks_total || '尚未开始'}`]);
  fields.forEach(([k,v])=>dl.append(el('dt','',k),el('dd','',v))); detail.append(dl);
  const feed = link(a.feed_url,'查看来源 RSS'); if (feed) detail.append(feed); return detail;
}
function select(a,{focus=false,mobileOpen=false,preserve=false}={}) {
  if (active && !preserve) captureReaderState();
  selectedId = a.id; const state = stateFor(a); reader.replaceChildren();
  const body = hasBody(a), segments = body ? array(a.segments).filter(s=>s && typeof s === 'object') : [];
  const segmentMap = new Map(segments.map(s=>[String(s.id),s])), indexes = new Map(segments.map((s,i)=>[String(s.id),i]));
  const heading = el('header','reader-heading'); const account = el('p','reader-show',a.account || '公众号'); account.append(el('span','reader-category',categoryName(a.category)));
  heading.append(account,el('h2','',a.title || '未命名文章'));
  const meta = el('div','reader-meta'), [label,kind] = status(a); meta.append(el('span','',`发布于 ${date(a.published_at,true)}`),el('span',`status ${kind}`,label));
  const original = link(a.source_url,'微信原文'); if (original) meta.append(original); else meta.append(el('span','','暂无可用微信原文链接')); heading.append(meta);
  if (body && a.status === 'summarized') heading.append(el('p',`review-caption ${reviewed(a) ? '' : 'pending'}`,reviewed(a) ? `助手于 ${date(a.quality_review.reviewed_at)} 对照 RSS 文字修订，非事实核验。` : `${recordedModelLabel(a)}初稿，未经对照复核；条件、数字和结论请核对出处。`));
  const scope = el('details','scope-note'); scope.id = 'scope-details'; scope.append(el('summary','',body ? '外部源文字 · 图片未解析 · 完整性未核实' : '仅有来源简介 · 未取得正文'));
  const scopeDetail = el('div','scope-detail'); scopeDetail.append(el('p','',body ? `当前取得 ${Number.isFinite(a.body_chars) ? `${a.body_chars.toLocaleString('zh-CN')} 字、` : ''}${segments.length} 段 RSS 文字；尚未逐段对照微信原页面。` : '简介不作为全文处理，即使来源提供了旧摘要，也不会当作本文的内容总结。'));
  scopeDetail.append(el('p','',Number.isFinite(a.image_count) && a.image_count > 0 ? `来源包含 ${a.image_count} 张图片，未识别其中的文字和图表。` : '只处理已获取文字，图片、图表与来源省略内容不在当前阅读范围。'));
  if(typeof a.source_body_note==='string'&&a.source_body_note){scopeDetail.append(el('p','source-body-note',a.source_body_note));if(a.fetched_at)scopeDetail.append(el('p','',`正文取得时间：${date(a.fetched_at,true)}`));if(a.source_checked_at)scopeDetail.append(el('p','',`本次来源检查：${date(a.source_checked_at,true)}`));}
  if (a.coverage?.note) scopeDetail.append(el('p','',a.coverage.note)); scope.append(scopeDetail); heading.append(scope); reader.append(heading);
  const toolbar = el('div','reading-modebar'); toolbar.append(button('文章列表','list-toggle',openList)); toolbar.firstChild.id = 'back-to-list';
  const modes = el('div','reading-modes'); modes.setAttribute('role','group'); modes.setAttribute('aria-label','阅读方式');
  const modeButtons = [];
  for (const [view,text] of [['summary','要点速读'],['original',body ? '阅读原文' : '来源简介'],['compare','对照阅读']]) {
    const b = button(text,'',()=>setView(view)); b.dataset.view = view; b.setAttribute('aria-pressed','false'); b.disabled = view === 'summary' && !body || view === 'compare' && !summaryReady(a); modes.append(b); modeButtons.push(b);
  }
  toolbar.append(modes); const returnButton = button('回到刚才的要点','return-to-point',returnToPoint); returnButton.id = 'return-to-point'; returnButton.hidden = true; toolbar.append(returnButton); reader.append(toolbar);
  const content = el('div','reader-content'); content.id = 'reader-content';
  const summaryPane = el('section','reading-pane summary-pane'), originalPane = el('section','reading-pane original-pane'); summaryPane.id = 'summary-pane'; originalPane.id = 'original-pane';
  summaryPane.setAttribute('aria-label','文章要点'); originalPane.setAttribute('aria-label',body ? 'RSS 已获取正文' : '来源简介');
  const pointNodes = new Map(), paragraphNodes = new Map();
  active = {article:a,signature:JSON.stringify(a),state,summaryPane,originalPane,modeButtons,returnButton,pointNodes,paragraphNodes,toolbar,content,generation:null};
  if (summaryReady(a)) {
    summaryPane.append(el('h3','','核心摘要')); if (a.summary.overview) summaryPane.append(el('p','summary-overview',a.summary.overview));
    const points = array(a.summary.points).filter(p=>p && typeof p === 'object'); if (points.length) summaryPane.append(el('h3','points-heading',`${points.length} 条要点，可查看出处与上下文`));
    points.forEach((p,i)=>{
      const point = el('section','insight'); point.tabIndex = -1; point.dataset.pointIndex = String(i); point.id = `insight-${i}`; pointNodes.set(i,point);
      const header = el('div','insight-header'); header.append(el('span','insight-number',String(i+1).padStart(2,'0')),el('h4','',p.text || '未提供观点文字')); point.append(header);
      const refs = [...new Set(array(p.segment_ids).filter(id=>typeof id === 'string' || typeof id === 'number').map(String))]; const valid = refs.filter(id=>segmentMap.has(id)), missing = refs.length-valid.length;
      const actions = el('div','insight-actions'), panel = el('div','evidence-panel'); panel.id = `evidence-${i}`; panel.hidden = !state.openPoints.has(i);
      const toggle = button(valid.length ? `查看 ${valid.length} 处出处` : '无可用出处','evidence-toggle',()=>{state.lastPoint=i;panel.hidden=!panel.hidden;toggle.setAttribute('aria-expanded',String(!panel.hidden));if (panel.hidden) state.openPoints.delete(i);else state.openPoints.add(i);});
      toggle.setAttribute('aria-expanded',String(!panel.hidden)); toggle.setAttribute('aria-controls',panel.id); toggle.disabled = !valid.length; actions.append(toggle);
      if (missing) actions.append(el('span','missing-evidence',`${missing} 条引用对应段落缺失`));
      if (!refs.length) actions.append(el('span','missing-evidence','此要点没有提供正文引用'));
      point.append(actions);
      valid.forEach(id=>{
        const seg = segmentMap.get(id), index = indexes.get(id), citation = el('div','citation'); citation.dataset.segmentId = id;
        const ch = el('div','citation-heading'); ch.append(el('span','',`出处 ${id}`),button('在原文中查看','jump-to-source',()=>jumpToSource(id,i))); citation.append(ch,el('blockquote','',seg.text || '此段没有正文。'));
        const context = el('details','citation-context'); context.dataset.stateKey=`context:${i}:${id}`; context.append(el('summary','','查看前后文'));
        for (const [offset,label] of [[-1,'上一段'],[1,'下一段']]) { const nearby = segments[index+offset]; if (nearby) { const cp=el('p','context-paragraph'); cp.append(el('span','',`${label} ${nearby.id || ''}`),document.createTextNode(String(nearby.text || ''))); context.append(cp); } }
        if (context.childElementCount===1) context.append(el('p','context-paragraph','当前已获取内容中没有相邻段落。'));
        citation.append(context); panel.append(citation);
      });
      point.append(panel); summaryPane.append(point);
    });
    summaryPane.append(buildReview(a));
  } else {
    const empty = el('div','reading-empty');
    if (body) {
      empty.id='summary-generation';
      const title=el('h3'), note=el('p','summary-job-note'), error=el('p','error-box'), provider=el('p','generation-provider');
      title.id='summary-job-status'; title.setAttribute('role','status'); error.id='summary-job-error'; error.setAttribute('role','alert'); error.hidden=true;
      const actions=el('div','generation-actions'), generate=button('立即生成摘要','generate-summary',()=>generateSummary(a.id)); generate.id='generate-summary';
      actions.append(generate,button('阅读正文','read-original-button',()=>setView('original'))); empty.append(title,note,error,actions,provider);
      active.generation={root:empty,title,note,error,generate,provider};
    } else {
      empty.append(el('h3','','尚未取得正文'),el('p','','目前只有来源简介，不能生成全文摘要。'));
      if (typeof a.error === 'string' && a.error) empty.append(el('p','error-box',a.error));
    }
    summaryPane.append(empty);
  }
  const originalHeading = el('div','pane-heading'); originalHeading.append(el('h3','',body ? 'RSS 已获取正文' : '来源简介'),el('span','',body ? `${segments.length} 段` : '非全文')); originalPane.append(originalHeading);
  originalPane.append(el('p','original-intro',body ? '外部源文字，未独立核实是否删节。段落标记用于回查摘要出处。' : '只有简介或元信息，尚未取得可供总结的正文。'));
  if (body) segments.forEach((s,i)=>{
    const p = el('section',`source-paragraph ${state.highlightId === String(s.id) ? 'is-highlighted' : ''}`); p.id = `source-paragraph-${i}`; p.dataset.segmentId = String(s.id); p.tabIndex = -1; p.append(el('span','paragraph-id',`段落 ${s.id || i+1}`),el('p','',s.text || ''));
    paragraphNodes.set(String(s.id),p); originalPane.append(p);
  }); else originalPane.append(el('p','source-excerpt',a.source_excerpt || '未提供简介，可打开微信原文阅读。'));
  content.append(summaryPane,originalPane); reader.append(content,buildProcessing(a,segments));
  reader.querySelectorAll('details').forEach(node=>{node.open=Boolean(state.openDetails?.has(node.id || node.dataset.stateKey));});
  if (state.returnPoint !== null && !pointNodes.has(state.returnPoint)) state.returnPoint = null;
  setView(state.view,{restoreScroll:false}); updateJobUI(); showList();
  if (mobileOpen) document.body.dataset.mobilePane = 'reader';
  if (focus) reader.focus({preventScroll:true});
  else if (preserve && state.focusId) document.getElementById(state.focusId)?.focus({preventScroll:true});
  if (focus || preserve || state.view === 'compare') restorePosition({fallback:focus ? Math.max(0,reader.offsetTop-18) : 0});
}
async function fetchSnapshot({background=false}={}) {
  const generation=session.revision();
  if(!session.valid(generation))return;
  if (!background) refresh.disabled = true;
  const controller = new AbortController(), timeout = setTimeout(()=>controller.abort(),15000);
  try {
    const response = await session.request(session.api('wechat'),{cache:'no-store',headers:{Accept:'application/json'},signal:controller.signal}); if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json(); if (!Array.isArray(data.articles) || !Array.isArray(data.sources)) throw new Error('接口缺少文章或来源列表');
    if(!session.valid(generation))return;
    articles = data.articles.filter(a=>a && typeof a==='object' && typeof a.id==='string').sort((a,b)=>(Date.parse(b.published_at)||0)-(Date.parse(a.published_at)||0)); sources = data.sources.filter(s=>s && typeof s==='object');
    automation = data.automation && typeof data.automation === 'object' ? data.automation : null;
    csrfToken = !session.isKol && typeof data.csrf_token === 'string' ? data.csrf_token : '';
    array(automation?.jobs).forEach(job=>{if (['queued','running','completed'].includes(job?.state)) requestErrors.delete(job.article_id);});
    setText(document.querySelector('#collection-status'),`${articles.length} 篇文章，${articles.filter(summaryReady).length} 篇有摘要`);
    const sourceSignature=JSON.stringify(sources); if (sourceSignature !== lastSourceSignature) { lastSourceSignature=sourceSignature; showSources(); }
    showAutomation(); const items = matchingArticles(), oldSelected = selectedId;
    const chosen = items.find(a=>a.id===selectedId) || items.find(summaryReady) || items.find(hasBody) || items[0];
    if (chosen) {
      if (!active || active.article.id !== chosen.id || active.signature !== JSON.stringify(chosen)) { captureReaderState(); select(chosen,{preserve:true}); }
      else { active.article=chosen; updateJobUI(); showList(); }
      if (oldSelected && oldSelected !== chosen.id) announce('原文章不在当前列表中，已展示另一篇可读文章。');
    }
    else { showList(); showEmpty('暂时没有可读文章','查看来源状态，或调整分类与搜索词。'); syncURL(); }
  } catch (error) {
    if(!session.valid(generation)||error.authExpired)return;
    setText(document.querySelector('#collection-status'),`列表读取失败${articles.length ? '，保留上次内容' : ''}`);
    if (!articles.length) { lastListSignature=''; document.querySelector('#source-count').textContent='暂时无法检查';list.replaceChildren(el('p','error-box','文章服务暂时不可用。'));showEmpty('文章暂时无法加载',`请稍后点击“刷新列表”重试。${error.message ? `（${error.name === 'AbortError' ? '请求超时' : error.message}）` : ''}`); }
    announce('列表刷新失败，已有阅读内容保持不变。');
  } finally { clearTimeout(timeout); if (!background) refresh.disabled=false; }
}
function load(options={}) {
  if(!session.authenticated())return Promise.resolve();
  if (loadInFlight) return loadInFlight;
  clearTimeout(pollTimer);
  const pending=fetchSnapshot(options).finally(()=>{if(loadInFlight===pending){loadInFlight=null;schedulePoll();}});loadInFlight=pending;
  return loadInFlight;
}
search.addEventListener('input',applyFilters);
document.querySelector('#clear-search').addEventListener('click',()=>{search.value='';applyFilters();search.focus();});
document.querySelectorAll('[data-category]').forEach(b=>b.addEventListener('click',()=>{category=b.dataset.category;document.querySelectorAll('[data-category]').forEach(n=>n.setAttribute('aria-pressed',String(n===b)));applyFilters();}));
document.querySelector('#show-reader').addEventListener('click',()=>{if (active) {document.body.dataset.mobilePane='reader';reader.focus({preventScroll:true});restorePosition({fallback:Math.max(0,reader.offsetTop-18)});}});
refresh.addEventListener('click',()=>load());
wideScreen.addEventListener('change',()=>{if (active?.state.view==='compare' && !wideScreen.matches) setView('summary');});
window.addEventListener('popstate',()=>{const p=new URL(location.href).searchParams;const a=articles.find(a=>a.id===p.get('article'));if(a){stateFor(a).view=p.get('view') || 'summary';select(a,{mobileOpen:true});}});
session.on('lock',()=>{clearTimeout(pollTimer);pollTimer=null;loadInFlight=null;articles=[];sources=[];automation=null;csrfToken='';selectedId=null;active=null;articleStates.clear();submitting.clear();requestErrors.clear();lastListSignature='';lastSourceSignature='';lastAutomationSignature='';reader.replaceChildren();list.replaceChildren();document.querySelector('#sources').replaceChildren();document.querySelector('#automation-info').replaceChildren();search.value='';document.querySelector('#collection-status').textContent='请先解锁阅读';document.querySelector('#article-count').textContent='—';document.querySelector('#source-count').textContent='—';document.querySelector('#automation-status').textContent='待解锁';document.querySelector('#reading-announcement').textContent='';workspace.classList.remove('is-comparing');refresh.disabled=false;});
session.on('unlock',()=>{selectedId=new URL(location.href).searchParams.get('article');load();});
session.init().then(ready=>{if(ready)load();});
