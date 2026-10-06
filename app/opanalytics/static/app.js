// Payment Ops dashboard. Plain JS + Chart.js, no build step.

const PROVIDERS = ["alphapay", "betagate", "gammapsp", "deltaacq"];
const PROVIDER_NAMES = {
  alphapay: "AlphaPay",
  betagate: "BetaGate",
  gammapsp: "GammaPSP",
  deltaacq: "DeltaAcq",
};
const WINDOWS = ["15m", "1h", "6h", "24h", "7d", "30d"];
const DIMENSIONS = {
  provider: "Provider",
  country: "Country",
  merchant: "Merchant",
  payment_method: "Method",
  currency: "Currency",
  decline_reason: "Decline reason",
};
const PAGE_TITLES = {
  overview: "Overview",
  breakdown: "Breakdown",
  anomalies: "Anomalies",
  feed: "Live feed",
  control: "Traffic & routing",
  chaos: "Chaos lab",
  tester: "Payment tester",
  data: "Dataset & export",
  sql: "SQL lab",
  services: "Services",
};
const EFFECT_HELP = {
  decline_rate: "Extra decline probability, 0-1 (0.3 means +30pp declines).",
  latency_ms: "Extra processing time in ms. Above the gateway timeout it becomes a timeout.",
  error_rate: "Probability (0-1) that the provider times out or returns 5xx.",
  refund_rate: "Extra probability (0-1) that an approved payment gets refunded.",
  volume_mult: "Multiplier for the share of traffic (0.05 - almost nothing, 3 - spike). " +
    "Only for merchants and countries.",
};
// pages where the time window and source filters make sense
const PAGES_WITH_FILTERS = ["overview", "breakdown", "data"];
const HOST = location.hostname;

const state = {
  page: "overview",
  window: loadSetting("window", "1h"),
  source: loadSetting("source", "all"),
  autoRefresh: true,
  dimension: "provider",
  reference: null,
  presets: {},
  charts: {},
  anomalyStatus: "open",
  anomalySource: "all",
  feedStatus: "",
  lastApprovedId: null,
  timers: {},
  loading: false,
};


// Helpers

function $(selector, root = document) {
  return root.querySelector(selector);
}

function $$(selector, root = document) {
  return Array.from(root.querySelectorAll(selector));
}

// localStorage can throw in private mode, settings are not important
function loadSetting(key, fallback) {
  try {
    return localStorage.getItem("payops." + key) ?? fallback;
  } catch (e) {
    return fallback;
  }
}

