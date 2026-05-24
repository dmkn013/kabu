'use strict';

const RUNS_URL     = '../data/runs.json';
const INITIAL_CASH = 500000;

let currentRunId = null;
let runsData = [];

// ---- ユーティリティ ----

function fmt(n) {
  return '¥' + Math.round(n).toLocaleString('ja-JP');
}

function parseCSV(text) {
  const lines = text.trim().split(/\r?\n/);
  if (lines.length < 2) return [];
  const headers = lines[0].split(',');
  return lines.slice(1).filter(l => l.trim()).map(line => {
    const vals = [];
    let cur = '', inQ = false;
    for (const ch of line) {
      if (ch === '"') { inQ = !inQ; }
      else if (ch === ',' && !inQ) { vals.push(cur); cur = ''; }
      else { cur += ch; }
    }
    vals.push(cur);
    return Object.fromEntries(headers.map((h, i) => [h, (vals[i] || '').trim()]));
  });
}

// ---- RUN セレクタ初期化 ----

function countWeekdays(startStr, endStr) {
  let count = 0;
  const cur = new Date(startStr);
  const end = new Date(endStr);
  while (cur < end) {
    const d = cur.getDay();
    if (d !== 0 && d !== 6) count++;
    cur.setDate(cur.getDate() + 1);
  }
  return count;
}

function getRunMeta(runId) {
  return runsData.find(r => r.id === runId) || null;
}

function renderDayCounter(summary, runMeta) {
  const el = document.getElementById('day-counter');
  if (!el || !runMeta) return;
  const elapsed = summary.length;
  const total = countWeekdays(runMeta.start_date, runMeta.end_date);
  el.textContent = elapsed === 0 ? `0/${total}日目（開始前）` : `${elapsed}/${total}日目`;
}

async function initRunSelector() {
  try {
    const runs = await fetch(RUNS_URL).then(r => r.json()).then(d => d.runs || []);
    runsData = runs;
    const sel = document.getElementById('run-selector');
    sel.innerHTML = '';
    runs.forEach(run => {
      const opt = document.createElement('option');
      opt.value = run.id;
      opt.textContent = `${run.name} (${run.status})`;
      sel.appendChild(opt);
    });
    if (runs.length > 0) {
      currentRunId = runs[0].id;
      await loadRunData(currentRunId);
    }
    sel.addEventListener('change', async () => {
      currentRunId = sel.value;
      await loadRunData(currentRunId);
    });
  } catch (e) {
    document.getElementById('last-updated').textContent = 'runs.json 読み込みエラー: ' + e.message;
  }
}

// ---- データ読み込み ----

async function loadRunData(runId) {
  const base = `../data/runs/${runId}`;
  try {
    const [portfolio, trades, summary] = await Promise.all([
      fetch(`${base}/portfolio.json`).then(r => { if (!r.ok) throw new Error('portfolio.json not found'); return r.json(); }),
      fetch(`${base}/trades.csv`).then(r => r.text()).then(parseCSV),
      fetch(`${base}/daily_summary.csv`).then(r => r.text()).then(parseCSV).catch(() => []),
    ]);
    renderSummary(portfolio);
    renderLongPositions(portfolio);
    renderShortPositions(portfolio);
    renderTradesTable(trades);
    renderChart(summary, portfolio.initial_cash || INITIAL_CASH);
    renderDayCounter(summary, getRunMeta(runId));
    const fetchedAt = new Date().toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' });
    document.getElementById('last-updated').textContent =
      'データ最終更新: ' + (portfolio.last_updated || '未実行') +
      '　取得: ' + fetchedAt;
  } catch (e) {
    document.getElementById('last-updated').textContent = 'データ読み込みエラー: ' + e.message;
    console.error(e);
  }
}

// ---- サマリカード ----

