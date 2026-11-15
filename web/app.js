/* FINN — static client. Reads web/data/*.json; talks to /api only for live research. */
"use strict";

// ---------------------------------------------------------------- utilities
const $ = (sel, root = document) => root.querySelector(sel);

/** Build DOM safely: strings become text nodes, never HTML. */
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style") el.setAttribute("style", v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const SVGNS = "http://www.w3.org/2000/svg";
function s(tag, attrs, ...children) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, v);
  for (const c of children.flat()) if (c != null) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}

const store = {
  get(key, fallback) { try { const v = localStorage.getItem("finn." + key); return v == null ? fallback : JSON.parse(v); } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem("finn." + key, JSON.stringify(value)); } catch { /* private mode */ } },
};

const INR = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
function cr(v) {
  if (v == null || isNaN(v)) return "—";
  const sign = v < 0 ? "−" : "", a = Math.abs(v);
  if (a >= 100000) return `${sign}₹${(a / 100000).toFixed(2)} lakh cr`;
  if (a >= 100) return `${sign}₹${INR.format(a)} cr`;
  return `${sign}₹${a.toFixed(1)} cr`;
}
function crShort(v) {
  if (v == null || isNaN(v)) return "—";
  const sign = v < 0 ? "−" : "", a = Math.abs(v);
  if (a >= 100000) return `${sign}${(a / 100000).toFixed(1)}L`;
  if (a >= 1000) return `${sign}${(a / 1000).toFixed(a >= 10000 ? 0 : 1)}K`;
  return `${sign}${a.toFixed(0)}`;
}
function sp(v, digits = 1, suffix = "%") {
  if (v == null || isNaN(v)) return "—";
  return `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(digits)}${suffix}`;
}
function dirClass(v, goodWhenUp = true) {
  if (v == null || Math.abs(v) < 0.05) return "";
  return (v > 0) === goodWhenUp ? "up" : "down";
}
/** The app's "now". A static deployment shows a snapshot, so windows and "x ago" are measured from
 *  the moment the snapshot was taken — otherwise "last 24h" goes empty the day after publishing. */
