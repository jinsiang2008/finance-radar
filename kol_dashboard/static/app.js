// 关联决策台 · 风险雷达 · KOL 动态 — works under "/" or "/kol/".
(() => {
  "use strict";

  const BASE = (window.KOL_BASE || "/").replace(/\/?$/, "/");
  const api = (p) => BASE + p.replace(/^\//, "");
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  const state = {
    view: "decision",
    hours: 24,
    timeStatus: "verified",
    impact: "",
    kol: "",
    q: "",
    authenticated: false,
    authConfigured: false,
    authStatusLoaded: false,
    logoutPending: false,
    decisionData: null,
    selectedDecisionKey: "",
    decisionLens: "all",
    decisionLensLoading: false,
    decisionLensRequestGeneration: 0,
    decisionQueueExpanded: false,
    matrixExpanded: false,
    decisionRequestGeneration: 0,
    decisionDetailRequestGeneration: 0,
    decisionDetailCache: new Map(),
    fullDecisionLoadError: null,
    fullDecisionLoadPromise: null,
    watchAssets: new Set(),
    macroData: null,
    macroHistory: [],
    macroRequestGeneration: 0,
    macroHistoryRequestGeneration: 0,
    stats: null,
    statsRequestGeneration: 0,
    kolsRequestGeneration: 0,
    feedItems: [],
    expandedClusters: new Set(),
    feedLoadedCount: 0,
    feedVisibleCount: 0,
    feedClusteredCount: 0,
    feedHighPriority: false,
    feedRegularCapped: false,
    feedRequestGeneration: 0,
    feedAbortController: null,
    viewLoadedAt: { decision: 0, macro: 0, kol: 0 },
    viewLastGoodAt: { decision: 0, macro: 0, kol: 0 },
    viewLastGoodDataAt: { decision: "", macro: "", kol: "" },
    viewLoadErrors: {},
    viewLoadPromises: {},
    systemSignals: { macro: null, decision: null },
    refreshTimer: null,
    supportFactsLoaded: false,
    drawerEventId: null,
    drawerKol: "",
    drawerSourceUrl: "",
    drawerRequestGeneration: 0,
    drawerAbortController: null,
    drawerReturnFocus: null,
    drawerPreviousOverflow: "",
    drawerInertNodes: [],
  };

  // ─── Helpers ──────────────────────────────

  const esc = (s) =>
    String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");

  const LEVEL_COLOR = {
    critical: "var(--crit)",
    high: "var(--high)",
    medium: "var(--med)",
    low: "var(--low)",
    unknown: "var(--neutral)",
  };

  const LEVEL_CN = {
    critical: "极高",
    high: "偏高",
    medium: "中等",
    low: "较低",
    unknown: "未知",
  };

  const PROB_CN = {
    high: "高概率",
    medium_to_high: "中高概率",
    medium: "中概率",
    low_to_medium: "中低概率",
    low: "低概率",
  };
  const PROB_LEVEL = {
    high: "critical",
    medium_to_high: "high",
    medium: "medium",
    low_to_medium: "medium",
    low: "low",
  };

  const IMPACT_CN = {
    catastrophic: "毁灭级",
    severe: "严重",
    high: "高",
    medium: "中",
    low: "低",
  };

  const URGENCY_CN = {
    imminent: "迫在眉睫",
    approaching: "正在逼近",
    gradual: "缓慢积累",
  };
  const URGENCY_LEVEL = {
    imminent: "critical",
    approaching: "high",
    gradual: "medium",
  };

  const VISIBILITY_CN = {
    highly_visible: "高度可见却被忽视",
    moderate: "部分市场已察觉",
    low: "尚未被定价",
  };

  const CONF_CN = { high: "高置信", medium: "中置信", low: "低置信" };
  const TOPIC_CN = {
    ai_semiconductors: "AI 与半导体",
    monetary_policy: "货币政策",
    recession_growth: "衰退与增长",
    inflation: "通胀与物价",
    geopolitics_trade: "地缘与贸易",
    crypto: "加密资产",
    financial_system: "金融系统",
    china_markets: "中国市场",
    market_risk: "市场风险",
    inflation_rates: "通胀与利率",
    geopolitical_risk: "地缘政治",
    china_macro: "中国宏观",
    crypto_regulation: "加密与监管",
    market_stress: "市场压力",
    general_market: "综合市场",
    general: "综合市场",
  };
  const ASSET_CN = {
    "US:SPY": "标普 500 ETF",
    "US:QQQ": "纳指 100 ETF",
    "US:NVDA": "英伟达",
    "US:AVGO": "博通",
    "US:AMD": "超威半导体",
    "US:TSM": "台积电",
    "US:SOXL": "三倍做多半导体 ETF",
    "US:TSLA": "特斯拉",
    "US:AAPL": "苹果",
    "US:MSFT": "微软",
    "US:META": "Meta",
    "US:GOOGL": "谷歌",
    "US:AMZN": "亚马逊",
    "US:BABA": "阿里巴巴",
    "US:TLT": "长期美国国债 ETF",
    "BOND:UST_LONG": "长期美国国债",
    "BOND:UST_INTERMEDIATE": "中期美国国债",
    "COMMODITY:GOLD": "黄金",
    "COMMODITY:OIL": "原油",
    "CRYPTO:BTC": "比特币",
    "CRYPTO:ETH": "以太坊",
    "CRYPTO:DOGE": "狗狗币",
    "FX:JPY": "日元",
    "FX:CNY": "人民币",
    "FX:USD/CNY": "美元兑人民币",
    "FX:DXY": "美元指数",
    "INDEX:HSI": "恒生指数",
    "INDEX:CSI300": "沪深 300",
    "INDEX:NIKKEI": "日经 225",
    "INDEX:VIX": "VIX 恐慌指数",
    "THEME:AI": "人工智能主题",
    "THEME:SEMICONDUCTOR": "半导体主题",
    "THEME:CHINA_EQUITY": "中国股票主题",
    "THEME:CRYPTO": "加密资产主题",
    "THEME:EMERGING_MARKETS": "新兴市场主题",
    "THEME:FINANCIALS": "金融板块",
    "THEME:GLOBAL_RISK_ASSETS": "全球风险资产",
  };
  const ASSET_TICKER = {
    "BOND:UST_LONG": "TLT",
    "BOND:UST_INTERMEDIATE": "IEF",
    "COMMODITY:GOLD": "GOLD",
    "COMMODITY:OIL": "WTI",
    "FX:USD/CNY": "USD/CNY",
    "INDEX:NIKKEI": "NIKKEI",
    "THEME:SEMICONDUCTOR": "SEMICONDUCTOR",
    "THEME:CHINA_EQUITY": "CHINA EQUITY",
    "THEME:EMERGING_MARKETS": "EM",
    "THEME:FINANCIALS": "FINANCIALS",
    "THEME:GLOBAL_RISK_ASSETS": "GLOBAL RISK",
  };
  const ACTION_CN = {
    reduce_or_hedge: { label: "减仓 / 对冲", icon: "▼", color: "var(--high)" },
    scale_in: { label: "分批布局", icon: "▲", color: "var(--low)" },
    candidate_reduce_or_hedge: {
      label: "候选减仓 / 对冲",
      icon: "▼",
      color: "var(--high)",
    },
    candidate_scale_in: {
      label: "候选分批布局",
      icon: "▲",
      color: "var(--low)",
    },
    verify: { label: "验证", icon: "◆", color: "var(--med)" },
    observe: { label: "观察", icon: "○", color: "var(--neutral)" },
  };
  const CLASS_CN = {
    risk: { label: "风险", icon: "▼", color: "var(--high)" },
    opportunity: { label: "机会", icon: "▲", color: "var(--low)" },
    conflict: { label: "分歧", icon: "◆", color: "var(--med)" },
  };

  // Strip leading emoji that the backend bakes into labels; the UI adds its own.
  const stripEmoji = (s) =>
    String(s ?? "").replace(/^[\s\p{Extended_Pictographic}\uFE0F]+/u, "").trim();

  function safeExternalUrl(value) {
    const raw = String(value || "").trim();
    if (!raw) return "";
    try {
      const parsed = new URL(raw, window.location.href);
      return parsed.protocol === "http:" || parsed.protocol === "https:"
        ? parsed.href
        : "";
    } catch (error) {
      return "";
    }
  }

  function assetTicker(key) {
    const value = String(key || "").trim();
    if (!value) return "未知";
    if (ASSET_TICKER[value]) return ASSET_TICKER[value];
    const separator = value.indexOf(":");
    return separator >= 0 ? value.slice(separator + 1) : value;
  }

  function assetLabel(key) {
    const value = String(key || "").trim();
    const ticker = assetTicker(value);
    return ASSET_CN[value] ? `${ASSET_CN[value]} · ${ticker}` : ticker;
  }

  function fmtTime(iso) {
    if (!iso) return "";
    const d = new Date(String(iso));
    if (Number.isNaN(d.getTime())) return String(iso);
    const mins = Math.floor((Date.now() - d.getTime()) / 60000);
    if (mins < 1) return "刚刚";
    if (mins < 60) return `${mins} 分钟前`;
    if (mins < 1440) return `${Math.floor(mins / 60)} 小时前`;
    const days = Math.floor(mins / 1440);
    if (days < 7) return `${days} 天前`;
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(d);
  }

  function fmtRelativeTime(iso) {
    if (!iso) return "";
    const d = new Date(String(iso));
    if (Number.isNaN(d.getTime())) return "";
    const mins = Math.floor((Date.now() - d.getTime()) / 60000);
    if (mins < 1) return "刚刚";
    if (mins < 60) return `${mins} 分钟前`;
    if (mins < 1440) return `${Math.floor(mins / 60)} 小时前`;
    return `${Math.floor(mins / 1440)} 天前`;
  }

  function fmtBeijingDateTime(iso) {
    if (!iso) return "";
    const d = new Date(String(iso));
    if (Number.isNaN(d.getTime())) return "";
    const parts = Object.fromEntries(
      new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hourCycle: "h23",
      })
        .formatToParts(d)
        .filter((part) => part.type !== "literal")
        .map((part) => [part.type, part.value])
    );
    const date = `${parts.year}-${parts.month}-${parts.day}`;
    return `${date} ${parts.hour}:${parts.minute}:${parts.second}`;
  }

  function publicationTimeView(iso) {
    const exact = fmtBeijingDateTime(iso);
    if (!exact) return null;
    const relative = fmtRelativeTime(iso);
    return {
      visible: `发布 ${exact}（北京时间） · ${relative}`,
      accessible: `发布时间 ${exact}，北京时间，${relative}`,
      datetime: new Date(String(iso)).toISOString(),
    };
  }

  const num = (v, digits = 2) =>
    typeof v === "number" && isFinite(v) ? v.toFixed(digits) : null;

  async function fetchJSON(url, timeoutMs = 12000, options = {}) {
    const ctrl = new AbortController();
    const externalSignal = options.signal;
    const abortFromExternal = () => ctrl.abort();
    if (externalSignal) {
      if (externalSignal.aborted) ctrl.abort();
      else externalSignal.addEventListener("abort", abortFromExternal, { once: true });
    }
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const r = await fetch(url, {
        signal: ctrl.signal,
        cache: options.cache || "no-cache",
      });
      if (!r.ok) {
        let payload = null;
        try {
          payload = await r.json();
        } catch (e) {}
        const error = new Error((payload && payload.detail) || `HTTP ${r.status}`);
        error.status = r.status;
        error.payload = payload;
        throw error;
      }
      return await r.json();
    } finally {
      clearTimeout(t);
      externalSignal?.removeEventListener("abort", abortFromExternal);
    }
  }

  async function requestJSON(url, options = {}, timeoutMs = 12000) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const r = await fetch(url, {
        ...options,
        credentials: "same-origin",
        cache: "no-store",
        signal: ctrl.signal,
        headers: {
          ...(options.body ? { "Content-Type": "application/json" } : {}),
          ...(options.headers || {}),
        },
      });
      let payload = null;
      try {
        payload = await r.json();
      } catch (e) {}
      if (!r.ok) {
        const err = new Error((payload && payload.detail) || `HTTP ${r.status}`);
        err.status = r.status;
        err.payload = payload;
        throw err;
      }
      return payload;
    } finally {
      clearTimeout(t);
    }
  }

  function errorHTML(err, url) {
    const msg = (err && err.message) || String(err || "");
    let cause;
    if (/abort/i.test((err && err.name) || msg)) cause = "请求超时";
    else if (/failed to fetch|networkerror/i.test(msg)) cause = "网络错误，无法连接服务";
    else cause = `加载失败：${esc(msg)}`;
    return `<div class="empty">
      <span class="empty-icon">⚠️</span>${cause}
      <code>${esc(url)}</code>
      <button class="retry-btn" onclick="location.reload()">重新加载</button>
    </div>`;
  }

  function loadErrorCause(error) {
    const message = (error && error.message) || String(error || "");
    if (/abort/i.test((error && error.name) || message)) return "请求超时";
    if (/failed to fetch|networkerror/i.test(message)) return "网络连接失败";
    return message ? `加载失败：${message}` : "加载失败";
  }

  function recordViewLastGoodDataAt(view, payload) {
    const candidates = [
      payload?.generated_at,
      payload?.source_as_of,
      payload?.created_at,
      payload?.timestamp,
    ];
    const value = candidates.find((candidate) => {
      if (!candidate) return false;
      return Number.isFinite(new Date(String(candidate)).getTime());
    });
    state.viewLastGoodDataAt[view] = value ? String(value) : "";
  }

  function renderViewLoadState(view) {
    const main = $(`#view-${view}`);
    const wrap = main?.querySelector(".wrap");
    if (!wrap) return;
    const existing = wrap.querySelector(`[data-view-load-state="${view}"]`);
    const failure = state.viewLoadErrors[view];
    if (!failure) {
      existing?.remove();
      return;
    }
    const host = existing || document.createElement("div");
    host.className = "view-load-state is-error";
    host.dataset.viewLoadState = view;
    host.setAttribute("role", "alert");
    host.setAttribute("aria-live", "assertive");
    const lastGoodDataAt = state.viewLastGoodDataAt[view];
    const hasLastGood = Number(state.viewLastGoodAt[view] || 0) > 0;
    const lastGoodCopy = lastGoodDataAt
      ? `继续显示数据截至 ${fmtAbsoluteTime(lastGoodDataAt)} 的上次成功结果，内容可能已过期。`
      : hasLastGood
        ? "继续显示上次成功结果；数据时间未提供，内容可能已过期。"
        : "当前没有可继续显示的成功数据。";
    host.innerHTML = `<span class="view-load-mark" aria-hidden="true">!</span>
      <div class="view-load-copy">
        <strong>当前视图刷新失败</strong>
        <span>${esc(loadErrorCause(failure.error))}；${esc(lastGoodCopy)}</span>
      </div>
      <button type="button" class="view-retry-btn" data-view-retry="${esc(view)}">
        重试当前视图
      </button>`;
    if (!existing) wrap.prepend(host);
  }

  function setViewLoadError(view, error, url = "") {
    state.viewLoadedAt[view] = 0;
    state.viewLoadErrors[view] = { error, url };
    renderViewLoadState(view);
  }

  function clearViewLoadError(view) {
    delete state.viewLoadErrors[view];
    renderViewLoadState(view);
  }

  // ─── Decision cockpit ──────────────────────

  const topicName = (key) =>
    TOPIC_CN[key] || String(key || "未分类").replaceAll("_", " ");
  const decisionKey = (card) => `${card.topic_key || ""}::${card.asset_key || ""}`;
  const pct = (value, digits = 1) =>
    typeof value === "number" && isFinite(value)
      ? `${value >= 0 ? "+" : ""}${(value * 100).toFixed(digits)}%`
      : "—";
  const confidencePct = (card) => {
    const value = card.confidence ?? card.score_components?.confidence;
    return typeof value === "number" ? `${Math.round(value * 100)}%` : "—";
  };

  const CANDIDATE_STAGE = {
    reduce_or_hedge: "candidate_reduce_or_hedge",
    scale_in: "candidate_scale_in",
  };

  function actionInfo(card) {
    const stage = String(card?.action_stage || "observe");
    const candidateStage =
      card?.human_review_required === true ? CANDIDATE_STAGE[stage] : "";
    return ACTION_CN[candidateStage || stage] || ACTION_CN.observe;
  }

  function isCandidateAction(card) {
    return [
      "reduce_or_hedge",
      "scale_in",
      "candidate_reduce_or_hedge",
      "candidate_scale_in",
    ].includes(String(card?.action_stage || ""));
  }

  const DECISION_WATCHLIST_STORAGE_KEY =
    "finance-radar-public-asset-watchlist-v1";
  const DECISION_WATCHLIST_LIMIT = 50;
  const PUBLIC_ASSET_KEY_PATTERN = /^[A-Z][A-Z0-9_]*:[A-Z0-9._\/-]+$/;
  const DECISION_LENS_LABEL = {
    all: "全部",
    candidate: "候选",
    portfolio: "我的资产",
    watchlist: "本机关注",
  };

  function normalizedPublicAssetKey(value) {
    const key = String(value || "").trim().toUpperCase();
    return key.length <= 80 && PUBLIC_ASSET_KEY_PATTERN.test(key) ? key : "";
  }

  function loadDecisionWatchlist() {
    try {
      const stored = JSON.parse(
        localStorage.getItem(DECISION_WATCHLIST_STORAGE_KEY) || "[]"
      );
      const keys = Array.from(
        new Set(
          (Array.isArray(stored) ? stored : [])
            .map(normalizedPublicAssetKey)
            .filter(Boolean)
        )
      ).slice(0, DECISION_WATCHLIST_LIMIT);
      state.watchAssets = new Set(keys);
    } catch (error) {
      state.watchAssets = new Set();
    }
  }

  function persistDecisionWatchlist() {
    try {
      localStorage.setItem(
        DECISION_WATCHLIST_STORAGE_KEY,
        JSON.stringify(Array.from(state.watchAssets).slice(0, DECISION_WATCHLIST_LIMIT))
      );
      return true;
    } catch (error) {
      return false;
    }
  }

  function isWatchedAsset(assetKey) {
    const key = normalizedPublicAssetKey(assetKey);
    return Boolean(key && state.watchAssets.has(key));
  }

  function setWatchlistStatus(message) {
    const status = $("#decision-watchlist-status");
    if (status) status.textContent = message;
  }

  function updateWatchButtons() {
    $$('[data-watch-asset]').forEach((button) => {
      const watched = isWatchedAsset(button.dataset.watchAsset);
      button.setAttribute("aria-pressed", String(watched));
      button.classList.toggle("is-watched", watched);
      const icon = button.querySelector("[aria-hidden]");
      if (icon) icon.textContent = watched ? "★" : "☆";
      const label = button.querySelector(".decision-watch-label");
      if (label) label.textContent = watched ? "已关注 · 本机保存" : "关注此资产";
    });
  }

  function toggleWatchAsset(assetKey, returnFocus = null) {
    const key = normalizedPublicAssetKey(assetKey);
    if (!key) return;
    const wasWatched = state.watchAssets.has(key);
    if (wasWatched) {
      state.watchAssets.delete(key);
    } else if (state.watchAssets.size < DECISION_WATCHLIST_LIMIT) {
      state.watchAssets.add(key);
    } else {
      setWatchlistStatus(`本机关注最多保存 ${DECISION_WATCHLIST_LIMIT} 个公开资产代码。`);
      return;
    }
    const persisted = persistDecisionWatchlist();
    setWatchlistStatus(
      `${assetLabel(key)}${wasWatched ? "已移出" : "已加入"}本机关注。${
        persisted
          ? "仅公开资产代码保存在本机浏览器。"
          : "浏览器拒绝持久保存，本次页面内仍保留。"
      }`
    );
    if (state.decisionData) {
      renderDecisions(state.decisionData);
    }
    updateWatchButtons();
    if (returnFocus) {
      requestAnimationFrame(() => {
        const replacement = $$('[data-watch-asset]').find(
          (button) => normalizedPublicAssetKey(button.dataset.watchAsset) === key
        );
        const selectedCard = $$("[data-decision-key]").find(
          (node) => node.dataset.decisionKey === state.selectedDecisionKey
        );
        const lensButton = $(`[data-decision-lens="${state.decisionLens}"]`);
        (replacement || selectedCard || lensButton)?.focus();
      });
    }
  }

  function evidenceStatusInfo(card) {
    const sourceCount = Math.max(0, Number(card?.source_count || 0));
    if (sourceCount >= 2) {
      return {
        tone: "ok",
        state: "multi_source",
        label: `多源证据 · ${sourceCount} 个独立来源`,
      };
    }
    if (sourceCount === 1) {
      return {
        tone: "warn",
        state: "single_source",
        label: "单一来源 · 待交叉核验",
      };
    }
    return { tone: "error", state: "no_source", label: "暂无可核验来源" };
  }

  function marketReasonCount(market, key) {
    const declared = Number(market?.reason_counts?.[key] || 0);
    if (Number.isFinite(declared) && declared > 0) return declared;
    return (Array.isArray(market?.records) ? market.records : []).filter(
      (record) =>
        String(record?.reason_code || record?.reason || "").toLowerCase() === key
    ).length;
  }

  function marketReasonTotal(market) {
    const declared = market?.reason_counts;
    if (declared && typeof declared === "object") {
      return Object.values(declared).reduce((sum, value) => {
        const count = Number(value);
        return sum + (Number.isFinite(count) && count > 0 ? count : 0);
      }, 0);
    }
    return (Array.isArray(market?.records) ? market.records : []).filter(
      (record) => String(record?.reason_code || record?.reason || "").trim()
    ).length;
  }

  function marketIsPurePending(market) {
    const status = String(market?.status || "").toLowerCase();
    const windowNotDue = marketReasonCount(market, "window_not_due");
    const reasonTotal = marketReasonTotal(market);
    const unavailableWindows = Array.isArray(market?.unavailable_windows)
      ? market.unavailable_windows.length
      : 0;
    return (
      market?.degraded !== true &&
      unavailableWindows === 0 &&
      (status === "pending" || (status === "unavailable" && windowNotDue > 0)) &&
      (reasonTotal === 0 || reasonTotal === windowNotDue)
    );
  }

  function earliestMarketDue(market) {
    return [
      market?.next_review_at,
      ...(Array.isArray(market?.records)
        ? market.records.map((record) => record?.next_due_at)
        : []),
    ]
      .map((value) => ({ value, epoch: new Date(String(value || "")).getTime() }))
      .filter((item) => Number.isFinite(item.epoch))
      .sort((a, b) => a.epoch - b.epoch)[0];
  }

  function decisionNextReview(card) {
    const market = card?.market_validation || {};
    const applicability = String(market.applicability || "").toLowerCase();
    const reason = String(market.applicability_reason || "").toLowerCase();
    const phase = String(market.phase || "").toLowerCase();
    if (
      reason === "no_event_anchor" ||
      applicability === "no_event_anchor" ||
      phase === "scenario_monitoring"
    ) {
      return { epoch: Number.POSITIVE_INFINITY, datetime: "", label: "等待触发指标", detail: "宏观情景监控" };
    }
    if (
      reason === "direction_missing" ||
      applicability === "direction_missing" ||
      ["direction_missing", "direction_unavailable"].includes(phase)
    ) {
      return { epoch: Number.POSITIVE_INFINITY, datetime: "", label: "先补方向证据", detail: "重试行情无法解决" };
    }
    if (phase === "contrary" || market.direction_confirmed === false) {
      return { epoch: Number.POSITIVE_INFINITY, datetime: "", label: "新证据出现时复核", detail: "候选已停止" };
    }
    const declared = earliestMarketDue(market);
    if (marketIsPurePending(market)) {
      if (declared) {
        return {
          epoch: declared.epoch,
          datetime: String(declared.value),
          label:
            declared.epoch <= Date.now()
              ? "现在需复核"
              : fmtAbsoluteTime(declared.value),
          detail: declared.epoch <= Date.now() ? "市场窗口已到" : "等待所需窗口",
        };
      }
      return { epoch: Number.POSITIVE_INFINITY, datetime: "", label: "等待所需窗口", detail: "窗口时间待补齐" };
    }
    if (
      market.degraded === true ||
      ["degraded", "unavailable", "error", "failed"].includes(
        String(market.status || "").toLowerCase()
      )
    ) {
      if (declared) {
        return {
          epoch: declared.epoch,
          datetime: String(declared.value),
          label:
            declared.epoch <= Date.now()
              ? "现在重试行情"
              : `${fmtAbsoluteTime(declared.value)} 重试行情`,
          detail: "技术 / 数据重试，不是市场窗口确认",
        };
      }
      return { epoch: Number.POSITIVE_INFINITY, datetime: "", label: "按采集计划重试", detail: "保持降级核验" };
    }
    if (
      phase.startsWith("confirmed_") &&
      market.required_window_complete !== true
    ) {
      if (declared) {
        return {
          epoch: declared.epoch,
          datetime: String(declared.value),
          label:
            declared.epoch <= Date.now()
              ? "现在需复核"
              : fmtAbsoluteTime(declared.value),
          detail: "等待所需确认窗口",
        };
      }
      return { epoch: Number.POSITIVE_INFINITY, datetime: "", label: "等待所需窗口", detail: "窗口时间待补齐" };
    }
    if (market.abstain === false && market.direction_confirmed === true) {
      return { epoch: Number.POSITIVE_INFINITY, datetime: "", label: "人工复核失效条件", detail: "不自动执行" };
    }
    return { epoch: Number.POSITIVE_INFINITY, datetime: "", label: "窗口时间待补齐", detail: "保持观察" };
  }

  function marketSourceScopeLabel(card) {
    const market = card?.market_validation || {};
    const scope = String(market.source_scope || market.validation_scope || "").toLowerCase();
    if (scope === "mixed") return "事件 + 宏观";
    if (scope === "macro_only") return "宏观情景";
    if (scope === "event_only" || scope === "event_anchored") return "事件锚点";
    return "来源范围待核验";
  }

  function marketStatusInfo(card) {
    const market = card?.market_validation || {};
    const status = String(market.status || "").toLowerCase();
    const phase = String(market.phase || "").toLowerCase();
    const applicability = String(market.applicability || "").toLowerCase();
    const applicabilityReason = String(
      market.applicability_reason || ""
    ).toLowerCase();
    const sourceScope = String(
      market.source_scope || market.validation_scope || ""
    ).toLowerCase();
    const directionConfirmed = market.direction_confirmed;
    const noEventAnchor =
      applicabilityReason === "no_event_anchor" ||
      applicability === "no_event_anchor" ||
      phase === "scenario_monitoring" ||
      ((applicability === "not_applicable" || status === "not_applicable") &&
        sourceScope === "macro_only");
    if (noEventAnchor) {
      return {
        tone: "warn",
        state: "scenario_monitoring",
        label: "宏观情景监控",
        guidance:
          "宏观情景无单一事件锚点，不套用事件后 1D/3D/5D 回测；请核验触发指标与失效条件。这不是行情链路故障。",
      };
    }
    if (
      applicabilityReason === "direction_missing" ||
      applicability === "direction_missing" ||
      ["direction_missing", "direction_unavailable"].includes(phase)
    ) {
      return {
        tone: "warn",
        state: "direction_missing",
        label: "机制方向未明确",
        guidance:
          "当前仅有提及或多空分歧，无法判断同向或反向；请先补方向证据，重试行情无法解决此问题。",
      };
    }
    if (phase === "contrary" || directionConfirmed === false) {
      return {
        tone: "warn",
        state: "contrary",
        label: "市场反向，候选停止并复核",
        guidance:
          "市场样本与机制方向相反；停止候选行动，先复核相反证据与失效条件。",
      };
    }
    if (
      ["inconclusive", "neutral"].includes(phase) ||
      (status === "complete" && directionConfirmed !== true)
    ) {
      return {
        tone: "warn",
        state: "inconclusive",
        label: "市场方向中性或不一致",
        guidance:
          "市场样本未形成一致方向；候选不能推进，需继续复核中性和相反证据。",
      };
    }
    if (
      marketIsPurePending(market) &&
      directionConfirmed !== true &&
      !phase.startsWith("confirmed_")
    ) {
      return {
        tone: "warn",
        state: "pending",
        label: "等待共同交易日窗口",
        guidance:
          "所需窗口尚未到期；未来窗口不构成数据故障，也不能提前视为市场确认。",
      };
    }
    if (
      market.degraded === true ||
      ["degraded", "unavailable", "error", "failed"].includes(status)
    ) {
      const unsupportedBenchmark = marketReasonCount(market, "unsupported_benchmark");
      const requestFailed =
        marketReasonCount(market, "request_failed") +
        marketReasonCount(market, "provider_error");
      const followUpUnavailable =
        marketReasonCount(market, "follow_up") +
        marketReasonCount(market, "follow_up_unavailable") +
        marketReasonCount(market, "insufficient") +
        marketReasonCount(market, "insufficient_follow_up");
      const noRecords = marketReasonCount(market, "no_records");
      const label = unsupportedBenchmark
        ? "独立基准暂不支持"
        : requestFailed
          ? "行情获取失败"
          : followUpUnavailable
            ? "所需窗口数据不足"
            : noRecords
              ? "暂无事件级验证记录"
              : "所需行情或基准数据不可用";
      const guidance = unsupportedBenchmark
        ? "事件与方向均已具备，但缺少独立基准，本轮无法完成验证；保持降级核验。"
        : requestFailed
          ? "市场验证暂不可用：行情获取失败，系统将按采集计划重试；当前保持降级核验。"
          : followUpUnavailable
            ? "事件与方向均已具备，但所需共同交易日数据不足；当前保持降级核验。"
            : noRecords
              ? "暂无事件级验证记录；请核验事件锚点与采集状态，不能把缺失记录视为市场确认。"
              : "市场验证暂不可用：所需行情或基准数据缺失；仅可核验证据，等待数据链路补齐。";
      return {
        tone: "error",
        state: "data_failure",
        label,
        guidance,
      };
    }
    if (
      status === "complete" &&
      market.abstain === false &&
      market.veto === false &&
      directionConfirmed === true
    ) {
      return {
        tone: "ok",
        state: "confirmed",
        label: "已有同向市场样本",
        guidance:
          "已有同向市场样本；仍需人工确认仓位、风险预算与失效条件。",
      };
    }
    if (market.veto === true) {
      return {
        tone: "warn",
        state: "vetoed",
        label: "验证门禁未通过",
        guidance:
          "市场验证未通过候选门槛；先复核机制方向、相反证据和数据完整性。",
      };
    }
    const earlyConfirmation =
      directionConfirmed === true && phase.startsWith("confirmed_");
    return {
      tone: "warn",
      state: earlyConfirmation ? "preliminary" : "pending",
      label: earlyConfirmation ? "市场初步同向，等待所需窗口" : "市场待确认",
      guidance: earlyConfirmation
        ? "早期窗口初步同向，但所需期限尚未完成；当前只形成候选行动。"
        : "共同交易日窗口尚未完成；当前只形成影响假设，不能据此直接交易。",
    };
  }

  const DECISION_SNAPSHOT_MAX_AGE_SECONDS = 90 * 60;

  function decisionSnapshotFreshness(data) {
    const nestedSummary =
      data?.summary && typeof data.summary === "object" ? data.summary : null;
    const metadata = nestedSummary || data || {};
    const declaredStale = metadata.stale === true || data?.stale === true;
    const rawAge = metadata.age_seconds ?? data?.age_seconds;
    const parsedRawAge =
      rawAge === null || rawAge === "" || typeof rawAge === "boolean"
        ? Number.NaN
        : Number(rawAge);
    const rawAgeSeconds =
      Number.isFinite(parsedRawAge) && parsedRawAge >= 0
        ? parsedRawAge
        : Number.NaN;
    const generatedAt = metadata.generated_at || data?.generated_at;
    const generatedEpoch = new Date(String(generatedAt || "")).getTime();
    const computedAgeSeconds = Number.isFinite(generatedEpoch)
      ? (Date.now() - generatedEpoch) / 1000
      : Number.NaN;
    const generatedTooFarFuture =
      Number.isFinite(computedAgeSeconds) && computedAgeSeconds < -300;
    const liveAgeSeconds =
      Number.isFinite(computedAgeSeconds) && !generatedTooFarFuture
        ? Math.max(0, computedAgeSeconds)
        : Number.NaN;
    const ageSeconds = generatedTooFarFuture
      ? Number.NaN
      : Number.isFinite(rawAgeSeconds) && Number.isFinite(liveAgeSeconds)
        ? Math.max(rawAgeSeconds, liveAgeSeconds)
        : Number.isFinite(liveAgeSeconds)
          ? liveAgeSeconds
          : rawAgeSeconds;
    const verifiable = Number.isFinite(ageSeconds);
    const stale =
      declaredStale ||
      (verifiable && ageSeconds > DECISION_SNAPSHOT_MAX_AGE_SECONDS);
    return {
      stale,
      verifiable,
      ageSeconds: verifiable ? Math.max(0, ageSeconds) : null,
    };
  }

  function businessHealthSeverity(data) {
    const healthObjects = [
      data?.business_health,
      data?.decision_health,
      data?.health,
    ].filter(Boolean);
    let severity = "ok";
    const promote = (next) => {
      const rank = { ok: 0, warn: 1, error: 2 };
      if (rank[next] > rank[severity]) severity = next;
    };
    healthObjects.forEach((health) => {
      if (typeof health === "string") {
        const status = health.toLowerCase();
        promote(
          ["error", "failed", "unavailable"].includes(status)
            ? "error"
            : status === "degraded"
              ? "warn"
              : "ok"
        );
        return;
      }
      if (typeof health !== "object") return;
      const market = health.market_validation || health.market || {};
      const rootStatus = String(health.status || health.state || "").toLowerCase();
      const marketStatus = String(market.status || market.state || "").toLowerCase();
      const rawAvailableRecords = market.available_records;
      const availableRecords =
        rawAvailableRecords === null ||
        rawAvailableRecords === "" ||
        typeof rawAvailableRecords === "boolean"
          ? Number.NaN
          : Number(rawAvailableRecords);
      const declaredDegraded =
        health.degraded === true ||
        market.degraded === true ||
        rootStatus === "degraded" ||
        marketStatus === "degraded";
      if (
        [rootStatus, marketStatus].some((status) =>
          ["error", "failed", "unavailable"].includes(status)
        ) ||
        (declaredDegraded &&
          Number.isFinite(availableRecords) &&
          availableRecords <= 0)
      ) {
        promote("error");
      } else if (declaredDegraded) {
        promote("warn");
      }
    });
    return severity;
  }

  function declaredBusinessDegraded(data) {
    const severity = businessHealthSeverity(data);
    const declared = Boolean(
      data?.business_health || data?.decision_health || data?.health
    );
    return {
      declared,
      severity,
      partial: severity === "warn",
      unavailable: severity === "error",
    };
  }

  function decisionBoundaryState(data) {
    const cards = Array.isArray(data?.decisions) ? data.decisions : [];
    const marketStates = cards.map(marketStatusInfo);
    const allUnavailable =
      marketStates.length > 0 &&
      marketStates.every((item) => item.tone === "error");
    const anyUnavailable = marketStates.some((item) => item.tone === "error");
    const allPending =
      marketStates.length > 0 &&
      marketStates.every((item) => item.tone !== "ok");
    const allNotApplicable =
      marketStates.length > 0 &&
      marketStates.every((item) =>
        ["scenario_monitoring", "direction_missing"].includes(item.state)
      );
    const businessHealth = declaredBusinessDegraded(data);
    const businessUnavailable = businessHealth.unavailable;
    const businessPartiallyDegraded = businessHealth.partial;
    const fallbackAllUnavailable = allUnavailable && !businessHealth.declared;
    const macroLoadFailed = state.systemSignals.macro?.kind === "error";
    const snapshotFreshness = decisionSnapshotFreshness(data);
    const needsReview = cards.some((card) => card.human_review_required === true);
    const hasEvidence = cards.some((card) => Number(card.source_count || 0) > 0);
    const contraryMarket = marketStates.find((item) => item.state === "contrary");
    const inconclusiveMarket = marketStates.find(
      (item) => item.state === "inconclusive" || item.state === "vetoed"
    );
    const aggregateMarket =
      contraryMarket ||
      inconclusiveMarket ||
      marketStates.find((item) => item.tone === "error") ||
      marketStates.find((item) => item.tone === "warn") ||
      marketStates[0];

    if (snapshotFreshness.stale) {
      const hasMarketFailure = businessUnavailable || fallbackAllUnavailable;
      return {
        tone: hasMarketFailure ? "error" : "warn",
        evidenceTone: "warn",
        evidenceLabel: "决策快照延迟",
        marketLabel: aggregateMarket?.label || "市场状态待核验",
        guidance: hasMarketFailure
          ? "决策快照已超过 90 分钟且市场验证链路降级；只可核验历史证据，请刷新快照后再评估。"
          : "决策快照已超过 90 分钟；当前结论可能过期，请等待新快照后再评估候选行动。",
        systemKind: hasMarketFailure ? "error" : "warn",
        systemLabel: "决策快照延迟",
      };
    }

    if (businessUnavailable || fallbackAllUnavailable) {
      return {
        tone: "error",
        evidenceTone: hasEvidence ? "warn" : "error",
        evidenceLabel: hasEvidence ? "有来源，待人工核验" : "证据不足",
        marketLabel: "市场验证暂不可用",
        guidance:
          "仅可核验证据，不能据此确认交易动作。待行情验证恢复并补齐共同交易日样本后再评估。",
        systemKind: "error",
        systemLabel: "市场验证异常",
      };
    }
    if (!snapshotFreshness.verifiable) {
      return {
        tone: "warn",
        evidenceTone: "warn",
        evidenceLabel: "快照时间待核验",
        marketLabel: aggregateMarket?.label || "市场状态待核验",
        guidance:
          "决策快照缺少可核验的生成时间；当前只可检查证据，不能推进候选行动。",
        systemKind: "warn",
        systemLabel: "快照时间待核验",
      };
    }
    if (allNotApplicable) {
      const scenarioCount = marketStates.filter(
        (item) => item.state === "scenario_monitoring"
      ).length;
      const directionCount = marketStates.filter(
        (item) => item.state === "direction_missing"
      ).length;
      return {
        tone: "warn",
        evidenceTone: hasEvidence ? "warn" : "error",
        evidenceLabel: hasEvidence ? "有来源，待补验证条件" : "证据不足",
        marketLabel: "当前无适用事件窗口",
        guidance: `当前卡片中 ${scenarioCount} 项为宏观情景监控，${directionCount} 项需先明确机制方向；这不表示行情链路故障。`,
        systemKind: "warn",
        systemLabel: "事件窗口不适用",
      };
    }
    if (contraryMarket || inconclusiveMarket) {
      const marketState = contraryMarket || inconclusiveMarket;
      return {
        tone: "warn",
        evidenceTone: "warn",
        evidenceLabel: hasEvidence ? "待人工复核" : "证据不足",
        marketLabel: marketState.label,
        guidance: marketState.guidance,
        systemKind: "warn",
        systemLabel: contraryMarket ? "市场方向反向" : "市场方向待复核",
      };
    }
    if (macroLoadFailed || businessPartiallyDegraded) {
      return {
        tone: "warn",
        evidenceTone: "warn",
        evidenceLabel: macroLoadFailed ? "宏观快照刷新失败" : "市场验证部分降级",
        marketLabel:
          aggregateMarket?.label ||
          (businessPartiallyDegraded ? "部分市场验证可用" : "市场状态待核验"),
        guidance:
          macroLoadFailed && businessPartiallyDegraded
            ? "宏观快照本轮刷新失败，且部分市场验证不可用；继续显示上次成功决策，请重试后再评估。"
            : macroLoadFailed
              ? "宏观快照本轮刷新失败；继续显示上次成功决策，请重试宏观数据后再评估。"
              : "部分市场验证不可用，但仍有可用样本；相关候选保持降级并等待补齐。",
        systemKind: "warn",
        systemLabel: macroLoadFailed ? "宏观刷新失败" : "市场部分降级",
      };
    }
    if (!cards.length || anyUnavailable || allPending) {
      return {
        tone: "warn",
        evidenceTone: "warn",
        evidenceLabel: hasEvidence ? "待人工核验" : "等待可核验证据",
        marketLabel: anyUnavailable ? "部分验证不可用" : "市场尚未确认",
        guidance:
          "当前仅形成影响假设；先核验证据与相反证据，等待市场窗口完成后再考虑交易。",
        systemKind: "warn",
        systemLabel: anyUnavailable ? "市场部分异常" : "市场待确认",
      };
    }
    return {
      tone: needsReview ? "warn" : "ok",
      evidenceTone: needsReview ? "warn" : "ok",
      evidenceLabel: needsReview ? "待人工核验" : "证据已复核",
      marketLabel: "已有同向市场样本",
      guidance: needsReview
        ? "市场样本支持当前方向，但只形成候选行动，仍需人工确认风险预算与失效条件。"
        : "证据与市场样本已就绪；执行前仍需核对仓位、价格与风险预算。",
      systemKind: "ok",
      systemLabel: "验证链路正常",
    };
  }

  function classInfo(card) {
    return CLASS_CN[card.classification] || CLASS_CN.conflict;
  }

  function shortText(value, limit = 52) {
    const text = String(value || "").trim();
    return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
  }

  function fmtAbsoluteTime(value) {
    const date = new Date(String(value || ""));
    if (Number.isNaN(date.getTime())) return String(value || "时间未知");
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(date);
  }

  function setSystemStatus(kind, label, title) {
    const host = $("#system-status");
    const text = $("#system-status-label");
    if (!host || !text) return;
    host.classList.remove("is-unknown", "is-ok", "is-warn", "is-error");
    host.classList.add(`is-${kind}`);
    text.textContent = label;
    host.title = title;
  }

  function applySystemStatus() {
    const rank = { error: 3, warn: 2, ok: 1, unknown: 0 };
    const signals = Object.values(state.systemSignals).filter(Boolean);
    if (!signals.length) {
      setSystemStatus("unknown", "状态待确认", "正在确认业务数据链路");
      return;
    }
    const signal = signals.sort(
      (a, b) => (rank[b.kind] || 0) - (rank[a.kind] || 0)
    )[0];
    setSystemStatus(signal.kind, signal.label, signal.title);
  }

  function setSystemSignal(scope, kind, label, title) {
    state.systemSignals[scope] = { kind, label, title };
    applySystemStatus();
  }

  function updateMacroStatus(snapshot) {
    if (!snapshot?.available) {
      setSystemSignal(
        "macro",
        "warn",
        "等待快照",
        snapshot?.reason || "尚未采集到宏观快照"
      );
      return;
    }

    const createdAt = snapshot.created_at || snapshot.timestamp;
    const createdDate = new Date(String(createdAt || ""));
    if (Number.isNaN(createdDate.getTime())) {
      setSystemSignal(
        "macro",
        "warn",
        "时间待核验",
        "宏观快照未提供可核验的生成时间"
      );
      return;
    }

    const ageMs = Date.now() - createdDate.getTime();
    const coverage = snapshot.data_coverage;
    const coverageText =
      typeof coverage?.available === "number" &&
      typeof coverage?.total === "number" &&
      coverage.total > 0
        ? ` · 数据源 ${coverage.available}/${coverage.total}`
        : "";
    const title = `宏观快照 ${fmtAbsoluteTime(createdAt)}${coverageText}`;
    if (ageMs < -5 * 60_000 || ageMs > 150 * 60_000) {
      setSystemSignal("macro", "warn", "快照延迟", title);
      return;
    }
    setSystemSignal("macro", "ok", "快照正常", title);
  }

  function normalizedMacroTrend(items) {
    return (Array.isArray(items) ? items : [])
      .map((item, index) => {
        const score =
          typeof item?.composite_score === "number"
            ? item.composite_score
            : Number.NaN;
        const time = String(item?.created_at || "");
        const epoch = new Date(time).getTime();
        return {
          score,
          time,
          epoch: Number.isFinite(epoch) ? epoch : index,
          hasTime: Number.isFinite(epoch),
          level: item?.composite_level || "unknown",
        };
      })
      .filter((item) => Number.isFinite(item.score))
      .sort((a, b) => a.epoch - b.epoch);
  }

  function macroTrendSummary(items = state.macroHistory) {
    const points = normalizedMacroTrend(items);
    if (!points.length) return { points, current: null, anchor: null, delta: null };
    const current = points[points.length - 1];
    let anchor = points[0];
    if (points.length > 1 && current.hasTime) {
      const target = current.epoch - 24 * 60 * 60 * 1000;
      anchor = points.reduce((closest, point) =>
        Math.abs(point.epoch - target) < Math.abs(closest.epoch - target)
          ? point
          : closest
      );
    } else if (points.length > 1) {
      anchor = points[Math.max(0, points.length - 25)];
    }
    return {
      points,
      current,
      anchor: points.length > 1 ? anchor : null,
      delta: points.length > 1 ? current.score - anchor.score : null,
    };
  }

  function trendDirection(delta) {
    if (!Number.isFinite(delta) || Math.abs(delta) < 0.5) return "持平";
    return delta > 0 ? "上涨" : "回落";
  }

  function sparklineSVG(points, className = "trend-svg", width = 560, height = 132) {
    if (!points.length) return "";
    const padX = 8;
    const padY = 10;
    const scores = points.map((point) => point.score);
    const low = Math.min(...scores);
    const high = Math.max(...scores);
    const span = Math.max(1, high - low);
    const coords = points.map((point, index) => {
      const x =
        points.length === 1
          ? width / 2
          : padX + (index / (points.length - 1)) * (width - padX * 2);
      const y = padY + ((high - point.score) / span) * (height - padY * 2);
      return { x, y };
    });
    const line = coords.map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ");
    const area = [
      `${coords[0].x.toFixed(1)},${height - padY}`,
      line,
      `${coords[coords.length - 1].x.toFixed(1)},${height - padY}`,
    ].join(" ");
    const latest = coords[coords.length - 1];
    return `<svg class="${esc(className)}" viewBox="0 0 ${width} ${height}"
      role="img" aria-label="关注优先级最近 ${points.length} 份快照走势" preserveAspectRatio="none">
      <line class="trend-axis" x1="${padX}" y1="${height - padY}" x2="${
        width - padX
      }" y2="${height - padY}"></line>
      <polygon class="trend-area" points="${area}"></polygon>
      <polyline class="trend-line" points="${line}" fill="none"></polyline>
      <circle class="trend-dot" cx="${latest.x.toFixed(1)}" cy="${latest.y.toFixed(
        1
      )}" r="3.5"></circle>
    </svg>`;
  }

  function cardHasPortfolioMatch(card) {
    return Array.isArray(card?.matched_positions) && card.matched_positions.length > 0;
  }

  function decisionMatchesLens(card, lens = state.decisionLens) {
    if (lens === "candidate") return isCandidateAction(card);
    if (lens === "portfolio") return cardHasPortfolioMatch(card);
    if (lens === "watchlist") return isWatchedAsset(card?.asset_key);
    return true;
  }

  function decisionLensCards(data) {
    return orderedDecisions(data?.decisions || []).filter((card) =>
      decisionMatchesLens(card)
    );
  }

  function renderDecisionLenses(data) {
    const cards = Array.isArray(data?.decisions) ? data.decisions : [];
    const overview = data?.decision_overview || {};
    const portfolio = data?.portfolio_overview || {};
    const counts = {
      all: Number(data?.total_decisions ?? overview.total ?? cards.length) || 0,
      candidate:
        Number.isFinite(Number(overview.candidate))
          ? Number(overview.candidate)
          : cards.filter(isCandidateAction).length,
      portfolio:
        Number.isFinite(Number(overview.portfolio_matched))
          ? Number(overview.portfolio_matched)
          : Number.isFinite(Number(portfolio.impacted_asset_count))
            ? Number(portfolio.impacted_asset_count)
            : cards.filter(cardHasPortfolioMatch).length,
      watchlist: cards.filter((card) => isWatchedAsset(card.asset_key)).length,
    };
    $$("#decision-lenses [data-decision-lens]").forEach((button) => {
      const active = button.dataset.decisionLens === state.decisionLens;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
      button.disabled = state.decisionLensLoading;
    });
    Object.entries(counts).forEach(([lens, count]) => {
      const target = $(`[data-lens-count="${lens}"]`);
      if (target) target.textContent = String(Math.max(0, count));
    });
    const group = $("#decision-lenses");
    if (group) group.setAttribute("aria-busy", String(state.decisionLensLoading));
    const notes = {
      all: "私人直接命中与本机关注资产会优先排序。",
      candidate: "只显示达到候选门槛且仍待人工确认的卡片。",
      portfolio: state.authenticated
        ? "仅显示精确 asset_key 直接命中的持仓；ETF、行业与相关性等间接暴露尚未计算。"
        : "请先解锁私人模式，才能查看精确 asset_key 直接命中的持仓。",
      watchlist:
        "仅显示本机关注资产；只保存公开 asset_key，不上传持仓、账户或成本信息。",
    };
    let note = notes[state.decisionLens] || notes.all;
    if (state.decisionLensLoading) {
      note = `正在加载完整决策集，以应用“${DECISION_LENS_LABEL[state.decisionLens]}”视角…`;
    } else if (
      state.decisionLens !== "all" &&
      data?.summary === true &&
      state.fullDecisionLoadError
    ) {
      note += " 完整决策集加载失败，当前结果仅基于首屏摘要。";
    }
    const status = $("#decision-lens-status");
    if (status) status.textContent = note;
  }

  function privatePortfolioSummaryHTML(data) {
    if (!state.authenticated) {
      return `<div class="private-summary is-public">
        <span aria-hidden="true">🔒</span>
        <span>公共模式不加载持仓；本机关注仅保存公开资产代码。</span>
      </div>`;
    }
    const snapshot = data?.portfolio_snapshot || null;
    const portfolio = data?.portfolio_overview || {};
    const cards = Array.isArray(data?.decisions) ? data.decisions : [];
    const fallbackMatchedAssets = new Set(
      cards.filter(cardHasPortfolioMatch).map((card) => String(card.asset_key || ""))
    ).size;
    const positionCount = Number(portfolio.position_count ?? snapshot?.position_count ?? 0) || 0;
    const matchedPositionsKnown = Number.isFinite(
      Number(portfolio.matched_position_count)
    );
    const matchedPositions = matchedPositionsKnown
      ? Number(portfolio.matched_position_count)
      : 0;
    const matchedAssets = Number.isFinite(Number(portfolio.impacted_asset_count))
      ? Number(portfolio.impacted_asset_count)
      : fallbackMatchedAssets;
    const leveragedMatches = Number.isFinite(Number(portfolio.leveraged_match_count))
      ? Number(portfolio.leveraged_match_count)
      : cards.filter((card) => cardHasPortfolioMatch(card) && card.leverage_flag).length;
    const candidateMatches = Number(portfolio.candidate_matched_decisions || 0);
    const stalePositions = Number(portfolio.stale_position_count || 0);
    const snapshotStale = snapshot?.staleness?.is_stale === true;
    if (positionCount <= 0) {
      return `<div class="private-summary is-empty">
        <span aria-hidden="true">🔓</span>
        <strong>私人模式已开启 · 尚无持仓快照</strong>
        <span>继续显示公开信号与本机关注；这不代表组合无风险。</span>
      </div>`;
    }
    if (matchedAssets <= 0) {
      return `<div class="private-summary is-empty">
        <span aria-hidden="true">🔓</span>
        <strong>持仓 ${positionCount} 项 · 暂无直接命中</strong>
        <span>当前仅做精确 asset_key 匹配；间接暴露尚未计算，不代表组合无风险。</span>
        ${
          snapshotStale || stalePositions
            ? '<span class="status-badge warn">⚠ 持仓快照含过期记录</span>'
            : ""
        }
      </div>`;
    }
    return `<div class="private-summary is-private">
      <span aria-hidden="true">🔓</span>
      <strong>私人覆盖层已开启</strong>
      <span>持仓 ${positionCount} 项 · ${
        matchedPositionsKnown
          ? `直接命中 ${matchedPositions} 项 / ${matchedAssets} 个资产`
          : `直接命中 ${matchedAssets} 个资产`
      }</span>
      ${
        leveragedMatches
          ? `<span class="status-badge warn">⚠ 杠杆命中 ${leveragedMatches}</span>`
          : ""
      }
      ${
        candidateMatches
          ? `<span class="status-badge warn">候选命中 ${candidateMatches} · 待人工确认</span>`
          : ""
      }
      ${
        snapshotStale || stalePositions
          ? `<span class="status-badge warn">⚠ 过期 ${Math.max(stalePositions, 1)} 项，置信度已降级</span>`
          : ""
      }
      <span>仅直接匹配；间接暴露未计算。</span>
    </div>`;
  }

  function decisionRunwayHTML(data, lensCards) {
    const allCards = orderedDecisions(data?.decisions || []);
    const portfolio = data?.portfolio_overview || {};
    const marketHealth = data?.business_health?.market_validation || {};
    const applicableCount = Number(marketHealth.applicable_decisions);
    const coveredCount = Number(marketHealth.covered_decisions);
    const scenarioCount = Number(
      marketHealth.scenario_monitoring_decisions ??
        marketHealth.no_event_anchor_decisions
    );
    const directionCount = Number(marketHealth.direction_missing_decisions);
    const failureCount = Number(marketHealth.data_failure_decisions);
    const hasSemanticCoverage =
      Number(marketHealth.semantics_version) >= 2 &&
      Number.isFinite(applicableCount) &&
      Number.isFinite(coveredCount);
    const marketCoverageLabel = hasSemanticCoverage
      ? applicableCount > 0
        ? `市场样本 ${coveredCount}/${applicableCount}`
        : "无适用事件窗口"
      : "覆盖口径待更新";
    const marketCoverageDetail = hasSemanticCoverage
      ? `情景 ${Number.isFinite(scenarioCount) ? scenarioCount : 0} · 方向待补 ${
          Number.isFinite(directionCount) ? directionCount : 0
        } · 数据故障 ${Number.isFinite(failureCount) ? failureCount : 0}`
      : "逐卡核验证据与市场状态";
    const matchedAssets = Number(portfolio.impacted_asset_count) ||
      allCards.filter(cardHasPortfolioMatch).length;
    const watchedVisible = allCards.filter((card) => isWatchedAsset(card.asset_key)).length;
    const reviewCards = lensCards.length ? lensCards : allCards;
    const nextReview = reviewCards
      .map((card) => ({ card, review: decisionNextReview(card) }))
      .sort((a, b) => a.review.epoch - b.review.epoch)[0];
    const ownedLabel = state.authenticated
      ? matchedAssets
        ? `${matchedAssets} 个直接命中`
        : "暂无直接命中"
      : `${state.watchAssets.size} 个本机关注`;
    const ownedDetail = state.authenticated
      ? `${watchedVisible} 个关注资产在当前数据中 · 仅精确匹配`
      : "解锁后叠加私人持仓直接命中";
    return `<div class="decision-runway" aria-label="个性化核验跑道">
      <div class="runway-cell is-market">
        <small>验证结构</small><strong>${esc(marketCoverageLabel)}</strong>
        <span>${esc(marketCoverageDetail)}</span>
      </div>
      <div class="runway-cell">
        <small>我的资产</small><strong>${esc(ownedLabel)}</strong>
        <span>${esc(ownedDetail)}</span>
      </div>
      <div class="runway-cell">
        <small>下一复核</small><strong>${esc(nextReview?.review.label || "等待新证据")}</strong>
        <span>${esc(
          nextReview
            ? `${assetLabel(nextReview.card.asset_key)} · ${nextReview.review.detail}`
            : "当前视角暂无待复核卡片"
        )}</span>
      </div>
    </div>`;
  }

  function renderDecisionHero(data) {
    const cards = decisionLensCards(data);
    const counts = cards.reduce(
      (acc, card) => {
        acc[card.classification] = (acc[card.classification] || 0) + 1;
        return acc;
      },
      { risk: 0, opportunity: 0, conflict: 0 }
    );
    const candidateActions = cards.filter(isCandidateAction).length;
    const boundary = decisionBoundaryState(data);
    const coverageValues = cards
      .map((card) => card.score_components?.coverage)
      .filter((value) => typeof value === "number" && isFinite(value));
    const coverage = coverageValues.length
      ? Math.round(
          (coverageValues.reduce((sum, value) => sum + value, 0) /
            coverageValues.length) *
            100
        )
      : 0;
    const sortedTimes = cards
      .map((card) => card.data_as_of)
      .filter(Boolean)
      .sort();
    const latest = sortedTimes[sortedTimes.length - 1];
    const lead = cards[0];
    const macro = state.macroData?.available === false ? null : state.macroData;
    const composite = macro?.composite_risk || {};
    const riskScore = typeof composite.score === "number" && isFinite(composite.score)
      ? composite.score
      : null;
    const riskLevel = composite.level || "unknown";
    const trend = macroTrendSummary();
    const delta = trend.delta;
    const direction = trendDirection(delta);
    const directionLabel = Number.isFinite(delta) ? direction : "暂无可比";
    const deltaText = Number.isFinite(delta)
      ? `${delta > 0 ? "+" : ""}${delta.toFixed(0)} 分`
      : "历史积累中";
    const freshnessTime = macro?.created_at || macro?.timestamp || latest;
    const freshness = freshnessTime ? fmtTime(freshnessTime) : "等待首份快照";
    const narrative = Number.isFinite(delta)
      ? `综合风险关注优先级较约 24 小时前${direction === "持平" ? "基本持平" : `${direction} ${Math.abs(delta).toFixed(0)} 分`}。${
          lead
            ? `当前先核验“${shortText(lead.trigger || topicName(lead.topic_key), 34)}”及其资产传导。`
            : "当前没有进入重点队列的资产信号。"
        }`
      : lead
        ? "历史快照仍在积累，先按当前重点信号核验证据链与市场确认。"
        : "历史快照仍在积累，当前以观察和补充证据为主。";
    const privateSummary = privatePortfolioSummaryHTML(data);

    const leadAction = lead ? actionInfo(lead) : ACTION_CN.observe;
    const leadIsCandidate = Boolean(lead && isCandidateAction(lead));
    const leadNeedsReview = lead?.human_review_required === true;
    const transmission = lead
      ? `<div class="transmission-ribbon" aria-label="首要信号传导路径">
          <span class="transmission-node" title="${esc(lead.trigger || "待补充触发条件")}">
            <small>信号</small><strong>${esc(shortText(lead.trigger || leadAction.label, 38))}</strong>
          </span>
          <span class="transmission-arrow" aria-hidden="true">→</span>
          <span class="transmission-node" title="${esc(lead.topic_key || "")}">
            <small>主题</small><strong>${esc(topicName(lead.topic_key))}</strong>
          </span>
          <span class="transmission-arrow" aria-hidden="true">→</span>
          <span class="transmission-node" title="${esc(lead.asset_key || "")}">
            <small>资产</small><strong>${esc(assetLabel(lead.asset_key))}</strong>
          </span>
        </div>`
      : `<div class="transmission-ribbon is-empty">等待形成可核验的信号 → 主题 → 资产传导</div>`;

    $("#decision-hero").innerHTML = `
      <div class="situation-brief">
        <section class="brief-risk" style="--lvl:${LEVEL_COLOR[riskLevel] || LEVEL_COLOR.unknown}">
          <p class="brief-eyebrow">10 秒态势简报 · 关注优先级</p>
          <div class="brief-score-line">
            <strong class="brief-score">${riskScore === null ? "—" : esc(riskScore)}</strong>
            <span>/ 100</span>
            <span class="brief-level">综合风险 ${esc(LEVEL_CN[riskLevel] || riskLevel)}</span>
          </div>
          <div class="brief-delta">约 24 小时 ${esc(deltaText)} · ${esc(directionLabel)}</div>
          <div class="brief-sparkline">${
            trend.points.length
              ? sparklineSVG(trend.points, "brief-sparkline-svg", 260, 58)
              : '<span class="empty-hint">等待历史快照</span>'
          }</div>
          <div class="brief-freshness">数据新鲜度：${esc(freshness)}</div>
          <p class="brief-narrative">${esc(narrative)}</p>
        </section>
        <section class="brief-lead">
          <p class="brief-kicker">${esc(DECISION_LENS_LABEL[state.decisionLens] || "全部")}视角 · 当前首要传导 · ${
            leadNeedsReview
              ? "候选行动 · 待人工确认"
              : leadIsCandidate
                ? "候选行动"
                : "待验证影响假设"
          }</p>
          <h1 class="brief-title">${
            lead
              ? `${leadAction.icon} ${esc(
                  leadIsCandidate ? leadAction.label : "待验证影响假设"
                )} · ${esc(assetLabel(lead.asset_key))}`
              : "等待重点信号"
          }</h1>
          ${transmission}
          <div class="brief-statline">
            <span>风险 ${counts.risk}</span><span>机会 ${counts.opportunity}</span>
            <span>分歧 ${counts.conflict}</span><span>${
              candidateActions
                ? `候选行动 ${candidateActions} · 待人工确认`
                : "本轮暂无候选行动"
            }</span>
            <span>宏观覆盖 ${coverage}%</span>
          </div>
          <p class="brief-policy">${esc(
            data.evidence_policy ||
              "机制关系与统计伴随分开展示；数据不足、方向冲突或过期时保持观察 / 验证，所有结论需人工复核。"
          )}</p>
        </section>
      </div>
      <div class="decision-boundary-rail is-${esc(boundary.tone)}"
           role="status" aria-live="polite"
           aria-label="证据状态与市场状态">
        <div class="boundary-cell is-${esc(boundary.evidenceTone)}">
          <small>证据状态</small><strong>${esc(boundary.evidenceLabel)}</strong>
        </div>
        <div class="boundary-cell is-${
          boundary.tone === "error" ? "error" : boundary.tone === "warn" ? "warn" : "ok"
        }">
          <small>市场状态</small><strong>${esc(boundary.marketLabel)}</strong>
        </div>
        <p>${esc(boundary.guidance)}</p>
      </div>
      ${decisionRunwayHTML(data, cards)}
      ${privateSummary}`;
  }

  function orderedDecisions(cards) {
    const order = {
      candidate_reduce_or_hedge: 0,
      reduce_or_hedge: 0,
      candidate_scale_in: 1,
      scale_in: 1,
      verify: 2,
      observe: 3,
    };
    return cards.slice().sort((a, b) => {
      const stageRank =
        (order[a.action_stage] ?? 9) - (order[b.action_stage] ?? 9);
      if (stageRank) return stageRank;
      const portfolioRank = Number(!cardHasPortfolioMatch(a)) - Number(!cardHasPortfolioMatch(b));
      if (portfolioRank) return portfolioRank;
      const watchRank = Number(!isWatchedAsset(a.asset_key)) - Number(!isWatchedAsset(b.asset_key));
      if (watchRank) return watchRank;
      const leverageRank = Number(!a.leverage_flag) - Number(!b.leverage_flag);
      if (leverageRank) return leverageRank;
      const aReview = decisionNextReview(a).epoch;
      const bReview = decisionNextReview(b).epoch;
      if (aReview !== bReview) {
        if (!Number.isFinite(aReview)) return 1;
        if (!Number.isFinite(bReview)) return -1;
        return aReview - bReview;
      }
      return (
        (b.total_score || 0) - (a.total_score || 0) ||
        decisionKey(a).localeCompare(decisionKey(b))
      );
    });
  }

  function renderDecisionQueue(data) {
    const cards = decisionLensCards(data);
    const allTotal = Number(data.total_decisions) || (data.decisions || []).length;
    const total = state.decisionLens === "all" ? allTotal : cards.length;
    $("#decision-count").textContent =
      state.decisionLens === "all" ? String(total) : `${cards.length}/${allTotal}`;
    if (!cards.length) {
      const emptyByLens = {
        candidate: [
          "本轮暂无候选",
          "其余影响假设仍需补齐证据、方向或市场窗口。",
        ],
        portfolio: state.authenticated
          ? [
              "当前持仓暂无直接命中",
              "仅按精确 asset_key 匹配；间接暴露尚未计算，不代表组合无风险。",
            ]
          : ["私人视角尚未解锁", "使用顶部私人模式后查看直接持仓命中。"],
        watchlist: [
          "当前关注资产暂无命中",
          state.watchAssets.size
            ? "关注列表有资产，但当前决策集中尚无对应卡片。"
            : "打开任一资产证据链，使用“关注此资产”保存在本机。",
        ],
        all: ["尚未生成可展示的关联", "等待事件或宏观快照完成关系提取。"],
      };
      const [title, hint] = emptyByLens[state.decisionLens] || emptyByLens.all;
      $("#decision-queue").innerHTML = `<div class="empty decision-lens-empty">
        <span class="empty-icon">🧭</span>${esc(title)}
        <div class="empty-hint">${esc(hint)}</div>
      </div>`;
      return;
    }
    const visibleCards = state.decisionQueueExpanded ? cards : cards.slice(0, 10);
    const cardHTML = visibleCards
      .map((card) => {
        const action = actionInfo(card);
        const key = decisionKey(card);
        const marketInfo = marketStatusInfo(card);
        const evidenceInfo = evidenceStatusInfo(card);
        const nextReview = decisionNextReview(card);
        const nextReviewLabelHTML = nextReview.datetime
          ? `<time datetime="${esc(nextReview.datetime)}">${esc(nextReview.label)}</time>`
          : esc(nextReview.label);
        const watched = isWatchedAsset(card.asset_key);
        const privateBadge = card.matched_positions?.length
          ? `<span class="status-badge private">私 · 匹配 ${card.matched_positions.length}</span>`
          : "";
        return `<button class="decision-card ${
          key === state.selectedDecisionKey ? "is-selected" : ""
        } ${cardHasPortfolioMatch(card) ? "is-owned" : ""} ${
          watched ? "is-watched" : ""
        }" type="button" data-decision-key="${esc(key)}"
          aria-label="${esc(
            `${assetLabel(card.asset_key)}，${action.label}，${evidenceInfo.label}，${marketInfo.label}，下一复核 ${nextReview.label}`
          )}"
          style="--decision-color:${action.color}">
          <div class="decision-card-top">
            <span class="decision-action">${action.icon} ${action.label}</span>
            <span class="decision-asset" title="${esc(card.asset_key)}">${esc(
              assetLabel(card.asset_key)
            )}</span>
            <span class="decision-card-score">${Math.round((card.total_score || 0) * 100)} 分</span>
          </div>
          <div class="decision-topic">${esc(topicName(card.topic_key))}</div>
          <div class="decision-card-trigger">${esc(card.trigger || "")}</div>
          <div class="decision-card-boundary" aria-label="证据状态、市场状态与下一复核">
            <span class="is-${esc(evidenceInfo.tone)}">
              <small>证据状态</small><strong>${esc(evidenceInfo.label)}</strong>
            </span>
            <span class="is-${esc(marketInfo.tone)}">
              <small>市场状态</small><strong>${esc(marketInfo.label)}</strong>
            </span>
            <span class="is-review">
              <small>下一复核</small><strong>${nextReviewLabelHTML}</strong>
            </span>
          </div>
          <div class="decision-card-meta">
            <span>置信 ${confidencePct(card)}</span>
            <span>· ${esc(marketSourceScopeLabel(card))}</span>
            ${
              card.human_review_required
                ? '<span class="status-badge warn">候选行动 · 待人工确认</span>'
                : ""
            }
            ${card.leverage_flag ? '<span class="status-badge warn">⚠ 杠杆</span>' : ""}
            ${card.stale ? '<span class="status-badge warn">⚠ 数据过期</span>' : ""}
            ${watched ? '<span class="status-badge watch">★ 本机关注</span>' : ""}
            ${privateBadge}
          </div>
        </button>`;
      })
      .join("");
    const moreHTML =
      total > 10
        ? `<button class="decision-more" id="decision-show-all" type="button"
            aria-expanded="${state.decisionQueueExpanded}">
            ${state.decisionQueueExpanded ? "收起到重点信号" : `查看全部 ${total} 条`}
          </button>`
        : "";
    $("#decision-queue").innerHTML = cardHTML + moreHTML;
  }

  function renderDecisionMatrix(data) {
    const matrix = data.impact_matrix || {};
    const columns = matrix.columns || [];
    const rows = matrix.rows || [];
    const lensCards = decisionLensCards(data);
    const allowedKeys = new Set(lensCards.map(decisionKey));
    const lensColumns = columns.filter((asset) =>
      rows.some((row) => allowedKeys.has(`${row.topic_key}::${asset}`))
    );
    const lensRows = rows.filter((row) =>
      lensColumns.some((asset) => allowedKeys.has(`${row.topic_key}::${asset}`))
    );
    if (!lensColumns.length || !lensRows.length) {
      $("#decision-matrix").innerHTML = `<div class="empty">
        <span class="empty-icon">▦</span>当前视角暂无主题 × 资产矩阵
      </div>`;
      return;
    }
    const assetScores = lensCards.reduce((scores, card) => {
      const key = String(card.asset_key || "");
      scores[key] = Math.max(scores[key] || 0, Number(card.total_score) || 0);
      return scores;
    }, {});
    const portfolioAssets = new Set(
      lensCards.filter(cardHasPortfolioMatch).map((card) => String(card.asset_key || ""))
    );
    const orderedColumns = lensColumns
      .slice()
      .sort(
        (a, b) =>
          Number(!portfolioAssets.has(a)) - Number(!portfolioAssets.has(b)) ||
          Number(!isWatchedAsset(a)) - Number(!isWatchedAsset(b)) ||
          (assetScores[b] || 0) - (assetScores[a] || 0)
      );
    const visibleColumns = state.matrixExpanded
      ? orderedColumns
      : orderedColumns.slice(0, 8);
    const totalAssets =
      state.decisionLens === "all"
        ? Number(data.total_assets) || orderedColumns.length
        : orderedColumns.length;
    $("#decision-matrix").innerHTML = `<table class="impact-matrix">
      <thead><tr><th scope="col">主题</th>${visibleColumns
        .map(
          (asset) =>
            `<th scope="col" title="${esc(asset)}">${esc(assetLabel(asset))}</th>`
        )
        .join("")}</tr></thead>
      <tbody>${lensRows
        .map(
          (row) => `<tr>
            <th scope="row">${esc(topicName(row.topic_key))}</th>
            ${visibleColumns
              .map((asset) => {
                const index = columns.indexOf(asset);
                const key = `${row.topic_key}::${asset}`;
                const cell = allowedKeys.has(key) ? (row.cells || [])[index] : null;
                if (!cell) return '<td class="matrix-empty">·</td>';
                const info = CLASS_CN[cell.classification] || CLASS_CN.conflict;
                return `<td><button type="button" class="matrix-cell ${
                  key === state.selectedDecisionKey ? "is-selected" : ""
                }" data-decision-key="${esc(key)}" style="--cell-color:${info.color}"
                  aria-label="${esc(topicName(row.topic_key))} ${esc(assetLabel(asset))} ${
                    info.label
                  }">
                  <span class="matrix-symbol">${info.icon}</span>
                  <span>${info.label} · ${Math.round((cell.total_score || 0) * 100)}</span>
                </button></td>`;
              })
              .join("")}
          </tr>`
        )
        .join("")}</tbody>
    </table>${
        totalAssets > 8
          ? `<button class="matrix-more" id="matrix-show-all" type="button"
              aria-expanded="${state.matrixExpanded}">
              ${state.matrixExpanded ? "收起到重点资产" : `查看全部 ${totalAssets} 个资产`}
            </button>`
          : ""
      }`;
  }

  function distinctEvidence(card) {
    const seen = new Set();
    return (card.evidence || []).filter((item) => {
      const detail = item.detail || {};
      const key = [
        item.source_type,
        item.source_id,
        detail.title || detail.name || detail.item_id,
        item.direction,
      ].join("|");
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function renderDecisionDetail(card, policy) {
    if (!card) return;
    const action = actionInfo(card);
    const kind = classInfo(card);
    const evidence = distinctEvidence(card);
    const market = card.market_validation || {};
    const marketInfo = marketStatusInfo(card);
    const evidenceInfo = evidenceStatusInfo(card);
    const nextReview = decisionNextReview(card);
    const watched = isWatchedAsset(card.asset_key);
    const records = market.records || [];
    const positions = card.matched_positions || [];
    const sourceNodes = evidence.slice(0, 3).map((item) => {
      const detail = item.detail || {};
      return `<div class="chain-node"><small>${esc(item.source_type || "来源")}</small>
        <strong>${esc(detail.title || detail.name || item.source_id || "机制证据")}</strong></div>`;
    });
    if (evidence.length > 3) {
      sourceNodes.push(`<div class="chain-node"><small>更多来源</small><strong>+${evidence.length - 3}</strong></div>`);
    }
    const chain = [
      ...sourceNodes,
      '<span class="chain-arrow" aria-hidden="true">→</span>',
      `<div class="chain-node"><small>主题</small><strong>${esc(topicName(card.topic_key))}</strong></div>`,
      '<span class="chain-arrow" aria-hidden="true">→</span>',
      `<div class="chain-node" title="${esc(card.asset_key)}"><small>资产</small><strong>${esc(
        assetLabel(card.asset_key)
      )}</strong></div>`,
    ];
    if (positions.length) {
      chain.push(
        '<span class="chain-arrow" aria-hidden="true">→</span>',
        `<div class="chain-node"><small>私人持仓</small><strong>${positions.length} 个直接匹配${
          card.leverage_flag ? " · 含杠杆" : ""
        }</strong></div>`
      );
    }
    const evidenceRows = evidence.length
      ? evidence
          .map((item) => {
            const detail = item.detail || {};
            return `<li>
              ${esc(detail.title || detail.name || item.rationale || "规则关联")}
              <div class="evidence-source">${esc(item.source_type || "")} · ${esc(
                item.source_id || "来源未标识"
              )} · ${esc(item.direction || "neutral")}</div>
            </li>`;
          })
          .join("")
      : "<li>暂无可公开展示的机制摘录</li>";
    const noMarketRows =
      marketInfo.state === "scenario_monitoring"
        ? "宏观情景不适用事件后市场窗口；请改用触发指标监控。"
        : marketInfo.state === "direction_missing"
          ? "机制方向未明确，当前样本不能判断同向或反向。"
          : "暂无可用共同交易日样本。";
    const marketRows = records.length
      ? records
          .map(
            (row) => `<tr>
              <td>${esc(row.window || "—")}</td>
              <td>${pct(row.asset_return)}</td>
              <td>${pct(row.abnormal_return)}</td>
              <td>${
                row.direction_confirmed === true
                  ? "✓ 同向"
                  : row.direction_confirmed === false
                    ? "✕ 反向"
                    : "— 中性 / 不足"
              }</td>
              <td>${esc(row.sample_count ?? "—")}</td>
            </tr>`
          )
          .join("")
      : `<tr><td colspan="5">${esc(noMarketRows)}</td></tr>`;
    const positionSection = positions.length
      ? `<section class="evidence-section wide">
          <h3>私人持仓影响 ${
            card.stale ? '<span class="status-badge warn">⚠ 数据已降级</span>' : ""
          }</h3>
          <div class="impact-matrix-wrap"><table class="position-table">
            <thead><tr><th>账户</th><th>资产</th><th>数量</th><th>成本</th><th>日期</th></tr></thead>
            <tbody>${positions
              .map(
                (position) => `<tr>
                  <td>${esc(position.account)}</td><td title="${esc(position.asset_key)}">${esc(
                    assetLabel(position.asset_key)
                  )}</td>
                  <td>${esc(position.quantity)}</td><td>${esc(position.avg_cost ?? "—")} ${esc(
                    position.currency || ""
                  )}</td><td>${esc(position.as_of || "—")}</td>
                </tr>`
              )
              .join("")}</tbody>
          </table></div>
          ${
            card.estimated_exposure
              ? `<p class="block-sub">按最新可用行情估算敞口：${esc(
                  card.estimated_exposure.value
                )} ${esc(card.estimated_exposure.currency || "")}</p>`
              : '<p class="block-sub">行情不足，未估算当前敞口。</p>'
          }
        </section>`
      : "";
    const contraryRows = (card.contrary_evidence || []).length
      ? (card.contrary_evidence || [])
          .map(
            (item) =>
              `<li>${esc(item.detail || item.rationale || "存在方向相反的证据")}
                <div class="evidence-source">${esc(item.source_type || "")} ${esc(
                  item.source_id || ""
                )}</div></li>`
          )
          .join("")
      : "<li>当前未记录独立的相反证据；这不表示反例不存在。</li>";
    const nextReviewHTML = nextReview.datetime
      ? `<time datetime="${esc(nextReview.datetime)}">${esc(nextReview.label)}</time>`
      : `<span>${esc(nextReview.label)}</span>`;

    $("#decision-detail").innerHTML = `
      <div class="evidence-head">
        <div>
          <div class="decision-card-top">
            <span class="decision-action" style="--decision-color:${action.color}">${action.icon} ${
              action.label
            }</span>
            <span class="pill" style="--lvl:${kind.color}">${kind.icon} ${kind.label}</span>
            <span class="status-badge is-${esc(evidenceInfo.tone)}">${esc(
              evidenceInfo.label
            )}</span>
            <span class="status-badge">${esc(marketSourceScopeLabel(card))}</span>
            ${
              card.human_review_required
                ? '<span class="status-badge warn">候选行动 · 待人工确认</span>'
                : ""
            }
          </div>
          <h2 class="evidence-title" id="decision-detail-title" title="${esc(
            card.asset_key
          )}">${esc(assetLabel(card.asset_key))} · ${esc(topicName(card.topic_key))}</h2>
          <div class="evidence-subtitle">数据截至 ${esc(
            card.data_as_of ? fmtTime(card.data_as_of) : "未知"
          )} · 期限 ${esc(card.horizon || "未知")} · 下一复核 ${nextReviewHTML}</div>
        </div>
        <div class="evidence-head-actions">
          <button class="decision-watch-btn ${watched ? "is-watched" : ""}" type="button"
                  data-watch-asset="${esc(card.asset_key)}" aria-pressed="${String(watched)}"
                  title="仅公开 asset_key 保存在本机浏览器，不上传持仓信息">
            <span aria-hidden="true">${watched ? "★" : "☆"}</span>
            <span class="decision-watch-label">${
              watched ? "已关注 · 本机保存" : "关注此资产"
            }</span>
          </button>
          <div class="evidence-score"><strong>${Math.round(
            (card.total_score || 0) * 100
          )}</strong><span>决策分 · 置信 ${confidencePct(card)}</span></div>
        </div>
      </div>
      <div class="relation-chain">${chain.join("")}</div>
      <div class="evidence-grid">
        <section class="evidence-section">
          <h3>机制证据（不是因果证明）</h3>
          <ul class="evidence-list">${evidenceRows}</ul>
        </section>
        <section class="evidence-section">
          <h3>相反证据与不确定性</h3>
          <ul class="evidence-list">${contraryRows}</ul>
        </section>
        <section class="evidence-section wide">
          <h3>市场验证 · ${esc(marketInfo.label)}</h3>
          <p class="validation-boundary is-${esc(marketInfo.tone)}">${esc(
            marketInfo.guidance
          )}</p>
          <p class="decision-next-review">
            <strong>下一复核</strong>${nextReviewHTML}<span>${esc(nextReview.detail)}</span>
          </p>
          <div class="impact-matrix-wrap"><table class="validation-table">
            <thead><tr><th>窗口</th><th>资产收益</th><th>超额收益</th><th>方向</th><th>样本</th></tr></thead>
            <tbody>${marketRows}</tbody>
          </table></div>
          <p class="block-sub">${esc(market.note || "")}</p>
        </section>
        <section class="evidence-section">
          <h3>进入条件</h3><p class="block-sub">${esc(card.trigger || "待补充")}</p>
        </section>
        <section class="evidence-section">
          <h3>失效条件</h3><p class="block-sub">${esc(card.invalidation || "待补充")}</p>
        </section>
        ${positionSection}
      </div>
      <p class="decision-disclaimer">${esc(
        `${card.human_review_required ? "候选行动，待人工确认。" : ""}${
          policy || "统计相关不等于因果；所有行动建议均需人工复核。"
        }`
      )}</p>`;
  }

  function renderDecisionDetailConflict(key) {
    $("#decision-detail").innerHTML = `<div class="decision-detail-conflict" role="alert">
      <strong>证据链版本连续变化，已停止自动重试</strong>
      <p>系统已自动刷新一次；为避免循环请求，请人工重试当前选择。</p>
      <button type="button" data-decision-detail-retry="${esc(key)}">人工重试证据链</button>
    </div>`;
  }

  async function selectDecision(
    key,
    { focusDetail = false, conflictRetryCount = 0 } = {}
  ) {
    const cards = state.decisionData?.decisions || [];
    const card = cards.find((item) => decisionKey(item) === key);
    if (!card) return;
    state.selectedDecisionKey = key;
    $$(".decision-card, .matrix-cell").forEach((node) =>
      node.classList.toggle("is-selected", node.dataset.decisionKey === key)
    );
    const hasInlineDetail = Array.isArray(card.evidence) || Array.isArray(card.mechanism_relations);
    if (hasInlineDetail || !card.detail_available) {
      renderDecisionDetail(card, state.decisionData.evidence_policy);
    } else {
      const snapshotId = Number(state.decisionData?.snapshot_id || 0);
      const cacheKey = `${snapshotId}:${key}`;
      const cached = state.decisionDetailCache.get(cacheKey);
      if (cached) {
        renderDecisionDetail(cached.decision, cached.evidence_policy);
      } else {
        const generation = ++state.decisionDetailRequestGeneration;
        $("#decision-detail").innerHTML = `<div class="decision-detail-loading" aria-busy="true">
          <div class="skeleton skeleton-card"></div>
          <p class="empty-hint">正在按需加载这条信号的证据链…</p>
        </div>`;
        const params = new URLSearchParams({
          topic_key: String(card.topic_key || ""),
          asset_key: String(card.asset_key || ""),
        });
        if (snapshotId > 0) params.set("snapshot_id", String(snapshotId));
        const url = api(`api/decisions/detail?${params}`);
        try {
          const payload = await fetchJSON(url, 12000);
          if (
            generation !== state.decisionDetailRequestGeneration ||
            state.selectedDecisionKey !== key ||
            Number(state.decisionData?.snapshot_id || 0) !== snapshotId
          ) {
            return;
          }
          state.decisionDetailCache.set(cacheKey, payload);
          renderDecisionDetail(payload.decision, payload.evidence_policy);
        } catch (error) {
          if (generation !== state.decisionDetailRequestGeneration) return;
          if (error.status === 409) {
            if (conflictRetryCount >= 1) {
              renderDecisionDetailConflict(key);
              return;
            }
            const refreshed = await loadDecisions({ autoSelect: false });
            if (!refreshed || !state.selectedDecisionKey) {
              renderDecisionDetailConflict(key);
              return;
            }
            await selectDecision(state.selectedDecisionKey, {
              focusDetail,
              conflictRetryCount: 1,
            });
            return;
          }
          $("#decision-detail").innerHTML = errorHTML(error, url);
        }
      }
    }
    if (focusDetail) {
      const reduceMotion =
        window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches === true;
      $("#decision-detail").scrollIntoView({
        behavior: reduceMotion ? "auto" : "smooth",
        block: "start",
      });
    }
  }

  function renderDecisions(data, { autoSelect = true } = {}) {
    state.decisionData = data;
    const boundary = decisionBoundaryState(data);
    setSystemSignal(
      "decision",
      boundary.systemKind,
      boundary.systemLabel,
      boundary.guidance
    );
    const cards = decisionLensCards(data);
    if (!cards.some((card) => decisionKey(card) === state.selectedDecisionKey)) {
      state.selectedDecisionKey = cards.length ? decisionKey(cards[0]) : "";
    }
    renderDecisionLenses(data);
    renderDecisionHero(data);
    renderDecisionQueue(data);
    renderDecisionMatrix(data);
    if (state.selectedDecisionKey && autoSelect) {
      void selectDecision(state.selectedDecisionKey);
    } else if (!state.selectedDecisionKey) {
      $("#decision-detail").innerHTML = `<div class="empty">
        <span class="empty-icon">⛓</span>暂无可展开的证据链
      </div>`;
    }
    renderSupportCard("decision");
  }

  function clearDecisionView(message = "私人数据已从当前页面清除") {
    state.decisionDetailRequestGeneration += 1;
    state.decisionLensRequestGeneration += 1;
    state.decisionDetailCache.clear();
    state.decisionData = null;
    state.selectedDecisionKey = "";
    state.decisionLens = "all";
    state.decisionLensLoading = false;
    state.decisionQueueExpanded = false;
    state.matrixExpanded = false;
    $$("#decision-lenses [data-decision-lens]").forEach((button) => {
      const active = button.dataset.decisionLens === "all";
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
      button.disabled = false;
    });
    $$('[data-lens-count]').forEach((node) => {
      node.textContent = "—";
    });
    const lensStatus = $("#decision-lens-status");
    if (lensStatus) {
      lensStatus.textContent = "私人决策与组合命中已从当前页面清除。";
    }
    $("#decision-hero").innerHTML = `<div class="empty">${esc(message)}</div>`;
    $("#decision-queue").innerHTML = "";
    $("#decision-matrix").innerHTML = "";
    $("#decision-detail").innerHTML = `<div class="empty">
      <span class="empty-icon">⛓</span>${esc(message)}
    </div>`;
  }

  async function loadDecisions({ autoSelect = true } = {}) {
    const requestedPrivate = state.authenticated;
    const requestGeneration = ++state.decisionRequestGeneration;
    const preserveFullPublicContext =
      !requestedPrivate &&
      state.decisionData &&
      state.decisionData.summary !== true;
    const endpoint = requestedPrivate
      ? "api/private/decisions"
      : preserveFullPublicContext
        ? "api/decisions"
        : "api/decisions/summary";
    const url = api(endpoint);
    try {
      const data = requestedPrivate
        ? await requestJSON(url)
        : await fetchJSON(url);
      if (
        requestGeneration !== state.decisionRequestGeneration ||
        requestedPrivate !== state.authenticated
      ) {
        return false;
      }
      if (data?.summary === true) {
        state.decisionLens = "all";
        state.decisionQueueExpanded = false;
        state.matrixExpanded = false;
      }
      recordViewLastGoodDataAt("decision", data);
      clearViewLoadError("decision");
      renderDecisions(data || { decisions: [], impact_matrix: {} }, { autoSelect });
      return true;
    } catch (error) {
      if (requestGeneration !== state.decisionRequestGeneration) return false;
      if (requestedPrivate && [401, 503].includes(error.status)) {
        state.authenticated = false;
        clearDecisionView();
        updatePrivateModeButton();
        return loadDecisions({ autoSelect });
      }
      setViewLoadError("decision", error, url);
      setSystemSignal(
        "decision",
        "error",
        "决策链路异常",
        "重点信号刷新失败；继续显示上次成功数据并等待重试"
      );
      if (!state.decisionData) {
        $("#decision-hero").innerHTML = `<div class="empty">
          <span class="empty-icon">⚠️</span>今日态势暂时无法加载
          <div class="empty-hint">请使用上方“重试当前视图”重新请求</div>
        </div>`;
      }
      return false;
    }
  }

  async function loadFullDecisions() {
    if (!state.decisionData?.summary || state.authenticated) return true;
    if (state.fullDecisionLoadPromise) return state.fullDecisionLoadPromise;
    state.fullDecisionLoadError = null;
    const promise = (async () => {
      const requestGeneration = ++state.decisionRequestGeneration;
      const url = api("api/decisions");
      try {
        const data = await fetchJSON(url, 15000);
        if (requestGeneration !== state.decisionRequestGeneration || state.authenticated) {
          state.fullDecisionLoadError = new Error("决策状态已变化，请重新尝试");
          return false;
        }
        renderDecisions(data || { decisions: [], impact_matrix: {} });
        return true;
      } catch (error) {
        if (requestGeneration === state.decisionRequestGeneration) {
          state.fullDecisionLoadError = error;
        }
        return false;
      }
    })();
    state.fullDecisionLoadPromise = promise;
    try {
      return await promise;
    } finally {
      if (state.fullDecisionLoadPromise === promise) {
        state.fullDecisionLoadPromise = null;
      }
    }
  }

  function clearDecisionExpansionError(button) {
    button?.parentElement?.querySelector(".decision-inline-error")?.remove();
  }

  function showDecisionExpansionError(button, scope) {
    if (!button?.parentElement) return;
    clearDecisionExpansionError(button);
    const alert = document.createElement("div");
    alert.className = "decision-inline-error";
    alert.setAttribute("role", "alert");
    alert.textContent = `${scope}加载失败（${loadErrorCause(
      state.fullDecisionLoadError
    )}）。当前仍显示重点内容，可重试。`;
    button.parentElement.insertBefore(alert, button);
  }

  async function activateDecisionLens(lens, returnFocus) {
    if (!(lens in DECISION_LENS_LABEL) || state.decisionLensLoading) return;
    const generation = ++state.decisionLensRequestGeneration;
    state.decisionLens = lens;
    state.decisionQueueExpanded = false;
    state.matrixExpanded = false;
    const canResolveWithoutFull =
      lens === "all" ||
      (lens === "portfolio" && !state.authenticated) ||
      (lens === "watchlist" && state.watchAssets.size === 0);
    const needsFull =
      !canResolveWithoutFull &&
      state.decisionData?.summary === true &&
      !state.authenticated;
    let fullLoaded = false;
    if (needsFull) {
      state.decisionLensLoading = true;
      renderDecisionLenses(state.decisionData);
      fullLoaded = await loadFullDecisions();
      if (generation !== state.decisionLensRequestGeneration) return;
      state.decisionLensLoading = false;
    }
    if (state.decisionData) {
      if (needsFull && fullLoaded) renderDecisionLenses(state.decisionData);
      else renderDecisions(state.decisionData);
    }
    requestAnimationFrame(() => {
      const current = returnFocus?.isConnected
        ? returnFocus
        : $(`[data-decision-lens="${state.decisionLens}"]`);
      current?.focus();
    });
  }

  function updatePrivateModeButton() {
    const button = $("#private-mode-btn");
    const icon = button.querySelector("[aria-hidden]");
    button.classList.toggle("is-private", state.authenticated);
    button.disabled = !state.authConfigured && !state.logoutPending;
    icon.textContent = state.authenticated ? "🔓" : "🔒";
    $("#private-mode-label").textContent = state.logoutPending
      ? "服务端注销待重试"
      : state.authenticated
        ? "私人模式已开启"
        : "私人模式";
    button.title = state.logoutPending
      ? "私人数据已在本页隐藏；点击重试服务端注销"
      : !state.authConfigured
      ? "服务端尚未配置私人模式"
      : state.authenticated
        ? "点击锁定并清除私人会话"
        : "输入页面口令解锁";
  }

  async function loadAuthStatus() {
    try {
      const status = await requestJSON(api("api/auth/status"));
      state.authConfigured = Boolean(status?.configured);
      state.authenticated = Boolean(status?.authenticated);
      state.logoutPending = false;
    } catch (error) {
      state.authConfigured = false;
      state.authenticated = false;
    }
    state.authStatusLoaded = true;
    updatePrivateModeButton();
  }

  function openAuth() {
    const modal = $("#auth-modal");
    $("#auth-error").textContent = "";
    $("#auth-passcode").value = "";
    modal.hidden = false;
    document.body.style.overflow = "hidden";
    setTimeout(() => $("#auth-passcode").focus(), 0);
  }

  function closeAuth() {
    $("#auth-modal").hidden = true;
    document.body.style.overflow = "";
    $("#private-mode-btn").focus();
  }

  async function lockPrivateMode() {
    state.authenticated = false;
    state.logoutPending = true;
    state.decisionLensRequestGeneration += 1;
    state.decisionLensLoading = false;
    if (state.decisionLens === "portfolio") state.decisionLens = "all";
    state.decisionRequestGeneration += 1;
    clearDecisionView();
    updatePrivateModeButton();
    try {
      await requestJSON(api("api/auth/logout"), { method: "POST" });
      state.logoutPending = false;
      updatePrivateModeButton();
      await loadDecisions();
    } catch (error) {
      clearDecisionView(
        "私人数据已在本页隐藏；服务端注销失败，请点击顶部按钮重试"
      );
      updatePrivateModeButton();
    }
  }

  async function submitAuth(event) {
    event.preventDefault();
    const button = event.currentTarget.querySelector(".auth-submit");
    const passcode = $("#auth-passcode").value;
    button.disabled = true;
    $("#auth-error").textContent = "";
    try {
      await requestJSON(api("api/auth/login"), {
        method: "POST",
        body: JSON.stringify({ passcode }),
      });
      state.logoutPending = false;
      state.authenticated = true;
      state.selectedDecisionKey = "";
      updatePrivateModeButton();
      closeAuth();
      await loadDecisions();
    } catch (error) {
      $("#auth-error").textContent =
        error.status === 429
          ? "尝试次数过多，请稍后再试。"
          : error.status === 401
            ? "口令不正确。"
            : "私人模式暂时不可用。";
      $("#auth-passcode").select();
    } finally {
      button.disabled = false;
    }
  }

  // ─── Macro view ───────────────────────────

  function renderHero(d) {
    const cr = d.composite_risk || {};
    const level = cr.level || "unknown";
    const score = typeof cr.score === "number" ? cr.score : 0;
    const cov = d.data_coverage;

    const coverageSources = Array.isArray(cov?.sources)
      ? cov.sources.map((source) => {
          const dataStatus = String(source?.data_status || "").toLowerCase();
          const sourceStatus = String(source?.status || "").toLowerCase();
          const unavailable =
            !source?.available ||
            dataStatus === "unavailable" ||
            sourceStatus === "unavailable";
          const stale =
            !unavailable &&
            (source?.stale === true ||
              source?.is_stale === true ||
              dataStatus === "stale" ||
              dataStatus === "delayed" ||
              sourceStatus === "stale" ||
              sourceStatus === "delayed");
          return {
            source,
            state: unavailable ? "off" : stale ? "stale" : "ok",
            symbol: unavailable ? "○" : stale ? "◐" : "●",
            statusLabel: unavailable ? "不可用" : stale ? "数据延迟" : "",
          };
        })
      : [];

    const covPills = coverageSources
      .map(
        ({ source, state, symbol, statusLabel }) =>
          `<span class="cov-pill ${state}">${symbol} ${esc(source.label)}${
            statusLabel ? ` · ${statusLabel}` : ""
          }</span>`
      )
      .join("");

    const coverageWarnings = [];
    if (cov && cov.available < cov.total) {
      coverageWarnings.push(
        `${cov.total - cov.available} 个数据源当前不可用，相关分项以基线分计算，综合分可能偏低。`
      );
    }
    const staleCount = coverageSources.filter(
      ({ state: sourceState }) => sourceState === "stale"
    ).length;
    if (staleCount) {
      coverageWarnings.push(`${staleCount} 个数据源延迟，当前值仅供参考。`);
    }
    const covWarn = coverageWarnings.length
      ? `<div class="cov-warn">${coverageWarnings.join(" ")}</div>`
      : "";

    $("#macro-hero").innerHTML = `
      <div style="--lvl:${LEVEL_COLOR[level]}">
        <div class="hero-top">
          <div class="gauge">
            <span class="gauge-score">${score}</span>
            <span class="gauge-max">/ 100</span>
          </div>
          <div class="hero-meta">
            <div class="hero-label">综合风险 ${LEVEL_CN[level] || level}</div>
            <div class="hero-note">${esc(stripEmoji(cr.label))}</div>
            <div class="hero-time">快照时间 ${esc(d.timestamp || "")} · 每小时更新</div>
          </div>
        </div>
        <div class="hero-bar"><span style="width:${Math.max(2, Math.min(100, score))}%"></span></div>
        <div class="hero-scale"><span>0 平静</span><span>40 关注</span><span>55 警戒</span><span>70+ 警报</span></div>
        <div class="coverage">
          <span class="coverage-title">数据源 ${cov ? cov.available + "/" + cov.total : "—"}</span>
          ${covPills}
          ${covWarn}
        </div>
      </div>`;
    $("#macro-hero").style.setProperty("--lvl", LEVEL_COLOR[level]);
  }

  function renderMacroTrend(items) {
    const host = $("#macro-trend");
    const block = $("#macro-trend-block");
    if (!host || !block) return;
    let sourceItems = Array.isArray(items) ? items : [];
    const currentRisk = state.macroData?.composite_risk || {};
    if (
      !normalizedMacroTrend(sourceItems).length &&
      typeof currentRisk.score === "number" &&
      isFinite(currentRisk.score)
    ) {
      sourceItems = [
        {
          composite_score: currentRisk.score,
          composite_level: currentRisk.level,
          created_at: state.macroData?.created_at || state.macroData?.timestamp || "",
        },
      ];
    }
    const trend = macroTrendSummary(sourceItems);
    if (!trend.points.length) {
      host.innerHTML = `<div class="empty">
        <span class="empty-icon">⌁</span>历史快照仍在积累
        <div class="empty-hint">至少需要两份快照才能比较约 24 小时变化</div>
      </div>`;
      block.hidden = false;
      return;
    }

    const direction = trendDirection(trend.delta);
    const deltaClass =
      direction === "上涨" ? "up" : direction === "回落" ? "down" : "flat";
    const deltaValue = Number.isFinite(trend.delta)
      ? `${trend.delta > 0 ? "+" : ""}${trend.delta.toFixed(0)} 分`
      : "样本不足";
    const narrative = !Number.isFinite(trend.delta)
      ? "历史样本不足，暂不能计算约 24 小时变化。"
      : direction === "上涨"
        ? `关注优先级约 24 小时上涨 ${Math.abs(trend.delta).toFixed(0)} 分，新增信号需要优先核验。`
        : direction === "回落"
          ? `关注优先级约 24 小时回落 ${Math.abs(trend.delta).toFixed(0)} 分，压力有所缓和，但仍需检查分项与证据。`
          : "关注优先级与约 24 小时前基本持平，暂未出现明显级别切换。";
    const first = trend.points[0];
    const last = trend.current;
    host.innerHTML = `<div class="trend-card" style="--lvl:${
      LEVEL_COLOR[last.level] || LEVEL_COLOR.unknown
    }">
      <div class="trend-summary">
        <div class="trend-current">
          <span>当前关注分</span>
          <strong class="trend-value">${esc(last.score)}</strong>
          <small>/ 100</small>
        </div>
        <div class="trend-delta ${deltaClass}">
          <strong>约 24 小时 ${esc(deltaValue)}</strong>
          <span>${esc(narrative)}</span>
        </div>
      </div>
      <div class="trend-chart">${sparklineSVG(trend.points, "trend-svg")}</div>
      <div class="trend-range">
        <time datetime="${esc(first.time)}">起 ${esc(fmtAbsoluteTime(first.time))}</time>
        <span>${trend.points.length} 份快照</span>
        <time datetime="${esc(last.time)}">止 ${esc(fmtAbsoluteTime(last.time))}</time>
      </div>
    </div>`;
    block.hidden = false;
  }

  const SUBSCORE_NAMES = {
    recession: "衰退风险",
    market_stress: "市场压力",
    geopolitical: "地缘政治",
    china_risk: "中国系统性",
  };

  function renderSubscores(d) {
    const subs = d.sub_scores || {};
    const keys = Object.keys(SUBSCORE_NAMES).filter((k) => subs[k]);
    if (!keys.length) return;

    $("#macro-subscores").innerHTML = keys
      .map((k) => {
        const s = subs[k];
        const lvl = s.level || "unknown";
        const signals = (s.signals || []).length
          ? `<ul class="signal-list">${s.signals
              .map((x) => `<li>${esc(x)}</li>`)
              .join("")}</ul>`
          : `<div class="signal-empty">无触发信号</div>`;
        return `<div class="subscore" style="--lvl:${LEVEL_COLOR[lvl]}">
          <div class="subscore-head">
            <span class="subscore-name">${SUBSCORE_NAMES[k]}</span>
            <span class="subscore-score">${s.score}</span>
          </div>
          <div class="subscore-bar"><span style="width:${Math.max(2, Math.min(100, s.score))}%"></span></div>
          ${signals}
        </div>`;
      })
      .join("");
    $("#macro-subscores-block").hidden = false;
  }

  function metricCard(label, value, sub, cls = "") {
    if (value === null || value === undefined) {
      return `<div class="metric na">
        <div class="metric-label">${esc(label)}</div>
        <div class="metric-value">数据缺失</div>
        <div class="metric-sub">数据源不可用</div>
      </div>`;
    }
    return `<div class="metric ${cls}">
      <div class="metric-label">${esc(label)}</div>
      <div class="metric-value">${value}</div>
      ${sub ? `<div class="metric-sub">${sub}</div>` : ""}
    </div>`;
  }

  const chgHTML = (pct) => {
    if (typeof pct !== "number" || !isFinite(pct)) return "";
    const cls = pct > 0 ? "up" : pct < 0 ? "down" : "";
    return `<span class="${cls}">${pct > 0 ? "+" : ""}${pct.toFixed(2)}%</span>`;
  };

  const signedMetric = (value, digits = 2) =>
    typeof value === "number" && isFinite(value)
      ? `${value >= 0 ? "+" : ""}${value.toFixed(digits)}`
      : "—";

  function fmtMonthDay(value) {
    const raw = String(value || "").trim();
    const isoDate = raw.match(/^\d{4}-(\d{2})-(\d{2})/);
    if (isoDate) return `${isoDate[1]}-${isoDate[2]}`;
    const date = new Date(raw);
    if (Number.isNaN(date.getTime())) return "—";
    return `${String(date.getMonth() + 1).padStart(2, "0")}-${String(
      date.getDate()
    ).padStart(2, "0")}`;
  }

  function financialStressMetric(stress) {
    const fsi =
      typeof stress?.ofr_fsi === "number" && isFinite(stress.ofr_fsi)
        ? stress.ofr_fsi
        : null;
    const dataStatus = String(stress?.data_status || "").toLowerCase();
    const stale =
      stress?.stale === true ||
      stress?.is_stale === true ||
      dataStatus === "stale" ||
      dataStatus === "delayed";
    const statusLabel =
      {
        critical: "压力极高",
        elevated: "压力偏高",
        normal: "高于长期均值",
        low: "低于长期均值",
      }[String(stress?.status || "").toLowerCase()] || "";
    const sub = [
      statusLabel,
      `信用 ${signedMetric(stress?.credit)}`,
      `融资 ${signedMetric(stress?.funding)}`,
      `截至 ${fmtMonthDay(stress?.observed_at)}`,
      "OFR FSI",
      stale ? "数据延迟" : "",
    ]
      .filter(Boolean)
      .join(" · ");
    return metricCard(
      "全球金融压力",
      fsi === null ? null : signedMetric(fsi),
      sub,
      stale ? "is-stale" : ""
    );
  }

  function renderMetrics(d) {
    const md = d.market_data || {};
    const vix = md.vix || {};
    const yc = md.yield_curve || {};
    const tr = md.treasury || {};
    const fx = md.usd_cny || {};
    const go = md.gold_oil || {};
    const dxy = md.dxy || {};
    const cs = md.credit_spreads || {};
    const hasFinancialStress = Object.prototype.hasOwnProperty.call(
      md,
      "financial_stress"
    );
    const financialStress =
      md.financial_stress && typeof md.financial_stress === "object"
        ? md.financial_stress
        : {};

    const YC_CN = {
      deep_inverted: "深度倒挂 · 衰退信号",
      inverted: "倒挂 · 需警惕",
      flat: "平坦化 · 前景不明",
      normal: "正常陡峭",
      steep: "陡峭 · 增长预期",
    };

    const cards = [
      metricCard(
        "VIX 恐慌指数",
        num(vix.value),
        vix.status
          ? `${{ critical: "极度恐慌", elevated: "恐慌加剧", normal: "偏高", low: "平静" }[vix.status] || vix.status}${
              typeof vix.change === "number" ? " · " + (vix.change > 0 ? "+" : "") + vix.change : ""
            }`
          : ""
      ),
      metricCard(
        "2Y–10Y 利差",
        typeof yc.spread_2y10y === "number"
          ? `${yc.spread_2y10y > 0 ? "+" : ""}${yc.spread_2y10y.toFixed(2)}%`
          : null,
        YC_CN[yc.status] || ""
      ),
      metricCard(
        "10Y 美债收益率",
        num(tr["10Y"]) ? `${num(tr["10Y"])}%` : null,
        tr["2Y"] ? `2Y ${num(tr["2Y"])}% · 30Y ${num(tr["30Y"]) || "—"}%` : ""
      ),
      metricCard(
        "美元 / 人民币",
        num(fx.rate, 4),
        fx.rate
          ? `${fx.rate > 7.3 ? "破 7.3 警戒" : fx.rate > 7.2 ? "接近 7.3" : "区间内"} ${chgHTML(fx.change_pct)}`
          : ""
      ),
      metricCard(
        "黄金",
        go.gold && go.gold.price ? `$${go.gold.price.toLocaleString()}` : null,
        go.gold ? chgHTML(go.gold.change_pct) : ""
      ),
      metricCard(
        "原油 WTI",
        go.oil && go.oil.price ? `$${num(go.oil.price)}` : null,
        go.oil ? chgHTML(go.oil.change_pct) : ""
      ),
      metricCard("美元指数 DXY", num(dxy.value), dxy.value ? chgHTML(dxy.change_pct) : ""),
      hasFinancialStress
        ? financialStressMetric(financialStress)
        : metricCard(
            "高收益债利差",
            num(cs.hy_oas) ? `${num(cs.hy_oas)}bp` : null,
            cs.ig_oas ? `IG ${num(cs.ig_oas)}bp` : ""
          ),
    ];

    $("#macro-metrics").innerHTML = cards.join("");
    $("#macro-market-block").hidden = false;
  }

  function detailRow(key, val, cls = "") {
    if (!val) return "";
    return `<div class="detail-row">
      <span class="detail-key">${esc(key)}</span>
      <span class="detail-val ${cls}">${esc(val)}</span>
    </div>`;
  }

  // Tradeable symbols and sector themes are rendered as separate rows so a
  // reader can tell at a glance what is directly actionable.
  function assetTagRow(item, limit = 10) {
    const tickers = (item.tickers || []).slice(0, limit);
    const sectors = (item.sectors || []).slice(0, limit);
    if (!tickers.length && !sectors.length) return "";
    const group = (label, values, cls) =>
      values.length
        ? `<span class="tag-group">
            <span class="tag-group-label">${label}</span>
            ${values.map((v) => `<span class="tag ${cls}">${esc(v)}</span>`).join("")}
          </span>`
        : "";
    return `<div class="tagline tagline-split">
      ${group("标的", tickers, "ticker")}
      ${group("板块", sectors, "sector")}
    </div>`;
  }

  function renderSwans(d) {
    const list = d.black_swan_scenarios || [];
    if (!list.length) return;
    $("#macro-swans").innerHTML = list
      .map((s) => {
        const lvl = PROB_LEVEL[s.probability] || "medium";
        return `<details class="scenario" style="--lvl:${LEVEL_COLOR[lvl]}">
          <summary class="scenario-summary">
            <span class="scenario-chev">▶</span>
            <div class="scenario-main">
              <div class="scenario-title">
                ${esc(s.name)}
                <span class="pill">${PROB_CN[s.probability] || esc(s.probability)}</span>
                <span class="tag">冲击 ${IMPACT_CN[s.impact] || esc(s.impact)}</span>
                ${s.timeframe ? `<span class="tag">${esc(s.timeframe)}</span>` : ""}
              </div>
              <div class="scenario-desc">${esc(s.description)}</div>
              ${assetTagRow(s)}
            </div>
          </summary>
          <div class="scenario-detail">
            ${detailRow("触发条件", s.trigger)}
            ${detailRow("对冲方案", s.hedge, "hedge")}
          </div>
        </details>`;
      })
      .join("");
    $("#macro-swans-block").hidden = false;
  }

  function renderRhinos(d) {
    const list = d.gray_rhinos || [];
    if (!list.length) return;
    const order = { imminent: 0, approaching: 1, gradual: 2 };
    const sorted = list
      .slice()
      .sort((a, b) => (order[a.urgency] ?? 9) - (order[b.urgency] ?? 9));

    $("#macro-rhinos").innerHTML = sorted
      .map((r) => {
        const lvl = URGENCY_LEVEL[r.urgency] || "medium";
        return `<details class="scenario" style="--lvl:${LEVEL_COLOR[lvl]}">
          <summary class="scenario-summary">
            <span class="scenario-chev">▶</span>
            <div class="scenario-main">
              <div class="scenario-title">
                ${esc(r.name)}
                <span class="pill">${URGENCY_CN[r.urgency] || esc(r.urgency)}</span>
                ${
                  r.visibility
                    ? `<span class="tag">${VISIBILITY_CN[r.visibility] || esc(r.visibility)}</span>`
                    : ""
                }
              </div>
              <div class="scenario-desc">${esc(r.description)}</div>
              ${assetTagRow(r)}
            </div>
          </summary>
          <div class="scenario-detail">
            ${detailRow("引爆点", r.catalyst)}
            ${detailRow("市场影响", r.market_impact, "hedge")}
          </div>
        </details>`;
      })
      .join("");
    $("#macro-rhinos-block").hidden = false;
  }

  const EVENT_KIND_CN = { policy: "政策原文", indicator: "指标异动" };
  const EVENT_SEVERITY_CN = { high: "高", medium: "中", low: "低" };
  const EVENT_CATEGORY_CN = {
    fomc_statement: "FOMC 声明",
    fomc_minutes: "FOMC 纪要",
    fed_press_release: "美联储公告",
    fed_speech: "美联储讲话",
    pboc_announcement: "人民银行公告",
    policy_update: "政策更新",
  };

  function compactMacroSourceText(value, maximum = 360) {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    return text.length > maximum ? `${text.slice(0, maximum).trim()}…` : text;
  }

  function macroEventCopy(event) {
    const rawEnrichment = eventEnrichment(event);
    const enrichment = macroAiImpactEligible(event) ? rawEnrichment : null;
    const originalTitle = String(event?.title || "").trim() || "未命名事件";
    const officialSummary = hasOfficialBodyEvidence(event)
      ? compactMacroSourceText(event.content_excerpt)
      : "";
    const fallbackSummary =
      officialSummary ||
      compactMacroSourceText(event?.note || event?.snippet || "");
    const status = String(event?.ai_status || "pending").toLowerCase();
    const contentStatus = String(event?.content_status || "").toLowerCase();
    const sourceUnavailableCopy =
      contentStatus === "unsupported"
        ? "当前仅展示原始标题；来源暂不支持正文抓取。"
        : contentStatus === "unavailable"
          ? "当前仅展示原始标题；正文抓取暂不可用，系统将重试。"
          : "";
    return {
      enrichment,
      rawEnrichment,
      originalTitle,
      headline:
        String(enrichment?.headline_zh || "").trim() || originalTitle,
      summary:
        String(enrichment?.summary_zh || "").trim() ||
        fallbackSummary ||
        sourceUnavailableCopy ||
        (status === "failed"
          ? "AI 解读暂不可用，请结合原始标题与来源人工核对。"
          : status === "retry"
            ? "AI 解读将在稍后重试，当前仅展示采集到的原始信息。"
            : rawEnrichment
              ? "AI 提示的证据或置信度不足，主内容未采用；请回到原始来源核验。"
              : "AI 解读正在生成，当前仅展示采集到的原始信息。"),
    };
  }

  function hasSubstantiveMacroEvidence(enrichment) {
    return ["official_body", "indicator_data", "title_and_snippet", "post_text"].includes(
      String(enrichment?.evidence_basis || "").toLowerCase()
    );
  }

  function macroAiImpactEligible(event) {
    const enrichment = eventEnrichment(event);
    if (!enrichment || !hasSubstantiveMacroEvidence(enrichment)) return false;
    const confidence = Number(enrichment.confidence);
    return Number.isFinite(confidence) && confidence >= 0.65;
  }

  function macroEventImpact(event) {
    if (!macroAiImpactEligible(event)) return "";
    const enrichment = eventEnrichment(event);
    const aiImpact = String(enrichment?.impact_level || "").toLowerCase();
    return ["high", "medium", "low", "none"].includes(aiImpact)
      ? aiImpact
      : "";
  }

  function macroAiImpactBadge(event) {
    const impact = macroEventImpact(event);
    if (!impact) {
      const enrichment = eventEnrichment(event);
      const reason = enrichment && !hasSubstantiveMacroEvidence(enrichment)
        ? "AI 没有正文或指标证据，不能覆盖事件类别重要度"
        : enrichment
          ? "AI 置信度低于 65%，不能覆盖事件类别重要度"
          : "AI 核验尚未就绪";
      return `<span class="tag event-ai-impact is-unverified" title="${esc(reason)}">AI 核验影响 待核验</span>`;
    }
    const label = { high: "高", medium: "中", low: "低", none: "低相关" }[impact];
    return `<span class="tag event-ai-impact is-${esc(impact)}">AI 核验影响 ${esc(label)}</span>`;
  }

  function macroAiStateHTML(event) {
    const enrichment = eventEnrichment(event);
    if (enrichment && !macroAiImpactEligible(event)) {
      const label = isTitleOnlyEvidence(enrichment)
        ? "仅标题证据 · 待核验"
        : "低置信 AI · 待核验";
      return `<span class="ai-state is-limited" title="主标题、摘要和完整研判未采用此 AI 结果">${label}</span>`;
    }
    if (enrichment) {
      return `<span class="ai-state is-ready">AI 研判可用</span>`;
    }
    return aiStateHTML(event);
  }

  function hasOfficialBodyEvidence(event) {
    return (
      String(event?.content_status || "").toLowerCase() === "ready" &&
      Boolean(String(event?.content_excerpt || "").trim())
    );
  }

  function macroContentEvidenceStatus(event) {
    if (String(event?.kind || "").toLowerCase() === "indicator") {
      return `<span class="content-evidence-status is-indicator">指标数据证据</span>`;
    }
    if (hasOfficialBodyEvidence(event)) {
      return `<span class="content-evidence-status is-ready">官方正文已读取</span>`;
    }
    const contentStatus = String(event?.content_status || "").toLowerCase();
    if (contentStatus === "unsupported") {
      return `<span class="content-evidence-status is-missing is-unsupported">来源暂不支持正文抓取</span>`;
    }
    if (contentStatus === "unavailable") {
      return `<span class="content-evidence-status is-missing is-unavailable">正文抓取暂不可用，系统将重试</span>`;
    }
    return `<span class="content-evidence-status is-missing">系统尚未读取正文</span>`;
  }

  function renderOfficialEvidence(event) {
    if (!hasOfficialBodyEvidence(event)) return "";
    const excerpt = String(event.content_excerpt || "").trim();
    const sections = (Array.isArray(event.evidence_sections)
      ? event.evidence_sections
      : []
    )
      .filter((section) =>
        ["paragraph", "table_row"].includes(String(section?.kind || "")) &&
        String(section?.text || "").trim()
      )
      .slice(0, 8);
    const sourceUrl = safeExternalUrl(event.content_source_url || event.url);
    const evidenceHTML = sections.length
      ? `<div class="event-evidence-sections">${sections
          .map((section) => {
            const kind = String(section.kind);
            return `<p class="event-evidence-section is-${esc(kind)}">${esc(section.text)}</p>`;
          })
          .join("")}</div>`
      : `<blockquote>${esc(excerpt)}</blockquote>`;
    return `<details class="event-source-evidence">
      <summary>
        <span>展开官方正文摘录</span>
        <span class="event-source-evidence-meta">${sections.length ? `${sections.length} 条证据片段` : "正文证据"}</span>
      </summary>
      <div class="event-source-evidence-body">
        ${evidenceHTML}
        <p class="event-source-evidence-note">系统仅展示清洗后的官方正文摘录，不公开原始 HTML。</p>
        ${
          sourceUrl
            ? `<a href="${esc(sourceUrl)}" target="_blank" rel="noopener noreferrer">核对官方原文 ↗</a>`
            : ""
        }
      </div>
    </details>`;
  }

  function renderMacroAiDigest(list) {
    const events = Array.isArray(list) ? list : [];
    const statusCounts = events.reduce(
      (counts, event) => {
        const status = String(event?.ai_status || "pending").toLowerCase();
        if (status === "ready" && eventEnrichment(event)) counts.ready += 1;
        else if (status === "failed") counts.failed += 1;
        else if (status === "retry") counts.retry += 1;
        else counts.pending += 1;
        return counts;
      },
      { ready: 0, pending: 0, retry: 0, failed: 0 }
    );
    const impactRank = { high: 0, medium: 1 };
    const qualifiedCount = events.filter((event) =>
      macroAiImpactEligible(event)
    ).length;
    const highlights = events
      .filter((event) => {
        const impact = macroEventImpact(event);
        return macroAiImpactEligible(event) && (impact === "high" || impact === "medium");
      })
      .sort((a, b) => impactRank[macroEventImpact(a)] - impactRank[macroEventImpact(b)])
      .slice(0, 3);
    const limitedCount = events.filter((event) =>
      isTitleOnlyEvidence(eventEnrichment(event))
    ).length;
    const lowConfidenceCount = events.filter((event) => {
      const enrichment = eventEnrichment(event);
      const confidence = Number(enrichment?.confidence);
      return (
        enrichment &&
        hasSubstantiveMacroEvidence(enrichment) &&
        (!Number.isFinite(confidence) || confidence < 0.65)
      );
    }).length;
    const statusParts = [
      statusCounts.pending ? `待生成 ${statusCounts.pending}` : "",
      statusCounts.retry ? `待重试 ${statusCounts.retry}` : "",
      statusCounts.failed ? `不可用 ${statusCounts.failed}` : "",
      limitedCount ? `仅标题证据 ${limitedCount}` : "",
      lowConfidenceCount ? `低置信待核验 ${lowConfidenceCount}` : "",
    ].filter(Boolean);
    const highlightHTML = highlights.length
      ? `<ol class="macro-ai-points">
          ${highlights
            .map((event, index) => {
              const copy = macroEventCopy(event);
              const impact = macroEventImpact(event);
              return `<li>
                <span class="macro-ai-index" aria-hidden="true">${String(index + 1).padStart(2, "0")}</span>
                <div>
                  <div class="macro-ai-point-head">
                    <span class="macro-ai-impact is-${esc(impact)}">${impact === "high" ? "高影响" : "中影响"}</span>
                    <span class="macro-ai-evidence">${copy.enrichment?.evidence_basis === "official_body" ? "官方正文" : "非标题证据"}</span>
                  </div>
                  <strong>${esc(copy.headline)}</strong>
                  <p>${esc(copy.summary)}</p>
                </div>
              </li>`;
            })
            .join("")}
        </ol>`
      : `<p class="macro-ai-empty">${
          statusCounts.ready
            ? "暂无基于正文或指标、且置信度不低于 65% 的高或中影响要点。"
            : "AI 正在生成首批解读；事件原始信息仍可继续查看。"
        }</p>`;
    return `<section class="macro-ai-digest" role="status" aria-live="polite" aria-atomic="true" aria-labelledby="macro-ai-digest-title">
      <div class="macro-ai-digest-head">
        <div>
          <span class="macro-ai-eyebrow">72H / AI 研判</span>
          <h3 id="macro-ai-digest-title">AI 态势摘录</h3>
        </div>
        <div class="macro-ai-readiness" aria-label="AI 处理与证据资格">
          <div class="macro-ai-readiness-item is-qualified">
            <strong>${qualifiedCount}<span> / ${events.length}</span></strong>
            <small>可用于研判</small>
          </div>
          <div class="macro-ai-readiness-item">
            <strong>${statusCounts.ready}</strong>
            <small>处理完成</small>
          </div>
        </div>
      </div>
      ${highlightHTML}
      ${statusParts.length ? `<p class="macro-ai-status">${esc(statusParts.join(" · "))}</p>` : ""}
    </section>`;
  }

  function renderMacroAiAssets(event, enrichment) {
    const assets = (Array.isArray(enrichment?.assets) ? enrichment.assets : []).filter(
      (asset) => String(asset?.asset_key || "").trim()
    );
    if (!assets.length) {
      const ruleAssets = assetTagRow(event, 8);
      return ruleAssets || `<p class="event-insight-empty">尚未识别到可交易资产，不能据此判断事件没有市场影响。</p>`;
    }
    return `<ul class="event-ai-assets">
      ${assets
        .slice(0, 8)
        .map((asset) => {
          const key = String(asset?.asset_key || "").trim();
          if (!key) return "";
          const direction = String(asset?.direction || "unclear").toLowerCase();
          const horizon = String(asset?.horizon || "short").toLowerCase();
          return `<li data-direction="${esc(direction)}">
            <div class="event-ai-asset-head">
              <strong>${esc(asset?.name_zh || assetLabel(key))}</strong>
              <code>${esc(key)}</code>
              <span>${esc(DIRECTION_CN[direction] || direction)}</span>
            </div>
            ${asset?.reason_zh ? `<p>${esc(asset.reason_zh)}</p>` : ""}
            <small>${esc(HORIZON_CN[horizon] || horizon)}${
              confidenceLabel(asset?.confidence)
                ? ` · ${esc(confidenceLabel(asset.confidence))}`
                : ""
            }</small>
          </li>`;
        })
        .filter(Boolean)
        .join("")}
    </ul>`;
  }

  function renderMacroEventInsight(event, copy) {
    const enrichment = copy.enrichment;
    if (!enrichment) return "";
    const paths = Array.isArray(enrichment.impact_path)
      ? enrichment.impact_path.filter(Boolean).slice(0, 3)
      : [];
    const tags = Array.isArray(enrichment.tags)
      ? enrichment.tags.filter(Boolean).slice(0, 8)
      : [];
    const meta = [
      confidenceLabel(enrichment.confidence),
      enrichment.model ? `模型 ${enrichment.model}` : "",
      enrichment.generated_at ? `生成 ${fmtTime(enrichment.generated_at)}` : "",
    ].filter(Boolean);
    return `<details class="event-insight">
      <summary>
        <span>展开完整解读</span>
        <span class="event-insight-summary-meta">为何重要 · 传导路径 · 资产</span>
      </summary>
      <div class="event-insight-body">
        <section>
          <h4>为何重要</h4>
          <p>${esc(enrichment.why_it_matters_zh || "当前证据不足以形成进一步判断。")}</p>
        </section>
        <section>
          <h4>传导路径</h4>
          ${
            paths.length
              ? `<ol class="event-impact-path">${paths.map((path) => `<li>${esc(path)}</li>`).join("")}</ol>`
              : `<p class="event-insight-empty">尚未形成可复核的传导路径。</p>`
          }
        </section>
        <section>
          <h4>可能受影响的资产</h4>
          ${renderMacroAiAssets(event, enrichment)}
        </section>
        ${
          tags.length
            ? `<section><h4>标签</h4><div class="event-ai-tags">${tags
                .map((tag) => `<span class="tag">${esc(tag)}</span>`)
                .join("")}</div></section>`
            : ""
        }
        ${meta.length ? `<p class="event-ai-meta">${esc(meta.join(" · "))}</p>` : ""}
      </div>
    </details>`;
  }

  function renderMonitoredEvents(d) {
    const list = Array.isArray(d.monitored_events) ? d.monitored_events : [];
    $("#macro-events").innerHTML = list.length
      ? renderMacroAiDigest(list) + list
          .map((e, index) => {
            const lvl = LEVEL_COLOR[e.severity === "high" ? "high" : e.severity === "medium" ? "medium" : "low"];
            const verified = e.time_status === "verified" && e.published_at;
            const when = verified
              ? `<span class="event-time">${esc(fmtTime(e.published_at))}</span>`
              : `<span class="event-time unverified" title="来源未提供可验证的发布时间">时间待核验</span>`;
            const move =
              e.previous_value !== undefined && e.current_value !== undefined
                ? `<div class="event-move">${esc(String(e.previous_value))} → <strong>${esc(
                    String(e.current_value)
                  )}</strong></div>`
                : "";
            const externalUrl = safeExternalUrl(e.url);
            const copy = macroEventCopy(e);
            const titleId = `macro-event-title-${index}`;
            const ready = Boolean(copy.enrichment);
            const primaryTitle = !ready && externalUrl
              ? `<a href="${esc(externalUrl)}" target="_blank" rel="noopener noreferrer">${esc(copy.headline)}</a>`
              : esc(copy.headline);
            const originalTitle = ready
              ? `<p class="event-original"><span>原始标题</span>${
                  externalUrl
                    ? `<a href="${esc(externalUrl)}" target="_blank" rel="noopener noreferrer" aria-label="打开原始事件，新窗口">${esc(copy.originalTitle)} ↗</a>`
                    : `<span>${esc(copy.originalTitle)}</span>`
                }</p>`
              : "";
            const category = String(e.category || "").toLowerCase();
            return `<article class="event" style="--lvl:${lvl}" aria-labelledby="${titleId}">
              <div class="event-head">
                <span class="pill event-kind ${esc(e.kind)}">${EVENT_KIND_CN[e.kind] || esc(e.kind)}</span>
                ${category && EVENT_CATEGORY_CN[category] ? `<span class="tag event-category">${esc(EVENT_CATEGORY_CN[category])}</span>` : ""}
                <span class="tag event-category-impact">事件类别重要度 ${EVENT_SEVERITY_CN[e.severity] || esc(e.severity)}</span>
                ${macroAiImpactBadge(e)}
                ${macroContentEvidenceStatus(e)}
                ${macroAiStateHTML(e)}
                <span class="event-source">${esc(e.source || "")}</span>
                ${when}
              </div>
              <h3 class="event-title" id="${titleId}">${primaryTitle}</h3>
              <p class="event-summary ${ready ? "" : "is-degraded"}">${esc(copy.summary)}</p>
              ${originalTitle}
              ${move}
              ${renderOfficialEvidence(e)}
              ${renderMacroEventInsight(e, copy)}
            </article>`;
          })
          .join("")
      : `<div class="empty">
          <span class="empty-icon">🛰</span>近 72 小时内未监控到新的政策事件或指标异动
          <div class="empty-hint">指标异动需要与上一份快照对比才会出现</div>
        </div>`;
    $("#macro-events-block").hidden = false;
  }

  function renderOpps(d) {
    const list = d.opportunities || [];
    $("#macro-opps").innerHTML = list.length
      ? list
          .map(
            (o) => `<div class="opp">
              <div class="opp-head">
                <span class="opp-asset">${esc(o.asset)}</span>
                <span class="tag">${CONF_CN[o.confidence] || esc(o.confidence)}</span>
                ${o.timeframe ? `<span class="tag">${esc(o.timeframe)}</span>` : ""}
              </div>
              <div class="opp-signal">${esc(o.signal)}</div>
              ${o.risk ? `<div class="opp-foot">风险：${esc(o.risk)}</div>` : ""}
            </div>`
          )
          .join("")
      : `<div class="empty">
          <span class="empty-icon">🌤</span>当前无极端定价触发的机会信号
          <div class="empty-hint">VIX 平静、利差正常时属预期内</div>
        </div>`;
    $("#macro-opps-block").hidden = false;
  }

  function renderMacroView() {
    const d = state.macroData;
    if (!d || state.view !== "macro") return;
    if (!d.available) {
      $("#macro-hero").innerHTML = `<div class="empty">
        <span class="empty-icon">📡</span>${esc(d.reason || "暂无宏观快照")}
        <div class="empty-hint">首次采集约需 45 秒</div>
      </div>`;
    } else {
      renderHero(d);
      renderSubscores(d);
      renderMetrics(d);
      renderMonitoredEvents(d);
      renderSwans(d);
      renderRhinos(d);
      renderOpps(d);
      renderSupportCard("macro");
    }
    renderMacroTrend(state.macroHistory);
  }

  async function loadMacroHistory() {
    const generation = ++state.macroHistoryRequestGeneration;
    const historyUrl = api("api/macro/history?limit=72");
    try {
      const payload = await fetchJSON(historyUrl);
      if (generation !== state.macroHistoryRequestGeneration) return;
      state.macroHistory = Array.isArray(payload?.items) ? payload.items : [];
      if (state.view === "macro") renderMacroTrend(state.macroHistory);
      if (state.decisionData) renderDecisionHero(state.decisionData);
    } catch (error) {
      if (generation !== state.macroHistoryRequestGeneration) return;
      console.warn("macro history", error);
      if (state.view === "macro") renderMacroTrend(state.macroHistory);
    }
  }

  async function loadMacro({ includeHistory = state.view === "macro" } = {}) {
    const generation = ++state.macroRequestGeneration;
    const currentUrl = api("api/macro");
    const currentRequest = fetchJSON(currentUrl)
      .then((d) => {
        if (generation !== state.macroRequestGeneration) return false;
        state.macroData = d;
        recordViewLastGoodDataAt("macro", d);
        clearViewLoadError("macro");
        updateMacroStatus(d);
        renderMacroView();
        if (state.decisionData) renderDecisionHero(state.decisionData);
        return true;
      })
      .catch((error) => {
        if (generation !== state.macroRequestGeneration) return false;
        setViewLoadError("macro", error, currentUrl);
        setSystemSignal(
          "macro",
          "error",
          "快照异常",
          `最新宏观快照加载失败：${error.message || error}`
        );
        if (state.view === "macro" && !state.macroData) {
          $("#macro-hero").innerHTML = `<div class="empty">
            <span class="empty-icon">⚠️</span>宏观快照暂时无法加载
            <div class="empty-hint">请使用上方“重试当前视图”重新请求</div>
          </div>`;
        }
        if (state.decisionData) renderDecisionHero(state.decisionData);
        return false;
      });
    const requests = [currentRequest];
    if (includeHistory) requests.push(loadMacroHistory());
    const [currentResult] = await Promise.allSettled(requests);
    return currentResult.status === "fulfilled" && currentResult.value === true;
  }

  // ─── KOL view ─────────────────────────────

  async function loadStats() {
    const generation = ++state.statsRequestGeneration;
    try {
      const s = await fetchJSON(api(`api/stats?hours=${state.hours}`), 8000);
      if (generation !== state.statsRequestGeneration) return;
      state.stats = s;
      $("#stat-total").textContent = s.total;
      $("#stat-high").textContent = s.high;
      $("#stat-med").textContent = s.medium;
      $("#stat-kol").textContent = s.active_kols;
      $("#stat-market").textContent = s.with_market_kw;
      $("#tab-kol-count").textContent = s.high > 0 ? s.high : "";
      if ($("#feed")?.getAttribute("aria-busy") !== "true") updateFeedStatus();
    } catch (e) {
      console.warn("stats", e);
    }
  }

  async function loadKols() {
    const generation = ++state.kolsRequestGeneration;
    try {
      const list = await fetchJSON(api("api/kols"), 8000);
      if (generation !== state.kolsRequestGeneration) return;
      const host = $("#kol-chips");
      host.querySelectorAll("button[data-kol]:not([data-kol=''])").forEach((b) => b.remove());
      list.forEach((k) => {
        const b = document.createElement("button");
        b.className = "chip";
        b.dataset.kol = k.kol_key;
        b.setAttribute("aria-pressed", String(k.kol_key === state.kol));
        const label = esc(k.kol_name_cn || k.kol_name || k.kol_key);
        const badge = k.total_24h > 0 ? `<span class="chip-badge">${k.total_24h}</span>` : "";
        b.innerHTML = label + badge;
        if (k.kol_key === state.kol) b.classList.add("active");
        host.appendChild(b);
      });
      $("#footer-meta").textContent = `追踪 ${list.length} 位 KOL`;
      bindKolChips();
    } catch (e) {
      console.warn("kols", e);
    }
  }

  function sourceKind(source) {
    const value = String(source || "").trim();
    if (/^(?:truth social\b|x\s*@|twitter\s*@|serenity\b)/i.test(value)) {
      return { key: "is-direct", label: "本人动态" };
    }
    if (/(?:bing|baidu|google)\s+news|百度新闻|媒体|news\b/i.test(value)) {
      return { key: "is-media", label: "媒体提及" };
    }
    return { key: "is-unverified", label: "来源待核验" };
  }

  function eventIdentity(item) {
    return String(
      item?.canonical_url ||
        item?.source_url ||
        item?.url ||
        item?.id ||
        item?.title ||
        ""
    )
      .trim()
      .toLowerCase();
  }

  function eventExternalUrl(item, preferSighting = false) {
    const sightingFirst = preferSighting || Boolean(state.kol);
    const candidates = sightingFirst
      ? [item?.source_url, item?.canonical_url, item?.url]
      : [item?.canonical_url, item?.url, item?.source_url];
    for (const candidate of candidates) {
      const safe = safeExternalUrl(candidate);
      if (safe) return safe;
    }
    return "";
  }

  function mergePriorityEvents(priorityItems, regularItems) {
    const seen = new Set();
    return [...priorityItems, ...regularItems].filter((item) => {
      const key = eventIdentity(item);
      if (key && seen.has(key)) return false;
      if (key) seen.add(key);
      return true;
    });
  }

  function updateFeedStatus() {
    const host = $("#feed-status");
    if (!host) return;
    const parts = [
      state.feedClusteredCount > 0
        ? `已加载 ${state.feedLoadedCount} 条，按事件折叠为 ${state.feedVisibleCount} 张卡片`
        : `已加载 ${state.feedLoadedCount} 条`,
    ];
    if (state.feedClusteredCount > 0) {
      parts.push(`已折叠 ${state.feedClusteredCount} 条同簇报道，可逐簇展开`);
    }
    if (typeof state.stats?.total === "number") {
      parts.push(`当前窗口采集记录 ${state.stats.total} 条`);
    }
    const filtered = Boolean(
      state.impact || state.kol || state.q || state.timeStatus !== "verified"
    );
    if (filtered) parts.push("筛选后");
    if (state.feedHighPriority) parts.push("高影响已优先");
    if (state.feedRegularCapped) parts.push("普通流仅展示前150条");
    host.textContent = parts.join(" · ");
  }

  function eventBody(item) {
    const title = String(item?.title || "").trim();
    const snippet = String(item?.snippet || "").trim();
    const stem = title.replace(/[…\.]+$/, "").trim();
    if (snippet && stem && snippet.startsWith(stem)) {
      return { headline: snippet, snippet: "" };
    }
    return { headline: title, snippet };
  }

  function eventEnrichment(item) {
    return item?.ai_status === "ready" && item?.ai_enrichment
      ? item.ai_enrichment
      : null;
  }

  function isTitleOnlyEvidence(enrichment) {
    return ["title", "title_only"].includes(
      String(enrichment?.evidence_basis || "").toLowerCase()
    );
  }

  function aiStateHTML(item) {
    const enrichment = eventEnrichment(item);
    if (enrichment && isTitleOnlyEvidence(enrichment)) {
      return `<span class="ai-state is-limited" title="AI 只获得标题，未读取正文">仅标题证据</span>`;
    }
    const status = String(item?.ai_status || "pending").toLowerCase();
    if (status === "failed") {
      return `<span class="ai-state is-failed">AI 解读暂不可用</span>`;
    }
    if (!enrichment) {
      const label = {
        processing: "AI 解读生成中",
        retry: "AI 解读等待重试",
        pending: "AI 解读待就绪",
      }[status] || "AI 解读待就绪";
      return `<span class="ai-state is-pending">${label}</span>`;
    }
    return `<span class="ai-state is-ready">AI 研判</span>`;
  }

  function eventCopy(item) {
    const original = eventBody(item);
    const enrichment = eventEnrichment(item);
    const status = String(item?.ai_status || "pending").toLowerCase();
    if (enrichment) {
      return {
        headline:
          String(enrichment.headline_zh || "").trim() ||
          original.headline ||
          "未命名事件",
        summary:
          String(enrichment.summary_zh || "").trim() ||
          original.snippet ||
          "中文摘要暂不可用，请核对原文。",
        original,
        enrichment,
      };
    }
    return {
      headline: original.headline || "未命名事件",
      summary:
        original.snippet ||
        (status === "failed"
          ? "AI 解读暂不可用；请先核对原文与来源。"
          : "AI 解读尚未就绪；请先核对原文与来源。"),
      original,
      enrichment: null,
    };
  }

  function foldEventClusters(items) {
    const folded = [];
    const byCluster = new Map();
    items.forEach((item) => {
      const enrichment = eventEnrichment(item);
      const timeStatus = String(
        item?.time_status || (item?.published_at ? "verified" : "unknown")
      );
      const clusterKey =
        timeStatus === "verified"
          ? String(enrichment?.cluster_key || "").trim()
          : "";
      if (!clusterKey) {
        folded.push({ primary: item, relatedItems: [], relatedCount: 0 });
        return;
      }
      const existing = byCluster.get(clusterKey);
      if (existing) {
        existing.relatedItems.push(item);
        existing.relatedCount = existing.relatedItems.length;
        return;
      }
      const group = {
        primary: item,
        relatedItems: [],
        relatedCount: 0,
        clusterKey,
        isExpanded: false,
      };
      byCluster.set(clusterKey, group);
      folded.push(group);
    });

    const groups = [];
    let hiddenCount = 0;
    folded.forEach((group) => {
      group.isExpanded = Boolean(
        group.clusterKey && state.expandedClusters.has(group.clusterKey)
      );
      groups.push(group);
      if (group.isExpanded) {
        group.relatedItems.forEach((item) => {
          groups.push({ primary: item, relatedItems: [], relatedCount: 0 });
        });
      } else {
        hiddenCount += group.relatedCount;
      }
    });
    return { groups, hiddenCount };
  }

  function renderEvents(items) {
    state.feedItems = Array.isArray(items) ? items : [];
    if (!items.length) {
      state.feedVisibleCount = 0;
      state.feedClusteredCount = 0;
      $("#feed").innerHTML = `<div class="empty">
        <span class="empty-icon">📭</span>当前筛选条件下没有动态
        <div class="empty-hint">试试放宽时间窗口或影响等级</div>
      </div>`;
      return;
    }
    const LVL = { high: "var(--high)", medium: "var(--med)" };
    const LBL = {
      high: "高影响",
      medium: "中影响",
      low: "低影响",
      none: "低相关",
    };
    const clusterResult = foldEventClusters(items);
    const groups = clusterResult.groups;
    state.feedVisibleCount = groups.length;
    state.feedClusteredCount = clusterResult.hiddenCount;

    $("#feed").innerHTML = groups
      .map((group) => {
        const it = group.primary;
        const copy = eventCopy(it);
        const sourceNature = sourceKind(it.source);
        const lvl = LVL[it.impact] || "transparent";
        const pillLvl = LEVEL_COLOR[it.impact] || "var(--neutral)";
        const aiAssets = (copy.enrichment?.assets || [])
          .map((asset) => asset?.asset_key)
          .filter(Boolean);
        const assetKeys = Array.from(
          new Set(aiAssets.length ? aiAssets : it.tickers || [])
        ).slice(0, 6);
        const tickers = assetKeys
          .map((key) => {
            const raw = String(key || "");
            const label = raw.includes(":") ? assetTicker(raw) : `$${raw}`;
            return `<span class="tag ticker">${esc(label)}</span>`;
          })
          .join("");
        const heat =
          it.source_count > 1
            ? `<span class="heat">🔗 ${it.source_count} 条来源链接</span>`
            : "";
        const related = group.relatedCount
          ? `<button type="button" class="cluster-count"
               data-cluster-toggle="${esc(group.clusterKey)}"
               aria-expanded="${group.isExpanded ? "true" : "false"}">
               ${group.isExpanded ? "收起" : "展开"} ${group.relatedCount} 条同簇相关报道
             </button>`
          : "";
        const collectedAt = it.first_seen_at || it.fetched_at;
        const timeStatus =
          it.time_status || (it.published_at ? "verified" : "unknown");
        const publicationTime =
          timeStatus === "verified" && it.published_at
            ? publicationTimeView(it.published_at)
            : null;
        const timeTitle = [
          publicationTime
            ? publicationTime.accessible
            : it.published_at
              ? `发布时间：${it.published_at}`
              : "发布时间：未知",
          collectedAt
            ? `首次抓取：${fmtBeijingDateTime(collectedAt) || collectedAt}（北京时间）`
            : "",
        ]
          .filter(Boolean)
          .join(" · ");
        const eventTime =
          publicationTime
            ? publicationTime.visible
            : timeStatus === "future"
              ? collectedAt
                ? `发布时间异常 · 抓取 ${fmtTime(collectedAt)}`
                : "发布时间异常 · 抓取时间未知"
              : collectedAt
                ? `发布时间未知 · 抓取 ${fmtTime(collectedAt)}`
                : "发布时间与抓取时间均未知";
        const externalUrl = eventExternalUrl(it, Boolean(state.kol));
        const titleId = `event-card-title-${esc(it.id)}`;
        const original =
          copy.enrichment && copy.original.headline
            ? `<p class="card-original"><span>原文</span>${esc(copy.original.headline)}</p>`
            : "";
        return `<article class="card" style="--lvl:${lvl}" aria-labelledby="${titleId}">
          <div class="card-head">
            <span class="card-kol">${esc(it.kol_name_cn || it.kol_name)}</span>
            ${
              it.impact !== "low"
                ? `<span class="pill" style="--lvl:${pillLvl}">${
                    LBL[it.impact] || "影响待评估"
                  }</span>`
                : ""
            }
            ${aiStateHTML(it)}
            <span class="card-src">${esc(it.source || "")}</span>
            <span class="source-kind ${sourceNature.key}">${sourceNature.label}</span>
            ${
              publicationTime
                ? `<time class="card-time" datetime="${esc(
                    publicationTime.datetime
                  )}" title="${esc(timeTitle)}" aria-label="${esc(
                    publicationTime.accessible
                  )}">${esc(eventTime)}</time>`
                : `<span class="card-time is-unverified" title="${esc(
                    timeTitle
                  )}">${esc(eventTime)}</span>`
            }
          </div>
          <h2 class="card-title" id="${titleId}">${esc(copy.headline)}</h2>
          <p class="card-snippet">${esc(copy.summary)}</p>
          ${original}
          ${
            tickers || heat || related
              ? `<div class="card-foot">${tickers}${heat}${related}</div>`
              : ""
          }
          <div class="card-actions">
            <button type="button" class="card-evidence-btn" data-event-detail="${esc(
              it.id
            )}" data-event-kol="${esc(it.source_url ? it.kol_key || "" : "")}"
                    data-event-source-url="${esc(it.source_url || "")}"
                    aria-haspopup="dialog" aria-controls="intel-drawer"
                    aria-label="查看${esc(shortText(copy.headline, 36))}的证据链">
              查看证据链
            </button>
            ${
              externalUrl
                ? `<a class="card-original-link" href="${esc(
                    externalUrl
                  )}" target="_blank" rel="noopener noreferrer"
                     aria-label="打开${esc(shortText(copy.original.headline || copy.headline, 36))}原文，新窗口">原文 ↗</a>`
                : `<span class="card-link-unavailable">原文链接不可用</span>`
            }
          </div>
        </article>`;
      })
      .join("");
    renderSupportCard("kol");
  }

  const DIRECTION_CN = {
    positive: "正向",
    negative: "负向",
    mixed: "双向",
    unclear: "待核验",
    neutral: "中性",
  };
  const HORIZON_CN = {
    intraday: "日内",
    short: "短期",
    medium: "中期",
    long: "长期",
  };

  function confidenceLabel(value) {
    if (value === null || value === undefined || value === "") return "";
    const number = Number(value);
    return Number.isFinite(number) ? `置信 ${Math.round(number * 100)}%` : "";
  }

  function intelSection(index, title, content, extraClass = "") {
    return `<section class="intel-section ${extraClass}" aria-labelledby="intel-section-${index}">
      <div class="intel-spine-index" aria-hidden="true">${String(index).padStart(2, "0")}</div>
      <div class="intel-section-content">
        <h3 id="intel-section-${index}">${esc(title)}</h3>
        ${content}
      </div>
    </section>`;
  }

  function marketReactionHTML(reaction) {
    if (!reaction) return "";
    const abnormal =
      typeof reaction.abnormal_return === "number"
        ? `异常收益 ${pct(reaction.abnormal_return)}`
        : "";
    const confirmation =
      reaction.direction_confirmed === true
        ? "方向已确认"
        : reaction.direction_confirmed === false
          ? "方向未确认"
          : reaction.status === "complete"
            ? "方向不明确"
            : "样本尚不完整";
    const windowLabel = reaction.window ? String(reaction.window).toUpperCase() : "";
    return `<div class="market-check ${
      reaction.direction_confirmed === true ? "is-confirmed" : ""
    }">
      <span>市场核验</span>
      <strong>${esc(confirmation)}</strong>
      ${windowLabel ? `<span>${esc(windowLabel)}</span>` : ""}
      ${abnormal ? `<span>${esc(abnormal)}</span>` : ""}
      ${
        typeof reaction.sample_count === "number"
          ? `<span>${reaction.sample_count} 个样本</span>`
          : ""
      }
    </div>`;
  }

  function renderIntelAssets(event, enrichment, relations, reactions) {
    const aiAssets = Array.isArray(enrichment?.assets) ? enrichment.assets : [];
    const reactionMap = new Map();
    (Array.isArray(reactions) ? reactions : []).forEach((reaction) => {
      const key = String(reaction?.asset_key || "");
      if (key && !reactionMap.has(key)) reactionMap.set(key, reaction);
    });

    const assetCards = aiAssets
      .map((asset) => {
        const key = String(asset?.asset_key || "");
        if (!key) return "";
        const direction = String(asset.direction || "unclear").toLowerCase();
        const horizon = String(asset.horizon || "short").toLowerCase();
        return `<article class="intel-asset" data-direction="${esc(direction)}">
          <div class="intel-asset-head">
            <div>
              <strong>${esc(asset.name_zh || assetLabel(key))}</strong>
              <code>${esc(key)}</code>
            </div>
            <span class="direction-badge">${esc(DIRECTION_CN[direction] || direction)}</span>
          </div>
          <p>${esc(asset.reason_zh || "暂无补充理由，请核对传导路径。")}</p>
          <div class="intel-asset-meta">
            <span>${esc(HORIZON_CN[horizon] || horizon)}</span>
            ${
              confidenceLabel(asset.confidence)
                ? `<span>${esc(confidenceLabel(asset.confidence))}</span>`
                : ""
            }
          </div>
          ${marketReactionHTML(reactionMap.get(key))}
        </article>`;
      })
      .filter(Boolean)
      .join("");

    const relationRows = (Array.isArray(relations) ? relations : [])
      .map((relation) => {
        const key = String(relation?.asset_key || "");
        if (!key) return "";
        const direction = String(relation.direction || "unclear").toLowerCase();
        const horizon = String(relation.horizon || "short").toLowerCase();
        return `<article class="mechanism-row">
          <div class="mechanism-row-head">
            <strong>${esc(assetLabel(key))}</strong>
            <code>${esc(key)}</code>
            <span>${esc(DIRECTION_CN[direction] || direction)}</span>
            <span>${esc(HORIZON_CN[horizon] || horizon)}</span>
          </div>
          <p>${esc(relation.rationale || "规则识别到关联，具体机制仍需人工复核。")}</p>
          ${marketReactionHTML(reactionMap.get(key))}
        </article>`;
      })
      .filter(Boolean)
      .join("");

    const fallbackTickers = (event?.tickers || [])
      .map((ticker) => {
        const raw = String(ticker || "");
        const label = raw.includes(":") ? assetTicker(raw) : `$${raw}`;
        return `<span class="tag ticker">${esc(label)}</span>`;
      })
      .join("");
    if (!assetCards && !relationRows && !fallbackTickers) {
      return `<p class="intel-degraded">尚未识别到可交易资产；这不代表事件没有影响，只表示当前证据不足。</p>`;
    }
    return `${assetCards ? `<div class="intel-assets">${assetCards}</div>` : ""}
      ${
        fallbackTickers && !assetCards
          ? `<div class="intel-fallback-assets"><span>规则标的</span>${fallbackTickers}</div>`
          : ""
      }
      ${
        relationRows
          ? `<div class="mechanism-block">
              <h4>规则机制与市场核验</h4>
              <p class="mechanism-note">规则关联用于发现线索；市场相关不等于因果。</p>
              ${relationRows}
            </div>`
          : ""
      }`;
  }

  function renderIntelSources(event, sightings) {
    const originalTitle = String(event?.title || "").trim() || "原文标题不可用";
    const originalSnippet = String(event?.snippet || "").trim();
    const originalUrl = eventExternalUrl(event, Boolean(event?.source_url));
    const sourceItems = Array.isArray(sightings) ? sightings : [];
    const sources = sourceItems.length
      ? sourceItems
          .map((sighting) => {
            const url = eventExternalUrl(sighting, true);
            const sourceLabel = String(
              sighting.kol_name_cn ||
                sighting.kol_name ||
                sighting.source ||
                "未知来源"
            );
            const timeStatus = String(sighting.time_status || "unknown");
            const sourceNature = sourceKind(sighting.source);
            const publicationTime =
              timeStatus === "verified" && sighting.published_at
                ? publicationTimeView(sighting.published_at)
                : null;
            const when = publicationTime
                ? publicationTime.visible
                : sighting.first_seen_at
                  ? `抓取 ${fmtTime(sighting.first_seen_at)}`
                  : "时间待核验";
            return `<li class="source-record">
              <div>
                <strong>${esc(sourceLabel)}</strong>
                <span>${esc(sighting.source || "来源待核验")}</span>
              </div>
              <div class="source-record-meta">
                <span class="source-kind ${sourceNature.key}">${sourceNature.label}</span>
                ${
                  publicationTime
                    ? `<time datetime="${esc(
                        publicationTime.datetime
                      )}" title="${esc(
                        publicationTime.accessible
                      )}" aria-label="${esc(
                        publicationTime.accessible
                      )}">${esc(when)}</time>`
                    : `<span class="is-unverified">${esc(when)}</span>`
                }
                ${
                  sighting.source_count > 1
                    ? `<span>采集 ${sighting.source_count} 次</span>`
                    : ""
                }
              </div>
              ${
                url
                  ? `<a class="drawer-source-link" href="${esc(
                      url
                    )}" target="_blank" rel="noopener noreferrer"
                       aria-label="核对${esc(shortText(sourceLabel, 32))}来源，新窗口">核对来源 ↗</a>`
                  : `<span class="source-link-missing">链接不可用</span>`
              }
            </li>`;
          })
          .join("")
      : `<li class="source-record is-empty"><span>暂无独立来源记录，请直接核对原文。</span></li>`;

    return `<article class="original-document">
        <p class="original-document-label">抓取原文</p>
        <h4>${esc(originalTitle)}</h4>
        ${
          originalSnippet && originalSnippet !== originalTitle
            ? `<p>${esc(originalSnippet)}</p>`
            : ""
        }
        ${
          originalUrl
            ? `<a class="drawer-primary-link" href="${esc(
                originalUrl
              )}" target="_blank" rel="noopener noreferrer"
                 aria-label="打开当前事件原文，新窗口">打开原文 ↗</a>`
            : `<span class="source-link-missing">原文链接不可用</span>`
        }
      </article>
      <div class="source-ledger">
        <h4>来源记录</h4>
        <ul>${sources}</ul>
      </div>`;
  }

  function renderIntelRelated(related) {
    if (!Array.isArray(related) || !related.length) {
      return `<p class="intel-degraded">当前未发现同一事件簇的其他报道。</p>`;
    }
    return `<ul class="related-ledger">${related
      .map((item) => {
        const headline = String(item.headline_zh || item.title || "未命名报道").trim();
        const original = String(item.title || "").trim();
        const url = eventExternalUrl(item);
        const publicationTime = item.published_at
          ? publicationTimeView(item.published_at)
          : null;
        return `<li>
          <div class="related-copy">
            <strong>${esc(headline)}</strong>
            ${original && original !== headline ? `<span>原文：${esc(original)}</span>` : ""}
            <small>${esc(item.kol_name_cn || item.source || "未知来源")}${
              publicationTime ? ` · ${esc(publicationTime.visible)}` : ""
            }</small>
          </div>
          ${
            url
              ? `<a class="drawer-source-link" href="${esc(
                  url
                )}" target="_blank" rel="noopener noreferrer"
                   aria-label="查看${esc(shortText(headline, 32))}报道，新窗口">查看报道 ↗</a>`
              : `<span class="source-link-missing">链接不可用</span>`
          }
        </li>`;
      })
      .join("")}</ul>`;
  }

  function renderIntelDetail(payload) {
    const event = payload?.event || {};
    const enrichment = eventEnrichment(event);
    const copy = eventCopy(event);
    const relations = Array.isArray(payload?.relations) ? payload.relations : [];
    const reactions = Array.isArray(payload?.market_reactions)
      ? payload.market_reactions
      : [];
    const impact = String(enrichment?.impact_level || event.impact || "unknown");
    const impactLabel = {
      high: "高影响",
      medium: "中影响",
      low: "低影响",
      none: "低相关",
      unknown: "影响待评估",
    }[impact] || "影响待评估";
    const status = String(event.ai_status || "pending").toLowerCase();
    const caveat =
      enrichment && isTitleOnlyEvidence(enrichment)
        ? `<div class="evidence-limit" role="note">
            <strong>仅标题证据</strong>
            <span>AI 未读取正文；摘要与影响均为条件性释义，必须回到原文核验。</span>
          </div>`
        : "";
    const conclusion = `<div class="intel-conclusion-meta">
        <span class="impact-badge is-${esc(impact)}">${esc(impactLabel)}</span>
        ${aiStateHTML(event)}
        ${
          enrichment && confidenceLabel(enrichment.confidence)
            ? `<span>${esc(confidenceLabel(enrichment.confidence))}</span>`
            : ""
        }
      </div>
      <h2 class="intel-headline">${esc(copy.headline)}</h2>
      <p class="intel-summary">${esc(copy.summary)}</p>
      ${caveat}`;

    const why = enrichment?.why_it_matters_zh
      ? `<p class="intel-prose">${esc(enrichment.why_it_matters_zh)}</p>`
      : `<p class="intel-degraded">${
          status === "failed"
            ? "AI 解读暂不可用。先核对原文、来源和下方规则关联，避免从标题直接外推。"
            : "AI 解读尚未就绪。先核对原文、来源和下方规则关联，避免从标题直接外推。"
        }</p>`;

    const paths = Array.isArray(enrichment?.impact_path)
      ? enrichment.impact_path.filter(Boolean)
      : [];
    const pathHTML = paths.length
      ? `<ol class="impact-path-list">${paths
          .map((path) => `<li>${esc(path)}</li>`)
          .join("")}</ol>`
      : `<p class="intel-degraded">暂无 AI 传导路径；可先核对资产规则关联与来源证据。</p>`;

    const topicTags = Array.from(
      new Set([
        ...((enrichment?.tags || []).filter(Boolean)),
        ...relations.map((relation) => topicName(relation.topic_key)).filter(Boolean),
      ])
    );
    const tagHTML = topicTags.length
      ? `<div class="intel-tags">${topicTags
          .map((tag) => `<span>${esc(tag)}</span>`)
          .join("")}</div>`
      : `<p class="intel-degraded">暂无主题标签。</p>`;
    const auditParts = enrichment
      ? [
          enrichment.language ? `语言 ${enrichment.language}` : "",
          enrichment.model ? `模型 ${enrichment.model}` : "",
          enrichment.generated_at ? `生成 ${fmtAbsoluteTime(enrichment.generated_at)}` : "",
        ].filter(Boolean)
      : [];
    const auditHTML = auditParts.length
      ? `<p class="intel-audit">${auditParts.map(esc).join(" · ")}</p>`
      : "";

    $("#intel-drawer-body").innerHTML = [
      intelSection(1, "结论", conclusion, "intel-conclusion"),
      intelSection(2, "为何重要", why),
      intelSection(3, "影响路径", pathHTML),
      intelSection(
        4,
        "资产",
        renderIntelAssets(event, enrichment, relations, reactions)
      ),
      intelSection(5, "标签", tagHTML + auditHTML),
      intelSection(6, "原文 / 来源", renderIntelSources(event, payload?.sightings)),
      intelSection(7, "关联报道", renderIntelRelated(payload?.related)),
    ].join("");
  }

  function setDrawerBusy(busy, announcement) {
    const drawer = $("#intel-drawer");
    if (drawer) drawer.setAttribute("aria-busy", String(Boolean(busy)));
    const live = $("#intel-drawer-live");
    if (live) live.textContent = announcement || "";
  }

  async function loadIntelDetail(eventId) {
    const generation = ++state.drawerRequestGeneration;
    state.drawerAbortController?.abort();
    const requestController = new AbortController();
    state.drawerAbortController = requestController;
    const params = new URLSearchParams();
    if (state.drawerKol) params.set("kol", state.drawerKol);
    if (state.drawerSourceUrl) params.set("source_url", state.drawerSourceUrl);
    const query = params.toString();
    const url = `${api(`api/events/${encodeURIComponent(eventId)}`)}${
      query ? `?${query}` : ""
    }`;
    setDrawerBusy(true, "正在加载事件证据链");
    $("#intel-drawer-body").innerHTML = `<div class="intel-drawer-loading" role="presentation">
      <div class="skeleton skeleton-card"></div>
      <div class="skeleton skeleton-card"></div>
      <p>正在组装结论、来源与关联报道…</p>
    </div>`;
    try {
      const payload = await fetchJSON(url, 15000, {
        signal: requestController.signal,
      });
      if (
        generation !== state.drawerRequestGeneration ||
        $("#intel-drawer-shell").hidden
      ) {
        return;
      }
      renderIntelDetail(payload);
      setDrawerBusy(false, "事件证据链已加载");
    } catch (error) {
      if (
        generation !== state.drawerRequestGeneration ||
        $("#intel-drawer-shell").hidden
      ) {
        return;
      }
      if (error?.name === "AbortError") return;
      const message = /abort/i.test(error?.name || error?.message || "")
        ? "证据链请求超时"
        : `证据链加载失败：${error?.message || error}`;
      $("#intel-drawer-body").innerHTML = `<div class="intel-drawer-error" role="alert">
        <span aria-hidden="true">⚠</span>
        <h2>${esc(message)}</h2>
        <p>卡片中的原文链接仍可独立打开，也可以重新请求详情。</p>
        <button type="button" data-event-retry="${esc(eventId)}">重新加载证据链</button>
      </div>`;
      setDrawerBusy(false, message);
    } finally {
      if (state.drawerAbortController === requestController) {
        state.drawerAbortController = null;
      }
    }
  }

  function openIntelDrawer(eventId, trigger) {
    const shell = $("#intel-drawer-shell");
    const wasClosed = shell.hidden;
    state.drawerEventId = eventId;
    state.drawerKol = String(trigger?.dataset?.eventKol || "");
    state.drawerSourceUrl = String(trigger?.dataset?.eventSourceUrl || "");
    if (wasClosed) {
      state.drawerReturnFocus = trigger || document.activeElement;
      state.drawerPreviousOverflow = document.body.style.overflow;
      state.drawerInertNodes = Array.from(document.body.children).filter(
        (node) => node !== shell && !node.inert
      );
      state.drawerInertNodes.forEach((node) => (node.inert = true));
      document.body.classList.add("intel-drawer-open");
      document.body.style.overflow = "hidden";
      shell.hidden = false;
      requestAnimationFrame(() => {
        $("#intel-drawer .intel-drawer-close")?.focus({ preventScroll: true });
      });
    }
    loadIntelDetail(eventId);
  }

  function closeIntelDrawer() {
    const shell = $("#intel-drawer-shell");
    if (!shell || shell.hidden) return;
    state.drawerRequestGeneration += 1;
    state.drawerAbortController?.abort();
    state.drawerAbortController = null;
    shell.hidden = true;
    setDrawerBusy(false, "");
    document.body.classList.remove("intel-drawer-open");
    document.body.style.overflow = state.drawerPreviousOverflow;
    state.drawerInertNodes.forEach((node) => (node.inert = false));
    state.drawerInertNodes = [];
    const returnFocus = state.drawerReturnFocus;
    const closedEventId = state.drawerEventId;
    state.drawerReturnFocus = null;
    state.drawerEventId = null;
    state.drawerKol = "";
    state.drawerSourceUrl = "";
    const focusTarget = returnFocus?.isConnected
      ? returnFocus
      : document.querySelector(`[data-event-detail="${Number(closedEventId)}"]`) ||
        $("#tab-kol");
    if (focusTarget) {
      requestAnimationFrame(() => focusTarget.focus({ preventScroll: true }));
    }
  }

  function drawerFocusableElements() {
    return $$(
      "#intel-drawer a[href], #intel-drawer button:not([disabled]), " +
        "#intel-drawer input:not([disabled]), #intel-drawer [tabindex]:not([tabindex='-1'])"
    ).filter((element) => !element.hidden && element.offsetParent !== null);
  }

  function bindIntelDrawer() {
    $("#feed").addEventListener("click", (event) => {
      const clusterToggle = event.target.closest("[data-cluster-toggle]");
      if (clusterToggle) {
        const clusterKey = String(clusterToggle.dataset.clusterToggle || "");
        if (!clusterKey) return;
        if (state.expandedClusters.has(clusterKey)) {
          state.expandedClusters.delete(clusterKey);
        } else {
          state.expandedClusters.add(clusterKey);
        }
        renderEvents(state.feedItems);
        updateFeedStatus();
        requestAnimationFrame(() => {
          $$('[data-cluster-toggle]').find(
            (button) => button.dataset.clusterToggle === clusterKey
          )?.focus({ preventScroll: true });
        });
        return;
      }
      const trigger = event.target.closest("[data-event-detail]");
      if (!trigger) return;
      const eventId = Number(trigger.dataset.eventDetail);
      if (!Number.isInteger(eventId) || eventId < 1) return;
      openIntelDrawer(eventId, trigger);
    });
    $("#intel-drawer-shell").addEventListener("click", (event) => {
      if (event.target.closest("[data-intel-close]") || event.target.matches("[data-intel-backdrop]")) {
        closeIntelDrawer();
        return;
      }
      const retry = event.target.closest("[data-event-retry]");
      if (retry) loadIntelDetail(retry.dataset.eventRetry);
    });
    document.addEventListener("keydown", (event) => {
      const shell = $("#intel-drawer-shell");
      if (!shell || shell.hidden) return;
      if (event.key === "Escape") {
        event.preventDefault();
        closeIntelDrawer();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = drawerFocusableElements();
      if (!focusable.length) {
        event.preventDefault();
        $("#intel-drawer").focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || !$("#intel-drawer").contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (
        !event.shiftKey &&
        (document.activeElement === last ||
          !$("#intel-drawer").contains(document.activeElement))
      ) {
        event.preventDefault();
        first.focus();
      }
    });
  }

  async function loadEvents() {
    const generation = ++state.feedRequestGeneration;
    state.feedAbortController?.abort();
    const requestController = new AbortController();
    state.feedAbortController = requestController;
    const feed = $("#feed");
    feed.setAttribute("aria-busy", "true");
    $("#feed-status").textContent = "正在更新信号流…";
    const p = new URLSearchParams();
    if (state.hours) p.set("hours", state.hours);
    if (state.impact) p.set("impact", state.impact);
    if (state.kol) p.set("kol", state.kol);
    if (state.q) p.set("q", state.q);
    p.set("time_status", state.timeStatus);
    p.set("limit", "150");
    const url = api(`api/events?${p}`);
    try {
      let regularData;
      let priorityItems = [];
      let highPriorityLoaded = false;
      if (state.impact) {
        regularData = await fetchJSON(url, 12000, { signal: requestController.signal });
      } else {
        const highParams = new URLSearchParams(p);
        highParams.set("impact", "high");
        highParams.set("limit", "50");
        const highUrl = api(`api/events?${highParams}`);
        const [regularResult, highResult] = await Promise.allSettled([
          fetchJSON(url, 12000, { signal: requestController.signal }),
          fetchJSON(highUrl, 12000, { signal: requestController.signal }),
        ]);
        if (regularResult.status === "rejected") throw regularResult.reason;
        regularData = regularResult.value;
        if (highResult.status === "fulfilled") {
          priorityItems = highResult.value?.items || [];
          highPriorityLoaded = true;
        } else {
          console.warn("high impact feed", highResult.reason);
        }
      }
      const regularItems = regularData?.items || [];
      const items = highPriorityLoaded
        ? mergePriorityEvents(priorityItems, regularItems)
        : regularItems;
      if (generation !== state.feedRequestGeneration) return false;
      state.feedLoadedCount = items.length;
      state.feedHighPriority = highPriorityLoaded;
      state.feedRegularCapped = regularItems.length >= 150;
      recordViewLastGoodDataAt("kol", regularData);
      clearViewLoadError("kol");
      renderEvents(items);
      updateFeedStatus();
      return true;
    } catch (e) {
      if (generation !== state.feedRequestGeneration) return false;
      if (e?.name === "AbortError") return false;
      setViewLoadError("kol", e, url);
      if (!state.feedItems.length) {
        $("#feed").innerHTML = `<div class="empty">
          <span class="empty-icon">⚠️</span>信号流暂时无法加载
          <div class="empty-hint">请使用上方“重试当前视图”重新请求</div>
        </div>`;
      }
      const host = $("#feed-status");
      if (host) {
        host.textContent = state.feedItems.length
          ? "刷新失败 · 继续显示上次成功信号"
          : "动态加载失败";
      }
      return false;
    } finally {
      if (generation === state.feedRequestGeneration) {
        if (state.feedAbortController === requestController) {
          state.feedAbortController = null;
        }
        feed.setAttribute("aria-busy", "false");
      }
    }
  }

  // ─── Support / tipping ────────────────────
  // The ask is earned, not pushed: it renders at the end of a view that
  // actually loaded data, never on a blank or failed screen. Dismissing it
  // snoozes the inline card for a month; the footer entry always remains.

  const SNOOZE_KEY = "kol-support-snoozed-until";
  const SNOOZE_DAYS = 30;
  const NUDGE_KEY = "kol-support-nudged";
  const NUDGE_DELAY = 25_000;
  const supportSnoozed = () => {
    try {
      return Number(localStorage.getItem(SNOOZE_KEY) || 0) > Date.now();
    } catch (e) {
      return false;
    }
  };

  function snoozeSupport() {
    try {
      localStorage.setItem(
        SNOOZE_KEY,
        String(Date.now() + SNOOZE_DAYS * 86400_000)
      );
    } catch (e) {}
    $$("[data-support-slot]").forEach((s) => (s.innerHTML = ""));
  }

  function renderSupportCard(view) {
    const slot = document.querySelector(`#view-${view} [data-support-slot]`);
    if (!slot || supportSnoozed() || slot.dataset.rendered === "1") return;
    slot.dataset.rendered = "1";
    slot.innerHTML = `
      <aside class="support-card">
        <div class="support-card-head">
          <span aria-hidden="true">☕</span>
          <span class="support-card-title">这个面板是自费在跑的</span>
        </div>
        <p class="support-card-body">
          每 30 分钟扫一遍 18 位 KOL，每小时重算一次宏观风险快照，跑在一台自己掏钱的云服务器上。
          没有广告，不收集数据，也不打算做会员。如果它帮你提前避开过一次风险，或者看到过一个机会，
          可以请我喝杯咖啡。
        </p>
        <div class="support-card-actions">
          <button type="button" class="support-cta" data-support-open>请我喝杯咖啡</button>
          <button type="button" class="support-later" data-support-later>以后再说</button>
        </div>
      </aside>`;
  }

  // Fixed 24h window regardless of the feed's current filter.
  async function loadSupportFacts() {
    if (state.supportFactsLoaded) return;
    try {
      const s = state.stats && Number(state.stats.hours) === 24
        ? state.stats
        : await fetchJSON(api("api/stats?hours=24"), 8000);
      $("#fact-events").textContent = s.total;
      $("#fact-kols").textContent = s.active_kols;
      state.supportFactsLoaded = true;
    } catch (e) {
      $("#support-facts").hidden = true;
    }
  }

  function openSupport() {
    const m = $("#support-modal");
    m.hidden = false;
    m.querySelector(".support-close").focus();
    document.body.style.overflow = "hidden";
    void loadSupportFacts();
    // Once opened intentionally, this session no longer needs a visual nudge.
    try {
      sessionStorage.setItem(NUDGE_KEY, "1");
    } catch (e) {}
    $("#support-fab")?.classList.remove("attention");
  }

  function closeSupport() {
    $("#support-modal").hidden = true;
    document.body.style.overflow = "";
  }

  // Nudge once after the visitor has spent time reading and has scrolled.
  function scheduleNudge() {
    try {
      if (sessionStorage.getItem(NUDGE_KEY)) return;
    } catch (e) {}

    let engaged = false;
    const onScroll = () => {
      engaged = true;
      window.removeEventListener("scroll", onScroll);
    };
    window.addEventListener("scroll", onScroll, { passive: true });

    setTimeout(() => {
      window.removeEventListener("scroll", onScroll);
      const fab = $("#support-fab");
      if (!engaged || !fab || supportSnoozed() || !$("#support-modal").hidden) return;
      fab.classList.add("attention");
      setTimeout(() => fab.classList.remove("attention"), 2600);
      try {
        sessionStorage.setItem(NUDGE_KEY, "1");
      } catch (e) {}
    }, NUDGE_DELAY);
  }

  function bindSupport() {
    document.addEventListener("click", (e) => {
      if (e.target.closest("[data-support-open]")) openSupport();
      else if (e.target.closest("[data-support-close]")) closeSupport();
      else if (e.target.closest("[data-support-later]")) snoozeSupport();
      else if (e.target.id === "support-modal") closeSupport();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !$("#support-modal").hidden) closeSupport();
    });
    scheduleNudge();
  }

  // ─── Wiring ───────────────────────────────

  function setActive(nodes, node) {
    nodes.forEach((n) => {
      n.classList.remove("active");
      n.setAttribute("aria-pressed", "false");
    });
    node.classList.add("active");
    node.setAttribute("aria-pressed", "true");
  }

  function bindChips(sel, dataKey, stateKey, onChange) {
    $$(sel).forEach((btn) => {
      btn.setAttribute("aria-pressed", String(btn.classList.contains("active")));
      if (btn.__bound) return;
      btn.__bound = true;
      btn.addEventListener("click", () => {
        setActive($$(sel), btn);
        const raw = btn.dataset[dataKey] || "";
        state[stateKey] = dataKey === "hours" ? Number(raw) || 0 : raw;
        onChange();
      });
    });
  }

  const bindKolChips = () =>
    bindChips("#kol-chips button[data-kol]", "kol", "kol", loadEvents);

  function updateTimeWindowBasis() {
    $("#time-window-basis").textContent =
      state.timeStatus === "verified"
        ? "按发布时间筛选"
        : "隔离区按首次抓取时间筛选";
  }

  function bindViewRetries() {
    document.addEventListener("click", async (event) => {
      const retry = event.target.closest("[data-view-retry]");
      if (!retry) return;
      const view = retry.dataset.viewRetry;
      if (!view || retry.disabled) return;
      retry.disabled = true;
      retry.textContent = "正在重试…";
      try {
        await ensureViewLoaded(view, { force: true });
      } finally {
        if (retry.isConnected) {
          retry.disabled = false;
          retry.textContent = "重试当前视图";
        }
      }
    });
  }

  const VIEW_REFRESH_MS = 300_000;

  function scheduleIdle(task) {
    if ("requestIdleCallback" in window) {
      window.requestIdleCallback(task, { timeout: 2000 });
    } else {
      setTimeout(task, 0);
    }
  }

  async function ensureViewLoaded(view, { force = false } = {}) {
    const loadedAt = Number(state.viewLoadedAt[view] || 0);
    if (!force && loadedAt && Date.now() - loadedAt < VIEW_REFRESH_MS) {
      if (view === "macro") renderMacroView();
      return true;
    }
    if (!force && state.viewLoadPromises[view]) {
      return state.viewLoadPromises[view];
    }
    const promise = (async () => {
      let criticalSucceeded = false;
      if (view === "decision") {
        const macroPromise = loadMacro({ includeHistory: false });
        if (!state.authStatusLoaded) await loadAuthStatus();
        const [decisionResult] = await Promise.allSettled([
          loadDecisions(),
          macroPromise,
        ]);
        criticalSucceeded =
          decisionResult.status === "fulfilled" &&
          decisionResult.value === true;
        if (!state.macroHistory.length) scheduleIdle(() => void loadMacroHistory());
      } else if (view === "macro") {
        const [macroResult] = await Promise.allSettled([
          loadMacro({ includeHistory: true }),
        ]);
        criticalSucceeded =
          macroResult.status === "fulfilled" && macroResult.value === true;
      } else if (view === "kol") {
        const [, , eventsResult] = await Promise.allSettled([
          loadStats(),
          loadKols(),
          loadEvents(),
        ]);
        criticalSucceeded =
          eventsResult.status === "fulfilled" && eventsResult.value === true;
      }
      if (criticalSucceeded) {
        const loadedNow = Date.now();
        state.viewLoadedAt[view] = loadedNow;
        state.viewLastGoodAt[view] = loadedNow;
        updateRefreshTime();
      } else {
        state.viewLoadedAt[view] = 0;
        renderViewLoadState(view);
      }
      return criticalSucceeded;
    })();
    state.viewLoadPromises[view] = promise;
    try {
      return await promise;
    } finally {
      if (state.viewLoadPromises[view] === promise) {
        delete state.viewLoadPromises[view];
      }
    }
  }

  function switchView(view, { load = true } = {}) {
    if (!["decision", "macro", "kol"].includes(view)) return;
    if (state.view === "kol" && view !== "kol") {
      state.feedAbortController?.abort();
    }
    state.view = view;
    $$("#tabs .tab").forEach((t) => {
      const on = t.dataset.view === view;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
    });
    $$(".view").forEach((node) =>
      node.classList.toggle("active", node.id === `view-${view}`)
    );
    try {
      history.replaceState(null, "", `#${view}`);
    } catch (e) {}
    if (view === "macro") renderMacroView();
    if (load) void ensureViewLoaded(view);
  }

  function updateRefreshTime() {
    const t = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    $("#last-update").textContent = `${pad(t.getHours())}:${pad(t.getMinutes())}`;
  }

  async function refreshCurrentView({ showSpinner = false } = {}) {
    const button = $("#refresh-btn");
    if (showSpinner) button.classList.add("spinning");
    try {
      await ensureViewLoaded(state.view, { force: true });
    } finally {
      if (showSpinner) button.classList.remove("spinning");
    }
  }

  function scheduleRefresh() {
    clearTimeout(state.refreshTimer);
    state.refreshTimer = null;
    if (document.hidden) return;
    state.refreshTimer = setTimeout(async () => {
      await refreshCurrentView();
      scheduleRefresh();
    }, VIEW_REFRESH_MS);
  }

  document.addEventListener("DOMContentLoaded", async () => {
    loadDecisionWatchlist();
    bindChips("#time-chips button", "hours", "hours", () => {
      loadEvents();
      loadStats();
    });
    bindChips(
      "#time-status-chips button",
      "timeStatus",
      "timeStatus",
      () => {
        updateTimeWindowBasis();
        loadEvents();
      }
    );
    bindChips("#impact-chips button", "impact", "impact", loadEvents);
    bindKolChips();
    bindSupport();
    bindIntelDrawer();
    bindViewRetries();

    $("#view-decision").addEventListener("click", async (event) => {
      const watchButton = event.target.closest("[data-watch-asset]");
      if (watchButton) {
        toggleWatchAsset(watchButton.dataset.watchAsset, watchButton);
        return;
      }
      const lensButton = event.target.closest("[data-decision-lens]");
      if (lensButton) {
        await activateDecisionLens(lensButton.dataset.decisionLens, lensButton);
        return;
      }
      const detailRetry = event.target.closest("[data-decision-detail-retry]");
      if (detailRetry) {
        void selectDecision(detailRetry.dataset.decisionDetailRetry, {
          focusDetail: true,
          conflictRetryCount: 0,
        });
        return;
      }
      const more = event.target.closest("#decision-show-all");
      if (more) {
        const nextExpanded = !state.decisionQueueExpanded;
        if (nextExpanded && state.decisionData?.summary) {
          const originalLabel = more.textContent;
          clearDecisionExpansionError(more);
          more.disabled = true;
          more.textContent = "正在加载完整决策集…";
          try {
            if (!(await loadFullDecisions())) {
              showDecisionExpansionError(more, "完整决策集");
              return;
            }
          } finally {
            if (more.isConnected) {
              more.disabled = false;
              more.textContent = originalLabel;
            }
          }
        }
        state.decisionQueueExpanded = nextExpanded;
        renderDecisionQueue(state.decisionData || { decisions: [] });
        requestAnimationFrame(() => $("#decision-show-all")?.focus());
        return;
      }
      const matrixMore = event.target.closest("#matrix-show-all");
      if (matrixMore) {
        const nextExpanded = !state.matrixExpanded;
        if (nextExpanded && state.decisionData?.summary) {
          const originalLabel = matrixMore.textContent;
          clearDecisionExpansionError(matrixMore);
          matrixMore.disabled = true;
          matrixMore.textContent = "正在加载完整矩阵…";
          try {
            if (!(await loadFullDecisions())) {
              showDecisionExpansionError(matrixMore, "完整矩阵");
              return;
            }
          } finally {
            if (matrixMore.isConnected) {
              matrixMore.disabled = false;
              matrixMore.textContent = originalLabel;
            }
          }
        }
        state.matrixExpanded = nextExpanded;
        renderDecisionMatrix(state.decisionData || { decisions: [], impact_matrix: {} });
        requestAnimationFrame(() => $("#matrix-show-all")?.focus());
        return;
      }
      const target = event.target.closest("[data-decision-key]");
      if (target) void selectDecision(target.dataset.decisionKey, { focusDetail: true });
    });

    const lensGroup = $("#decision-lenses");
    lensGroup?.addEventListener("keydown", (event) => {
      const buttons = $$("#decision-lenses [data-decision-lens]");
      const current = event.target.closest("[data-decision-lens]");
      if (!current || !buttons.length) return;
      let index = buttons.indexOf(current);
      if (event.key === "ArrowRight") index = (index + 1) % buttons.length;
      else if (event.key === "ArrowLeft") index = (index - 1 + buttons.length) % buttons.length;
      else if (event.key === "Home") index = 0;
      else if (event.key === "End") index = buttons.length - 1;
      else return;
      event.preventDefault();
      buttons[index].focus();
    });

    $("#private-mode-btn").addEventListener("click", () => {
      if (state.authenticated || state.logoutPending) lockPrivateMode();
      else if (state.authConfigured) openAuth();
    });
    $("#auth-form").addEventListener("submit", submitAuth);
    $$("[data-auth-close]").forEach((button) =>
      button.addEventListener("click", closeAuth)
    );
    $("#auth-modal").addEventListener("click", (event) => {
      if (event.target.id === "auth-modal") closeAuth();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !$("#auth-modal").hidden) closeAuth();
    });

    const tabs = $$("#tabs .tab");
    tabs.forEach((tab) => {
      tab.addEventListener("click", () => switchView(tab.dataset.view));
      tab.addEventListener("keydown", (event) => {
        let index = tabs.indexOf(tab);
        if (event.key === "ArrowRight") index = (index + 1) % tabs.length;
        else if (event.key === "ArrowLeft") index = (index - 1 + tabs.length) % tabs.length;
        else if (event.key === "Home") index = 0;
        else if (event.key === "End") index = tabs.length - 1;
        else return;
        event.preventDefault();
        const next = tabs[index];
        switchView(next.dataset.view);
        next.focus();
      });
    });
    if (location.hash === "#kol") switchView("kol", { load: false });
    else if (location.hash === "#macro") switchView("macro", { load: false });
    else switchView("decision", { load: false });

    $(".brand")?.addEventListener("click", (event) => {
      event.preventDefault();
      switchView("decision");
    });

    $("#refresh-btn").addEventListener("click", () =>
      void refreshCurrentView({ showSpinner: true })
    );

    $("#theme-btn").addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try {
        localStorage.setItem("kol-theme", next);
      } catch (e) {}
    });

    window.addEventListener("storage", (event) => {
      if (event.key !== DECISION_WATCHLIST_STORAGE_KEY) return;
      loadDecisionWatchlist();
      if (state.decisionData) renderDecisions(state.decisionData);
      updateWatchButtons();
    });

    let qTimer = null;
    const qInput = $("#q-input");
    qInput.addEventListener("input", () => {
      clearTimeout(qTimer);
      qTimer = setTimeout(() => {
        state.q = qInput.value.trim();
        loadEvents();
      }, 250);
    });

    if (state.view !== "decision") void loadAuthStatus();
    await ensureViewLoaded(state.view);
    scheduleRefresh();

    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        clearTimeout(state.refreshTimer);
        state.refreshTimer = null;
        return;
      }
      const age = Date.now() - Number(state.viewLoadedAt[state.view] || 0);
      if (age >= VIEW_REFRESH_MS) void refreshCurrentView();
      scheduleRefresh();
    });
    window.addEventListener("pagehide", () => {
      clearTimeout(state.refreshTimer);
      state.feedAbortController?.abort();
      state.drawerAbortController?.abort();
    });
  });
})();