function saveSetting(key, value) {
  try {
    localStorage.setItem("payops." + key, value);
  } catch (e) {
    // ignore
  }
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function providerColor(provider) {
  const index = PROVIDERS.indexOf(provider);
  return index >= 0 ? cssVar(`--s${index + 1}`) : cssVar("--s5");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function merchantName(id) {
  const merchant = state.reference?.merchants.find((m) => m.merchant_id === id);
  return merchant ? merchant.name : id;
}

function windowQuery() {
  return `window=${state.window}&source=${state.source}`;
}

function option(value, label = value) {
  return `<option value="${value}">${escapeHtml(label)}</option>`;
}


// Formatting

function formatPercent(value, digits = 1) {
  if (value == null) return "-";
  return (value * 100).toFixed(digits) + "%";
}

function formatNumber(value) {
  if (value == null) return "-";
  return Number(value).toLocaleString("en-US");
}

function formatMs(value) {
  if (value == null) return "-";
  return Math.round(value).toLocaleString("en-US") + " ms";
}

function formatUsd(value) {
  if (value == null) return "-";
  if (Math.abs(value) >= 1e6) return "$" + (value / 1e6).toFixed(2) + "M";
  if (Math.abs(value) >= 1e4) return "$" + (value / 1e3).toFixed(1) + "k";
  return "$" + Number(value).toFixed(0);
}

function formatTime(value) {
  if (!value) return "-";
  return new Date(value).toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

function formatDate(value) {
  if (!value) return "-";
  return new Date(value).toLocaleString([], {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function timeAgo(value) {
  if (!value) return "-";
  const seconds = (Date.now() - new Date(value)) / 1000;
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h ago`;
  return `${(seconds / 86400).toFixed(1)}d ago`;
}

function formatMetric(metric, value) {
  return metric === "latency" ? formatMs(value) : formatPercent(value);
}

function chartLabel(time) {
  const date = new Date(time);
  if (["15m", "1h", "6h", "24h"].includes(state.window)) {
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  return date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit" });
}


// API

async function api(path, options = {}) {
  const request = { method: options.method || "GET", headers: {} };
  if (options.body !== undefined) {
    request.body = JSON.stringify(options.body);
    request.headers["Content-Type"] = "application/json";
  }

  const response = await fetch(path, request);
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (e) {
      data = text;
    }
  }

  if (!response.ok) {
    let message = data?.detail ?? data ?? response.statusText;
    if (typeof message !== "string") {
      message = JSON.stringify(message);
    }
    throw new Error(`${response.status}: ${message}`.slice(0, 400));
  }
  return data;
}

function toast(message, isError = false) {
  const element = document.createElement("div");
  element.textContent = message;
  if (isError) element.className = "err";
  $("#toast").append(element);
  setTimeout(() => element.remove(), isError ? 7000 : 3500);
}

// Runs an action, shows a toast on success or error.
async function run(action, successMessage) {
  try {
    const result = await action();
    if (successMessage) toast(successMessage);
    return result;
  } catch (e) {
    toast(e.message, true);
  }
}

function updateSettings(key, body, message) {
  return run(() => api(`/api/settings/${key}`, { method: "PATCH", body }), message);
}


// Tables

// columns: [{label, key or render, num}], rows: array of objects
function renderTable(container, columns, rows, options = {}) {
  if (!rows || rows.length === 0) {
    container.innerHTML = `<div class="empty">${options.empty || "No data"}</div>`;
    return;
  }

  const head = columns
    .map((col) => `<th class="${col.num ? "num" : ""}">${col.label}</th>`)
    .join("");

  const body = rows.map((row, i) => {
    const cells = columns.map((col) => {
      const value = col.render ? col.render(row) : escapeHtml(row[col.key]);
      const classes = [col.num ? "num" : "", col.cls || ""].join(" ");
      return `<td class="${classes}">${value}</td>`;
    });
    return `<tr data-index="${i}">${cells.join("")}</tr>`;
  }).join("");

  container.innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;

  if (options.onRow) {
    for (const tr of $$("tbody tr", container)) {
      options.onRow(tr, rows[Number(tr.dataset.index)]);
    }
  }
}

function statusLabel(status) {
  return `<span class="status ${status}">${status}</span>`;
}

function severityLabel(severity) {
  return `<span class="sev ${severity}">${severity}</span>`;
}

function providerLabel(provider) {
  const name = escapeHtml(PROVIDER_NAMES[provider] || provider);
  return `<span class="dot" style="background:${providerColor(provider)}"></span>${name}`;
}


// Charts

function chartOptions() {
  const textColor = cssVar("--ink-2");
  const mutedColor = cssVar("--muted");
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: {
        position: "top",
        align: "start",
        labels: {
          color: textColor,
          boxWidth: 10,
          boxHeight: 10,
          useBorderRadius: true,
          borderRadius: 2,
          font: { size: 12 },
        },
      },
      tooltip: {
        backgroundColor: cssVar("--surface"),
        titleColor: cssVar("--ink"),
        bodyColor: textColor,
        borderColor: cssVar("--border"),
        borderWidth: 1,
        padding: 10,
        boxPadding: 4,
        usePointStyle: true,
      },
    },
    scales: {
      x: {
        ticks: {
          color: mutedColor,
          maxRotation: 0,
          autoSkip: true,
          maxTicksLimit: 8,
          font: { size: 11 },
        },
        grid: { display: false },
        border: { color: cssVar("--axis") },
      },
      y: {
        ticks: { color: mutedColor, font: { size: 11 } },
        grid: { color: cssVar("--grid") },
        border: { display: false },
      },
    },
  };
}

function drawChart(id, config) {
  const canvas = document.getElementById(id);
  if (!canvas || typeof Chart === "undefined") return;
  if (state.charts[id]) {
    state.charts[id].destroy();
  }
  state.charts[id] = new Chart(canvas, config);
}

function lineDataset(label, data, color) {
  return {
    label,
    data,
    borderColor: color,
    backgroundColor: color,
    borderWidth: 2,
    pointRadius: 0,
    pointHoverRadius: 4,
    pointHoverBorderWidth: 2,
    pointHoverBorderColor: cssVar("--surface"),
    tension: 0,
    spanGaps: true,
  };
}

function barDataset(label, data, color) {
  return {
    label,
    data,
    backgroundColor: color,
    borderColor: cssVar("--surface"),
    borderWidth: { top: 1 },
    borderRadius: 2,
    maxBarThickness: 18,
    stack: "volume",
  };
}


// Overview page

async function loadOverview() {
  const [kpis, series, reasons, providers, anomalies] = await Promise.all([
    api(`/api/kpis?${windowQuery()}`),
    api(`/api/timeseries?${windowQuery()}&dim=provider`),
    api(`/api/decline_reasons?${windowQuery()}`),
    api(`/api/breakdown?dim=provider&${windowQuery()}`),
    api("/api/anomalies?status=open&limit=8"),
  ]);

  $("#empty-state").hidden = kpis.current.tx > 0;
  renderKpis(kpis);
  drawTimeCharts(series);
  drawReasonsChart(reasons);

  renderTable($("#ov-providers"), [
    { label: "Provider", render: (r) => providerLabel(r.key) },
    { label: "Tx", num: true, render: (r) => formatNumber(r.tx) },
    { label: "Share", num: true, render: (r) => formatPercent(r.share) },
    { label: "Approval", num: true, render: (r) => formatPercent(r.approval_rate) },
    { label: "Errors", num: true, render: (r) => formatPercent(r.error_rate, 2) },
    { label: "Avg", num: true, render: (r) => formatMs(r.avg_ms) },
    { label: "p95", num: true, render: (r) => formatMs(r.p95_ms) },
    { label: "Volume", num: true, render: (r) => formatUsd(r.volume_usd) },
  ], providers);

  renderAnomalyList($("#ov-anoms"), anomalies);
}

function drawTimeCharts(series) {
  const times = [...new Set(series.rows.map((r) => r.t))].sort();
  const indexOf = {};
  times.forEach((t, i) => { indexOf[t] = i; });
  const labels = times.map(chartLabel);
  $("#bucket-label").textContent = `bucket ${series.bucket}`;

  // one value per time bucket for a provider, null where there is no data
  function providerSeries(provider, getValue) {
    const values = new Array(times.length).fill(null);
    for (const row of series.rows) {
      if (row.k === provider) values[indexOf[row.t]] = getValue(row);
    }
    return values;
  }

  // sum over all providers per time bucket
  function totalSeries(getValue) {
    const values = new Array(times.length).fill(0);
    for (const row of series.rows) {
      values[indexOf[row.t]] += getValue(row);
    }
    return values;
  }

  const providers = PROVIDERS.filter((p) => series.rows.some((r) => r.k === p));

  // approval rate, skip buckets with too few payments, they are just noise
  const approvalOptions = chartOptions();
  approvalOptions.scales.y.max = 100;
  approvalOptions.scales.y.ticks.callback = (value) => value + "%";
  approvalOptions.plugins.tooltip.callbacks = {
    label: (ctx) => ` ${ctx.dataset.label}: ${ctx.parsed.y?.toFixed(1)}%`,
  };
  drawChart("ch-approval", {
    type: "line",
    data: {
      labels,
      datasets: providers.map((p) => lineDataset(
        PROVIDER_NAMES[p],
        providerSeries(p, (r) => (r.n >= 5 ? (r.ok / r.n) * 100 : null)),
        providerColor(p),
      )),
    },
    options: approvalOptions,
  });

  const latencyOptions = chartOptions();
  latencyOptions.plugins.tooltip.callbacks = {
    label: (ctx) => ` ${ctx.dataset.label}: ${Math.round(ctx.parsed.y)} ms`,
  };
  drawChart("ch-latency", {
    type: "line",
    data: {
      labels,
      datasets: providers.map((p) => lineDataset(
        PROVIDER_NAMES[p],
        providerSeries(p, (r) => (r.avg_ms == null ? null : Number(r.avg_ms))),
        providerColor(p),
      )),
    },
    options: latencyOptions,
  });

  const volumeOptions = chartOptions();
  volumeOptions.scales.x.stacked = true;
  volumeOptions.scales.y.stacked = true;
  drawChart("ch-volume", {
    type: "bar",
    data: {
      labels,
      datasets: [
        barDataset("Approved", totalSeries((r) => r.ok), cssVar("--good")),
        barDataset("Declined", totalSeries((r) => r.declined), cssVar("--critical")),
        barDataset("Error", totalSeries((r) => r.err), cssVar("--warning")),
      ],
    },
    options: volumeOptions,
  });
}

function drawReasonsChart(reasons) {
  const top = reasons.slice(0, 10);
  const options = chartOptions();
  options.indexAxis = "y";
  options.interaction = { mode: "nearest", axis: "y", intersect: false };
  options.plugins.legend = { display: false };
  options.plugins.tooltip.callbacks = {
    label: (ctx) => {
      const count = formatNumber(top[ctx.dataIndex].n);
      return ` ${ctx.parsed.x.toFixed(1)}%, ${count} tx`;
    },
  };
  // horizontal bars: swap the axis styles
  const { x, y } = options.scales;
  options.scales = {
    x: { ...y, ticks: { ...y.ticks, callback: (value) => value + "%" } },
    y: { ...x, ticks: { color: cssVar("--ink-2"), font: { size: 11 } } },
  };

  drawChart("ch-reasons", {
    type: "bar",
    data: {
      labels: top.map((r) => r.reason),
      datasets: [{
        label: "Share of non-approved",
        data: top.map((r) => r.share * 100),
        backgroundColor: cssVar("--s1"),
        borderRadius: 3,
        maxBarThickness: 16,
      }],
    },
    options,
  });
}

function kpiTile(label, value, current, previous, higherIsBetter, mode) {
  let delta = "&nbsp;";
  let deltaClass = "";

  if (current != null && previous != null && previous !== 0) {
    // rates are compared in percentage points, the rest in percent
    const change = mode === "pp"
      ? (current - previous) * 100
      : (current / previous - 1) * 100;

    if (Math.abs(change) < 0.05) {
      delta = "same as before";
    } else {
      const unit = mode === "pp" ? "pp" : "%";
      const arrow = change > 0 ? "▲" : "▼";
      delta = `${arrow} ${Math.abs(change).toFixed(1)}${unit} vs prev`;

      const noticeable = Math.abs(change) >= (mode === "pp" ? 0.5 : 3);
      const good = higherIsBetter ? change > 0 : change < 0;
      if (noticeable) deltaClass = good ? "good" : "bad";
    }
  }

  return `
    <div class="kpi">
      <div class="lbl">${label}</div>
      <div class="val">${value}</div>
      <div class="delta ${deltaClass}">${delta}</div>
    </div>`;
}

function renderKpis(kpis) {
  const cur = kpis.current;
  const prev = kpis.previous;
  $("#kpis").innerHTML = [
    kpiTile("Transactions", formatNumber(cur.tx), cur.tx, prev.tx, true, "rel"),
    kpiTile("Approved volume", formatUsd(cur.volume_usd),
      cur.volume_usd, prev.volume_usd, true, "rel"),
    kpiTile("Approval rate", formatPercent(cur.approval_rate),
      cur.approval_rate, prev.approval_rate, true, "pp"),
    kpiTile("Conversion (after cascade)", formatPercent(cur.conversion_rate),
      cur.conversion_rate, prev.conversion_rate, true, "pp"),
    kpiTile("Decline rate", formatPercent(cur.decline_rate),
      cur.decline_rate, prev.decline_rate, false, "pp"),
    kpiTile("Error rate", formatPercent(cur.error_rate, 2),
      cur.error_rate, prev.error_rate, false, "pp"),
    kpiTile("Avg processing", formatMs(cur.avg_ms), cur.avg_ms, prev.avg_ms, false, "rel"),
    kpiTile("p95 processing", formatMs(cur.p95_ms), cur.p95_ms, prev.p95_ms, false, "rel"),
    kpiTile("Refund rate", formatPercent(cur.refund_rate, 2),
      cur.refund_rate, prev.refund_rate, false, "pp"),
    kpiTile("Open anomalies", formatNumber(kpis.open_anomalies), null, null),
  ].join("");
}

function renderAnomalyList(container, anomalies) {
  if (anomalies.length === 0) {
    container.innerHTML = '<div class="empty">No open anomalies</div>';
    return;
  }
  // root causes first, the "explained by" ones go to the bottom
  const sorted = [...anomalies].sort(
    (a, b) => Boolean(a.correlated_with) - Boolean(b.correlated_with),
  );
  container.innerHTML = sorted.map((a) => `
    <div class="anom-item" style="${a.correlated_with ? "opacity:.6" : ""}">
      ${severityLabel(a.severity)}
      <div class="msg">
        ${escapeHtml(a.message)}
        <div class="muted small">${a.detected_by}, since ${timeAgo(a.first_seen)}</div>
      </div>
    </div>`).join("");
}


// Breakdown page

async function loadBreakdown() {
  const heatmapRows = $("#hm-rows").value;
  const heatmapCols = $("#hm-cols").value;
  const [rows, cells] = await Promise.all([
    api(`/api/breakdown?dim=${state.dimension}&${windowQuery()}`),
    api(`/api/heatmap?${windowQuery()}&rows=${heatmapRows}&cols=${heatmapCols}`),
  ]);

  const maxShare = Math.max(...rows.map((r) => r.share), 0.0001);
  renderTable($("#breakdown-table"), [
    {
      label: DIMENSIONS[state.dimension],
      render: (r) => (state.dimension === "provider"
        ? providerLabel(r.key)
        : escapeHtml(r.label || r.key || "-")),
    },
    { label: "Tx", num: true, render: (r) => formatNumber(r.tx) },
    {
      label: "Share",
      cls: "bar-cell",
      render: (r) => {
        const width = (r.share / maxShare) * 70;
        return `<i style="width:${width}px"></i><span>${formatPercent(r.share)}</span>`;
      },
    },
    { label: "Approval", num: true, render: (r) => formatPercent(r.approval_rate) },
    { label: "Decline", num: true, render: (r) => formatPercent(r.decline_rate) },
    { label: "Error", num: true, render: (r) => formatPercent(r.error_rate, 2) },
    { label: "Refund", num: true, render: (r) => formatPercent(r.refund_rate, 2) },
    { label: "Avg", num: true, render: (r) => formatMs(r.avg_ms) },
    { label: "p95", num: true, render: (r) => formatMs(r.p95_ms) },
    { label: "Approved volume", num: true, render: (r) => formatUsd(r.volume_usd) },
  ], rows);

  renderHeatmap(cells, heatmapRows, heatmapCols);
}

function renderHeatmap(cells, rowDimension, colDimension) {
  const container = $("#heatmap");
  if (cells.length === 0) {
    container.innerHTML = '<div class="empty">No data</div>';
    return;
  }

  const rowKeys = [...new Set(cells.map((c) => c.r))].sort();
  const colKeys = [...new Set(cells.map((c) => c.c))].sort();
  const byKey = {};
  for (const cell of cells) {
    byKey[cell.r + "|" + cell.c] = cell;
  }

  // color scale from the lowest to the highest approval rate
  const rates = cells.filter((c) => c.n >= 20).map((c) => c.approval_rate);
  const low = Math.min(...rates, 0.6);
  const high = Math.max(...rates, 0.95);

  function label(key, dimension) {
    if (dimension === "provider") return providerLabel(key);
    if (dimension === "merchant") return escapeHtml(merchantName(key));
    return escapeHtml(key);
  }

  function cellHtml(cell) {
    if (!cell) return '<td class="cell muted">.</td>';
    const t = Math.max(0, Math.min(1, (cell.approval_rate - low) / (high - low || 1)));
    const background = `color-mix(in oklab, var(--seq-hi) ${Math.round(t * 100)}%, var(--seq-lo))`;
    const textColor = t > 0.55 ? "#fff" : "var(--ink)";
    const dim = cell.n < 20 ? "dim" : "";
    return `<td class="cell ${dim}" style="background:${background};color:${textColor}"
      title="${cell.n} tx">${formatPercent(cell.approval_rate, 0)}</td>`;
  }

  const header = colKeys
    .map((c) => `<th class="num">${label(c, colDimension)}</th>`)
    .join("");
  const body = rowKeys.map((r) => {
    const tds = colKeys.map((c) => cellHtml(byKey[r + "|" + c])).join("");
    return `<tr><td>${label(r, rowDimension)}</td>${tds}</tr>`;
  }).join("");

  container.innerHTML = `
    <table class="heat">
      <thead><tr><th></th>${header}</tr></thead>
      <tbody>${body}</tbody>
    </table>`;
}


// Anomalies page

function detectorStatusText(status) {
  const s = status.settings;
  let text = `Live detector: every ${s.interval_s}s, last ${s.window_min} min of live traffic ` +
    `vs ${s.baseline_min} min baseline ending ${s.guard_min ?? 5} min earlier`;

  const lastRun = status.last_run;
  if (lastRun) {
    text += `. Last run ${timeAgo(lastRun.at * 1000)}`;
    if (lastRun.error) {
      text += `, ERROR: ${lastRun.error}`;
    } else {
      text += ` (${lastRun.findings} findings, ${lastRun.ms} ms)`;
    }
  }
  if (status.last_scan) {
    text += `. History scan ${timeAgo(status.last_scan.at * 1000)} ` +
      `(${status.last_scan.findings} incidents)`;
  }
  return text;
}

function anomalyLocation(a) {
  if (a.dimension === "provider") return providerLabel(a.dim_value);
  let value = a.dim_value;
  if (a.dimension === "merchant") {
    value = `${merchantName(a.dim_value)} (${a.dim_value})`;
  }
  return `${escapeHtml(a.dimension)} = <b>${escapeHtml(value)}</b>`;
}

async function loadAnomalies() {
  const query = `status=${state.anomalyStatus}&detected_by=${state.anomalySource}`;
  const [anomalies, detector] = await Promise.all([
    api(`/api/anomalies?${query}`),
    api("/api/detector/status").catch(() => null),
  ]);

  if (detector) {
    $("#detector-status").textContent = detectorStatusText(detector);
  }

  const emptyText = state.anomalyStatus === "open"
    ? "No open anomalies. Inject an incident in the Chaos lab and wait about a minute."
    : "Nothing here yet";

  renderTable($("#anomaly-table"), [
    { label: "Severity", render: (a) => severityLabel(a.severity) },
    {
      label: "Status",
      render: (a) => (a.status === "open" ? "<b>open</b>" : '<span class="muted">resolved</span>'),
    },
    { label: "Mode", render: (a) => (a.detected_by === "live" ? "live" : "historical") },
    { label: "Where", render: anomalyLocation },
    {
      label: "Metric",
      render: (a) => {
        let html = escapeHtml(a.metric);
        if (a.correlated_with) {
          html += `<div class="muted small">explained by ${escapeHtml(a.correlated_with)}</div>`;
        }
        return html;
      },
    },
    { label: "Observed", num: true, render: (a) => formatMetric(a.metric, a.observed) },
    { label: "Expected", num: true, render: (a) => formatMetric(a.metric, a.expected) },
    { label: "z", num: true, render: (a) => a.z_score?.toFixed(1) },
    { label: "n", num: true, render: (a) => formatNumber(a.sample_size) },
    {
      label: "Window",
      render: (a) => {
        if (a.detected_by === "batch") {
          return `${formatDate(a.window_start)} - ${formatDate(a.window_end)}`;
        }
        return `${timeAgo(a.first_seen)}, last seen ${timeAgo(a.last_seen)}`;
      },
    },
    {
      label: "",
      render: (a) => {
        let html = '<button class="small ghost" data-sql>Investigate</button>';
        if (a.status === "open") {
          html += ' <button class="small ghost" data-resolve>Resolve</button>';
        }
        return html;
      },
    },
  ], anomalies, {
    empty: emptyText,
    onRow: (tr, anomaly) => {
      tr.title = anomaly.message;
      $("[data-sql]", tr).addEventListener("click", () => investigate(anomaly));
      const resolveButton = $("[data-resolve]", tr);
      if (resolveButton) {
        resolveButton.addEventListener("click", async () => {
          const url = `/api/anomalies/${anomaly.anomaly_id}/resolve`;
          await run(() => api(url, { method: "POST" }));
          loadAnomalies();
        });
      }
    },
  });

  loadEvaluation();
}

async function loadEvaluation() {
  const result = await api("/api/evaluation");
  const container = $("#evaluation");
  if (result.truth.length === 0) {
    container.innerHTML = '<div class="empty">Generate a dataset with injected anomalies, ' +
      'then run "Scan history".</div>';
    return;
  }

  container.innerHTML = `
    <div class="kpis" style="margin-bottom:12px">
      <div class="kpi">
        <div class="lbl">Recall (injected anomalies found)</div>
        <div class="val">${formatPercent(result.recall, 0)}</div>
      </div>
      <div class="kpi">
        <div class="lbl">Precision (findings that match an injection)</div>
        <div class="val">${formatPercent(result.precision, 0)}</div>
      </div>
      <div class="kpi">
        <div class="lbl">Unexplained findings</div>
        <div class="val">${result.unmatched.length}</div>
        <div class="delta">noise or false positives</div>
      </div>
      <div class="kpi">
        <div class="lbl">Grouped under a root cause</div>
        <div class="val">${result.correlated}</div>
        <div class="delta">echoes of an injected incident</div>
      </div>
    </div>
    <div class="table-wrap" id="eval-table"></div>`;

  renderTable($("#eval-table"), [
    {
      label: "Injected anomaly",
      render: (t) => `<b>${escapeHtml(t.name)}</b>
        <div class="muted small">${escapeHtml(t.description || "")}</div>`,
    },
    { label: "Where", render: (t) => `${escapeHtml(t.dimension)}=${escapeHtml(t.dim_value)}` },
    { label: "Metric", key: "metric" },
    { label: "Window", render: (t) => `${formatDate(t.start_at)} - ${formatDate(t.end_at)}` },
    {
      label: "Detected",
      render: (t) => (t.detected
        ? '<span class="status ok">yes</span>'
        : '<span class="status declined">missed</span>'),
    },
    {
      label: "Delay",
      num: true,
      render: (t) => (t.detection_delay_h == null ? "-" : `${t.detection_delay_h.toFixed(1)} h`),
    },
  ], result.truth);
}

// Opens the SQL lab with a query that shows the anomaly minute by minute
// (or hour by hour for historical ones).
function investigate(anomaly) {
  const columns = {
    provider: "provider",
    country: "country",
    merchant: "merchant_id",
    payment_method: "payment_method",
  };
  const column = columns[anomaly.dimension];
  const isLive = anomaly.detected_by === "live";
  const sixHours = 6 * 3600 * 1000;

  let from = "now() - interval '30 minutes'";
  let to = "now()";
  let extraFilter = " AND source = 'live'";
  let bucket = "minute";
  if (!isLive) {
    from = `'${new Date(new Date(anomaly.window_start).getTime() - sixHours).toISOString()}'`;
    to = `'${new Date(new Date(anomaly.window_end).getTime() + sixHours).toISOString()}'`;
    extraFilter = "";
    bucket = "hour";
  }

  $("#sql-text").value = `-- Investigate: ${anomaly.message}
SELECT date_trunc('${bucket}', created_at) AS t,
       count(*) AS tx,
       round(100.0 * avg((status = 'approved')::int), 2) AS approval_pct,
       round(100.0 * avg((status = 'error')::int), 2) AS error_pct,
       round(avg(processing_time_ms)) AS avg_ms,
       round(100.0 * sum(refunded::int)
             / nullif(sum((status = 'approved')::int), 0), 2) AS refund_pct,
       mode() WITHIN GROUP (ORDER BY decline_reason) AS top_decline_reason
FROM transactions
WHERE ${column} = '${anomaly.dim_value}'
  AND created_at BETWEEN ${from} AND ${to}${extraFilter}
GROUP BY 1
ORDER BY 1;`;

  location.hash = "#sql";
  setTimeout(runSql, 50);
}


// Live feed page

async function loadFeed() {
  const params = new URLSearchParams({ limit: 100 });
  if (state.feedStatus) params.set("status", state.feedStatus);
  if ($("#feed-provider").value) params.set("provider", $("#feed-provider").value);
  if ($("#feed-merchant").value) params.set("merchant", $("#feed-merchant").value);

  const rows = await api(`/api/feed?${params}`);
  $("#feed-meta").textContent =
    `latest ${rows.length} attempts, updated ${formatTime(Date.now())}`;

  renderTable($("#feed-table"), [
    { label: "Time", render: (r) => formatTime(r.created_at) },
    { label: "Merchant", render: (r) => escapeHtml(merchantName(r.merchant_id)) },
    { label: "Country", key: "country" },
    {
      label: "Amount",
      num: true,
      render: (r) => `${Number(r.amount).toFixed(2)} ${r.currency}`,
    },
    { label: "Method", key: "payment_method" },
    { label: "Provider", render: (r) => providerLabel(r.provider) },
    {
      label: "Try",
      num: true,
      render: (r) => (r.attempt_no > 1 ? `<b>#${r.attempt_no}</b>` : "1"),
    },
    {
      label: "Status",
      render: (r) => {
        const refunded = r.refunded ? ' <span class="muted small">refunded</span>' : "";
        return statusLabel(r.status) + refunded;
      },
    },
    { label: "Reason", render: (r) => escapeHtml(r.decline_reason || "") },
    { label: "Time, ms", num: true, render: (r) => formatNumber(r.processing_time_ms) },
    { label: "Src", render: (r) => `<span class="muted small">${r.source}</span>` },
  ], rows, {
    empty: "No transactions yet. Start the traffic on the Traffic & routing page " +
      "or send a payment from the Payment tester.",
  });
}


// Traffic & routing page

function fillSettingsForm(settings) {
  const traffic = settings.traffic;
  const gateway = settings.gateway;
  const detector = settings.detector;

  $("#tps").value = traffic.tps;
  $("#tps-val").textContent = traffic.tps;
  $("#gw-cascade").checked = gateway.cascade;
  $("#gw-smart").checked = gateway.smart_routing;
  $("#gw-timeout").value = gateway.timeout_ms;
  $("#det-enabled").checked = detector.enabled;
  $("#det-interval").value = detector.interval_s;
  $("#det-window").value = detector.window_min;
  $("#det-guard").value = detector.guard_min ?? 5;
  $("#det-baseline").value = detector.baseline_min;
}

function renderTrafficStats(stats) {
  const running = stats.settings.running;
  const badge = $("#traffic-state");
  badge.textContent = running ? `running, ${stats.settings.tps} TPS` : "stopped";
  badge.className = running ? "badge on" : "badge";

  const finished = stats.approved + stats.declined + stats.error;
  const items = [
    ["sent", formatNumber(stats.sent)],
    ["approved", finished ? formatPercent(stats.approved / finished) : "-"],
    ["declined", formatNumber(stats.declined)],
    ["errors", formatNumber(stats.error)],
    ["in flight", stats.in_flight],
    ["refunds", formatNumber(stats.refunds)],
    ["HTTP errors", stats.http_errors],
    ["uptime", `${Math.round(stats.uptime_s / 60)} min`],
  ];
  let html = items.map(([label, value]) => `<div><b>${value}</b><span>${label}</span></div>`);
  html = html.join("");
  if (stats.last_error) {
    html += `<div style="grid-column:1/-1"><span>last error</span>
      <code>${escapeHtml(stats.last_error)}</code></div>`;
  }
  $("#traffic-stats").innerHTML = html;
}

function onProviderChange(provider, input) {
  const field = input.dataset.field;
  const value = input.type === "checkbox" ? input.checked : Number(input.value);
  const name = PROVIDER_NAMES[provider.provider_id];
  run(
    () => api(`/api/providers/${provider.provider_id}`, {
      method: "PATCH",
      body: { [field]: value },
    }),
    `${name}: ${field} = ${value}`,
  );
}

async function loadControl(withSettings = true) {
  const [settings, providers, stats] = await Promise.all([
    withSettings ? api("/api/settings") : null,
    api("/api/providers"),
    api("/api/traffic/stats").catch(() => null),
  ]);

  if (settings) fillSettingsForm(settings);
  if (stats) renderTrafficStats(stats);

  // don't re-render while the user is typing into one of the inputs
  if ($("#providers-table").contains(document.activeElement)) return;

  const health = providers.routing?.health || {};
  const weights = providers.routing?.weights_by_method?.card || {};
  const totalWeight = Object.values(weights).reduce((a, b) => a + b, 0) || 1;

  renderTable($("#providers-table"), [
    { label: "Provider", render: (p) => providerLabel(p.provider_id) },
    {
      label: "Enabled",
      render: (p) => `<label class="toggle">
        <input type="checkbox" data-field="enabled" ${p.enabled ? "checked" : ""}><span></span>
      </label>`,
    },
    {
      label: "Weight",
      render: (p) => `<input type="number" data-field="weight" value="${p.weight}"
        min="0" max="1000" style="width:80px">`,
    },
    {
      label: "Card traffic share",
      num: true,
      render: (p) => {
        const weight = weights[p.provider_id];
        return weight == null ? "-" : formatPercent(weight / totalWeight);
      },
    },
    {
      label: "Rolling approval",
      num: true,
      render: (p) => formatPercent(health[p.provider_id]?.approval_rate),
    },
    {
      label: "Base approval",
      render: (p) => `<input type="number" data-field="base_approval"
        value="${p.base_approval}" step="0.01" min="0" max="1" style="width:80px">`,
    },
    {
      label: "Base latency, ms",
      render: (p) => `<input type="number" data-field="base_latency_ms"
        value="${p.base_latency_ms}" step="10" min="10" style="width:90px">`,
    },
    {
      label: "Methods",
      render: (p) => `<span class="muted small">${p.methods.join(", ")}</span>`,
    },
  ], providers.providers, {
    onRow: (tr, provider) => {
      for (const input of $$("[data-field]", tr)) {
        input.addEventListener("change", () => onProviderChange(provider, input));
      }
    },
  });
}


// Chaos lab page

function ruleDescription(rule) {
  let text = rule.target_type;
  if (rule.target_value) text += "=" + escapeHtml(rule.target_value);
  text += `, ${rule.effect} ${rule.value}`;
  if (rule.reason) text += `, ${rule.reason}`;

  if (!rule.live) {
    text += ", inactive";
  } else if (rule.expires_at) {
    const minutes = Math.round((new Date(rule.expires_at) - Date.now()) / 60000);
    text += `, expires in ${Math.max(0, minutes)} min`;
  } else {
    text += ", until stopped";
  }
  return text;
}

async function injectPreset(key) {
  const minutes = $("#preset-min").value || 15;
  await run(
    () => api(`/api/chaos/presets/${key}?minutes=${minutes}`, { method: "POST" }),
    "Incident injected, watch the Overview and Anomalies pages",
  );
  loadChaos();
}

async function stopRule(id) {
  await run(() => api(`/api/chaos/${id}`, { method: "DELETE" }), "Rule stopped");
  loadChaos();
}

async function loadChaos() {
  const rules = await api("/api/chaos");
  const activeNames = new Set(rules.filter((r) => r.live).map((r) => r.name));

  $("#presets").innerHTML = Object.entries(state.presets).map(([key, preset]) => {
    const active = activeNames.has(preset.name);
    return `
      <div class="preset ${active ? "active" : ""}">
        <b>${escapeHtml(preset.name)}</b>
        <p>${escapeHtml(preset.description)}</p>
        <div class="row">
          <button class="small ${active ? "" : "primary"}" data-preset="${key}">
            ${active ? "Inject again" : "Inject"}
          </button>
          <span class="muted small">
            ${preset.target_type}=${preset.target_value}, ${preset.effect} ${preset.value}
          </span>
        </div>
      </div>`;
  }).join("");

  for (const button of $$("[data-preset]")) {
    button.addEventListener("click", () => injectPreset(button.dataset.preset));
  }

  const list = $("#chaos-list");
  if (rules.length === 0) {
    list.innerHTML = '<div class="empty">No rules yet</div>';
    return;
  }
  list.innerHTML = rules.map((rule) => `
    <div class="rule ${rule.live ? "" : "off"}">
      <div>
        <b>${escapeHtml(rule.name)}</b>
        <div class="muted small">${ruleDescription(rule)}</div>
      </div>
      ${rule.live ? `<button class="small" data-stop="${rule.id}">Stop</button>` : ""}
    </div>`).join("");

  for (const button of $$("[data-stop]", list)) {
    button.addEventListener("click", () => stopRule(button.dataset.stop));
  }
}

function fillChaosTargets() {
  const form = $("#chaos-form");
  const ref = state.reference;
  let options = [];

  switch (form.target_type.value) {
    case "global":
      options = [["", "(all traffic)"]];
      break;
    case "provider":
      options = PROVIDERS.map((p) => [p, PROVIDER_NAMES[p]]);
      break;
    case "country":
      options = Object.keys(ref.countries).map((c) => [c, c]);
      break;
    case "merchant":
      options = ref.merchants.map((m) => [m.merchant_id, `${m.name} (${m.merchant_id})`]);
      break;
    case "payment_method":
      options = ref.methods.map((m) => [m, m]);
      break;
  }
  form.target_value.innerHTML = options.map(([value, label]) => option(value, label)).join("");
}


// Dataset page

async function loadData() {
  const exports = [
    "transactions", "hourly_provider", "daily_kpis", "daily_merchant",
    "country_provider", "anomalies", "ground_truth",
  ];
  $("#exports").innerHTML = exports.map((name) => `
    <a href="/api/export/${name}.csv?${windowQuery()}" download>
      <button class="small">${name}.csv</button>
    </a>`).join("");

  const jobs = await api("/api/jobs");
  if (jobs.length > 0) showJob(jobs[0]);
}

function showJob(job) {
  const progress = $("#seed-progress");
  const percent = Math.round(job.progress * 100);
  progress.hidden = false;
  $("i", progress).style.width = `${percent}%`;
  $("span", progress).textContent = `${job.status}, ${percent}%: ${job.message}`;
}

async function watchJob(id) {
  const job = await api(`/api/jobs/${id}`);
  showJob(job);
  if (job.status === "running") {
    setTimeout(() => watchJob(id), 1000);
    return;
  }

  if (job.status === "done") {
    toast(`Dataset ready: ${job.message}`);
    // the seed covers the last 30 days, show all of it
    state.window = "30d";
    saveSetting("window", "30d");
    renderWindowButtons();
  } else {
    toast(`Seed failed: ${job.message}`, true);
  }
}

async function startSeed(body) {
  const job = await run(
    () => api("/api/seed", { method: "POST", body }),
    "Generating the dataset...",
  );
  if (job) watchJob(job.id);
}


// Services page

const SERVICE_DESCRIPTIONS = {
  postgres: "PostgreSQL 16",
  gateway: "Payment API for merchants",
  providers: "PSP simulator (4 providers)",
  traffic: "Merchant traffic generator",
  detector: "Anomaly detector",
  adminer: "Database web UI",
  dashboard: "This UI and the analytics API",
};

function serviceCard(service) {
  const base = `http://${HOST}:${service.port}`;
  let links = "";
  if (service.name === "adminer") {
    links = `<a target="_blank" href="${base}/?pgsql=postgres&username=opa&db=opanalytics">
      open (password: opa)</a>`;
  } else if (service.name === "postgres") {
    links = `<span class="muted">postgresql://opa:opa@${HOST}:5432/opanalytics</span>`;
  } else {
    links = `<a target="_blank" href="${base}/docs">Swagger /docs</a>`;
  }

  let info = service.info;
  if (service.name === "postgres" && info?.size) {
    info = [
      `size ${info.size}`,
      `transactions ${formatNumber(info.tx)} (live ${formatNumber(info.live_tx)})`,
      `from ${formatDate(info.first_tx)}`,
      `to   ${formatDate(info.last_tx)}`,
    ].join("\n");
  } else if (info && typeof info === "object") {
    info = JSON.stringify(info, null, 1).slice(0, 400);
  }

  const timing = service.ms != null ? `, ${service.ms} ms` : "";
  return `
    <div class="svc">
      <h3>
        <span>${service.name} <span class="muted small">:${service.port}</span></span>
        <span class="status ${service.ok ? "ok" : "declined"}">${service.ok ? "up" : "down"}</span>
      </h3>
      <div class="muted small">${SERVICE_DESCRIPTIONS[service.name] || ""}${timing}</div>
      <div class="links">${links}</div>
      ${info ? `<pre>${escapeHtml(info)}</pre>` : ""}
    </div>`;
}

async function loadServices() {
  const services = await api("/api/services");
  const metabase = `
    <div class="svc">
      <h3><span>metabase <span class="muted small">:3000</span></span>
        <span class="muted small">optional</span></h3>
      <div class="muted small">Free BI tool, start it with <code>python opa.py up --bi</code></div>
      <div class="links"><a target="_blank" href="http://${HOST}:3000">open</a></div>
    </div>`;
  $("#services").innerHTML = services.map(serviceCard).join("") + metabase;
}


// SQL lab

function sqlCell(value) {
  if (value === null) return "NULL";
  if (typeof value === "number" && !Number.isInteger(value)) {
    return value.toFixed(4);
  }
  return value;
}

async function runSql() {
  const meta = $("#sql-meta");
  meta.textContent = "running...";
  try {
    const result = await api("/api/sql", {
      method: "POST",
      body: { query: $("#sql-text").value },
    });
    const truncated = result.truncated ? " (truncated)" : "";
    meta.textContent = `${result.rows.length} rows${truncated}, ${result.ms} ms`;

    const columns = result.columns.map((name, i) => ({
      label: escapeHtml(name),
      render: (row) => escapeHtml(sqlCell(row[i])),
    }));
    renderTable($("#sql-result"), columns, result.rows, { empty: "Query returned no rows" });
  } catch (e) {
    meta.textContent = "";
    $("#sql-result").innerHTML =
      `<pre class="code" style="color:var(--critical)">${escapeHtml(e.message)}</pre>`;
  }
}


// Header badges (open anomalies, active chaos rules, traffic)

async function refreshHeader() {
  try {
    const [openAnomalies, rules, settings] = await Promise.all([
      api("/api/anomalies?status=open&limit=500"),
      api("/api/chaos"),
      api("/api/settings"),
    ]);

    const anomalyPill = $("#nav-anoms");
    anomalyPill.hidden = openAnomalies.length === 0;
    anomalyPill.textContent = openAnomalies.length;

    const activeRules = rules.filter((r) => r.live).length;
    const chaosPill = $("#nav-chaos");
    chaosPill.hidden = activeRules === 0;
    chaosPill.textContent = activeRules;

    const traffic = settings.traffic;
    const badge = $("#traffic-badge");
    badge.textContent = traffic.running ? `traffic ${traffic.tps} TPS` : "traffic stopped";
    badge.classList.toggle("on", Boolean(traffic.running));
  } catch (e) {
    $("#traffic-badge").textContent = "API unreachable";
  }
}


// Navigation and auto refresh

const PAGE_LOADERS = {
  overview: loadOverview,
  breakdown: loadBreakdown,
  anomalies: loadAnomalies,
  feed: loadFeed,
  control: () => loadControl(false),
  chaos: loadChaos,
  data: loadData,
  services: loadServices,
};

async function refresh() {
  const load = PAGE_LOADERS[state.page];
  if (!load || state.loading) return;
  state.loading = true;
  try {
    await load();
  } catch (e) {
    console.error(e);
    toast(e.message, true);
  } finally {
    state.loading = false;
  }
}

function startTimers() {
  clearInterval(state.timers.page);
  clearInterval(state.timers.header);

  const interval = state.page === "feed" ? 2500 : 5000;
  state.timers.page = setInterval(() => {
    if (state.autoRefresh && !document.hidden) refresh();
  }, interval);
  state.timers.header = setInterval(() => {
    if (!document.hidden) refreshHeader();
  }, 5000);
}

function showPage() {
  const page = location.hash.slice(1) || "overview";
  state.page = PAGE_TITLES[page] ? page : "overview";

  for (const section of $$(".page")) {
    section.hidden = section.id !== "page-" + state.page;
  }
  for (const link of $$("#nav a")) {
    link.classList.toggle("on", link.dataset.page === state.page);
  }
  $("#page-title").textContent = PAGE_TITLES[state.page];

  const filtersVisible = PAGES_WITH_FILTERS.includes(state.page) ? "visible" : "hidden";
  $("#window-seg").style.visibility = filtersVisible;
  $("#source-sel").style.visibility = filtersVisible;

  if (state.page === "control") {
    loadControl(true).catch((e) => toast(e.message, true));
  } else {
    refresh();
  }
  startTimers();
}

function renderWindowButtons() {
  $("#window-seg").innerHTML = WINDOWS.map((w) => {
    const active = w === state.window ? "on" : "";
    return `<button data-v="${w}" class="${active}">${w}</button>`;
  }).join("");
}

// segmented control: buttons with data-v, one of them is active
function onSegmentChange(container, callback) {
  container.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-v]");
    if (!button) return;
    for (const b of $$("button", container)) {
      b.classList.toggle("on", b === button);
    }
    callback(button.dataset.v);
  });
}


