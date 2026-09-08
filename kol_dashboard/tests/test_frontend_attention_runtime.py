from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "kol_dashboard" / "static" / "app.js"


class FrontendAttentionRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is required for frontend runtime tests")
    def test_attention_state_machine_and_staging_contract(self) -> None:
        script = textwrap.dedent(
            r"""
            const fs = require("fs");
            const vm = require("vm");
            const assert = require("assert/strict");
            const path = process.argv[1];
            let source = fs.readFileSync(path, "utf8");
            const tail = /\n\}\)\(\);\s*$/;
            assert.ok(tail.test(source), "app.js IIFE tail changed");
            source = source.replace(tail, `
              globalThis.__attentionTest = {
                state,
                publicAttentionEmpty,
                publicAttentionPrune,
                publicAttentionObserve,
                publicAttentionStore,
                loadPublicAttention,
                markPublicAttentionRead,
                attentionAssessment,
                changedPublicItems,
                stageOrApplyFeedPayload,
              };
            })();`);

            const storage = new Map();
            global.window = {
              KOL_BASE: "/",
              location: { href: "https://zlstreet.xyz/kol/" },
              addEventListener() {},
            };
            global.document = {
              hidden: false,
              addEventListener() {},
              querySelector() { return null; },
              querySelectorAll() { return []; },
            };
            global.localStorage = {
              getItem(key) { return storage.has(key) ? storage.get(key) : null; },
              setItem(key, value) { storage.set(key, String(value)); },
              removeItem(key) { storage.delete(key); },
            };
            global.requestAnimationFrame = (callback) => callback();
            vm.runInThisContext(source, { filename: path });
            const api = global.__attentionTest;

            const iso = new Date(Date.now() - 60_000).toISOString();
            const event = (id, overrides = {}) => ({
              id,
              title: `event ${id}`,
              snippet: "bounded public evidence",
              impact: "medium",
              attribution_basis: "person_mention",
              source: "Bing News",
              source_url: `https://example.com/${id}`,
              published_at: iso,
              time_status: "verified",
              tickers: [],
              ...overrides,
            });
            const reset = () => {
              storage.clear();
              api.state.publicAttention = api.publicAttentionEmpty();
              api.state.feedItems = [];
              api.state.pendingFeedUpdate = null;
              api.state.loadedKolFilterSignature = "";
            };

            // First load establishes a baseline; unchanged rows never become unread.
            reset();
            const initial = event(1);
            api.publicAttentionObserve("kol", [initial], { allowUnread: true });
            assert.equal(api.state.publicAttention.items.length, 0);
            api.state.publicAttention = null;
            api.loadPublicAttention();
            api.publicAttentionObserve("kol", [initial], { allowUnread: true });
            assert.equal(api.state.publicAttention.items.length, 0);

            // Comparison fields survive persistence and a market-impact upgrade re-enters.
            const upgraded = event(1, {
              impact: "high",
              impact_basis: "ai_reviewed_rule_medium",
            });
            assert.equal(api.attentionAssessment(upgraded, "kol").tier, "immediate");
            assert.equal(api.attentionAssessment({
              title: "verified reporting",
              market_impact: "high",
              impact: "low",
              source_tier: "reporting",
              source_url: "https://example.com/daily",
              published_at: iso,
              publication_time_verified: true,
            }, "daily").tier, "immediate");
            api.publicAttentionObserve("kol", [upgraded], { allowUnread: true });
            assert.equal(api.state.publicAttention.items.length, 1);
            assert.equal(api.state.publicAttention.items[0].impact, "high");
            api.markPublicAttentionRead("event:1");
            api.publicAttentionObserve("kol", [upgraded], { allowUnread: true });
            assert.equal(api.state.publicAttention.items.length, 0);
            api.publicAttentionObserve("kol", [event(1, {
              impact: "medium",
              impact_basis: "deterministic_rule",
              attribution_basis: "direct_source",
              source: "X @issuer",
            })], { allowUnread: true });
            assert.equal(api.state.publicAttention.items.length, 0);

            // Filter changes suppress historical backfill, never a known material upgrade.
            reset();
            const filterBase = event(20);
            api.publicAttentionObserve("kol", [filterBase], { allowUnread: true });
            api.publicAttentionObserve("kol", [event(20, {
              attribution_basis: "direct_source",
              source: "X @issuer",
            })], { allowUnread: true, suppressUnread: true });
            assert.equal(api.state.publicAttention.items.length, 1);
            api.markPublicAttentionRead("event:20");
            api.publicAttentionObserve("kol", [event(21, {
              impact: "high",
              attribution_basis: "direct_source",
              source: "X @issuer",
            })], { allowUnread: true, suppressUnread: true });
            assert.equal(api.state.publicAttention.items.length, 0);

            // Direct-source confirmation and a newly exposed asset are material promotions.
            reset();
            const media = event(2);
            api.publicAttentionObserve("kol", [media], { allowUnread: true });
            api.publicAttentionObserve("kol", [event(2, {
              attribution_basis: "direct_source",
              source: "X @issuer",
            })], { allowUnread: true });
            assert.equal(api.state.publicAttention.items.length, 1);

            reset();
            const noAsset = event(3, {
              impact: "high",
              attribution_basis: "direct_source",
              source: "X @issuer",
            });
            api.publicAttentionObserve("kol", [noAsset], { allowUnread: true });
            api.publicAttentionObserve("kol", [{ ...noAsset, tickers: ["AAPL"] }], {
              allowUnread: true,
            });
            assert.equal(api.state.publicAttention.items.length, 1);
            assert.deepEqual(api.state.publicAttention.items[0].assets, ["AAPL"]);

            // Routine fetch metadata and engagement counts are not material revisions.
            reset();
            const stable = event(4, {
              impact: "high",
              attribution_basis: "direct_source",
              source: "X @issuer",
              tickers: ["MSFT"],
            });
            api.publicAttentionObserve("kol", [stable], { allowUnread: true });
            api.publicAttentionObserve("kol", [{
              ...stable,
              last_seen_at: "2026-09-08T01:15:00+00:00",
              source_count: 99,
              hn_score: 999,
            }], { allowUnread: true });
            assert.equal(api.state.publicAttention.items.length, 0);

            // AI enrichment must not turn a bare ticker alias into a fake new asset.
            reset();
            const pendingEnrichment = event(40, {
              impact: "high",
              attribution_basis: "direct_source",
              source: "X @issuer",
              tickers: ["NVDA"],
              ai_status: "pending",
            });
            api.publicAttentionObserve("kol", [pendingEnrichment], {
              allowUnread: true,
            });
            api.publicAttentionObserve("kol", [{
              ...pendingEnrichment,
              ai_status: "ready",
              ai_enrichment: {
                status: "ready",
                assets: [{ asset_key: "US:NVDA", name_zh: "英伟达" }],
              },
            }], { allowUnread: true });
            assert.equal(api.state.publicAttention.items.length, 0);

            reset();
            const cryptoPending = event(41, {
              impact: "high",
              attribution_basis: "direct_source",
              source: "X @issuer",
              tickers: ["BTC"],
              ai_status: "pending",
            });
            api.publicAttentionObserve("kol", [cryptoPending], {
              allowUnread: true,
            });
            api.publicAttentionObserve("kol", [{
              ...cryptoPending,
              ai_status: "ready",
              ai_enrichment: {
                status: "ready",
                assets: [{ asset_key: "CRYPTO:BTC", name_zh: "比特币" }],
              },
            }], { allowUnread: true });
            assert.equal(api.state.publicAttention.items.length, 0);

            // An unread high snapshot is updated or removed when evidence is downgraded.
            reset();
            const high = event(5, {
              impact: "high",
              attribution_basis: "direct_source",
              source: "X @issuer",
            });
            api.publicAttentionObserve("kol", [event(99)], { allowUnread: true });
            api.publicAttentionObserve("kol", [event(99), high], { allowUnread: true });
            assert.equal(api.state.publicAttention.items.length, 1);
            const originalExpiry = api.state.publicAttention.items[0].expiresAt;
            api.publicAttentionObserve("kol", [event(99), high], { allowUnread: true });
            assert.equal(api.state.publicAttention.items[0].expiresAt, originalExpiry);
            api.publicAttentionObserve("kol", [event(99), event(5, { impact: "low" })], {
              allowUnread: true,
            });
            assert.equal(api.state.publicAttention.items.length, 0);

            // Capacity pruning preserves high-impact unread ahead of newer medium rows.
            const now = Date.now();
            const records = Array.from({ length: 200 }, (_, index) => ({
              key: `medium:${index}`,
              impact: "medium",
              tier: "important",
              discoveredAt: now + index,
              expiresAt: now + 60_000,
              assets: [],
            }));
            records.push({
              key: "high:old",
              impact: "high",
              tier: "immediate",
              discoveredAt: now - 60_000,
              expiresAt: now + 60_000,
              assets: [],
            });
            const pruned = api.publicAttentionPrune({
              version: 1,
              items: records,
              baselines: api.publicAttentionEmpty().baselines,
            });
            assert.equal(pruned.items.length, 200);
            assert.equal(pruned.items[0].key, "high:old");
            assert.ok(pruned.items.some((item) => item.key === "high:old"));

            // Each successful background response replaces or clears older staging.
            reset();
            const current = event(10, { impact: "low" });
            const next = event(11, { impact: "low" });
            api.state.feedItems = [current];
            api.state.loadedKolFilterSignature = "sig";
            api.publicAttentionObserve("kol", [current], { allowUnread: true });
            const meta = { loadedCount: 1, highPriority: false, regularCapped: false };
            api.stageOrApplyFeedPayload([current, next], meta, "sig", {
              background: true,
            });
            assert.ok(api.state.pendingFeedUpdate);
            assert.equal(api.state.pendingFeedUpdate.items.length, 2);
            api.stageOrApplyFeedPayload([current], meta, "sig", { background: true });
            assert.equal(api.state.pendingFeedUpdate, null);

            const revised = api.changedPublicItems(
              "kol",
              [event(10, { impact: "high" })],
              [current]
            );
            assert.equal(revised.added.length, 0);
            assert.equal(revised.revised.length, 1);
            """
        )
        result = subprocess.run(
            ["node", "-e", script, str(APP_JS)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=json.dumps(
                {"stdout": result.stdout, "stderr": result.stderr},
                ensure_ascii=False,
                indent=2,
            ),
        )


if __name__ == "__main__":
    unittest.main()