function refNow() {
  const snap = state.feed?.generated_at ? Date.parse(state.feed.generated_at) : NaN;
  return isNaN(snap) ? Date.now() : Math.min(Date.now(), snap);
}
function snapshotAgeHours() {
  const snap = state.feed?.generated_at ? Date.parse(state.feed.generated_at) : NaN;
  return isNaN(snap) ? 0 : (Date.now() - snap) / 3600000;
}
function ago(iso) {
  const d = new Date(iso), mins = (refNow() - d) / 60000;
  if (mins < 60) return `${Math.max(1, Math.round(mins))}m ago`;
  if (mins < 60 * 24) return `${Math.round(mins / 60)}h ago`;
  if (mins < 60 * 24 * 7) return `${Math.round(mins / 1440)}d ago`;
  return d.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
}
function day(iso) { return new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short" }); }
function initials(name) { return name.split(/\s+/).filter(w => /^[A-Za-z]/.test(w)).slice(0, 2).map(w => w[0].toUpperCase()).join(""); }
function pageLink(url, page) { return page ? `${url}#page=${page}` : url; }
const safeName = sym => sym.toUpperCase().replace(/[^A-Z0-9]/g, "_");

async function getJSON(path) {
  const res = await fetch(path, { cache: "no-cache" });
  if (!res.ok) throw new Error(`${res.status} ${path}`);
  return res.json();
}

// ---------------------------------------------------------------- state
const CATEGORY_LABEL = {
  results: "Results", m_and_a: "M&A", fund_raise: "Fund raise", management_change: "Management", litigation: "Legal & regulatory",
  insider_trading: "Insider trades", earnings_call: "Earnings call", investor_meetings: "Investor meetings", credit_rating: "Credit rating",
  dividend: "Dividend", bonus_split: "Bonus / split", capex: "Capex", press_release: "Press release", postal_ballot: "Postal ballot",
  agm_egm: "AGM / EGM", pledging: "Pledging", unclassified: "Routine",
};
const ALERTABLE = ["results", "m_and_a", "fund_raise", "management_change", "litigation", "credit_rating", "pledging", "insider_trading", "capex", "dividend", "bonus_split", "press_release"];
const WINDOWS = [["24h", 1], ["3 days", 3], ["7 days", 7], ["30 days", 30]];

const state = {
  feed: null, prices: null, meta: null, index: [], symbols: null, api: null,
  prefs: Object.assign(
    { windowDays: 1, volume: 2.0, move: 4.0, cats: ALERTABLE.slice(0, 9) },
    store.get("prefs", {}),
  ),
  seen: new Set(store.get("seen", [])),
  research: new Map(),
};
function savePrefs() { store.set("prefs", state.prefs); }
function markSeen(ids) { ids.forEach(id => state.seen.add(id)); store.set("seen", [...state.seen].slice(-2000)); updateBadge(); }

// ---------------------------------------------------------------- tooltip
const tip = $("#tip");
function showTip(evt, lines) {
  tip.replaceChildren(...lines.map((l, i) => i === 0 ? h("b", {}, l) : h("div", {}, l)));
  const x = Math.min(window.innerWidth - 70, Math.max(70, evt.clientX));
  tip.style.left = `${x}px`; tip.style.top = `${evt.clientY}px`; tip.style.opacity = 1;
}
function hideTip() { tip.style.opacity = 0; }

// ---------------------------------------------------------------- charts (SVG, single axis, thin marks)
function sparkline(values, { w = 96, h: ht = 32, color = "var(--series-1)" } = {}) {
  const pts = values.filter(v => v != null);
  if (pts.length < 2) return h("span");
  const lo = Math.min(...pts), hi = Math.max(...pts), span = hi - lo || 1;
  const xy = pts.map((v, i) => [i / (pts.length - 1) * (w - 6) + 3, ht - 3 - (v - lo) / span * (ht - 6)]);
  return s("svg", { viewBox: `0 0 ${w} ${ht}`, width: w, height: ht, role: "img", "aria-label": "price trend" },
    s("polyline", { points: xy.map(p => p.join(",")).join(" "), fill: "none", stroke: color, "stroke-width": 1.6, "stroke-linejoin": "round", "stroke-linecap": "round" }),
    s("circle", { cx: xy.at(-1)[0], cy: xy.at(-1)[1], r: 3, fill: color, stroke: "var(--surface)", "stroke-width": 1.5 }));
}

function niceTicks(lo, hi, n = 4) {
  const span = hi - lo || Math.abs(hi) || 1, raw = span / n, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(st => span / st <= n) || mag * 10;
  const start = Math.floor(lo / step) * step, out = [];
  for (let v = start; v <= hi + step * 0.5; v += step) out.push(+v.toFixed(10));
  return out;
}

/** Column chart with one series; `fmt` formats values; tooltip per column; last column emphasised. */
function columns(labels, values, { title, sub, fmt = crShort, tipFmt, extra = () => [] } = {}) {
  const W = 340, H = 150, L = 34, B = 20, T = 8;
  const vals = values.map(v => v ?? 0);
  const lo = Math.min(0, ...vals), hi = Math.max(...vals, 0) || 1;
  const ticks = niceTicks(lo, hi), tLo = Math.min(lo, ticks[0]), tHi = Math.max(hi, ticks.at(-1));
  const y = v => T + (H - T - B) * (1 - (v - tLo) / (tHi - tLo || 1));
  const band = (W - L) / labels.length, bw = Math.min(22, band * 0.62);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": title || "chart" });
  ticks.forEach(t => {
    svg.append(s("line", { x1: L, x2: W, y1: y(t), y2: y(t), stroke: t === 0 ? "var(--axis)" : "var(--line)", "stroke-width": 1 }));
    svg.append(s("text", { x: L - 6, y: y(t) + 3.5, "text-anchor": "end", "font-size": 10, fill: "var(--ink-3)", class: "tabular" }, fmt(t)));
  });
  labels.forEach((lab, i) => {
    const v = values[i], x = L + band * i + (band - bw) / 2;
    if (v != null) {
      const top = y(Math.max(v, 0)), bot = y(Math.min(v, 0)), hgt = Math.max(1, bot - top), r = Math.min(4, hgt / 2);
      const up = v >= 0;
      const d = up
        ? `M${x},${bot} V${top + r} Q${x},${top} ${x + r},${top} H${x + bw - r} Q${x + bw},${top} ${x + bw},${top + r} V${bot} Z`
        : `M${x},${top} V${bot - r} Q${x},${bot} ${x + r},${bot} H${x + bw - r} Q${x + bw},${bot} ${x + bw},${bot - r} V${top} Z`;
      svg.append(s("path", { d, fill: "var(--series-1)", opacity: i === labels.length - 1 ? 1 : 0.55 }));
    }
    if (labels.length <= 8 || i % 2 === labels.length % 2 || i === labels.length - 1)
      svg.append(s("text", { x: L + band * i + band / 2, y: H - 5, "text-anchor": "middle", "font-size": 9.5, fill: "var(--ink-3)" }, shortLabel(lab)));
    const hit = s("rect", { x: L + band * i, y: T, width: band, height: H - T - B, fill: "transparent", tabindex: 0 });
    const lines = () => [tipFmt ? tipFmt(v) : (v == null ? "—" : cr(v)), lab, ...extra(i)];
    hit.addEventListener("pointermove", e => showTip(e, lines()));
    hit.addEventListener("pointerleave", hideTip);
    hit.addEventListener("focus", e => { const r = e.target.getBoundingClientRect(); showTip({ clientX: r.x + r.width / 2, clientY: r.y }, lines()); });
    hit.addEventListener("blur", hideTip);
    svg.append(hit);
  });
  return h("div", { class: "chart" }, title && h("div", { class: "ct" }, title), sub && h("div", { class: "cs" }, sub), svg);
}

/** Single-series line on its own axis (never paired with another scale). */
function line(labels, values, { title, sub, fmt = v => `${v.toFixed(0)}%` } = {}) {
  const W = 340, H = 120, L = 34, B = 20, T = 8;
  const pts = values.map((v, i) => [i, v]).filter(p => p[1] != null);
  if (pts.length < 2) return h("span");
  const vs = pts.map(p => p[1]), ticks = niceTicks(Math.min(...vs), Math.max(...vs), 3);
  const lo = ticks[0], hi = ticks.at(-1);
  const x = i => L + (W - L - 8) * (i / (labels.length - 1)), y = v => T + (H - T - B) * (1 - (v - lo) / (hi - lo || 1));
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": title || "chart" });
  ticks.forEach(t => {
    svg.append(s("line", { x1: L, x2: W, y1: y(t), y2: y(t), stroke: "var(--line)", "stroke-width": 1 }));
    svg.append(s("text", { x: L - 6, y: y(t) + 3.5, "text-anchor": "end", "font-size": 10, fill: "var(--ink-3)" }, fmt(t)));
  });
  svg.append(s("polyline", { points: pts.map(([i, v]) => `${x(i)},${y(v)}`).join(" "), fill: "none", stroke: "var(--series-1)", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
  const [li, lv] = pts.at(-1);
  svg.append(s("circle", { cx: x(li), cy: y(lv), r: 4, fill: "var(--series-1)", stroke: "var(--surface)", "stroke-width": 2 }));
  labels.forEach((lab, i) => {
    if (labels.length <= 8 || i % 2 === labels.length % 2 || i === labels.length - 1)
      svg.append(s("text", { x: x(i), y: H - 5, "text-anchor": "middle", "font-size": 9.5, fill: "var(--ink-3)" }, shortLabel(lab)));
  });
  const cross = s("line", { y1: T, y2: H - B, stroke: "var(--axis)", "stroke-width": 1, opacity: 0 });
  svg.append(cross);
  const hit = s("rect", { x: L, y: T, width: W - L, height: H - T - B, fill: "transparent" });
  hit.addEventListener("pointermove", e => {
    const box = svg.getBoundingClientRect(), px = (e.clientX - box.left) / box.width * W;
    const i = Math.max(0, Math.min(labels.length - 1, Math.round((px - L) / (W - L - 8) * (labels.length - 1))));
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("opacity", 1);
    showTip(e, [values[i] == null ? "—" : `${values[i].toFixed(1)}%`, labels[i]]);
  });
  hit.addEventListener("pointerleave", () => { cross.setAttribute("opacity", 0); hideTip(); });
  svg.append(hit);
  return h("div", { class: "chart" }, title && h("div", { class: "ct" }, title), sub && h("div", { class: "cs" }, sub), svg);
}
function shortLabel(lab) { const m = /^([A-Za-z]{3})\w* (\d{4})$/.exec(lab || ""); return m ? `${m[1]} ’${m[2].slice(2)}` : lab; }

// ---------------------------------------------------------------- signals (same definitions as finn/push/signals.py)
function dailyMetrics(series, lookback = 20) {
  const out = [];
  for (let i = 1; i < series.d.length; i++) {
    const prev = series.c[i - 1], move = prev ? (series.c[i] / prev - 1) * 100 : null;
    const win = series.v.slice(Math.max(0, i - lookback), i).filter(Boolean);
    const avg = win.length >= Math.max(5, lookback / 2) ? win.reduce((a, b) => a + b, 0) / win.length : null;
    out.push({ date: series.d[i], close: series.c[i], move, volMult: avg && series.v[i] ? series.v[i] / avg : null });
  }
  return out;
}
function signals(sinceDate) {
  if (!state.prices) return [];
  const idx = state.prices.index ? Object.fromEntries(dailyMetrics(state.prices.index).map(m => [m.date, m.move])) : {};
  const lookback = state.feed?.thresholds?.lookback_sessions || 20, out = [];
  for (const [sym, series] of Object.entries(state.prices.symbols)) {
    for (const m of dailyMetrics(series, lookback)) {
      if (m.date < sinceDate) continue;
      const kinds = [];
      if (m.move != null && Math.abs(m.move) >= state.prefs.move) kinds.push("price");
      if (m.volMult != null && m.volMult >= state.prefs.volume) kinds.push("volume");
      if (kinds.length) out.push({ sym, kinds, ...m, index: idx[m.date] ?? null, closes: series.c.slice(-60) });
    }
  }
  return out.sort((a, b) => (b.date.localeCompare(a.date)) || Math.abs(b.move) - Math.abs(a.move));
}

// ---------------------------------------------------------------- feed slicing
function effectiveTier(item) {
  // An exchange asking the company to explain itself is always worth an interruption.
  if (item.tier === "act" && item.tag === "exchange_query") return "act";
  if (item.tier === "act" && !state.prefs.cats.includes(item.category)) return "know";
  return item.tier;
}
function windowSlice() {
  const items = state.feed?.items || [], since = refNow() - state.prefs.windowDays * 86400000;
  const inWin = items.filter(i => new Date(i.ts).getTime() >= since);
  const tiers = { act: [], know: [], buried: [] };
  inWin.forEach(i => tiers[effectiveTier(i)].push(i));
  tiers.act.sort((a, b) => b.score - a.score || b.ts.localeCompare(a.ts));
  tiers.know.sort((a, b) => b.ts.localeCompare(a.ts));
  return { tiers, total: inWin.length };
}
function sinceSessionDate() {
  // EOD data: "24h" means the latest session; longer windows count back by calendar days.
  const dates = state.prices ? Object.values(state.prices.symbols)[0]?.d || [] : [];
  if (!dates.length) return "9999";
  if (state.prefs.windowDays <= 1) return dates.at(-1);
  const d = new Date(dates.at(-1)); d.setDate(d.getDate() - state.prefs.windowDays + 1);
  return d.toISOString().slice(0, 10);
}
function updateBadge() {
  const tab = document.querySelector('.tab[data-route="today"]');
  tab.querySelector(".dot")?.remove();
  if (!state.feed) return;
  const { tiers } = windowSlice(), unseen = tiers.act.filter(i => !state.seen.has(i.id)).length;
  if (unseen) tab.append(h("span", { class: "dot" }, unseen));
}

// ---------------------------------------------------------------- cards
function head(item, extra) {
  return h("div", { class: "card-head" },
    h("a", { class: "sym", href: `#/c/${encodeURIComponent(item.symbol)}` }, item.symbol),
    h("span", { class: "cat" }, CATEGORY_LABEL[item.category] || item.category),
    extra,
    h("span", { class: "when", title: new Date(item.ts).toLocaleString("en-IN") }, ago(item.ts)));
}
function foot(item) {
  const why = item.read?.status === "ok" ? `Read by model · ${item.read.kind}` : item.read?.status === "failed" ? `Not readable: ${item.read.reason || "unknown"}` : `Sorted by rule · ${(item.rule || "—").replace(/_/g, " ")}`;
  return h("div", { class: "card-foot" },
    h("a", { href: item.url, target: "_blank", rel: "noopener" }, `Filing${item.pages ? ` · ${item.pages}p` : ""} ↗`),
    item.related?.length ? h("span", {}, `+${item.related.length} related filing${item.related.length > 1 ? "s" : ""}`) : null,
    h("span", { class: "trail", title: `score ${item.score}` }, why));
}

function resultsBlock(card, why = "") {
  const m = Object.fromEntries(card.metrics.map(x => [x.key, x]));
  const rows = card.metrics.map(x => h("tr", {},
    h("td", {}, x.label), h("td", { class: "v" }, cr(x.value).replace(" cr", "")),
    h("td", { class: dirClass(x.qoq_pct) }, sp(x.qoq_pct, 0)), h("td", { class: dirClass(x.yoy_pct) }, sp(x.yoy_pct, 0))));
  const rec = card.reconciliation || {};
  let badge;
  if (card.source === "standardized") badge = h("span", { class: "badge neutral" }, "From standardized data");
  else if (card.checks?.length) badge = h("span", { class: "badge bad" }, "⚠ Arithmetic check failed — verify in filing");
  else if (rec.status === "matched") badge = h("span", { class: "badge ok" }, "✓ Matches standardized data");
  else if (rec.status === "differs") badge = h("span", { class: "badge warn" }, `Differs from standardized data by ${Math.max(...Object.values(rec.diffs).map(d => d.diff_pct)).toFixed(1)}%`);
  else badge = h("span", { class: "badge neutral" }, card.page ? `Read from filing p.${card.page}` : "Read from filing");
  const ebitda = m.ebitda;
  return h("div", {},
    h("table", { class: "rtable" },
      h("thead", {}, h("tr", {}, h("th", {}, `${card.quarter || ""} · ${card.basis}`), h("th", {}, "₹ cr"), h("th", {}, "QoQ"), h("th", {}, "YoY"))),
      h("tbody", {}, rows)),
    h("div", { class: "facts" },
      ebitda?.margin_pct != null && !card.is_lender ? h("span", { class: "fact" }, "EBITDA margin ", h("b", {}, `${ebitda.margin_pct.toFixed(1)}%`), ebitda.margin_year_ago_pct != null ? ` vs ${ebitda.margin_year_ago_pct.toFixed(1)}% a year ago` : "") : null,
      m.pat?.margin_pct != null ? h("span", { class: "fact" }, "PAT margin ", h("b", {}, `${m.pat.margin_pct.toFixed(1)}%`)) : null,
      badge),
    card.flags?.filter(f => !why.includes(f)).length ? h("div", { class: "why tiny" }, "Filing notes: ", card.flags.filter(f => !why.includes(f)).join(" · ")) : null);
}

function peopleBlock(enr) {
  return enr.people.map(p => h("div", { class: "person" },
    h("div", { class: "avatar", "aria-hidden": "true" }, initials(p.name)),
    h("div", {},
      h("div", {}, h("b", {}, p.name), " ", h("span", { class: "badge neutral" }, p.change)),
      h("div", { class: "role" }, p.role, p.effective ? ` · from ${p.effective}` : ""),
      p.background ? h("div", { class: "bio" }, p.background)
        : ["ceo", "cfo", "chair"].includes(p.level) && p.change === "appointment"
          ? h("div", { class: "bio muted" }, "No profile in this filing. SEBI requires one in the original appointment disclosure, which is older than the filings FINN can see. ",
              h("a", { href: `https://www.google.com/search?q=${encodeURIComponent(`${p.name} ${p.role} profile`)}`, target: "_blank", rel: "noopener", style: "color:var(--accent)" }, "Find profile ↗"))
          : h("div", { class: "bio muted" }, "No profile in the filing."),
      p.previous_roles?.length ? h("ul", {}, p.previous_roles.map(r => h("li", {}, r))) : null,
      p.education ? h("div", { class: "tiny muted", style: "margin-top:4px" }, p.education) : null,
      p.background_source === "model"
        ? h("div", { class: "tiny", style: "margin-top:6px" }, h("span", { class: "badge warn" }, "AI-generated · unverified"), " Not from any filing — the company's disclosure had no profile, so this comes from the model's general knowledge. Check before relying on it. ",
            h("a", { href: `https://www.google.com/search?q=${encodeURIComponent(`${p.name} ${state.feed?.items?.find(i => i.enrichment === enr)?.company || ""}`)}`, target: "_blank", rel: "noopener", style: "color:var(--accent)" }, "Verify ↗"))
        : p.background_source ? h("div", { class: "tiny muted", style: "margin-top:4px" }, p.background_source === "filing" ? "Profile from the company's filing" : "Profile from web search", p.background_links?.length ? [" · ", ...p.background_links.map((l, i) => [i ? ", " : "", h("a", { href: l.url, target: "_blank", rel: "noopener" }, l.title || "source")])] : "") : null,
      p.reason ? h("div", { class: "tiny muted" }, `Reason given: ${p.reason}`) : null)));
}

/** For a results card: the company's promises still in play, pulled from its research file.
 *  Renders nothing until (and unless) the research loads, so it never blocks the card. */
function promisesFor(sym) {
  const wrap = h("div", { class: "promised" });
  const known = state.index.find(r => r.symbol === sym);
  if (!known) return wrap;
  const rank = { behind: 0, on_track: 1, open: 2 };
  const fill = d => {
    const live = (d.guidance || []).filter(g => g.verdict.status in rank)
      .sort((a, b) => ((a.scope === "segment") - (b.scope === "segment")) || rank[a.verdict.status] - rank[b.verdict.status] || b.call.localeCompare(a.call))
      .slice(0, 3);
    if (!live.length) return;
    wrap.append(
      h("div", { class: "promised-h" }, "What management promised"),
      ...live.map(g => h("div", { class: "promised-row" },
        h("span", { class: `vchip ${g.verdict.status}` }, g.verdict.status === "open" ? "not due" : g.verdict.status.replace("_", " ")),
        h("span", { class: "pt" }, h("b", {}, g.label), " — ", g.verdict.detail))),
      h("a", { class: "promised-link", href: `#/c/${encodeURIComponent(sym)}` }, "Full promise ledger →"));
  };
  if (state.research.has(sym)) fill(state.research.get(sym));
  else getJSON(`data/research/${known.file}`).then(d => { state.research.set(sym, d); fill(d); }).catch(() => { /* card stands alone */ });
  return wrap;
}

function itemCard(item) {
  const seen = state.seen.has(item.id);
  const enr = item.enrichment;
  const title = item.headline || item.title;
  const card = h("article", { class: `card${seen ? " seen" : ""}`, "data-id": item.id },
    head(item),
    h("h3", {}, title),
    item.why ? h("p", { class: "why" }, item.why) : (item.source_text && item.source_text !== title ? h("p", { class: "why" }, item.source_text) : null),
    enr?.type === "results" ? resultsBlock(enr, item.why || "") : null,
    enr?.type === "results" ? promisesFor(item.symbol) : null,
    enr?.type === "people" ? peopleBlock(enr) : null,
    item.facts?.length ? h("div", { class: "facts" }, item.facts.map(f => h("span", { class: "fact" }, `${f.label}: `, h("b", {}, f.value)))) : null,
    item.size_note ? h("p", { class: "tiny muted", style: "margin-top:8px" }, item.size_note) : null,
    foot(item));
  card.addEventListener("click", e => { if (!e.target.closest("a")) { markSeen([item.id]); card.classList.add("seen"); } });
  return card;
}

function alertCard(sig) {
  const kinds = sig.kinds.map(k => k === "price" ? `${sp(sig.move, 1)} move` : `${sig.volMult.toFixed(1)}× volume`).join(" · ");
  const filed = (state.feed?.items || []).filter(i => i.symbol === sig.sym && Math.abs(new Date(i.ts) - new Date(sig.date)) <= 2.5 * 86400000 && i.tier !== "buried");
  // Stock-specific only if the stock moved clearly more than the market, or against it.
  const rel = sig.index == null ? null : sig.move - sig.index;
  const ctx = sig.index == null ? "" : (Math.abs(rel) < Math.max(1, Math.abs(sig.move) * 0.5) ? `In line with the market (Nifty ${sp(sig.index)})` : `Nifty ${sp(sig.index)} — ${sp(rel)} vs market, stock-specific`);
  return h("article", { class: "card" },
    h("div", { class: "card-head" }, h("a", { class: "sym", href: `#/c/${encodeURIComponent(sig.sym)}` }, sig.sym), h("span", { class: "cat" }, sig.kinds.includes("price") ? "Price move" : "Volume spike"), h("span", { class: "when" }, day(sig.date))),
    h("div", { class: "alert" },
      h("div", {},
        h("div", { class: `big ${dirClass(sig.move)}` }, sp(sig.move, 1)),
        h("div", { class: "ctx" }, kinds, sig.volMult != null && !sig.kinds.includes("volume") ? ` · vol ${sig.volMult.toFixed(1)}×` : ""),
        ctx ? h("div", { class: "ctx" }, ctx) : null),
      sparkline(sig.closes, { w: 96, h: 40, color: sig.move >= 0 ? "var(--good)" : "var(--bad)" })),
    filed.length ? h("p", { class: "why small" }, "Filed around then: ", filed.slice(0, 2).map((r, i) => [i ? " · " : "", h("a", { href: r.url, target: "_blank", rel: "noopener" }, r.headline || r.title)])) : null,
    h("div", { class: "card-foot" }, h("span", {}, `Close ₹${INR.format(sig.close)} · your thresholds: ±${state.prefs.move}% / ${state.prefs.volume}× vol`)));
}

function knowRow(item) {
  const row = h("button", { class: `row${state.seen.has(item.id) ? " seen" : ""}`, "aria-expanded": "false" },
    h("div", {}, h("div", { class: "t" }, h("b", {}, item.symbol), " ", item.headline || item.title),
      h("div", { class: "s" }, CATEGORY_LABEL[item.category], item.why ? ` · ${item.why}` : item.source_text ? ` · ${item.source_text}` : "")),
    h("div", { class: "r" }, ago(item.ts)));
  const wrap = h("div", {}, row);
  row.addEventListener("click", () => {
    const open = row.getAttribute("aria-expanded") === "true";
    row.setAttribute("aria-expanded", String(!open));
    if (open) { wrap.querySelector(".row-detail")?.remove(); return; }
    markSeen([item.id]); row.classList.add("seen");
    const enr = item.enrichment;
    wrap.append(h("div", { class: "row-detail" },
      enr?.type === "results" ? resultsBlock(enr, item.why || "") : null,
      enr?.type === "results" ? promisesFor(item.symbol) : null,
      enr?.type === "people" ? peopleBlock(enr) : null,
      item.facts?.length ? h("div", { class: "facts" }, item.facts.map(f => h("span", { class: "fact" }, `${f.label}: `, h("b", {}, f.value)))) : null,
      item.size_note ? h("p", { class: "tiny muted", style: "margin-top:6px" }, item.size_note) : null,
      h("div", { style: "margin-top:8px" }, h("a", { href: item.url, target: "_blank", rel: "noopener" }, "Open filing ↗"), " · ", h("a", { href: `#/c/${encodeURIComponent(item.symbol)}` }, `${item.symbol} research`))));
  });
  return wrap;
}

// ---------------------------------------------------------------- views
function setTop(...children) { $("#top").replaceChildren(...children); }
function brandTop(sub) { setTop(h("div", { class: "brand" }, "FINN", h("span", {}, ".")), h("div", { class: "sub" }, sub)); }

function viewToday() {
  const feed = state.feed;
  const asOf = feed ? new Date(feed.generated_at).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" }) : "";
  brandTop(feed ? (snapshotAgeHours() > 3 ? `Snapshot as of ${asOf} IST` : `Updated ${Math.max(1, Math.round(snapshotAgeHours() * 60))}m ago · prices ${feed.prices_as_of ? day(feed.prices_as_of) : "—"}`) : "");
  if (!feed) return [h("div", { class: "empty" }, "No feed yet. Run python -m finn push.")];
  const { tiers, total } = windowSlice();
  const sigs = signals(sinceSessionDate());
  const unseenAct = tiers.act.filter(i => !state.seen.has(i.id));
  const chips = h("div", { class: "chips", role: "group", "aria-label": "Time window" }, WINDOWS.map(([label, days]) =>
    h("button", { class: "chip", "aria-pressed": String(state.prefs.windowDays === days), onclick: () => { state.prefs.windowDays = days; savePrefs(); render(); } }, label)));

  const companies = new Set(tiers.act.map(i => i.symbol));
  const lead = unseenAct.length
    ? h("p", { class: "lead" }, h("em", {}, `${unseenAct.length} ${unseenAct.length === 1 ? "thing needs" : "things need"} you`), ` across ${companies.size} ${companies.size === 1 ? "company" : "companies"}.`)
    : tiers.act.length ? h("p", { class: "lead" }, "You're caught up. ", h("span", { class: "muted" }, "Nothing new needs action.")) : h("p", { class: "lead" }, "Quiet window. ", h("span", { class: "muted" }, "No material filings."));
  const brief = h("section", { class: "brief" }, lead,
    h("p", { class: "meta" }, `${tiers.know.length} worth knowing · ${tiers.buried.length} routine filing${tiers.buried.length === 1 ? "" : "s"} hidden · ${sigs.length} price/volume alert${sigs.length === 1 ? "" : "s"}`),
    unseenAct.length > 1 ? h("p", { style: "margin-top:8px" }, h("button", { class: "linkbtn", onclick: () => { markSeen(tiers.act.map(i => i.id)); render(); } }, "Mark all as seen")) : null,
    freshnessLine(feed, asOf),
    ...dataWarnings(feed));

  const out = [chips, brief];
  if (!total && state.prefs.windowDays < 7) {
    const wider = (feed.items || []).filter(i => new Date(i.ts) >= refNow() - 7 * 86400000 && effectiveTier(i) === "act").length;
    if (wider) out.push(h("p", { class: "small muted", style: "margin-top:10px" }, `${wider} material filing${wider === 1 ? "" : "s"} in the last 7 days. `, h("button", { class: "linkbtn", onclick: () => { state.prefs.windowDays = 7; savePrefs(); render(); } }, "Widen to 7 days")));
  }
  if (tiers.act.length) out.push(h("h2", {}, "Needs you ", h("span", { class: "count" }, tiers.act.length)), ...tiers.act.map(itemCard));
  if (sigs.length) out.push(h("h2", {}, "Unusual moves ", h("span", { class: "count" }, sigs.length)), ...sigs.slice(0, 8).map(alertCard));
  if (tiers.know.length) out.push(h("h2", {}, "Worth knowing ", h("span", { class: "count" }, tiers.know.length)), h("div", { class: "rows" }, tiers.know.map(knowRow)));
  if (tiers.buried.length) {
    const byCat = {};
    tiers.buried.forEach(i => { byCat[CATEGORY_LABEL[i.category]] = (byCat[CATEGORY_LABEL[i.category]] || 0) + 1; });
    out.push(h("h2", {}, "Hidden"), h("details", { class: "buried" },
      h("summary", {}, `${tiers.buried.length} routine filings`, h("span", { class: "muted small" }, Object.entries(byCat).sort((a, b) => b[1] - a[1]).slice(0, 3).map(([k, v]) => `${v} ${k.toLowerCase()}`).join(", "))),
      h("div", { class: "buried-list" }, tiers.buried.map(i => h("div", {}, h("span", { class: "muted" }, `${i.symbol}`), h("a", { href: i.url, target: "_blank", rel: "noopener" }, i.title))))));
  }
  if (feed.stats) out.push(h("p", { class: "tiny muted", style: "margin-top:22px;text-align:center" },
    `${feed.stats.total} filings tracked over 60 days · ${feed.stats.settled_by_rules} sorted by rules, ${feed.stats.read_by_model} read by a model`));
  return out;
}

/** Last-updated times for each source, always visible under the briefing. */
function freshnessLine(feed, asOf) {
  const st = feed.status || {};
  const filings = st.filings?.updated_at ? stamp(st.filings.updated_at) : `${asOf} IST`;
  const prices = feed.prices_as_of ? `${day(feed.prices_as_of)} close` : "—";
  const window = snapshotAgeHours() > 3 ? " Time windows are measured from the filings update." : "";
  return h("p", { class: "tiny muted", style: "margin-top:8px" }, `Filings updated ${filings} · Prices as of ${prices}.${window}`);
}
function stamp(iso) {
  return new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" }) + " IST";
}
/** Explicit warnings when the last refresh could not update something; the data shown is the last good copy. */
function dataWarnings(feed) {
  const st = feed.status || {}, out = [];
  const failed = Object.keys(st.filings?.companies_failed || {});
  if (failed.length) out.push(`${failed.length} ${failed.length === 1 ? "company" : "companies"} could not be refreshed (${failed.slice(0, 4).join(", ")}${failed.length > 4 ? "…" : ""}); showing their last good data.`);
  if (st.prices && st.prices.ok === false) out.push(`Price refresh failed${st.prices.error ? ` (${st.prices.error})` : ""}; showing prices as of ${feed.prices_as_of ? day(feed.prices_as_of) : "the last good update"}.`);
  return out.map(text => h("p", { class: "small", role: "status", style: "margin-top:8px;color:var(--bad-ink)" }, "⚠ ", text));
}

function viewPortfolio() {
  brandTop(state.feed?.prices_as_of ? `End of day ${day(state.feed.prices_as_of)}` : "");
  const rows = (state.feed?.portfolio || []).slice();
  if (!rows.length) return [h("div", { class: "empty" }, "No portfolio data yet.")];
  const sortKey = store.get("psort", "move");
  rows.sort((a, b) => sortKey === "move" ? Math.abs(b.price?.move_pct ?? 0) - Math.abs(a.price?.move_pct ?? 0) : sortKey === "mcap" ? (b.mcap_cr || 0) - (a.mcap_cr || 0) : a.symbol.localeCompare(b.symbol));
  const latestFiling = sym => (state.feed.items || []).find(i => i.symbol === sym && i.tier !== "buried");
  const ups = rows.filter(r => (r.price?.move_pct ?? 0) > 0).length;
  return [
    h("h1", {}, "Portfolio"),
    h("p", { class: "muted small" }, `${rows.length} holdings · ${ups} up, ${rows.length - ups} down on the day`),
    h("div", { class: "chips", style: "margin:12px 0" }, [["move", "Biggest movers"], ["mcap", "Size"], ["az", "A–Z"]].map(([k, l]) =>
      h("button", { class: "chip", "aria-pressed": String(sortKey === k), onclick: () => { store.set("psort", k); render(); } }, l))),
    h("div", { class: "rows" }, rows.map(r => {
      const p = r.price || {}, f = latestFiling(r.symbol);
      return h("a", { class: "hold", href: `#/c/${encodeURIComponent(r.symbol)}` },
        h("div", { style: "min-width:0" }, h("b", {}, r.symbol),
          r.stale ? h("div", { class: "n", style: "color:var(--bad-ink)", title: r.stale_reason || "" }, `⚠ Not refreshed since ${r.refreshed_at ? day(r.refreshed_at) : "the last run"}`)
                  : h("div", { class: "n" }, f ? `${CATEGORY_LABEL[f.category]} · ${ago(f.ts)}` : r.industry || r.name)),
        sparkline(state.prices?.symbols?.[r.symbol]?.c.slice(-60) || [], { w: 72, h: 28, color: (p.move_1m_pct ?? 0) >= 0 ? "var(--good)" : "var(--bad)" }),
        h("div", { class: "p" }, h("div", { class: "tabular" }, p.close ? INR.format(p.close) : "—"), h("div", { class: `d ${dirClass(p.move_pct)}` }, sp(p.move_pct, 1))));
    })),
    h("p", { class: "tiny muted", style: "margin-top:10px" }, "Line: last 60 sessions. Tap a holding for its research."),
  ];
}

function viewResearchHome() {
  brandTop("Grounded in filings and transcripts");
  const input = h("input", { type: "search", placeholder: "Any NSE symbol or company…", autocomplete: "off", autocapitalize: "characters", "aria-label": "Search companies" });
  const suggest = h("div", { class: "suggest", hidden: true });
  const go = sym => { if (sym) location.hash = `#/c/${encodeURIComponent(sym.toUpperCase())}`; };
  input.addEventListener("keydown", e => { if (e.key === "Enter") { const first = suggest.querySelector("button"); go(first ? first.dataset.sym : input.value.trim()); } });
  input.addEventListener("input", async () => {
    const q = input.value.trim().toUpperCase();
    if (q.length < 2) { suggest.hidden = true; return; }
    if (!state.symbols) { try { state.symbols = await getJSON("data/symbols.json"); } catch { state.symbols = []; } }
    const have = new Set(state.index.map(r => r.symbol));
    const hits = state.symbols.filter(([sym, name]) => sym.startsWith(q) || name.toUpperCase().includes(q))
      .sort((a, b) => (b[0].startsWith(q) - a[0].startsWith(q)) || (have.has(b[0]) - have.has(a[0])) || a[0].length - b[0].length).slice(0, 7);
    suggest.replaceChildren(...hits.map(([sym, name]) => h("button", { "data-sym": sym, onclick: () => go(sym) }, h("b", {}, sym), h("span", { class: "muted" }, name.replace(/ Limited$/, "")), have.has(sym) ? h("span", { class: "tag" }, "ready") : null)));
    suggest.hidden = !hits.length;
  });
  const ready = state.index.slice().sort((a, b) => b.generated_at.localeCompare(a.generated_at));
  return [
    h("h1", {}, "Research"),
    h("p", { class: "muted small" }, "What management promised, what it delivered, and what the numbers say. Primary sources only."),
    h("div", { class: "search" }, input, suggest),
    h("h2", {}, "Ready ", h("span", { class: "count" }, ready.length)),
    ready.length ? h("div", { class: "rows" }, ready.map(r => h("a", { class: "row", href: `#/c/${encodeURIComponent(r.symbol)}`, style: "text-decoration:none" },
      h("div", {}, h("div", { class: "t" }, h("b", {}, r.symbol), " ", h("span", { class: "muted small" }, r.industry || "")),
        r.one_liner ? h("div", { class: "s", style: "color:var(--ink-2)" }, r.one_liner) : null,
        h("div", { class: "s" }, `${r.calls} calls read · ${r.delivered} delivered · ${r.missed} missed${r.behind ? ` · ${r.behind} behind` : ""}`)),
      h("div", { class: "r" }, ago(r.generated_at))))) : h("div", { class: "empty" }, "Nothing researched yet."),
    h("p", { class: "tiny muted", style: "margin-top:12px", "data-api-status": "", role: "status" }, API_STATUS_TEXT[state.apiStatus] || API_STATUS_TEXT.off),
  ];
}

// ---------------------------------------------------------------- company research view
/** Reference chips (f12, g3, c7). Hover shows a title on desktop; tap/click toggles the cited
 *  text inline under the line, so citations work on phones where hover does not exist. */
function refChips(refs, lookup) {
  return (refs || []).map(r => {
    const text = lookup.get(r) || "Not found in this research file.";
    const btn = h("button", { type: "button", class: "ref", title: text, "aria-expanded": "false", "aria-label": `Show source fact ${r}` }, r);
    btn.addEventListener("click", e => {
      e.stopPropagation();
      const host = btn.closest("li, .nvn > div, p, .card") || btn.parentElement;
      const existing = host.querySelector(`:scope > .ref-pop[data-ref="${r}"]`);
      if (existing) { existing.remove(); btn.setAttribute("aria-expanded", "false"); return; }
      host.append(h("div", { class: "ref-pop", role: "note", "data-ref": r }, h("b", {}, `${r} `), text));
      btn.setAttribute("aria-expanded", "true");
    });
    return btn;
  });
}
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  document.querySelectorAll(".ref-pop").forEach(pop => pop.remove());
  document.querySelectorAll('button.ref[aria-expanded="true"]').forEach(b => b.setAttribute("aria-expanded", "false"));
});
function tileEl(t) {
  const fmt = { cr: cr, pct: v => `${v.toFixed(1)}%`, pct0: v => `${v.toFixed(0)}%`, pct2: v => `${v.toFixed(2)}%`, x: v => `${v.toFixed(2)}×`, x1: v => `${v.toFixed(1)}×`, days: v => `${v.toFixed(0)} days` }[t.fmt] || (v => v);
  const dfmt = t.delta_fmt === "pp" ? sp(t.delta, 1, " pp") : sp(t.delta, 0);
  return h("div", { class: "tile" }, h("div", { class: "l" }, t.label), h("div", { class: "v" }, t.value == null ? "—" : fmt(t.value)),
    t.delta != null ? h("div", { class: `d ${dirClass(t.delta, t.good_when !== "low")}` }, `${dfmt} ${t.delta_label || ""}`) : null);
}

/** "Check at FY27 results" / "Check at Q3 FY27 results", or the stated horizon. */
function nextCheckpoint(x) {
  if (x.target_fy) {
    const fy = `FY${String(x.target_fy % 100).padStart(2, "0")}`;
    return `Check at ${x.target_quarter ? `Q${x.target_quarter} ${fy}` : fy} results`;
  }
  return x.horizon ? `Horizon: ${x.horizon}` : null;
}

function ledgerSection(d) {
  const sc = d.scorecard, g = d.guidance || [];
  if (!g.length) return [h("h2", {}, "Promise ledger"), h("div", { class: "card small muted" }, d.coverage.transcripts_available ? "Transcripts were not read for this company (no model access in this run)." : "This company publishes no earnings-call transcripts, so there is no guidance to track.")];
  const order = { missed: 0, behind: 1, delivered: 2, on_track: 3, open: 4, unmeasured: 5 };
  const scored = g.filter(x => x.verdict.status !== "unmeasured").sort((a, b) => order[a.verdict.status] - order[b.verdict.status] || b.call.localeCompare(a.call));
  const watch = g.filter(x => x.verdict.status === "unmeasured");
  const promise = x => h("div", { class: "promise" },
    h("div", { class: "top-line" }, h("span", { class: "label" }, x.label), h("span", { class: "when" }, `${x.quarter} call`)),
    h("div", { class: "said" }, x.statement),
    h("div", { class: "verdict" }, h("span", { class: `vchip ${x.verdict.status}` }, x.verdict.status.replace("_", " ")), h("span", {}, x.verdict.detail)),
    x.quote ? h("blockquote", {}, `“${x.quote}” `, h("a", { href: pageLink(x.url, x.page), target: "_blank", rel: "noopener", style: "color:var(--accent);font-style:normal" }, `p.${x.page} ↗`)) : null);
  const hit = sc.hit_rate_pct;
  if (!sc.scored) {
    // Nothing is checkable yet: a 0/0/0/0 grid would read as "broken". Show what to watch and when.
    const due = g.filter(x => x.verdict.status === "open"), rest = g.filter(x => x.verdict.status !== "open");
    const watchItem = x => h("div", { class: "promise" },
      h("div", { class: "top-line" }, h("span", { class: "label" }, x.label), h("span", { class: "when" }, `${x.quarter} call`)),
      h("div", { class: "said" }, x.statement),
      h("div", { class: "verdict" }, h("span", { class: "vchip open" }, x.verdict.status === "open" ? "not due" : "watch"),
        h("span", {}, nextCheckpoint(x) || x.verdict.detail)),
      nextCheckpoint(x) ? h("div", { class: "tiny muted", style: "margin-top:4px" }, x.verdict.detail) : null,
      x.quote ? h("blockquote", {}, `“${x.quote}” `, h("a", { href: pageLink(x.url, x.page), target: "_blank", rel: "noopener", style: "color:var(--accent);font-style:normal" }, `p.${x.page} ↗`)) : null);
    return [
      h("h2", {}, "Promise ledger ", h("span", { class: "count" }, `${g.length} commitments · ${d.coverage.transcripts_read} calls`)),
      h("div", { class: "card small" },
        h("p", {}, h("b", {}, `Nothing here is checkable against headline numbers yet — ${g.length} commitment${g.length === 1 ? "" : "s"} to watch.`)),
        h("p", { class: "muted", style: "margin-top:4px" }, "A verdict appears once the target period is reported and the promise is on a line the financials carry (revenue, EBITDA, PAT and their growth or margins, capex, gross debt, bank deposits). Order books, capacity, segment and ratio targets stay on watch.")),
      h("div", { class: "rows" }, [...due, ...rest].map(watchItem)),
    ];
  }
  return [
    h("h2", {}, "Promise ledger ", h("span", { class: "count" }, `${g.length} commitments · ${d.coverage.transcripts_read} calls`)),
    h("div", { class: "score", role: "group", "aria-label": "Ledger scorecard" },
      h("div", {}, h("b", { class: "up" }, sc.delivered), h("span", {}, "Delivered")),
      h("div", {}, h("b", { class: "down" }, sc.missed), h("span", {}, "Missed")),
      h("div", {}, h("b", {}, sc.on_track + sc.behind), h("span", {}, sc.behind ? `${sc.behind} behind` : "Running")),
      h("div", {}, h("b", { class: "muted" }, sc.open), h("span", {}, "Not due"))),
    h("p", { class: "tiny muted", style: "margin:8px 0 10px" }, hit != null ? `${hit}% of settled promises kept. ` : "", "Verdicts are computed from reported numbers, not by the model."),
    scored.length ? h("div", { class: "rows" }, scored.map(promise)) : null,
    (d.walks || []).length ? h("div", { class: "card", style: "margin-top:10px" }, h("h3", { class: "small" }, "How guidance moved"), (d.walks || []).map(w =>
      h("p", { class: "small", style: "margin-top:6px" }, h("b", {}, `${w.label}: `), w.steps.map(s => `${s.text} (${s.quarter})`).join(" → "), " ", h("span", { class: `vchip ${w.drift === "cut" ? "missed" : w.drift === "raised" ? "delivered" : "open"}` }, w.drift)))) : null,
    watch.length ? h("details", { class: "buried", style: "margin-top:10px" }, h("summary", {}, `${watch.length} more commitments to watch`, h("span", { class: "muted small" }, "not checkable from headline numbers")),
      h("div", { class: "rows", style: "margin-top:10px;border:0;box-shadow:none" }, watch.map(promise))) : null,
  ];
}

function analysisSections(d, lookup) {
  const syn = d.synthesis;
  if (!syn) return [];
  const out = [];
  if (syn.narrative_vs_numbers?.length) {
    out.push(h("h2", {}, "Narrative vs numbers"));
    syn.narrative_vs_numbers.forEach(n => out.push(h("div", { class: "card" },
      h("div", { class: "card-head" }, h("b", { style: "color:var(--ink);font-size:14px" }, n.theme), h("span", { class: `vchip ${n.verdict}`, style: "margin-left:auto" }, n.verdict)),
      h("div", { class: "nvn" }, h("div", {}, h("div", { class: "k" }, "Management says"), n.says), h("div", {}, h("div", { class: "k" }, "Numbers show"), n.shows, refChips(n.refs, lookup))))));
  }
  if (syn.credibility) out.push(h("div", { class: "card small" }, h("b", {}, "Say–do record. "), syn.credibility));
  if (syn.bull?.length || syn.bear?.length) {
    const side = (title, items, icon) => h("div", { class: "card" }, h("h3", {}, h("span", { "aria-hidden": "true" }, icon), title),
      h("ol", {}, items.map(b => h("li", {}, b.point, refChips(b.refs, lookup), h("span", { class: "e" }, b.evidence)))));
    out.push(h("h2", {}, "Bull vs bear"), h("div", { class: "bb" }, side("Bull case", syn.bull || [], "▲"), side("Bear case", syn.bear || [], "▼")));
  }
  if (syn.watch?.length) out.push(h("h2", {}, "Watch next quarter"), h("div", { class: "card" }, h("ul", { class: "factlist", style: "margin:0" }, syn.watch.map(w => h("li", {}, w)))));
  return out;
}

function fundamentalsSection(d) {
  const f = d.fundamentals, q = f.series.quarterly, a = f.series.annual;
  const tabs = [["snapshot", "Snapshot"], ["trajectory", "Trend"], ["balance", "Balance"], ["cash", "Cash"]];
  let current = store.get("ftab", "snapshot");
  const body = h("div", {});
  const factsFor = section => h("ul", { class: "factlist" }, f.facts.filter(x => x.section === section).map(x =>
    h("li", { class: x.tone, id: `fact-${x.id}` }, x.text, h("span", { class: "ref ref-id", title: `Fact ${x.id} — cited by the analysis above` }, x.id))));
  const draw = () => {
    seg.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.k === current)));
    let content;
    if (current === "snapshot") content = [h("div", { class: "tiles" }, f.snapshot.tiles.map(tileEl)), factsFor("snapshot")];
    else if (current === "trajectory") content = [
      columns(q.labels, q.revenue, { title: "Quarterly revenue", sub: "₹ cr · latest quarter in full colour", extra: i => q.rev_yoy[i] != null ? [`${sp(q.rev_yoy[i], 0)} YoY`] : [] }),
      f.is_lender ? columns(q.labels, q.pat, { title: "Quarterly PAT", sub: "₹ cr" }) : line(q.labels, q.opm, { title: "Operating margin", sub: "% of revenue, quarterly" }),
      factsFor("trajectory")];
    else if (current === "balance") content = [h("div", { class: "tiles" }, f.balance_sheet.tiles.map(tileEl)),
      f.is_lender ? null : columns(a.labels, a.borrowings, { title: "Borrowings", sub: "₹ cr, year end" }), factsFor("balance_sheet")];
    else content = [h("div", { class: "tiles" }, f.cash.tiles.map(tileEl)),
      f.cash.applicable ? columns(a.labels, a.fcf, { title: "Free cash flow", sub: "₹ cr per year (operating cash flow − capex)", extra: i => [`CFO ${cr(a.cfo[i])}`, `Operating profit ${cr(a.op_profit[i])}`] }) : null, factsFor("cash")];
    body.replaceChildren(...content.flat().filter(Boolean));
  };
  const seg = h("div", { class: "seg", role: "group", "aria-label": "Fundamentals" }, tabs.map(([k, l]) => h("button", { "data-k": k, onclick: () => { current = k; store.set("ftab", k); draw(); } }, l)));
  draw();
  return [h("h2", {}, "Fundamentals ", h("span", { class: "count" }, `${f.snapshot.basis} · to ${f.snapshot.quarter || "—"}`)), seg, body];
}