// Setup

function setupTheme() {
  const saved = loadSetting("theme", null);
  if (saved) document.documentElement.dataset.theme = saved;

  $("#theme-btn").addEventListener("click", () => {
    const systemDark = matchMedia("(prefers-color-scheme: dark)").matches;
    const current = document.documentElement.dataset.theme || (systemDark ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    saveSetting("theme", next);
    refresh(); // charts read colors from CSS, redraw them
  });
}

function setupFilters() {
  renderWindowButtons();
  onSegmentChange($("#window-seg"), (value) => {
    state.window = value;
    saveSetting("window", value);
    refresh();
  });

  $("#source-sel").value = state.source;
  $("#source-sel").addEventListener("change", (event) => {
    state.source = event.target.value;
    saveSetting("source", state.source);
    refresh();
  });

  $("#auto-refresh").addEventListener("change", (event) => {
    state.autoRefresh = event.target.checked;
  });

  $("#dim-seg").innerHTML = Object.entries(DIMENSIONS).map(([key, label]) => {
    const active = key === state.dimension ? "on" : "";
    return `<button data-v="${key}" class="${active}">${label}</button>`;
  }).join("");
  onSegmentChange($("#dim-seg"), (value) => {
    state.dimension = value;
    refresh();
  });

  $("#hm-rows").addEventListener("change", refresh);
  $("#hm-cols").addEventListener("change", refresh);

  onSegmentChange($("#an-status"), (value) => {
    state.anomalyStatus = value;
    refresh();
  });
  onSegmentChange($("#an-by"), (value) => {
    state.anomalySource = value;
    refresh();
  });
  onSegmentChange($("#feed-status"), (value) => {
    state.feedStatus = value;
    refresh();
  });
  $("#feed-provider").addEventListener("change", refresh);
  $("#feed-merchant").addEventListener("change", refresh);
}

function setupPaymentTester(ref) {
  const form = $("#pay-form");
  form.merchant_id.innerHTML = ref.merchants
    .map((m) => option(m.merchant_id, `${m.name}, ${m.vertical}`))
    .join("");
  form.country.innerHTML = Object.entries(ref.countries)
    .map(([code, info]) => option(code, `${code} (${info[0]})`))
    .join("");
  form.payment_method.innerHTML = ref.methods.map((m) => option(m)).join("");
  form.force_provider.innerHTML += PROVIDERS
    .map((p) => option(p, PROVIDER_NAMES[p]))
    .join("");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(form));
    const count = Math.max(1, Math.min(200, Number(data.repeat) || 1));
    const body = {
      merchant_id: data.merchant_id,
      country: data.country,
      payment_method: data.payment_method,
      amount: Number(data.amount),
    };
    if (data.force_provider) body.force_provider = data.force_provider;

    // send in batches of 20 so we don't flood the gateway
    const results = [];
    for (let i = 0; i < count; i += 20) {
      const size = Math.min(20, count - i);
      const requests = [];
      for (let j = 0; j < size; j++) {
        requests.push(
          api("/api/test-payment", { method: "POST", body })
            .catch((e) => ({ error: e.message })),
        );
      }
      results.push(...(await Promise.all(requests)));
    }

    const countStatus = (status) => results.filter((r) => r.status === status).length;
    const cascaded = results.filter((r) => r.attempts?.length > 1).length;
    $("#pay-summary").innerHTML = `
      <div class="row" style="margin-bottom:8px">
        ${statusLabel("approved")} ${countStatus("approved")} &nbsp;
        ${statusLabel("declined")} ${countStatus("declined")} &nbsp;
        ${statusLabel("error")} ${countStatus("error")} &nbsp;
        <span class="muted small">cascaded: ${cascaded}</span>
      </div>`;
    $("#pay-result").textContent = JSON.stringify(results[results.length - 1], null, 2);

    const approved = results.filter((r) => r.status === "approved").pop();
    if (approved) {
      state.lastApprovedId = approved.transaction_id;
      $("#refund-btn").disabled = false;
    }
  });
}

