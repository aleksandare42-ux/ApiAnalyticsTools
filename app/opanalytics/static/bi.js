// "BI tools" page: connection details, files for Tableau / Power BI,
// datasets and exercises. Uses the helpers from app.js.

PAGE_TITLES.bi = "BI tools";
PAGE_LOADERS.bi = loadBiPage;

const biState = {
  info: null,
  tool: "tableau",
  dataset: "transactions",
};

// Exercises from bi/GUIDE_RU.md. Answers come from the seed ground truth.
const EXERCISES = [
  {
    truth: "AlphaPay decline spike",
    question: "Which provider had a sudden approval drop, and when?",
    hint: "Approval rate by provider, hourly, line chart.",
  },
  {
    truth: "BetaGate latency degradation",
    question: "Which provider became slow and for how long?",
    hint: "Average processing time by provider over time.",
  },
  {
    truth: "LuvMatch refund wave",
    question: "Which merchant had an abnormal refund rate?",
    hint: "Refund rate by merchant and day, heat table.",
  },
  {
    truth: "Brazil success collapse",
    question: "Which country had a bad day, and what was the main decline reason?",
    hint: "Approval rate by country and day, then decline_reason for that day.",
  },
  {
    truth: "GammaPSP timeouts",
    question: "Which provider started returning technical errors?",
    hint: "Error rate (is_error) by provider and hour.",
  },
  {
    truth: "MelodyBox integration outage",
    question: "Which merchant's traffic disappeared for several hours?",
    hint: "Transactions per merchant per hour, look for a gap.",
  },
];

function copyButton(value) {
  return `<button class="small ghost" data-copy="${escapeHtml(value)}">copy</button>`;
}

function downloadButton(url, label, primary = false) {
  const cls = primary ? "small primary" : "small";
  return `<a href="${url}" download><button class="${cls}">${label}</button></a>`;
}

function renderConnection(conn) {
  const rows = [
    ["Server / host", conn.host],
    ["Port", conn.port],
    ["Database", conn.database],
    ["Schema", conn.schema],
    ["User", conn.user],
    ["Password", conn.password],
  ];
  const html = rows.map(([label, value]) => `
    <tr><td class="muted">${label}</td><td><code>${escapeHtml(value)}</code></td>
    <td>${copyButton(value)}</td></tr>`).join("");
  $("#bi-connection").innerHTML = `
    <table>${html}</table>
    <p class="muted small">Read-only user, safe to use from any BI tool.
      SSL is not configured: in Tableau leave "Require SSL" off, in Power BI
      answer "OK" when it offers an unencrypted connection.</p>`;
}

function tableauHtml(dataset) {
  const hyper = biState.info.hyper_available
    ? downloadButton("/api/bi/tableau/payops.hyper", "payops.hyper (all datasets)", true)
    : `<p class="muted small">The Hyper API is not installed. Run
       <code>.venv\\Scripts\\pip install -r requirements-bi.txt</code>
       and restart the dashboard (<code>python opa.py restart dashboard</code>).</p>`;

  return `
    <div class="grid g2">
      <div>
        <h3>Tableau Desktop: live connection</h3>
        <ol>
          <li>Tableau Desktop needs the PostgreSQL driver: tableau.com/support/drivers,
            choose PostgreSQL, copy the .jar file to
            <code>C:\\Program Files\\Tableau\\Drivers</code>.</li>
          <li>Download the data source file and double-click it. Tableau asks for the
            password: <code>analyst</code>.</li>
          <li>Or connect by hand: Connect > To a Server > PostgreSQL, use the details above,
            then drag the views (v_transactions, v_hourly_provider...) to the canvas.</li>
          <li>Live connection means the charts show new live traffic after a refresh (F5).</li>
        </ol>
        <div class="row wrap">
          ${downloadButton(`/api/bi/tableau/${dataset}.tds`, `payops_${dataset}.tds`, true)}
        </div>
      </div>
      <div>
        <h3>Tableau Public (free): extract file</h3>
        <ol>
          <li>Tableau Public can't connect to PostgreSQL, but it opens Tableau extracts.</li>
          <li>Download the extract (about 50 MB, takes ~20 s to build). It is a snapshot of
            the data at the moment of download.</li>
          <li>Tableau Public: Connect > To a File > More... > choose payops.hyper. All
            datasets are tables inside, drag the ones you need to the canvas.</li>
          <li>Alternative: Connect > Text file and use the CSV files from the table below.</li>
        </ol>
        <div class="row wrap">${hyper}</div>
      </div>
    </div>`;
}