function businessSection(d) {
  const b = d.business, ar = d.annual_report;
  const out = [];
  if (b) out.push(h("h2", {}, "The business"), h("div", { class: "card" },
    h("p", {}, b.what),
    b.segments?.length ? h("div", { class: "facts" }, b.segments.map(sg => h("span", { class: "fact" }, sg.name, sg.share_pct ? h("b", {}, ` ${sg.share_pct}%`) : ""))) : null,
    b.geography ? h("p", { class: "small muted", style: "margin-top:8px" }, b.geography) : null,
    h("p", { class: "tiny muted", style: "margin-top:8px" }, "Source: ", h("a", { href: b.url, target: "_blank", rel: "noopener", style: "color:var(--accent)" }, b.source))));
  if (ar) {
    const notes = [];
    if (ar.audit) notes.push(["Auditor", `${ar.audit.opinion.replace(/_/g, " ")}${ar.audit.note ? ` — ${ar.audit.note}` : ""}`, ar.audit.page, ar.audit.opinion !== "unmodified"]);
    if (ar.contingent_liabilities) notes.push(["Contingent liabilities", [ar.contingent_liabilities.amount, ar.contingent_liabilities.note].filter(Boolean).join(" — "), ar.contingent_liabilities.page, false]);
    if (ar.related_party?.note) notes.push(["Related parties", ar.related_party.note, ar.related_party.page, false]);
    (ar.risks || []).forEach(r => notes.push(["Risk (management's view)", r.risk, r.page, false]));
    if (notes.length) out.push(h("h2", {}, `From the ${ar.year} annual report`), h("div", { class: "rows" }, notes.map(([k, v, p, warn]) => h("div", { class: "promise" },
      h("div", { class: "top-line" }, h("span", { class: "label" }, k), warn ? h("span", { class: "vchip missed" }, "flag") : null, p ? h("a", { class: "when", href: pageLink(ar.url, p), target: "_blank", rel: "noopener", style: "color:var(--accent);text-decoration:none" }, `p.${p} ↗`) : null),
      h("div", { class: "said" }, v)))));
  }
  if (d.pressure?.length) out.push(h("h2", {}, "What analysts pressed on"), h("div", { class: "rows" }, d.pressure.slice(0, 6).map(p => h("div", { class: "promise" },
    h("div", { class: "top-line" }, h("span", { class: "label" }, p.topic), h("span", { class: "when" }, `${p.quarter} call`)),
    h("div", { class: "said" }, p.question),
    h("div", { class: "verdict" }, h("span", { class: `vchip ${p.response === "direct" ? "aligned" : p.response === "deflected" ? "diverging" : "stretched"}` }, p.response === "direct" ? "answered" : p.response))))));
  return out;
}