function setupChaosForm(ref) {
  const form = $("#chaos-form");
  form.target_type.innerHTML = ref.chaos_targets.map((t) => option(t)).join("");
  form.target_type.value = "provider";
  form.effect.innerHTML = ref.chaos_effects.map((e) => option(e)).join("");
  form.reason.innerHTML += ref.decline_reasons.map((r) => option(r)).join("");

  function showEffectHelp() {
    $("#effect-help").textContent = EFFECT_HELP[form.effect.value] || "";
  }
  form.target_type.addEventListener("change", fillChaosTargets);
  form.effect.addEventListener("change", showEffectHelp);
  if (state.reference) fillChaosTargets();
  showEffectHelp();

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(form));
    const body = {
      target_type: data.target_type,
      target_value: data.target_value || null,
      effect: data.effect,
      value: Number(data.value),
      reason: data.reason || null,
      minutes: Number(data.minutes) || null,
    };
    await run(() => api("/api/chaos", { method: "POST", body }), "Rule injected");
    loadChaos();
  });
}

function setupControlForm() {
  $("#tps").addEventListener("input", (event) => {
    $("#tps-val").textContent = event.target.value;
  });
  $("#tps").addEventListener("change", (event) => {
    const tps = Number(event.target.value);
    updateSettings("traffic", { tps }, `TPS set to ${tps}`);
  });
  $("#gw-cascade").addEventListener("change", (event) => {
    const on = event.target.checked;
    updateSettings("gateway", { cascade: on }, `Cascading ${on ? "on" : "off"}`);
  });
  $("#gw-smart").addEventListener("change", (event) => {
    const on = event.target.checked;
    updateSettings("gateway", { smart_routing: on }, `Smart routing ${on ? "on" : "off"}`);
  });
  $("#gw-timeout").addEventListener("change", (event) => {
    updateSettings("gateway", { timeout_ms: Number(event.target.value) }, "Timeout updated");
  });
  $("#det-enabled").addEventListener("change", (event) => {
    const on = event.target.checked;
    updateSettings("detector", { enabled: on }, `Detector ${on ? "on" : "off"}`);
  });

  const detectorFields = {
    "#det-interval": "interval_s",
    "#det-window": "window_min",
    "#det-guard": "guard_min",
    "#det-baseline": "baseline_min",
  };
  for (const [selector, key] of Object.entries(detectorFields)) {
    $(selector).addEventListener("change", (event) => {
      const value = Number(event.target.value);
      updateSettings("detector", { [key]: value }, `Detector ${key} = ${value}`);
    });
  }
}

