'use strict';

const RUNS_URL     = '../data/runs.json';
const NAMES_URL    = '../data/topix_symbols.json';
const PRICES_URL   = '../data/market_prices.json';
const INITIAL_CASH = 500000;

let currentRunId  = null;
let runsData      = [];
let symMap        = {};   // code -> name
let marketPrices  = {};   // code -> current price

// ---- ユーティリティ ----

function fmt(n) {
  return '¥' + Math.round(n).toLocaleString('ja-JP');
}

function fmtPnl(n) {
  const sign = n >= 0 ? '+' : '';
  return sign + fmt(n);
}

function pnlClass(n) {
  return n > 0 ? 'positive' : n < 0 ? 'negative' : 'neutral';
}

function symName(code) {
  return symMap[code] ? `<span class="sym-name">${symMap[code]}</span>` : '';
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

function renderDayCounter(trades, runMeta) {
  const el = document.getElementById('day-counter');
  if (!el || !runMeta) return;
  // start_date 以降の FILLED 取引がある日をカウント
  const start = runMeta.start_date || '0000-00-00';
  const tradeDates = new Set(
    trades.filter(t => t.status === 'FILLED' && t.date >= start).map(t => t.date)
  );
  const elapsed = tradeDates.size;
  const total = countWeekdays(runMeta.start_date, runMeta.end_date);
  el.textContent = elapsed === 0 ? `0/${total}日目（開始前）` : `${elapsed}/${total}日目`;
}

async function initRunSelector() {
  // 銘柄名マスタを一度だけロード
  try {
    const data = await fetch(NAMES_URL).then(r => r.json());
    symMap = Object.fromEntries((data.symbols || []).map(s => [s.code, s.name]));
  } catch (_) {}

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
    const [portfolio, trades, summary, intraday, mp] = await Promise.all([
      fetch(`${base}/portfolio.json`).then(r => { if (!r.ok) throw new Error('portfolio.json not found'); return r.json(); }),
      fetch(`${base}/trades.csv`).then(r => r.ok ? r.text() : '').then(parseCSV).catch(() => []),
      fetch(`${base}/daily_summary.csv`).then(r => r.ok ? r.text() : '').then(parseCSV).catch(() => []),
      fetch(`${base}/intraday.csv`).then(r => r.ok ? r.text() : '').then(parseCSV).catch(() => []),
      fetch(PRICES_URL).then(r => r.ok ? r.json() : { prices: {} }).catch(() => ({ prices: {} })),
    ]);

    marketPrices = mp.prices || {};

    renderSummary(portfolio);
    renderLongPositions(portfolio);
    renderShortPositions(portfolio);
    renderTradesTable(trades);
    renderChart(summary, intraday, portfolio.initial_cash || INITIAL_CASH);
    renderDayCounter(trades, getRunMeta(runId));

    const fetchedAt = new Date().toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' });
    const priceAt   = mp.updated_at ? `　現在値: ${mp.updated_at}` : '';
    document.getElementById('last-updated').textContent =
      'データ最終更新: ' + (portfolio.last_updated || '未実行') +
      priceAt + '　取得: ' + fetchedAt;
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

  const longValue = Object.entries(positions).reduce((s, [sym, p]) => {
    return s + p.shares * (marketPrices[sym] || p.avg_price);
  }, 0);
  const shortExp = Object.entries(shortPos).reduce((s, [sym, p]) => {
    return s + p.shares * (marketPrices[sym] || p.avg_short_price);
  }, 0);
  const total = cash + longValue - shortExp;
  const pnl   = total - initialCash;
  const pct   = initialCash > 0 ? (pnl / initialCash * 100) : 0;

  setText('cash',           fmt(cash));
  setText('long-value',     fmt(longValue));
  setText('short-exposure', fmt(shortExp));
  setText('total-value',    fmt(total));

  const pnlEl = document.getElementById('pnl');
  pnlEl.textContent = fmtPnl(pnl);
  pnlEl.className   = 'card-value ' + pnlClass(pnl);

  const pctEl = document.getElementById('pnl-pct');
  pctEl.textContent = (pnl >= 0 ? '+' : '') + pct.toFixed(2) + '%';
  pctEl.className   = 'card-value ' + pnlClass(pnl);
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
    const p        = positions[sym];
    const curPrice = marketPrices[sym] || p.avg_price;
    const value    = p.shares * curPrice;
    const pnl      = (curPrice - p.avg_price) * p.shares;
    return `<tr>
      <td><span class="sym-code">${sym}</span>${symName(sym)}</td>
      <td>${p.shares.toLocaleString()}株</td>
      <td>${fmt(p.avg_price)}</td>
      <td>${fmt(curPrice)}</td>
      <td>${fmt(value)}</td>
      <td class="${pnlClass(pnl)}">${fmtPnl(pnl)}</td>
    </tr>`;
  }).join('');

  wrapper.innerHTML = `
    <div class="table-scroll">
      <table>
        <thead><tr>
          <th>銘柄</th><th>株数</th><th>取得単価</th>
          <th>現在値</th><th>評価額</th><th>評価損益</th>
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
    const p        = shortPos[sym];
    const curPrice = marketPrices[sym] || p.avg_short_price;
    const exp      = p.shares * p.avg_short_price;
    const pnl      = (p.avg_short_price - curPrice) * p.shares;
    return `<tr>
      <td><span class="sym-code">${sym}</span>${symName(sym)}</td>
      <td>${p.shares.toLocaleString()}株</td>
      <td>${fmt(p.avg_short_price)}</td>
      <td>${fmt(curPrice)}</td>
      <td>${fmt(exp)}</td>
      <td class="${pnlClass(pnl)}">${fmtPnl(pnl)}</td>
    </tr>`;
  }).join('');

  wrapper.innerHTML = `
    <div class="table-scroll">
      <table>
        <thead><tr>
          <th>銘柄</th><th>株数</th><th>建値</th>
          <th>現在値</th><th>建玉額</th><th>含み損益</th>
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
      <td><span class="sym-code">${t.symbol}</span>${symName(t.symbol)}</td>
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