function trustSection(d) {
  const c = d.coverage;
  return [h("h2", {}, "Sources & checks"), h("div", { class: "card" },
    h("div", { class: "trust" },
      h("div", {}, h("b", {}, `${c.quotes_verified}/${c.quotes_total}`), "quotes found verbatim in the source"),
      h("div", {}, h("b", {}, `${c.statements_checked - c.statements_dropped}/${c.statements_checked}`), "synthesis statements passed the figure check"),
      h("div", {}, h("b", {}, `${c.transcripts_read}`), `of ${c.transcripts_available} transcripts read`)),
    h("ul", { class: "sources" }, d.sources.map(src => h("li", {}, h("a", { href: src.url, target: "_blank", rel: "noopener" }, src.label), src.pages ? h("span", { class: "muted" }, ` · ${src.pages} pages`) : ""))),
    c.notes?.length ? h("p", { class: "tiny muted", style: "margin-top:8px" }, c.notes.join(" ")) : null,
    h("p", { class: "tiny muted", style: "margin-top:8px" }, `Generated ${new Date(d.generated_at).toLocaleString("en-IN")}${c.synthesis_model ? ` · analysis written by ${c.synthesis_model}` : ""} · uses only company filings, annual reports and call transcripts — no news, broker notes or model knowledge.`))];
}