function setupSeedForm() {
  $("#seed-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.target;
    startSeed({
      rows: Number(form.rows.value),
      days: Number(form.days.value),
      inject_anomalies: form.inject_anomalies.checked,
      replace: form.replace.checked,
      scan_after: form.scan_after.checked,
    });
  });
}

async function setupSqlLab() {
  const examples = await api("/api/sql/examples").catch(() => []);
  $("#sql-examples").innerHTML += examples
    .map((example, i) => option(i, example.title))
    .join("");

  $("#sql-examples").addEventListener("change", (event) => {
    if (event.target.value === "") return;
    $("#sql-text").value = examples[Number(event.target.value)].sql;
    runSql();
  });
  $("#sql-text").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) runSql();
  });
}

// For dangerous buttons: the first click only asks for confirmation.
function confirmedClick(button) {
  if (button.dataset.armed) return true;
  const text = button.textContent;
  button.dataset.armed = "1";
  button.textContent = "Click again to confirm";
  setTimeout(() => {
    delete button.dataset.armed;
    button.textContent = text;
  }, 3000);
  return false;
}

function exportBreakdownCsv() {
  const lines = $$("#breakdown-table tr").map((tr) => {
    const cells = $$("th,td", tr).map((cell) => `"${cell.textContent.replace(/"/g, '""')}"`);
    return cells.join(",");
  });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }));
  link.download = `breakdown_${state.dimension}_${state.window}.csv`;
  link.click();
}

