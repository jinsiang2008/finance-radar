from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest


APP_JS = Path(__file__).resolve().parents[1] / "static" / "app.js"


class FrontendNewsRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is required for frontend runtime tests")
    def test_news_evidence_safety_refresh_and_request_races(self) -> None:
        script = textwrap.dedent(
            r"""
            const fs = require("fs");
            const vm = require("vm");
            const assert = require("assert/strict");
            let source = fs.readFileSync(process.argv[1], "utf8");
            source = source.replace(/\n\}\)\(\);\s*$/, `
              globalThis.__newsTest = { state, newsItemHTML, newsSourceHTML,
                newsContentSignature, loadNews, renderNews, loadNewsContext,
                clearAssetNewsContext, newsSummaryStatus, renderNewsCatalog };
            })();`);
            const node = () => ({
              innerHTML: "", textContent: "", hidden: false, value: "", dataset: {}, attrs: {},
              setAttribute(name, value) { this.attrs[name] = value; },
              remove() {}, focus() {},
              querySelector(selector) {
                if (!this.innerHTML.includes("shared-news-items")) return null;
                if (selector === ".shared-news-items") return {};
                if (selector === ".shared-news-status") {
                  this.banner ||= { innerHTML: "", hidden: false }; return this.banner;
                }
                return null;
              },
            });
            const nodes = new Map();
            for (const id of ["news-items", "news-count", "news-source-list", "news-cadence",
                              "news-publisher", "news-live-status", "news-update-banner", "news-topic-filters", "news-instrument",
                              "news-instrument-focus", "news-context-home", "news-context-daily", "news-context-macro",
                              "news-context-asset", "news-context-kol"]) {
              nodes.set(`#${id}`, node());
            }
            const wrap = { querySelector() { return null; }, prepend() {} };
            nodes.set("#view-news", { querySelector() { return wrap; } });
            global.document = {
              querySelector(selector) { return nodes.get(selector) || null; },
              querySelectorAll() { return []; },
              createElement() { return node(); },
              addEventListener() {},
            };
            global.window = { KOL_BASE: "/kol/", location: { href: "https://example.com/kol/" }, addEventListener() {} };
            global.requestAnimationFrame = (fn) => fn();
            vm.runInThisContext(source);
            const api = global.__newsTest;
            const article = {
              id: 7, title: "Copper supply update", snippet: "Supply needs verification.",
              url: "https://example.com/story", publisher: "Bloomberg",
              published_at: "2026-10-13T00:00:00Z", published_at_status: "provided",
              first_seen_at: "2026-10-13T00:05:00Z", last_seen_at: "2026-10-13T00:05:00Z",
              commodity_tags: ["copper"], evidence_scope: "title_and_snippet",
            };
            const analysis = { status: "ready", evidence_scope: "title_and_snippet", evidence_ids: [7],
              headline_zh: "铜供应消息", summary_zh: "仍需核验的供应变化", why_it_matters_zh: "关注库存变化",
              watchpoints: ["核对矿山公告"],
            };
            const payload = (items = [article]) => ({ items, sources: [], total: items.length,
              polling_interval_seconds: 300, generated_at: "2026-10-13T00:05:00Z" });
            const html = api.newsItemHTML({ ...article, analysis });
            assert.ok(html.includes("铜供应消息") && html.includes("Copper supply update"));
            assert.ok(html.includes("基于标题与简介，待复核"));
            assert.ok(html.includes('rel="noopener noreferrer"'));
            assert.ok(html.includes("首次发现 10-13 08:05"));
            assert.ok(html.includes("发布 10-13 08:00"));
            const attack = api.newsItemHTML({ ...article, title: '<img src=x onerror="bad()">',
              snippet: "<script>bad()</script>", url: "javascript:bad()", publisher: '<svg onload="bad()">' });
            assert.ok(!attack.includes("<script>") && !attack.includes("<img") && !attack.includes("<svg"));
            assert.ok(!attack.includes('href="javascript:'));
            assert.ok(attack.includes("&lt;script&gt;"));
            for (const invalid of [{ ...analysis, status: "error" }, { ...analysis, evidence_ids: [8] },
                                   { ...analysis, evidence_scope: "full_text" }]) {
              assert.ok(!api.newsItemHTML({ ...article, analysis: invalid }).includes("铜供应消息"));
            }
            assert.ok(api.newsItemHTML({ ...article, published_at_status: "future" }).includes("发布时间异常，待核验"));
            assert.ok(!api.newsItemHTML({ ...article, published_at_status: "missing" }).includes("发布 10-13"));
            assert.equal(api.newsContentSignature(payload()), api.newsContentSignature(payload([
              { ...article, last_seen_at: "2026-10-13T00:10:00Z" }
            ])), "collector visits do not masquerade as new stories");
            const sourceState = { name: "Publisher", status: "healthy", last_success_at: "2026-10-13T00:05:00Z" };
            assert.ok(api.newsSourceHTML({ ...sourceState, stale: true }).includes("采集记录已过期"));
            assert.ok(api.newsSourceHTML({ ...sourceState, consecutive_failures: 1 }).includes("最近检查失败"));
            assert.ok(api.newsSourceHTML({ ...sourceState, status: "unknown" }).includes("等待采集确认"));
            assert.ok(api.newsSourceHTML({ status: "not_checked", stale: true }).includes("等待采集确认"));
            assert.ok(api.newsSourceHTML(sourceState).includes("上游延迟未知"));
            const titleOnly = { ...article, snippet: "", evidence_scope: "title_only" };
            const titleOnlyHTML = api.newsItemHTML({ ...titleOnly,
              analysis: { ...analysis, evidence_scope: "title_only" } });
            assert.ok(titleOnlyHTML.includes("仅基于标题，待复核"));
            assert.ok(titleOnlyHTML.includes("收录证据仅为来源标题"));
            assert.ok(titleOnlyHTML.includes("铜供应消息"));
            assert.ok(!titleOnlyHTML.includes("标题与简介"));
            assert.ok(!api.newsItemHTML({ ...titleOnly, analysis }).includes("铜供应消息"),
              "summary cannot overstate available evidence scope");

            (async () => {
              const responses = [];
              global.fetch = (url) => new Promise((resolve, reject) => responses.push({ url, resolve, reject }));
              const succeed = (index, data) => responses[index].resolve({ ok: true, json: async () => data });
              const oldRequest = api.loadNews();
              api.state.newsCommodity = "gold";
              const newRequest = api.loadNews();
              assert.ok(responses[1].url.includes("/kol/api/news?limit=40&commodity=gold"));
              const gold = { ...article, id: 9, title: "Gold news", commodity_tags: ["gold"] };
              succeed(1, payload([gold]));
              assert.equal(await newRequest, true);
              succeed(0, payload());
              assert.equal(await oldRequest, false);
              assert.equal(api.state.newsData.items[0].id, 9, "older filter response must not overwrite newer filter");
              const visible = nodes.get("#news-items").innerHTML;
              const backgroundRequest = api.loadNews({ background: true });
              succeed(2, payload([{ ...gold, analysis: { ...analysis, evidence_ids: [9] } }]));
              assert.equal(await backgroundRequest, true);
              assert.equal(nodes.get("#news-items").innerHTML, visible, "refresh must preserve reading position");
              assert.ok(api.state.pendingNewsData);
              assert.equal(nodes.get("#news-update-banner").hidden, false);
              const failedRequest = api.loadNews({ background: true });
              responses[3].reject(new Error("Failed to fetch"));
              assert.equal(await failedRequest, false);
              assert.equal(nodes.get("#news-items").innerHTML, visible, "failed refresh retains successful result");
              assert.equal(api.state.pendingNewsData, null, "a failed refresh clears obsolete staged updates");
              assert.equal(nodes.get("#news-update-banner").hidden, true);
              assert.ok(api.state.viewLoadErrors.news);
              assert.equal(nodes.get("#news-items").attrs["aria-busy"], "false");
              api.state.newsData = null;
              const initialFailure = api.loadNews();
              responses[4].reject(new Error("Failed to fetch"));
              await initialFailure;
              assert.ok(nodes.get("#news-items").innerHTML.includes("暂时无法读取市场快讯"));
              assert.equal(nodes.get("#news-count").textContent, "读取失败");

              // Shared reports contain summaries in the current page, independently of decision snapshots.
              const oldAssetRequest = api.loadNewsContext("asset", { asset: "US:NVDA" });
              const newAssetRequest = api.loadNewsContext("asset", { asset: "US:SPCX" });
              assert.ok(responses[6].url.includes("view=asset&limit=4&asset=US%3ASPCX"));
              succeed(6, payload([{ ...gold, analysis: { ...analysis, evidence_ids: [9] } }]));
              assert.equal(await newAssetRequest, true);
              succeed(5, payload());
              assert.equal(await oldAssetRequest, false);
              assert.equal(api.state.newsContexts.asset.asset, "US:SPCX");
              assert.ok(nodes.get("#news-context-asset").innerHTML.includes("仍需核验的供应变化"));
              assert.ok(nodes.get("#news-context-asset").innerHTML.includes("影响与核验"));
              assert.ok(nodes.get("#news-context-asset").innerHTML.includes("原文证据"));
              const privateAssetRequest = api.loadNewsContext("asset", { asset: "US:NVDA" });
              api.clearAssetNewsContext();
              succeed(7, payload());
              assert.equal(await privateAssetRequest, false);
              assert.equal(api.state.newsContexts.asset, undefined, "logout removes asset-selection context");
              assert.equal(nodes.get("#news-context-asset").innerHTML, "");

              // KOL filter changes never relabel an older person's publisher stories as the new selection.
              api.state.selectedKols = new Set(["musk"]);
              const oldKolContext = api.loadNewsContext("kol");
              assert.ok(responses[8].url.includes("view=kol&limit=3&kols=musk"));
              api.state.selectedKols = new Set(["altman"]);
              const newKolContext = api.loadNewsContext("kol");
              const media = { ...gold, kol_mentions: [{ name: "Sam Altman", key: "altman", attribution_basis: "company_mention" }] };
              succeed(9, payload([media]));
              assert.equal(await newKolContext, true);
              succeed(8, payload());
              assert.equal(await oldKolContext, false);
              const kolVisible = nodes.get("#news-context-kol").innerHTML;
              assert.ok(kolVisible.includes("关联公司报道：Sam Altman"));
              assert.ok(kolVisible.includes("不等同于本人表态"));
              const kolRefresh = api.loadNewsContext("kol", { force: true, background: true });
              succeed(10, payload([{ ...media, title: "Revised company report" }]));
              assert.equal(await kolRefresh, true);
              assert.equal(nodes.get("#news-context-kol").innerHTML, kolVisible);
              assert.ok(api.state.newsContexts.kol.pending);
              const kolFailure = api.loadNewsContext("kol", { force: true, background: true });
              responses[11].reject(new Error("Failed to fetch"));
              assert.equal(await kolFailure, false);
              assert.equal(nodes.get("#news-context-kol").innerHTML, kolVisible);
              assert.equal(api.state.newsContexts.kol.pending, null);
              assert.ok(nodes.get("#news-context-kol").banner.innerHTML.includes("保留上次结果"));

              assert.equal(api.newsSummaryStatus({ published_at_status: "provided", analysis: { status: "not_scheduled" } }),
                "未自动解读 · 超出近期处理范围");
              assert.equal(api.newsSummaryStatus({ published_at_status: "missing", analysis: { status: "not_scheduled" } }),
                "未自动解读 · 发布时间待核实");
              api.state.newsInstrument = "US:SPCX";
              api.renderNewsCatalog({ catalog: { topics: [{ key: "space", label: "商业航天", count: 2 }], instruments: [
                { key: "US:SPCX", label: "SpaceX", ticker: "SPCX", market: "NASDAQ", type: "stock", direct_count: 2 }
              ] }});
              assert.ok(nodes.get("#news-topic-filters").innerHTML.includes("商业航天"));
              assert.ok(nodes.get("#news-instrument").innerHTML.includes("SpaceX · SPCX"));
              assert.ok(nodes.get("#news-instrument-focus").innerHTML.includes("只显示标题或简介直接提及"));
              const association = api.newsItemHTML({ ...article, assets: [
                { asset_key: "US:COPX", name: "Copper miners ETF", relation: "topic_related" }
              ] });
              assert.ok(association.includes("主题相关，原文未直接提及"));
              assert.ok(!association.includes('data-news-instrument="US:COPX"'));
              console.log("news runtime safety and lifecycle checks passed");
            })().catch((error) => { console.error(error); process.exitCode = 1; });
            """
        )
        result = subprocess.run(
            ["node", "-e", script, str(APP_JS)], capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("news runtime safety and lifecycle checks passed", result.stdout)