function companyPage(d) {
  const lookup = new Map([...d.fundamentals.facts.map(f => [f.id, f.text]), ...(d.guidance || []).map(g => [g.id, g.statement]), ...(d.claims || []).map(c => [c.id, c.claim])]);
  const m = d.market || {};
  const inPortfolio = (state.meta?.portfolio || []).includes(d.symbol);
  return [
    h("section", { class: "co-head" },
      h("h1", {}, d.name),
      h("div", { class: "muted small" }, `${d.symbol} · ${d.industry || ""}`),
      h("div", { class: "tags" },
        m.price != null ? h("span", { class: "fact" }, h("b", {}, `₹${INR.format(m.price)}`)) : null,
        m.mcap_cr != null ? h("span", { class: "fact" }, `Mcap ${cr(m.mcap_cr)}`) : null,
        m.pe != null ? h("span", { class: "fact" }, `P/E ${m.pe}`) : null,
        inPortfolio ? h("span", { class: "badge ok" }, "In portfolio") : null)),
    d.synthesis?.one_liner ? h("p", { class: "oneliner" }, d.synthesis.one_liner) : null,
    ...ledgerSection(d),
    ...analysisSections(d, lookup),
    ...fundamentalsSection(d),
    ...businessSection(d),
    ...trustSection(d),
  ];
}