// Buttons with data-action="..." in index.html
const ACTIONS = {
  "quick-seed": () => startSeed({
    rows: 500000,
    days: 30,
    inject_anomalies: true,
    replace: true,
    scan_after: true,
  }),

  "quick-traffic": async () => {
    await updateSettings("traffic", { running: true, tps: 15 }, "Traffic started");
    state.window = "15m";
    renderWindowButtons();
    refresh();
    refreshHeader();
  },

  "traffic-start": async () => {
    const tps = Number($("#tps").value);
    await updateSettings("traffic", { running: true, tps }, "Traffic started");
    await loadControl(false);
    refreshHeader();
  },

  "traffic-stop": async () => {
    await updateSettings("traffic", { running: false }, "Traffic stopped");
    await loadControl(false);
    refreshHeader();
  },

  "burst": () => {
    const count = $("#burst-n").value;
    return run(
      () => api(`/api/traffic/burst?count=${count}`, { method: "POST" }),
      `Burst of ${count} payments queued`,
    );
  },

  "traffic-reset-stats": async () => {
    await run(() => api("/api/traffic/reset-stats", { method: "POST" }));
    loadControl(false);
  },

  "providers-reset": async () => {
    await run(() => api("/api/providers/reset", { method: "POST" }), "Providers reset");
    loadControl(false);
  },

  "detector-run": async () => {
    const result = await run(() => api("/api/detector/run", { method: "POST" }));
    if (result) toast(`Live detection: ${result.findings} findings in ${result.ms} ms`);
    loadAnomalies();
  },

  "detector-scan": async () => {
    toast("Scanning history...");
    const result = await run(() => api("/api/detector/scan", { method: "POST" }));
    if (result) toast(`History scan: ${result.findings} incidents in ${result.ms} ms`);
    loadAnomalies();
  },

  "clear-anomalies": async () => {
    await run(
      () => api("/api/anomalies?detected_by=live", { method: "DELETE" }),
      "Live anomalies cleared",
    );
    loadAnomalies();
  },

  "chaos-clear": async () => {
    await run(() => api("/api/chaos", { method: "DELETE" }), "All chaos rules stopped");
    loadChaos();
  },

  "refund-last": async () => {
    if (!state.lastApprovedId) return;
    const url = `/api/test-refund/${state.lastApprovedId}`;
    const result = await run(() => api(url, { method: "POST" }), "Refunded");
    if (result) $("#pay-result").textContent = JSON.stringify(result, null, 2);
    $("#refund-btn").disabled = true;
  },

  "sql-run": runSql,

  "export-breakdown": exportBreakdownCsv,

  "reset": (button) => {
    const scope = button.dataset.scope;
    if (scope === "all" && !confirmedClick(button)) return;
    return run(
      () => api(`/api/reset?scope=${scope}`, { method: "POST" }),
      `Reset: ${scope}`,
    );
  },
};

async function init() {
  setupTheme();
  setupFilters();

  try {
    state.reference = await api("/api/reference");
    state.presets = await api("/api/chaos/presets");
  } catch (e) {
    toast("API is not reachable: " + e.message, true);
  }
  const ref = state.reference || {
    merchants: [],
    countries: {},
    methods: [],
    decline_reasons: [],
    chaos_targets: [],
    chaos_effects: [],
  };

  $("#feed-provider").innerHTML += PROVIDERS.map((p) => option(p, PROVIDER_NAMES[p])).join("");
  $("#feed-merchant").innerHTML += ref.merchants
    .map((m) => option(m.merchant_id, m.name))
    .join("");

  setupPaymentTester(ref);
  setupChaosForm(ref);
  setupControlForm();
  setupSeedForm();
  await setupSqlLab();

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    const action = ACTIONS[button.dataset.action];
    if (action) action(button);
  });

  window.addEventListener("hashchange", showPage);
  showPage();
  refreshHeader();
}

init();
