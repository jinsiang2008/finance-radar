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
    logoutPending: false,
    decisionData: null,
    selectedDecisionKey: "",
    decisionQueueExpanded: false,
    matrixExpanded: false,
    decisionRequestGeneration: 0,
    macroData: null,
    macroHistory: [],
    stats: null,
    feedLoadedCount: 0,
    feedHighPriority: false,
    feedRegularCapped: false,
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

  const num = (v, digits = 2) =>
    typeof v === "number" && isFinite(v) ? v.toFixed(digits) : null;

  async function fetchJSON(url, timeoutMs = 12000) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const r = await fetch(url, { signal: ctrl.signal, cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return await r.json();
    } finally {
      clearTimeout(t);
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

  function actionInfo(card) {
    return ACTION_CN[card.action_stage] || ACTION_CN.observe;
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

  function updateMacroStatus(snapshot) {
    if (!snapshot?.available) {
      setSystemStatus(
        "warn",
        "等待快照",
        snapshot?.reason || "尚未采集到宏观快照"
      );
      return;
    }

    const createdAt = snapshot.created_at || snapshot.timestamp;
    const createdDate = new Date(String(createdAt || ""));
    if (Number.isNaN(createdDate.getTime())) {
      setSystemStatus("warn", "时间待核验", "宏观快照未提供可核验的生成时间");
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
      setSystemStatus("warn", "快照延迟", title);
      return;
    }
    setSystemStatus("ok", "快照正常", title);
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

  function renderDecisionHero(data) {
    const cards = orderedDecisions(data.decisions || []);
    const counts = cards.reduce(
      (acc, card) => {
        acc[card.classification] = (acc[card.classification] || 0) + 1;
        return acc;
      },
      { risk: 0, opportunity: 0, conflict: 0 }
    );
    const actionable = cards.filter((card) =>
      ["reduce_or_hedge", "scale_in"].includes(card.action_stage)
    ).length;
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
    const snapshot = data.portfolio_snapshot;
    const privateSummary = state.authenticated
      ? `<div class="private-summary">
          <span aria-hidden="true">🔓</span>
          <strong>私人覆盖层已开启</strong>
          ${
            snapshot
              ? `<span>持仓 ${snapshot.position_count} 项 · 数据 ${esc(
                  snapshot.as_of || "日期未知"
                )}${
                  snapshot.staleness?.is_stale
                    ? ' · <span class="status-badge warn">⚠ 已过期，置信度已降级</span>'
                    : ""
                }</span>`
              : "<span>尚无持仓快照</span>"
          }
        </div>`
      : `<div class="private-summary">
          <span aria-hidden="true">🔒</span>
          <span>公共模式不加载持仓；解锁后才显示直接敞口与杠杆风险。</span>
        </div>`;

    const leadAction = lead ? actionInfo(lead) : ACTION_CN.observe;
    const leadIsActionable = Boolean(
      lead && ["reduce_or_hedge", "scale_in"].includes(lead.action_stage)
    );
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
          <p class="brief-kicker">当前首要传导 · ${
            leadIsActionable ? "已进入分级行动" : "待验证影响假设"
          }</p>
          <h1 class="brief-title">${
            lead
              ? `${leadAction.icon} ${esc(
                  leadIsActionable ? leadAction.label : "待验证影响假设"
                )} · ${esc(assetLabel(lead.asset_key))}`
              : "等待重点信号"
          }</h1>
          ${transmission}
          <div class="brief-statline">
            <span>风险 ${counts.risk}</span><span>机会 ${counts.opportunity}</span>
            <span>分歧 ${counts.conflict}</span><span>${
              actionable ? `已确认行动 ${actionable}` : "本轮无已确认行动"
            }</span>
            <span>宏观覆盖 ${coverage}%</span>
          </div>
          <p class="brief-policy">${esc(
            data.evidence_policy ||
              "机制关系与统计伴随分开展示；数据不足、方向冲突或过期时保持观察 / 验证，所有结论需人工复核。"
          )}</p>
        </section>
      </div>
      ${privateSummary}`;
  }

  function orderedDecisions(cards) {
    const order = { reduce_or_hedge: 0, scale_in: 1, verify: 2, observe: 3 };
    return cards.slice().sort(
      (a, b) =>
        (order[a.action_stage] ?? 9) - (order[b.action_stage] ?? 9) ||
        (b.total_score || 0) - (a.total_score || 0)
    );
  }

  function renderDecisionQueue(data) {
    const cards = orderedDecisions(data.decisions || []);
    $("#decision-count").textContent = cards.length;
    if (!cards.length) {
      $("#decision-queue").innerHTML = `<div class="empty">
        <span class="empty-icon">🧭</span>尚未生成可展示的关联
        <div class="empty-hint">等待事件或宏观快照完成关系提取</div>
      </div>`;
      return;
    }
    const visibleCards = state.decisionQueueExpanded ? cards : cards.slice(0, 10);
    const cardHTML = visibleCards
      .map((card) => {
        const action = actionInfo(card);
        const key = decisionKey(card);
        const market = card.market_validation || {};
        const privateBadge = card.matched_positions?.length
          ? `<span class="status-badge private">私 · 匹配 ${card.matched_positions.length}</span>`
          : "";
        return `<button class="decision-card ${
          key === state.selectedDecisionKey ? "is-selected" : ""
        }" type="button" data-decision-key="${esc(key)}"
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
          <div class="decision-card-meta">
            <span>置信 ${confidencePct(card)}</span>
            <span>· ${card.source_count || 0} 个来源</span>
            <span class="status-badge ${market.abstain ? "warn" : ""}">
              ${market.abstain ? "⚠ 市场未确认" : "✓ 市场同向"}
            </span>
            ${card.leverage_flag ? '<span class="status-badge warn">⚠ 杠杆</span>' : ""}
            ${card.stale ? '<span class="status-badge warn">⚠ 数据过期</span>' : ""}
            ${privateBadge}
          </div>
        </button>`;
      })
      .join("");
    const moreHTML =
      cards.length > 10
        ? `<button class="decision-more" id="decision-show-all" type="button"
            aria-expanded="${state.decisionQueueExpanded}">
            ${state.decisionQueueExpanded ? "收起到重点信号" : `查看全部 ${cards.length} 条`}
          </button>`
        : "";
    $("#decision-queue").innerHTML = cardHTML + moreHTML;
  }

  function renderDecisionMatrix(data) {
    const matrix = data.impact_matrix || {};
    const columns = matrix.columns || [];
    const rows = matrix.rows || [];
    if (!columns.length || !rows.length) {
      $("#decision-matrix").innerHTML = `<div class="empty">
        <span class="empty-icon">▦</span>暂无主题 × 资产矩阵
      </div>`;
      return;
    }
    const assetScores = (data.decisions || []).reduce((scores, card) => {
      const key = String(card.asset_key || "");
      scores[key] = Math.max(scores[key] || 0, Number(card.total_score) || 0);
      return scores;
    }, {});
    const orderedColumns = columns
      .slice()
      .sort((a, b) => (assetScores[b] || 0) - (assetScores[a] || 0));
    const visibleColumns = state.matrixExpanded
      ? orderedColumns
      : orderedColumns.slice(0, 8);
    $("#decision-matrix").innerHTML = `<table class="impact-matrix">
      <thead><tr><th scope="col">主题</th>${visibleColumns
        .map(
          (asset) =>
            `<th scope="col" title="${esc(asset)}">${esc(assetLabel(asset))}</th>`
        )
        .join("")}</tr></thead>
      <tbody>${rows
        .map(
          (row) => `<tr>
            <th scope="row">${esc(topicName(row.topic_key))}</th>
            ${visibleColumns
              .map((asset) => {
                const index = columns.indexOf(asset);
                const cell = (row.cells || [])[index];
                if (!cell) return '<td class="matrix-empty">·</td>';
                const info = CLASS_CN[cell.classification] || CLASS_CN.conflict;
                const key = `${row.topic_key}::${asset}`;
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
        columns.length > 8
          ? `<button class="matrix-more" id="matrix-show-all" type="button"
              aria-expanded="${state.matrixExpanded}">
              ${state.matrixExpanded ? "收起到重点资产" : `查看全部 ${columns.length} 个资产`}
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
      : '<tr><td colspan="5">暂无共同交易日样本</td></tr>';
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

    $("#decision-detail").innerHTML = `
      <div class="evidence-head">
        <div>
          <div class="decision-card-top">
            <span class="decision-action" style="--decision-color:${action.color}">${action.icon} ${
              action.label
            }</span>
            <span class="pill" style="--lvl:${kind.color}">${kind.icon} ${kind.label}</span>
          </div>
          <h2 class="evidence-title" id="decision-detail-title" title="${esc(
            card.asset_key
          )}">${esc(assetLabel(card.asset_key))} · ${esc(topicName(card.topic_key))}</h2>
          <div class="evidence-subtitle">数据截至 ${esc(
            card.data_as_of ? fmtTime(card.data_as_of) : "未知"
          )} · 期限 ${esc(card.horizon || "未知")} · ${card.source_count || 0} 个独立来源</div>
        </div>
        <div class="evidence-score"><strong>${Math.round(
          (card.total_score || 0) * 100
        )}</strong><span>决策分 · 置信 ${confidencePct(card)}</span></div>
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
          <h3>市场验证 · ${market.abstain ? "⚠ 保持 abstain" : "✓ 方向已确认"}</h3>
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
        policy || "统计相关不等于因果；所有行动建议均需人工复核。"
      )}</p>`;
  }

  function selectDecision(key, { focusDetail = false } = {}) {
    const cards = state.decisionData?.decisions || [];
    const card = cards.find((item) => decisionKey(item) === key);
    if (!card) return;
    state.selectedDecisionKey = key;
    $$(".decision-card, .matrix-cell").forEach((node) =>
      node.classList.toggle("is-selected", node.dataset.decisionKey === key)
    );
    renderDecisionDetail(card, state.decisionData.evidence_policy);
    if (focusDetail) $("#decision-detail").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderDecisions(data) {
    state.decisionData = data;
    const cards = orderedDecisions(data.decisions || []);
    if (!cards.some((card) => decisionKey(card) === state.selectedDecisionKey)) {
      state.selectedDecisionKey = cards.length ? decisionKey(cards[0]) : "";
    }
    renderDecisionHero(data);
    renderDecisionQueue(data);
    renderDecisionMatrix(data);
    if (state.selectedDecisionKey) {
      selectDecision(state.selectedDecisionKey);
    } else {
      $("#decision-detail").innerHTML = `<div class="empty">
        <span class="empty-icon">⛓</span>暂无可展开的证据链
      </div>`;
    }
    renderSupportCard("decision");
  }

  function clearDecisionView(message = "私人数据已从当前页面清除") {
    state.decisionData = null;
    state.selectedDecisionKey = "";
    $("#decision-hero").innerHTML = `<div class="empty">${esc(message)}</div>`;
    $("#decision-queue").innerHTML = "";
    $("#decision-matrix").innerHTML = "";
    $("#decision-detail").innerHTML = `<div class="empty">
      <span class="empty-icon">⛓</span>${esc(message)}
    </div>`;
  }

  async function loadDecisions() {
    const requestedPrivate = state.authenticated;
    const requestGeneration = ++state.decisionRequestGeneration;
    const endpoint = requestedPrivate
      ? "api/private/decisions"
      : "api/decisions";
    const url = api(endpoint);
    try {
      const data = await requestJSON(url);
      if (
        requestGeneration !== state.decisionRequestGeneration ||
        requestedPrivate !== state.authenticated
      ) {
        return;
      }
      renderDecisions(data || { decisions: [], impact_matrix: {} });
    } catch (error) {
      if (requestGeneration !== state.decisionRequestGeneration) return;
      if (requestedPrivate && [401, 503].includes(error.status)) {
        state.authenticated = false;
        clearDecisionView();
        updatePrivateModeButton();
        return loadDecisions();
      }
      clearDecisionView("决策数据加载失败，已清除上一份页面数据");
      $("#decision-hero").innerHTML = errorHTML(error, url);
    }
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

  function renderMonitoredEvents(d) {
    const list = d.monitored_events || [];
    $("#macro-events").innerHTML = list.length
      ? list
          .map((e) => {
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
            const title = externalUrl
              ? `<a href="${esc(externalUrl)}" target="_blank" rel="noopener noreferrer">${esc(e.title)}</a>`
              : esc(e.title);
            return `<article class="event" style="--lvl:${lvl}">
              <div class="event-head">
                <span class="pill event-kind ${esc(e.kind)}">${EVENT_KIND_CN[e.kind] || esc(e.kind)}</span>
                <span class="tag">影响 ${EVENT_SEVERITY_CN[e.severity] || esc(e.severity)}</span>
                <span class="event-source">${esc(e.source || "")}</span>
                ${when}
              </div>
              <div class="event-title">${title}</div>
              ${move}
              ${e.note ? `<div class="event-note">${esc(e.note)}</div>` : ""}
              ${assetTagRow(e, 8)}
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

  async function loadMacro() {
    const currentUrl = api("api/macro");
    const historyUrl = api("api/macro/history?limit=72");
    const currentRequest = fetchJSON(currentUrl)
      .then((d) => {
        state.macroData = d;
        updateMacroStatus(d);
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
        if (state.decisionData) renderDecisionHero(state.decisionData);
      })
      .catch((error) => {
        setSystemStatus(
          "error",
          "快照异常",
          `最新宏观快照加载失败：${error.message || error}`
        );
        $("#macro-hero").innerHTML = errorHTML(error, currentUrl);
      });
    const historyRequest = fetchJSON(historyUrl)
      .then((payload) => {
        state.macroHistory = Array.isArray(payload?.items) ? payload.items : [];
        renderMacroTrend(state.macroHistory);
        if (state.decisionData) renderDecisionHero(state.decisionData);
      })
      .catch((error) => {
        console.warn("macro history", error);
        renderMacroTrend(state.macroHistory);
      });
    await Promise.allSettled([currentRequest, historyRequest]);
  }

  // ─── KOL view ─────────────────────────────

  async function loadStats() {
    try {
      const s = await fetchJSON(api(`api/stats?hours=${state.hours}`), 8000);
      state.stats = s;
      $("#stat-total").textContent = s.total;
      $("#stat-high").textContent = s.high;
      $("#stat-med").textContent = s.medium;
      $("#stat-kol").textContent = s.active_kols;
      $("#stat-market").textContent = s.with_market_kw;
      $("#tab-kol-count").textContent = s.high > 0 ? s.high : "";
      updateFeedStatus();
    } catch (e) {
      console.warn("stats", e);
    }
  }

  async function loadKols() {
    try {
      const list = await fetchJSON(api("api/kols"), 8000);
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
    return String(item?.canonical_url || item?.url || item?.title || "")
      .trim()
      .toLowerCase();
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
    const parts = [`已加载 ${state.feedLoadedCount} 条`];
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

  function renderEvents(items) {
    if (!items.length) {
      $("#feed").innerHTML = `<div class="empty">
        <span class="empty-icon">📭</span>当前筛选条件下没有动态
        <div class="empty-hint">试试放宽时间窗口或影响等级</div>
      </div>`;
      return;
    }
    const LVL = { high: "var(--high)", medium: "var(--med)" };
    const LBL = { high: "高影响", medium: "中影响", low: "低影响" };

    // X posts carry the same text as both title and snippet; show it once.
    const bodyOf = (it) => {
      const title = String(it.title || "");
      const snippet = String(it.snippet || "");
      const stem = title.replace(/[…\.]+$/, "").trim();
      if (snippet && stem && snippet.startsWith(stem)) {
        return { headline: snippet, snippet: "" };
      }
      return { headline: title, snippet };
    };

    $("#feed").innerHTML = items
      .map((it) => {
        const body = bodyOf(it);
        const sourceNature = sourceKind(it.source);
        const lvl = LVL[it.impact] || "transparent";
        const pillLvl = LEVEL_COLOR[it.impact] || "var(--neutral)";
        const tickers = (it.tickers || [])
          .map((t) => `<span class="tag ticker">$${esc(t)}</span>`)
          .join("");
        const heat =
          it.source_count > 1
            ? `<span class="heat">🔗 ${it.source_count} 个独立来源</span>`
            : "";
        const collectedAt = it.first_seen_at || it.fetched_at;
        const timeStatus =
          it.time_status || (it.published_at ? "verified" : "unknown");
        const timeTitle = [
          it.published_at ? `发布时间：${it.published_at}` : "发布时间：未知",
          collectedAt ? `首次抓取：${collectedAt}` : "",
        ]
          .filter(Boolean)
          .join(" · ");
        const eventTime =
          timeStatus === "verified" && it.published_at
            ? `发布 ${fmtTime(it.published_at)}`
            : timeStatus === "future"
              ? `发布时间异常 · 抓取 ${fmtTime(collectedAt)}`
              : `发布时间未知 · 抓取 ${fmtTime(collectedAt)}`;
        const externalUrl = safeExternalUrl(it.canonical_url || it.url);
        const headline = externalUrl
          ? `<a href="${esc(externalUrl)}" target="_blank" rel="noopener noreferrer">${esc(
              body.headline
            )}</a>`
          : esc(body.headline);
        return `<article class="card" style="--lvl:${lvl}">
          <div class="card-head">
            <span class="card-kol">${esc(it.kol_name_cn || it.kol_name)}</span>
            ${
              it.impact !== "low"
                ? `<span class="pill" style="--lvl:${pillLvl}">${LBL[it.impact]}</span>`
                : ""
            }
            <span class="card-src">${esc(it.source || "")}</span>
            <span class="source-kind ${sourceNature.key}">${sourceNature.label}</span>
            <span class="card-time ${
              timeStatus === "verified" ? "" : "is-unverified"
            }" title="${esc(timeTitle)}">${esc(eventTime)}</span>
          </div>
          <div class="card-title${body.snippet ? "" : " card-title-body"}">
            ${headline}
          </div>
          ${body.snippet ? `<div class="card-snippet">${esc(body.snippet)}</div>` : ""}
          ${
            tickers || heat
              ? `<div class="card-foot">${tickers}${heat}</div>`
              : ""
          }
        </article>`;
      })
      .join("");
    renderSupportCard("kol");
  }

  async function loadEvents() {
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
        regularData = await fetchJSON(url);
      } else {
        const highParams = new URLSearchParams(p);
        highParams.set("impact", "high");
        highParams.set("limit", "50");
        const highUrl = api(`api/events?${highParams}`);
        const [regularResult, highResult] = await Promise.allSettled([
          fetchJSON(url),
          fetchJSON(highUrl),
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
      state.feedLoadedCount = items.length;
      state.feedHighPriority = highPriorityLoaded;
      state.feedRegularCapped = regularItems.length >= 150;
      renderEvents(items);
      updateFeedStatus();
    } catch (e) {
      $("#feed").innerHTML = errorHTML(e, url);
      const host = $("#feed-status");
      if (host) host.textContent = "动态加载失败";
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
    try {
      const s = await fetchJSON(api("api/stats?hours=24"), 8000);
      $("#fact-events").textContent = s.total;
      $("#fact-kols").textContent = s.active_kols;
    } catch (e) {
      $("#support-facts").hidden = true;
    }
  }

  function openSupport() {
    const m = $("#support-modal");
    m.hidden = false;
    m.querySelector(".support-close").focus();
    document.body.style.overflow = "hidden";
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

  function switchView(view) {
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
  }

  async function refreshAll() {
    const btn = $("#refresh-btn");
    btn.classList.add("spinning");
    try {
      await Promise.all([
        loadDecisions(),
        loadMacro(),
        loadStats(),
        loadKols(),
        loadEvents(),
      ]);
      updateRefreshTime();
    } finally {
      btn.classList.remove("spinning");
    }
  }

  function updateRefreshTime() {
    const t = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    $("#last-update").textContent = `${pad(t.getHours())}:${pad(t.getMinutes())}`;
  }

  async function refreshCurrentView() {
    const tasks = [loadDecisions(), loadMacro()];
    if (state.view === "kol") {
      tasks.push(loadStats(), loadKols(), loadEvents());
    }
    await Promise.all(tasks);
    updateRefreshTime();
  }

  document.addEventListener("DOMContentLoaded", () => {
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

    $("#view-decision").addEventListener("click", (event) => {
      const more = event.target.closest("#decision-show-all");
      if (more) {
        state.decisionQueueExpanded = !state.decisionQueueExpanded;
        renderDecisionQueue(state.decisionData || { decisions: [] });
        requestAnimationFrame(() => $("#decision-show-all")?.focus());
        return;
      }
      const matrixMore = event.target.closest("#matrix-show-all");
      if (matrixMore) {
        state.matrixExpanded = !state.matrixExpanded;
        renderDecisionMatrix(state.decisionData || { decisions: [], impact_matrix: {} });
        requestAnimationFrame(() => $("#matrix-show-all")?.focus());
        return;
      }
      const target = event.target.closest("[data-decision-key]");
      if (target) selectDecision(target.dataset.decisionKey, { focusDetail: true });
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
    if (location.hash === "#kol") switchView("kol");
    else if (location.hash === "#macro") switchView("macro");
    else switchView("decision");

    $(".brand")?.addEventListener("click", (event) => {
      event.preventDefault();
      switchView("decision");
    });

    $("#refresh-btn").addEventListener("click", refreshAll);

    $("#theme-btn").addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try {
        localStorage.setItem("kol-theme", next);
      } catch (e) {}
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

    loadAuthStatus().then(refreshAll);
    loadSupportFacts();
    setInterval(refreshCurrentView, 300_000);
  });
})();