async function viewCompany(sym) {
  setTop(h("button", { class: "back", onclick: () => history.length > 1 ? history.back() : (location.hash = "#/research") }, "‹ Back"), h("div", { class: "sub" }, sym));
  if (state.research.has(sym)) return companyPage(state.research.get(sym));
  const known = state.index.find(r => r.symbol === sym);
  if (known) {
    try { const d = await getJSON(`data/research/${known.file}`); state.research.set(sym, d); return companyPage(d); } catch { /* fall through to live */ }
  }
  if (state.api) {
    try { const d = await getJSON(apiUrl(`/api/research/${encodeURIComponent(sym)}`)); state.research.set(sym, d); return companyPage(d); } catch { /* not cached */ }
    return [liveResearch(sym)];
  }
  return [notResearched(sym)];
}

function notResearched(sym, problem) {
  return h("div", {}, h("h1", {}, sym), h("div", { class: "card" },
    h("p", {}, problem || `${sym} hasn't been researched yet.`),
    h("p", { class: "tiny muted", style: "margin-top:6px", "data-api-status": "", role: "status" }, API_STATUS_TEXT[state.apiStatus] || ""),
    h("p", { class: "small muted", style: "margin-top:6px" }, "The engine works on any NSE symbol — run it locally:"),
    h("pre", { class: "small", style: "background:var(--surface-2);padding:10px;border-radius:8px;overflow:auto" }, `python -m finn research ${sym}`)));
}