function powerBiHtml(dataset) {
  return `
    <div class="grid g2">
      <div>
        <h3>Power BI Desktop: PostgreSQL</h3>
        <ol>
          <li>Install Power BI Desktop (free, Microsoft Store or microsoft.com/power-bi).</li>
          <li>Download a connection file and double-click it. Power BI opens with the
            connection ready, enter user <code>analyst</code> / password <code>analyst</code>
            (Database tab, not Windows).</li>
          <li>In the Navigator tick <code>public.v_transactions</code> and the other views,
            press Load.</li>
          <li><b>Import</b> copies the data into the report (fast, refresh by button).
            <b>DirectQuery</b> sends a SQL query on every click (always live, slower).</li>
        </ol>
        <div class="row wrap">
          ${downloadButton("/api/bi/powerbi/postgres.pbids?mode=Import", "Import .pbids", true)}
          ${downloadButton("/api/bi/powerbi/postgres.pbids?mode=DirectQuery", "DirectQuery .pbids")}
        </div>
      </div>
      <div>
        <h3>Power BI: without the database (Web / OData)</h3>
        <ol>
          <li>Web: one dataset as CSV over HTTP. Download the file below or use
            Get data > Web with the CSV link from the datasets table.</li>
          <li>OData: Get data > OData feed, URL <code>${escapeHtml(biState.info.odata_url)}</code>,
            Anonymous. You get all datasets at once.</li>
          <li>Measures: Modeling > New measure, paste them one by one from measures.dax.</li>
        </ol>
        <div class="row wrap">
          ${downloadButton(`/api/bi/powerbi/${dataset}.pbids`, `${dataset} (Web) .pbids`)}
          ${downloadButton("/api/bi/powerbi/measures.dax", "measures.dax")}
          ${downloadButton("/api/bi/powerbi/queries.pq", "queries.pq (Power Query)")}
          ${copyButton(biState.info.odata_url)}
        </div>
      </div>
    </div>`;
}

function excelHtml(dataset) {
  const csvUrl = `${location.origin}/api/bi/csv/${dataset}.csv`;
  return `
    <div class="grid g2">
      <div>
        <h3>Excel</h3>
        <ol>
          <li>Data > Get Data > From Other Sources > From OData Feed, URL
            <code>${escapeHtml(biState.info.odata_url)}</code>, Anonymous.</li>
          <li>Or Data > From Web with the CSV link: <code>${escapeHtml(csvUrl)}</code></li>
          <li>Data > Refresh All reloads the data from the running app.</li>
        </ol>
      </div>
      <div>
        <h3>Anything else</h3>
        <ul>
          <li>Any SQL tool (DBeaver, DataGrip, Metabase, Looker Studio via a connector)
            works with the PostgreSQL details above.</li>
          <li>Any tool that reads CSV over HTTP can use the links in the datasets table.</li>
          <li>Python / R / Jupyter: <code>pandas.read_csv("${escapeHtml(csvUrl)}")</code></li>
        </ul>
      </div>
    </div>`;
}

function renderTool() {
  const dataset = biState.dataset;
  const renderers = { tableau: tableauHtml, powerbi: powerBiHtml, excel: excelHtml };
  $("#bi-tool").innerHTML = renderers[biState.tool](dataset);
}

