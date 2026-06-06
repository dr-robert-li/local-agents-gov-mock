// Front-end: consume the SSE agent stream + poll the portfolio every 30s.
const streamEl = document.getElementById('stream');
const runBtn = document.getElementById('run-btn');
const runStatus = document.getElementById('run-status');

// ---- Live agent stream via SSE ----
function appendEvent(evt) {
  const div = document.createElement('div');
  div.className = `evt ${evt.type}`;
  let body = '';
  switch (evt.type) {
    case 'run_start': body = `run ${evt.run_id} started (${evt.trigger})`; break;
    case 'run_end': body = `run ${evt.run_id} done · equity $${evt.total_equity} · cash $${evt.cash_balance}`; break;
    case 'session': body = `${evt.ticker} → ${evt.session_id}`; break;
    case 'assistant': body = `${evt.ticker ? evt.ticker + ': ' : ''}${evt.text}`; break;
    case 'thinking': body = `${evt.ticker ? evt.ticker + ': ' : ''}${evt.text}`; break;
    case 'tool_use': body = `${evt.ticker}: ${evt.tool_name} ${JSON.stringify(evt.input)}`; break;
    case 'tool_result': body = `${evt.ticker}: ${evt.preview}`; break;
    case 'recommendation': {
      const r = evt.recommendation;
      body = `${r.ticker}: ${r.recommendation} (${r.confidence}) — ${r.rationale}`;
      break;
    }
    case 'portfolio': renderPortfolio(evt.snapshot); return; // don't print blob
    default: body = JSON.stringify(evt);
  }
  div.innerHTML = `<span class="tag">${evt.type}</span>${escapeHtml(body)}`;
  streamEl.appendChild(div);
  streamEl.scrollTop = streamEl.scrollHeight;

  if (evt.type === 'run_start') runStatus.textContent = 'running', runStatus.classList.add('running');
  if (evt.type === 'run_end') { runStatus.textContent = 'idle'; runStatus.classList.remove('running'); loadPortfolio(); }
}

function connectStream() {
  const es = new EventSource('/api/stream');
  es.onmessage = (e) => { try { appendEvent(JSON.parse(e.data)); } catch { /* keep-alive */ } };
  es.onerror = () => { /* EventSource auto-reconnects */ };
}

// ---- Portfolio ----
let chart;
async function loadPortfolio() {
  try {
    const r = await fetch('/api/portfolio');
    if (r.ok) renderPortfolio(await r.json());
  } catch { /* agent may still be warming up */ }
}

function renderPortfolio(p) {
  if (!p) return;
  document.getElementById('cash').textContent = `$${fmt(p.cash_balance)}`;
  document.getElementById('equity').textContent = `$${fmt(p.total_equity)}`;
  document.getElementById('last-run').textContent = p.last_run ? new Date(p.last_run).toLocaleString() : '—';
  const tu = p.token_usage || {};
  document.getElementById('tokens').textContent = tu.total_tokens != null ? Number(tu.total_tokens).toLocaleString() : '—';
  document.getElementById('cost').textContent = tu.cost_usd != null ? `$${Number(tu.cost_usd).toFixed(4)}` : '—';

  const posBody = document.querySelector('#positions tbody');
  posBody.innerHTML = (p.positions || []).map((x) => `
    <tr><td>${x.ticker}</td><td>${fmt(x.qty, 4)}</td><td>$${fmt(x.avg_cost)}</td>
    <td>$${fmt(x.current_price)}</td>
    <td class="${x.pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}">$${fmt(x.pnl)}</td></tr>`).join('')
    || '<tr><td colspan="5" style="color:var(--muted)">no open positions</td></tr>';

  const recBody = document.querySelector('#recs tbody');
  recBody.innerHTML = (p.last_recommendations || []).map((r) => `
    <tr><td>${r.ticker}</td><td class="act-${r.recommendation}">${r.recommendation}</td>
    <td>${r.confidence}</td><td>${escapeHtml(r.rationale || '')}</td></tr>`).join('')
    || '<tr><td colspan="4" style="color:var(--muted)">no recommendations yet</td></tr>';

  renderChart(p.equity_history || []);
}

function renderChart(history) {
  const labels = history.map((h, i) => i + 1);
  const data = history.map((h) => h.total_equity);
  const ctx = document.getElementById('equity-chart');
  if (chart) { chart.data.labels = labels; chart.data.datasets[0].data = data; chart.update(); return; }
  chart = new Chart(ctx, {
    type: 'line',
    data: { labels, datasets: [{ data, borderColor: '#58a6ff', borderWidth: 2, fill: true,
      backgroundColor: 'rgba(88,166,255,0.1)', tension: 0.3, pointRadius: 0 }] },
    options: { responsive: true, plugins: { legend: { display: false } },
      scales: { x: { display: false }, y: { ticks: { color: '#8b949e' }, grid: { color: '#21262d' } } } },
  });
}

// ---- Manual run ----
runBtn.addEventListener('click', async () => {
  runBtn.disabled = true;
  runStatus.textContent = 'running'; runStatus.classList.add('running');
  try { await fetch('/api/run', { method: 'POST' }); } catch { /* surfaced via stream */ }
  runBtn.disabled = false;
});

// ---- Helpers ----
function fmt(n, d = 2) { return (n === null || n === undefined) ? '—' : Number(n).toFixed(d); }
function escapeHtml(s) { return String(s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c])); }

// ---- Boot ----
connectStream();
loadPortfolio();
setInterval(loadPortfolio, 30000);