function liveResearch(sym) {
  const steps = h("ul", { class: "steps" });
  const box = h("div", {}, h("h1", {}, sym), h("p", { class: "muted small" }, "Researching from primary sources. This takes about a minute the first time."), steps);
  const es = new EventSource(apiUrl(`/api/research/${encodeURIComponent(sym)}/stream`));
  let finished = false;
  const seen = new Map();
  es.addEventListener("progress", e => {
    const { stage, detail } = JSON.parse(e.data);
    steps.querySelectorAll("li.now").forEach(li => li.classList.remove("now"));
    const key = stage === "transcripts" ? "transcripts" : stage;
    let li = seen.get(key);
    if (!li) { li = h("li", {}); seen.set(key, li); steps.append(li); }
    li.textContent = detail; li.classList.add("now");
  });
  es.addEventListener("result", e => {
    es.close(); finished = true;
    const d = JSON.parse(e.data); state.research.set(sym, d);
    if (!state.index.find(r => r.symbol === sym)) state.index.push({ symbol: d.symbol, file: `${safeName(d.symbol)}.json`, name: d.name, industry: d.industry, generated_at: d.generated_at, one_liner: d.synthesis?.one_liner, delivered: d.scorecard.delivered, missed: d.scorecard.missed, behind: d.scorecard.behind, calls: d.coverage.transcripts_read });
    if (location.hash === `#/c/${encodeURIComponent(sym)}`) render();
  });
  const fail = (message, status) => {
    es.close(); finished = true;
    if (status) setApiStatus(status);
    box.replaceChildren(notResearched(sym, message));
  };
  const said = e => { try { return String(JSON.parse(e.data).message || ""); } catch { return ""; } };
  // Server messages start with a machine reason ("busy: …"); show them as a sentence.
  const sentence = text => { const t = text.replace(/^(busy|rate limited|daily limit[^:]*):\s*/i, ""); return t.charAt(0).toUpperCase() + t.slice(1) + (/[.!?]$/.test(t) ? "" : "."); };
  es.addEventListener("failed", e => fail(sentence(said(e) || "Research failed partway"), null));   // the run started and broke
  es.addEventListener("refused", e => {                                                            // busy, rate limited or daily cap
    const msg = said(e);
    fail(`Live research couldn't start: ${sentence(msg || "the server declined")}`, /^busy/i.test(msg) ? "busy" : null);
  });
  es.addEventListener("error", () => {                                                              // connection dropped or never opened
    if (!finished) fail("The live research server stopped responding (it may have gone to sleep). Precomputed research is still available.", "unavailable");
  });
  return box;
}