function renderDatasets(info) {
  $("#bi-odata").innerHTML = `OData feed: <code>${escapeHtml(info.odata_url)}</code>`;
  renderTable($("#bi-datasets"), [
    { label: "Dataset", render: (d) => `<b>${d.name}</b>` },
    { label: "Table / view", render: (d) => `<code>${d.table}</code>` },
    { label: "Rows", num: true, render: (d) => formatNumber(d.rows) },
    { label: "Columns", num: true, render: (d) => d.columns.length },
    { label: "Description", render: (d) => escapeHtml(d.description) },
    {
      label: "",
      render: (d) => `
        <a href="/api/bi/csv/${d.name}.csv" download>csv</a>
        <a href="/odata/${d.entity}?$top=100" target="_blank">odata</a>
        <a href="/api/bi/tableau/${d.name}.tds" download>tds</a>`,
    },
  ], info.datasets);

  $("#bi-dataset").innerHTML = info.datasets
    .map((d) => option(d.name, `${d.name} (${formatNumber(d.rows)} rows)`))
    .join("");
  $("#bi-dataset").value = biState.dataset;
}

async function renderExercises() {
  const evaluation = await api("/api/evaluation");
  const byName = {};
  for (const item of evaluation.truth) {
    byName[item.name] = item;
  }

  const items = EXERCISES.map((exercise, i) => {
    const truth = byName[exercise.truth];
    let answer = "Generate the historical dataset first (Dataset & export page).";
    if (truth) {
      answer = `${truth.dimension} = <b>${escapeHtml(truth.dim_value)}</b>, ` +
        `${escapeHtml(truth.metric)}, ${formatDate(truth.start_at)} - ` +
        `${formatDate(truth.end_at)}. ${escapeHtml(truth.description || "")}`;
    }
    return `
      <li style="margin-bottom:8px">
        ${escapeHtml(exercise.question)}
        <div class="muted small">Hint: ${escapeHtml(exercise.hint)}</div>
        <details class="small"><summary>answer</summary>${answer}</details>
      </li>`;
  });
  $("#bi-exercises").innerHTML = `<ol>${items.join("")}</ol>`;
}

async function checkBiAccess() {
  const container = $("#bi-check-result");
  container.innerHTML = '<p class="muted small">checking...</p>';
  const result = await run(() => api("/api/bi/check"));
  if (!result) {
    container.innerHTML = "";
    return;
  }
  if (result.error) {
    container.innerHTML = `<p class="status declined">Can't connect as
      ${escapeHtml(result.user || "analyst")}: ${escapeHtml(result.error)}</p>`;
    return;
  }
  const failed = result.datasets.filter((d) => !d.ok);
  if (failed.length === 0) {
    container.innerHTML = `<p><span class="status ok">ok</span>
      user <code>${result.user}</code> can read all ${result.datasets.length} datasets
      (${result.ms} ms)</p>`;
  } else {
    const list = failed.map((d) => `${d.name}: ${escapeHtml(d.error)}`).join("<br>");
    container.innerHTML = `<p class="status declined">no access to some datasets</p>
      <p class="small">${list}</p>`;
  }
}

async function loadBiPage() {
  // the page is static, no need to reload it on every auto refresh
  if (biState.info) return;
  biState.info = await api("/api/bi/info");
  renderConnection(biState.info.connection);
  renderDatasets(biState.info);
  renderTool();
  renderExercises();
}

function setupBiPage() {
  onSegmentChange($("#bi-tool-seg"), (tool) => {
    biState.tool = tool;
    if (biState.info) renderTool();
  });

  $("#bi-dataset").addEventListener("change", (event) => {
    biState.dataset = event.target.value;
    renderTool();
  });

  ACTIONS["bi-check"] = checkBiAccess;

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy]");
    if (!button) return;
    try {
      await navigator.clipboard.writeText(button.dataset.copy);
      toast("Copied");
    } catch (e) {
      toast("Can't access the clipboard, copy it by hand", true);
    }
  });
}

setupBiPage();
