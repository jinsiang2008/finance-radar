/* Isolated browser contracts: all HTTP requests are fulfilled from fixtures. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const web = path.join(__dirname, 'web');
const transcript = [{id:'p0001',text:'仅供前端契约测试的正文。',start_seconds:0},{id:'p0002',text:'测试第二段。',start_seconds:5}];
const summary = {overview:'契约测试摘要，不是真实资讯。',points:[{text:'测试引用',segment_ids:['p0001']}]};
const article = {id:'wx-test',account:'测试来源',title:'私有文章测试标记',category:'AI',published_at:'2026-10-08T00:00:00Z',source_kind:'external_rss_body',status:'summarized',coverage:{scope:'source_text'},segments:transcript,summary,processing:{inference:'deepseek',summary_model:'deepseek-v4-flash'}};
article.source_body_available=false;article.source_body_note='本次订阅源未提供正文；显示上次保存的正文及对应摘要。';article.fetched_at='2026-10-03T00:00:00Z';article.source_checked_at='2026-10-08T00:00:00Z';
const episode = {id:'podcast-test',show:'测试节目',title:'私有播客测试标记',source_kind:'official_transcript',status:'transcribed',coverage:{scope:'full'},segments:transcript,processing:{}};
const meta = {...episode,id:'podcast-meta',title:'只有简介的节目',status:'discovered',coverage:{scope:'metadata_only'},summary};
const automation = {enabled:false,provider:'deepseek',model:'deepseek-v4-flash',worker_alive:true,period:'peak',peak_minutes:240,off_peak_minutes:60,calendar_status:'verified',jobs:[]};
(async()=>{
  const browser = await chromium.launch({headless:true,channel:process.env.BROWSER_CHANNEL || 'chrome',args:['--no-proxy-server']});
  const context=await browser.newContext({viewport:{width:1440,height:1000}});
  let authed=false, privateFailure=0, releaseHeld=null, holdNext=false, privateGets=0, posts=0;
  const errors=[], unexpected=[];
  context.on('page',page=>page.on('pageerror',e=>errors.push(e.message)));
  await context.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url()),pathname=url.pathname;
    if(url.origin!=='http://reader.test'){unexpected.push(url.href);return route.abort();}
    if(pathname==='/kol/api/auth/status')return route.fulfill({json:{configured:true,authenticated:authed}});
    if(pathname==='/kol/api/auth/login'){assert.equal(request.postDataJSON().passcode,'fixture-passcode');authed=true;return route.fulfill({json:{authenticated:true}});}
    if(pathname==='/kol/api/auth/logout'){authed=false;return route.fulfill({json:{authenticated:false}});}
    if(pathname.startsWith('/kol/api/private/reading/')){
      if(!authed||privateFailure)return route.fulfill({status:privateFailure||401,json:{detail:'private_mode_required'}});
      if(pathname.endsWith('/summary')){
        posts++;assert.deepEqual(request.postDataJSON(),{id:episode.id});assert.equal(request.headers()['x-finance-radar-action'],'request-reading-summary');assert.equal(request.headers()['x-reader-token'],undefined);
        episode.status='summarized';episode.summary=summary;episode.processing={inference:'deepseek',summary_model:'deepseek-v4-flash'};const job={article_id:episode.id,state:'completed'};automation.jobs=[job];return route.fulfill({status:202,json:{job}});
      }
      privateGets++;
      if(holdNext){holdNext=false;await new Promise(resolve=>releaseHeld=resolve);}
      const data=pathname.endsWith('/wechat')?{articles:[article],sources:[],automation}:{episodes:[episode,meta],automation};
      try{return await route.fulfill({json:data});}catch{return;}
    }
    if(['/kol/wechat','/kol/podcasts'].includes(pathname)){
      let html=fs.readFileSync(path.join(web,pathname.endsWith('wechat')?'wechat.html':'index.html'),'utf8').replace('<html lang="zh-CN">','<html lang="zh-CN" data-reading-mode="kol" data-kol-base="/kol">');
      html=html.replace(/(href|src)="(?:\.\/|\/)([a-z-]+\.(?:css|js))"/g,'$1="/kol/reading-assets/$2?v=40"');return route.fulfill({contentType:'text/html',body:html});
    }
    if(pathname.startsWith('/kol/reading-assets/')){const file=path.basename(pathname);return route.fulfill({contentType:file.endsWith('.js')?'application/javascript':'text/css',body:fs.readFileSync(path.join(web,file),'utf8')});}
    unexpected.push(pathname);return route.fulfill({status:404,body:''});
  });
  const page=await context.newPage();await page.goto('http://reader.test/kol/wechat');await page.locator('#reader-passcode:enabled').waitFor();assert.equal(privateGets,0);assert.equal(await page.locator('#reader').innerText(),'');
  async function unlock(p){await p.locator('#reader-passcode').fill('fixture-passcode');await p.locator('#reader-auth-form button').click();await p.locator('#reader h2').waitFor();}
  await unlock(page);assert((await page.locator('#reader').innerText()).includes(article.title));assert.equal(await page.locator('#reader-passcode').inputValue(),'');assert.equal(await page.locator('.content-nav a').last().getAttribute('href'),'/kol/wechat');assert(!(await page.locator('.wx-footer').innerText()).includes('本地'));assert.equal(await page.evaluate(()=>localStorage.length),0);
  assert.equal(await page.locator('#scope-details').getAttribute('open'),null);await page.locator('#scope-details summary').click();assert.equal(await page.locator('.source-body-note').innerText(),article.source_body_note);assert((await page.locator('#scope-details').innerText()).includes('正文取得时间：2026/10/03'));assert((await page.locator('#scope-details').innerText()).includes('本次来源检查：2026/10/08'));await page.locator('#scope-details summary').click();assert.equal(await page.locator('.summary-overview').innerText(),summary.overview);
  automation.enabled=true;automation.provider_blocked='insufficient_balance';automation.automatic_paused_reason='DeepSeek 余额不足。<strong>请检查账户</strong>';automation.next_check_at='2026-10-09T03:00:00Z';
  await page.evaluate(()=>document.querySelector('#refresh').click());await page.waitForFunction(()=>document.querySelector('#automation-status').textContent==='自动处理已暂停');await page.locator('#automation-details').evaluate(node=>node.open=true);
  assert.equal(await page.locator('.automation-paused-reason').innerText(),automation.automatic_paused_reason);assert.equal(await page.locator('.automation-paused-reason strong').count(),0);assert.equal(await page.locator('.automation-worker').innerText(),'后台进程：运行中');assert.equal(await page.locator('.automation-next-check').innerText(),'下次检查：暂停中，当前不安排自动检查');
  automation.worker_alive=false;await page.evaluate(()=>document.querySelector('#refresh').click());await page.waitForFunction(()=>document.querySelector('.automation-worker').textContent==='后台进程：未运行');assert.equal(await page.locator('#automation-status').innerText(),'自动处理已暂停');
  automation.worker_alive=true;automation.provider_blocked=null;automation.automatic_paused_reason=null;await page.evaluate(()=>document.querySelector('#refresh').click());await page.waitForFunction(()=>document.querySelector('#automation-status').textContent==='已开启');assert.equal(await page.locator('.automation-paused-reason').count(),0);assert(!(await page.locator('.automation-next-check').innerText()).includes('暂停'));
  automation.enabled=false;await page.evaluate(()=>document.querySelector('#refresh').click());await page.waitForFunction(()=>document.querySelector('#automation-status').textContent==='未开启');assert.equal(await page.locator('.automation-next-check').innerText(),'下次检查：未安排');await page.locator('#automation-details').evaluate(node=>node.open=false);
  await page.locator('.evidence-toggle').click();const originalNode=await page.locator('.source-paragraph').first().elementHandle();await page.evaluate(()=>document.querySelector('#refresh').click());await page.waitForTimeout(60);assert(await originalNode.evaluate(n=>n.isConnected));assert.equal(await page.locator('.evidence-toggle').getAttribute('aria-expanded'),'true');
  const podcast=await context.newPage();await podcast.goto('http://reader.test/kol/podcasts');await podcast.locator('#generate-podcast-summary').waitFor();assert.equal(await podcast.locator('audio').count(),0);await podcast.locator('#generate-podcast-summary').click();await podcast.locator('.overview').waitFor();assert.equal(posts,1);assert.equal(await podcast.locator('#generate-podcast-summary').count(),0);await podcast.locator('.episode').filter({hasText:meta.title}).click();assert.equal(await podcast.locator('.overview').count(),0);assert.equal(await podcast.locator('#generate-podcast-summary').count(),0);
  holdNext=true;await page.evaluate(()=>document.querySelector('#refresh').click());await page.waitForFunction(()=>document.querySelector('#refresh').disabled);await podcast.locator('#reader-session-button').click();await page.waitForFunction(()=>document.querySelector('#reader').childElementCount===0);releaseHeld();await page.waitForTimeout(100);assert.equal(await page.locator('#reader').innerText(),'');assert.equal(await page.evaluate(()=>articles.length),0);assert.equal(await podcast.evaluate(()=>episodes.length),0);
  await unlock(page);privateFailure=403;await page.evaluate(()=>document.querySelector('#refresh').click());await page.waitForFunction(()=>document.querySelector('#reader').childElementCount===0);assert(await page.locator('#reader-auth-gate').isVisible());privateFailure=0;
  await unlock(page);authed=false;await page.evaluate(()=>dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true})));await page.waitForFunction(()=>document.querySelector('#reader').childElementCount===0);assert.equal(await page.evaluate(()=>articles.length),0);
  await unlock(page);await page.evaluate(()=>{Object.defineProperty(document,'hidden',{value:true,configurable:true});document.dispatchEvent(new Event('visibilitychange'));});assert.equal(await page.locator('#reader').innerText(),'');authed=false;await page.evaluate(()=>{Object.defineProperty(document,'hidden',{value:false,configurable:true});document.dispatchEvent(new Event('visibilitychange'));});await page.waitForTimeout(80);assert.equal(await page.locator('#reader').innerText(),'');
  await page.setViewportSize({width:390,height:844});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));assert((await page.locator('#reader-auth-form button').boundingBox()).height>=44);await unlock(page);assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);console.log(JSON.stringify({passed:true,checks:['private gate','in-page login','production navigation','no token persistence','action header','no automatic audio','podcast metadata guard','cross-tab logout','stale response discard','403 clears memory and DOM','BFCache recheck','visibility recheck','390px touch targets'],privateGets,summaryPosts:posts}));await browser.close();
})().catch(error=>{console.error(error);process.exit(1);});