function renderSummary(portfolio) {
  const cash        = parseFloat(portfolio.cash) || 0;
  const initialCash = parseFloat(portfolio.initial_cash) || INITIAL_CASH;
  const positions   = portfolio.positions || {};
  const shortPos    = portfolio.short_positions || {};

  const longValue    = Object.values(positions).reduce((s, p) => s + p.shares * p.avg_price, 0);
  const shortExp     = Object.values(shortPos).reduce((s, p) => s + p.shares * p.avg_short_price, 0);
  const total        = cash + longValue - shortExp;
  const pnl          = total - initialCash;
  const pct          = initialCash > 0 ? (pnl / initialCash * 100) : 0;

  setText('cash',           fmt(cash));
  setText('long-value',     fmt(longValue));
  setText('short-exposure', fmt(shortExp));
  setText('total-value',    fmt(total));

  const cls  = pnl > 0 ? 'positive' : pnl < 0 ? 'negative' : 'neutral';
  const sign = pnl >= 0 ? '+' : '';

  const pnlEl = document.getElementById('pnl');
  pnlEl.textContent = sign + fmt(pnl);
  pnlEl.className   = 'card-value ' + cls;

  const pctEl = document.getElementById('pnl-pct');
  pctEl.textContent = sign + pct.toFixed(2) + '%';
  pctEl.className   = 'card-value ' + cls;
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

// ---- ロングポジション ----

function renderLongPositions(portfolio) {
  const positions = portfolio.positions || {};
  const wrapper   = document.getElementById('long-positions-wrapper');
  const keys      = Object.keys(positions);

  if (keys.length === 0) {
    wrapper.innerHTML = '<p class="empty-msg">ポジションなし</p>';
    return;
  }

  const rows = keys.map(sym => {
    const p     = positions[sym];
    const value = p.shares * p.avg_price;
    return `<tr>
      <td>${sym}</td>
      <td>${p.shares.toLocaleString()}株</td>
      <td>${fmt(p.avg_price)}</td>
      <td>${fmt(p.avg_price)}</td>
      <td>${fmt(value)}</td>
      <td class="neutral">±¥0</td>
    </tr>`;
  }).join('');

  wrapper.innerHTML = `
    <div class="table-scroll">
      <table>
        <thead><tr>
          <th>銘柄</th><th>株数</th><th>取得単価</th>
          <th>参考単価</th><th>評価額</th><th>評価損益</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

// ---- ショートポジション ----

function renderShortPositions(portfolio) {
  const shortPos = portfolio.short_positions || {};
  const wrapper  = document.getElementById('short-positions-wrapper');
  const keys     = Object.keys(shortPos);

  if (keys.length === 0) {
    wrapper.innerHTML = '<p class="empty-msg">ポジションなし</p>';
    return;
  }

  const rows = keys.map(sym => {
    const p   = shortPos[sym];
    const exp = p.shares * p.avg_short_price;
    return `<tr>
      <td>${sym}</td>
      <td>${p.shares.toLocaleString()}株</td>
      <td>${fmt(p.avg_short_price)}</td>
      <td>${fmt(p.avg_short_price)}</td>
      <td>${fmt(exp)}</td>
      <td class="neutral">±¥0</td>
    </tr>`;
  }).join('');

  wrapper.innerHTML = `
    <div class="table-scroll">
      <table>
        <thead><tr>
          <th>銘柄</th><th>株数</th><th>建値</th>
          <th>参考単価</th><th>建玉額</th><th>含み損益</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

// ---- 取引履歴テーブル ----

function renderTradesTable(trades) {
  const tbody = document.getElementById('trades-body');
  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty-msg">取引なし</td></tr>';
    return;
  }

  const rows = [...trades].reverse().map(t => {
    const action = (t.action || '').toUpperCase();
    const status = (t.status || '').toUpperCase();
    const actionBadge = `<span class="badge badge-${action.toLowerCase()}">${action}</span>`;
    const statusBadge = `<span class="badge badge-${status.toLowerCase()}">${status}</span>`;
    const price   = parseFloat(t.price);
    const cash    = parseFloat(t.cash_after) || 0;
    const priceStr = (status === 'WAIT' || isNaN(price) || price === 0) ? '—' : fmt(price);
    const rowClass = status === 'WAIT' ? ' class="row-wait"' : '';
    return `<tr${rowClass}>
      <td>${t.date}</td>
      <td>${t.symbol}</td>
      <td>${actionBadge}</td>
      <td>${parseInt(t.shares || 0).toLocaleString()}株</td>
      <td>${priceStr}</td>
      <td>${statusBadge}</td>
      <td>${fmt(cash)}</td>
    </tr>`;
  }).join('');

  tbody.innerHTML = rows;
}

// ---- チャート ----

function renderChart(summary, initialCash) {
  if (!summary.length) {
    const empty = '<p class="empty-msg">チャートデータなし（取引後に表示されます）</p>';
    document.getElementById('total-chart').parentElement.innerHTML = empty;
    document.getElementById('cash-chart').parentElement.innerHTML  = empty;
    return;
  }

  const labels    = ['開始前', ...summary.map(r => r.date)];
  const totalData = [initialCash, ...summary.map(r => parseFloat(r.total_value))];
  const cashData  = [initialCash, ...summary.map(r => parseFloat(r.cash))];

  _makeChart('total-chart', labels, totalData, '総資産', '#e63946', 'rgba(230,57,70,0.07)');
  _makeChart('cash-chart',  labels, cashData,  '現金残高',           '#1a6fc9', 'rgba(26,111,201,0.07)');
}

function _makeChart(canvasId, labels, data, label, color, bgColor) {
  const canvas   = document.getElementById(canvasId);
  const existing = Chart.getChart(canvas);
  if (existing) existing.destroy();
  new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label,
        data,
        borderColor: color,
        backgroundColor: bgColor,
        borderWidth: 2,
        pointRadius: 4,
        pointHoverRadius: 6,
        fill: true,
        tension: 0,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: ctx => label + ': ¥' + Math.round(ctx.raw).toLocaleString('ja-JP'),
          },
        },
      },
      scales: {
        y: { ticks: { callback: v => '¥' + Math.round(v / 1000) + 'k' } },
      },
    },
  });
}

// ---- 自動更新 ----

async function refresh() {
  if (currentRunId) {
    try {
      const runs = await fetch(RUNS_URL).then(r => r.json()).then(d => d.runs || []);
      runsData = runs;
      const sel = document.getElementById('run-selector');
      runs.forEach(run => {
        const opt = sel.querySelector(`option[value="${run.id}"]`);
        if (opt) opt.textContent = `${run.name} (${run.status})`;
      });
    } catch (_) {}
    await loadRunData(currentRunId);
  }
}

initRunSelector();
setInterval(refresh, 60 * 1000);