function viewSettings() {
  brandTop("You decide what's unusual");
  const p = state.prefs;
  const slider = (key, label, min, max, step, fmt, hint) => {
    const out = h("span", {}, fmt(p[key]));
    const input = h("input", { type: "range", min, max, step, value: p[key], "aria-label": label });
    input.addEventListener("input", () => { p[key] = +input.value; out.textContent = fmt(p[key]); savePrefs(); updateBadge(); preview(); });
    return h("div", { class: "setting" }, h("label", {}, label, out), input, h("div", { class: "hint" }, hint));
  };
  const previewEl = h("p", { class: "small", style: "margin-top:10px" });
  const preview = () => {
    const last30 = (() => { const d = Object.values(state.prices?.symbols || {})[0]?.d || []; return d.length > 22 ? d.at(-22) : "0"; })();
    const n = signals(last30).length;
    previewEl.textContent = `At these settings: ${n} alert${n === 1 ? "" : "s"} across your portfolio in the last month (about ${(n / 21).toFixed(1)} a day).`;
  };
  preview();
  const meta = state.meta, u = meta?.usage_total, st = state.feed?.stats;
  return [
    h("h1", {}, "Alerts"),
    h("div", { class: "card" },
      slider("move", "Daily price move", 1, 10, 0.5, v => `±${v}%`, "Flag a stock when its close moves at least this much in a day."),
      slider("volume", "Volume spike", 1.2, 5, 0.1, v => `${v.toFixed(1)}×`, "Flag when volume is this multiple of its 20-session average."),
      previewEl),
    h("h2", {}, "Filing types that can interrupt you"),
    h("div", { class: "card" },
      h("p", { class: "small muted" }, "Material filings of these types go to “Needs you”. Others stay in “Worth knowing”."),
      h("div", { class: "chips", style: "flex-wrap:wrap;margin-top:10px" }, ALERTABLE.map(cat => h("button", {
        class: "chip", "aria-pressed": String(p.cats.includes(cat)),
        onclick: e => { p.cats = p.cats.includes(cat) ? p.cats.filter(c => c !== cat) : [...p.cats, cat]; savePrefs(); e.target.setAttribute("aria-pressed", String(p.cats.includes(cat))); updateBadge(); },
      }, CATEGORY_LABEL[cat])))),
    meta ? [h("h2", {}, "What it cost"), h("div", { class: "card" },
      st ? [h("p", { class: "small" }, `Of ${st.total} filings, ${st.settled_by_rules} were sorted by rules for free and ${st.read_by_model} were read by a model.`),
        h("div", { class: "bar2", role: "img", "aria-label": "rules versus model share" }, h("i", { style: `width:${st.settled_by_rules / st.total * 100}%;background:var(--axis)` }), h("i", { style: `width:${st.read_by_model / st.total * 100}%;background:var(--series-1)` })),
        h("p", { class: "tiny muted", style: "margin-top:4px" }, "Grey: rules · Blue: model")] : null,
      u ? h("div", { class: "kv" },
        h("span", {}, "Model calls (all runs)"), h("span", {}, INR.format(u.calls)),
        h("span", {}, "Tokens in / out"), h("span", {}, `${INR.format(u.tokens_in)} / ${INR.format(u.tokens_out)}`),
        h("span", {}, "At paid list prices"), h("span", {}, `$${u.list_cost_usd.toFixed(2)}`),
        h("span", {}, "Actually paid"), h("span", {}, "$0 (free tier)")) : null,
      h("p", { class: "tiny muted", style: "margin-top:8px" }, `Cheap model for reading: ${(meta.routes?.fast || [])[0]?.split(":")[1] || "—"} · stronger model for synthesis: ${(meta.routes?.smart || [])[0]?.split(":")[1] || "—"}`))] : null,
    h("h2", {}, "Appearance"),
    h("div", { class: "chips" }, [["", "System"], ["light", "Light"], ["dark", "Dark"]].map(([v, l]) => h("button", {
      class: "chip", "aria-pressed": String((store.get("theme", "") || "") === v), onclick: () => { store.set("theme", v); applyTheme(); render(); },
    }, l))),
  ];
}

// ---------------------------------------------------------------- router
function applyTheme() { const t = store.get("theme", ""); if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme; }
let renderToken = 0;
async function render() {
  const token = ++renderToken;
  const hash = location.hash || "#/today";
  const [, route, arg] = hash.split("/");
  document.querySelectorAll(".tab").forEach(t => t.setAttribute("aria-current", t.dataset.route === route || (route === "c" && t.dataset.route === "research") ? "page" : "false"));
  const view = $("#view");
  view.style.opacity = 0.6;
  let nodes;
  try {
    if (route === "c" && arg) nodes = await viewCompany(decodeURIComponent(arg).toUpperCase());
    else if (route === "portfolio") nodes = viewPortfolio();
    else if (route === "research") nodes = viewResearchHome();
    else if (route === "settings") nodes = viewSettings();
    else nodes = viewToday();
  } catch (err) {
    console.error(err);
    nodes = [h("p", { class: "err" }, "Something went wrong rendering this view.")];
  }
  if (token !== renderToken) return;
  view.replaceChildren(...nodes.flat().filter(Boolean));
  view.style.opacity = 1;
  updateBadge();
}
document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => { location.hash = `#/${t.dataset.route}`; }));
window.addEventListener("hashchange", () => { render(); window.scrollTo(0, 0); });

/* ---------------------------------------------------------------- live research API
   The API is optional. `data/config.json` may set {"api_base": "https://…"}; without it the API is
   used only when the app itself is served by `python -m finn serve` (same origin). On a static host
   with no API configured, the app is purely the precomputed snapshot. */
const API_STATUS_TEXT = {
  off: "Live research is not configured on this deployment; precomputed research is shown.",
  checking: "Checking the live research server…",
  waking: "The live research server is waking up (free hosting sleeps when idle). This can take about a minute.",
  ready: "Live research is on: any listed NSE symbol can be researched in about a minute.",
  busy: "Live research is busy with another company; try again in a minute.",
  degraded: "Live research is reachable but a data source or the model is not; results may be incomplete.",
  unavailable: "The live research server is unreachable right now; precomputed research is shown.",
};
function apiUrl(path) { return `${state.apiBase || ""}${path}`; }
async function fetchWithTimeout(url, ms) {
  const ctrl = new AbortController(), timer = setTimeout(() => ctrl.abort(), ms);
  try { return await fetch(url, { cache: "no-store", signal: ctrl.signal }); } finally { clearTimeout(timer); }
}
function setApiStatus(status) {
  state.apiStatus = status;
  state.api = status === "ready" || status === "busy" || status === "degraded";
  document.querySelectorAll("[data-api-status]").forEach(el => { el.textContent = API_STATUS_TEXT[status] || ""; });
}
async function checkApi() {
  if (state.apiBase === null) return setApiStatus("off");
  setApiStatus("checking");
  const deadline = Date.now() + (state.apiWakeMs || 100000);  // a sleeping free instance can take ~60s to start
  let first = true;
  while (Date.now() < deadline) {
    try {
      const res = await fetchWithTimeout(apiUrl("/api/health"), first ? 8000 : 15000);
      if (res.ok) {
        const h = await res.json();
        return setApiStatus(h.busy ? "busy" : h.ok ? "ready" : (h.checks?.model_configured ? "degraded" : "unavailable"));
      }
    } catch { /* asleep, unreachable or timed out */ }
    if (state.apiBase === "") return setApiStatus("off");  // same-origin static file server: no API here
    setApiStatus("waking");
    first = false;
    await new Promise(r => setTimeout(r, 5000));
  }
  setApiStatus("unavailable");
}

async function boot() {
  applyTheme();
  const [feed, prices, meta, index, cfg] = await Promise.allSettled([getJSON("data/feed.json"), getJSON("data/prices.json"), getJSON("data/meta.json"), getJSON("data/research/index.json"), getJSON("data/config.json")]);
  state.feed = feed.value || null; state.prices = prices.value || null; state.meta = meta.value || null; state.index = index.value || [];
  const configured = (cfg.value && typeof cfg.value.api_base === "string" && cfg.value.api_base.trim()) ? cfg.value.api_base.trim().replace(/\/+$/, "") : null;
  const static_host = /\.github\.io$|\.pages\.dev$|\.netlify\.app$|\.vercel\.app$/.test(location.hostname);
  state.apiBase = configured ?? (static_host ? null : "");
  state.apiWakeMs = Number(cfg.value?.wake_timeout_s) > 0 ? Number(cfg.value.wake_timeout_s) * 1000 : 100000;
  state.apiStatus = state.apiBase === null ? "off" : "checking";
  render();
  await checkApi();
  if (location.hash.startsWith("#/research") || location.hash.startsWith("#/c/")) render();
  if (typeof notifyNew === "function") notifyNew();
}
boot();
// Installable app: network first, last snapshot when offline. Not registered for the local dev server.
if ("serviceWorker" in navigator && location.protocol === "https:") navigator.serviceWorker.register("sw.js").catch(() => {});