function fmtLabel(dt) {
  if (!dt) return '';
  // "2026-06-19 09:00" → "06/19 09:00"、"2026-06-19" → "06/19"
  return dt.slice(5).replace('-', '/');
}

function renderChart(summary, intraday, initialCash) {
  const hasIntraday = intraday && intraday.length > 0;
  const hasSummary  = summary && summary.length > 0;

  if (!hasIntraday && !hasSummary) {
    const empty = '<p class="empty-msg">チャートデータなし（取引後に表示されます）</p>';
    const tc = document.getElementById('total-chart');
    const cc = document.getElementById('cash-chart');
    if (tc) tc.parentElement.innerHTML = empty;
    if (cc) cc.parentElement.innerHTML = empty;
    return;
  }

  // 総資産チャート: intraday (10分おき) をメインに使い、ない日は daily_summary で補完
  let totalLabels, totalData;
  if (hasIntraday) {
    const intradayDates = new Set(intraday.map(r => (r.datetime || '').slice(0, 10)).filter(Boolean));
    const supplement = hasSummary
      ? summary
          .filter(r => !intradayDates.has(r.date))
          .map(r => ({ datetime: r.date + ' 15:30', total_value: r.total_value }))
      : [];
    const allPoints = [...supplement, ...intraday].sort((a, b) => a.datetime.localeCompare(b.datetime));
    totalLabels = ['開始前', ...allPoints.map(r => fmtLabel(r.datetime))];
    totalData   = [initialCash, ...allPoints.map(r => parseFloat(r.total_value))];
  } else {
    totalLabels = ['開始前', ...summary.map(r => fmtLabel(r.date))];
    totalData   = [initialCash, ...summary.map(r => parseFloat(r.total_value))];
  }

  // 現金残高チャート: 日次 (daily_summary) — 取引日に一度しか変化しないため
  let cashLabels, cashData;
  if (hasSummary) {
    cashLabels = ['開始前', ...summary.map(r => fmtLabel(r.date))];
    cashData   = [initialCash, ...summary.map(r => parseFloat(r.cash))];
  } else {
    // summary がなければ intraday の日ごと最終値を使う
    const byDate = {};
    intraday.forEach(r => {
      const d = (r.datetime || '').slice(0, 10);
      if (d) byDate[d] = parseFloat(r.cash);
    });
    const sorted = Object.keys(byDate).sort();
    cashLabels = ['開始前', ...sorted.map(fmtLabel)];
    cashData   = [initialCash, ...sorted.map(d => byDate[d])];
  }

  _makeChart('total-chart', totalLabels, totalData, '総資産',   '#e63946', 'rgba(230,57,70,0.07)');
  _makeChart('cash-chart',  cashLabels,  cashData,  '現金残高', '#1a6fc9', 'rgba(26,111,201,0.07)');
}

function _makeChart(canvasId, labels, data, label, color, bgColor) {
  const canvas   = document.getElementById(canvasId);
  if (!canvas) return;
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
        pointRadius: 3,
        pointHoverRadius: 5,
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
        x: { ticks: { maxTicksLimit: 12, maxRotation: 30 } },
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

// 銘柄コード・名称のインラインスタイル
const _style = document.createElement('style');
_style.textContent = `
  .sym-code { font-weight: 600; }
  .sym-name { display: block; font-size: 0.75em; color: #888; margin-top: 1px; }
  .muted { color: #aaa; }
`;
document.head.appendChild(_style);

initRunSelector();
setInterval(refresh, 60 * 1000);
