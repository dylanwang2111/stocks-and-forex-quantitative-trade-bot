/* ── Column-header tooltip (1.5s hover delay, body-level div) ──────────────── */
(function () {
  const tip = document.createElement('div');
  tip.id = 'col-tooltip';
  document.body.appendChild(tip);

  let timer = null;

  document.addEventListener('mouseover', function (e) {
    const th = e.target.closest('th[data-tip]');
    if (!th) return;
    clearTimeout(timer);
    timer = setTimeout(function () {
      const rect = th.getBoundingClientRect();
      tip.textContent = th.dataset.tip;

      // Position below the th, horizontally centred
      const centreX = rect.left + rect.width / 2;
      tip.style.top  = (rect.bottom + 6) + 'px';
      tip.style.left = centreX + 'px';
      tip.style.transform = 'translateX(-50%)';

      // Clamp so it doesn't overflow the right edge
      tip.classList.add('visible');
      const tipRect = tip.getBoundingClientRect();
      if (tipRect.right > window.innerWidth - 8) {
        tip.style.left = (window.innerWidth - 8 - tipRect.width / 2) + 'px';
      }
      if (tipRect.left < 8) {
        tip.style.left = (8 + tipRect.width / 2) + 'px';
      }
    }, 1500);
  });

  document.addEventListener('mouseout', function (e) {
    const th = e.target.closest('th[data-tip]');
    if (!th) return;
    clearTimeout(timer);
    tip.classList.remove('visible');
  });
})();

/* ── XSS escape helper ─────────────────────────────────────────────────────── */
function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#x27;');
}

/* ── Dark mode ─────────────────────────────────────────────────────────────── */
function isDark() {
  return document.documentElement.classList.contains('dark');
}

function updateChartDefaults() {
  if (isDark()) {
    Chart.defaults.color       = '#7A6E62';
    Chart.defaults.borderColor = 'rgba(237,232,223,0.05)';
  } else {
    Chart.defaults.color       = '#B0A898';
    Chart.defaults.borderColor = 'rgba(26,22,20,0.07)';
  }
}

/* Theme-aware chart color helpers — call at render time so they read current mode */
function cLine()  { return isDark() ? '#C9A55A' : '#1A1614'; }
function cFill()  { return isDark() ? 'rgba(201,165,90,0.07)' : 'rgba(26,22,20,0.04)'; }
function cGrid()  { return isDark() ? 'rgba(237,232,223,0.05)' : 'rgba(26,22,20,0.06)'; }
function cNavy()  { return isDark() ? '#6B9FD4' : '#1A3356'; }

/* Tooltip style — matches light/dark theme */
function cTooltip(extra = {}) {
  return {
    backgroundColor:  isDark() ? '#1C1814' : '#FFFFFF',
    borderColor:      isDark() ? '#B8975A' : '#DDD8CE',
    borderWidth:      1,
    titleColor:       isDark() ? '#EDE8DF' : '#1A1614',
    bodyColor:        isDark() ? '#A89880' : '#6B6050',
    titleFont:        { family: "'Syne', sans-serif", weight: '700', size: 11 },
    bodyFont:         { family: "'IBM Plex Mono', monospace", size: 11 },
    padding:          { top: 8, right: 12, bottom: 8, left: 12 },
    cornerRadius:     2,
    caretSize:        5,
    displayColors:    false,
    ...extra,
  };
}

/* Zoom + pan config for time-series charts */
function cZoom(minRangeMs = 3600000) {
  return {
    zoom: {
      wheel:  { enabled: true, speed: 0.08 },
      pinch:  { enabled: true },
      mode:   'x',
      onZoomComplete: ({ chart }) => showResetBtn(chart.canvas.id),
    },
    pan: {
      enabled:   true,
      mode:      'x',
      threshold: 5,
      onPanComplete: ({ chart }) => showResetBtn(chart.canvas.id),
    },
    limits: {
      x: { min: 'original', max: 'original', minRange: minRangeMs },
    },
  };
}

function showResetBtn(canvasId) {
  document.getElementById(`reset-${canvasId}`)?.classList.add('visible');
}

function resetZoom(id) {
  charts[id]?.resetZoom();
  document.getElementById(`reset-${id}`)?.classList.remove('visible');
}

function setDark(enable) {
  const html = document.documentElement;
  html.classList.add('transitioning');
  html.classList.toggle('dark', enable);
  localStorage.setItem('trade-signet-theme', enable ? 'dark' : 'light');
  const icon = document.getElementById('dm-icon');
  if (icon) icon.textContent = enable ? '☀' : '☽';
  updateChartDefaults();
  // Re-render charts after transition completes
  setTimeout(() => {
    html.classList.remove('transitioning');
    if (typeof refreshPage === 'function' && activePage) refreshPage(activePage);
  }, 250);
}

function initSettings() {
  const btn   = document.getElementById('settings-btn');
  const panel = document.getElementById('settings-panel');
  const tog   = document.getElementById('dm-toggle');
  const icon  = document.getElementById('dm-icon');

  // Sync icon to current state on load
  if (icon) icon.textContent = isDark() ? '☀' : '☽';

  if (btn && panel) {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const open = panel.classList.toggle('open');
      btn.classList.toggle('active', open);
    });
    // Close on outside click
    document.addEventListener('click', () => {
      panel.classList.remove('open');
      btn.classList.remove('active');
    });
    panel.addEventListener('click', e => e.stopPropagation());
  }

  if (tog) {
    tog.addEventListener('click', () => setDark(!isDark()));
  }
}

/* ── Global state ──────────────────────────────────────────────────────────── */
const PAGES = ['overview','positions','signals','trades','costs','strategies','optimizer','status'];
let activePage = 'overview';
let charts = {};
let tradesPage = 1;
let optPage = 1;
let sigPage = 1;
let sigAllSignals = [];
const TRADES_PAGE_SIZE = 50;
const OPT_PAGE_SIZE = 50;
const SIG_PAGE_SIZE = 25;

// Trade ledger sort/filter state
let tradesAllRows    = [];        // full unfiltered page data from API
let tradesSortCol    = 'time';
let tradesSortDir    = -1;        // -1 = desc, 1 = asc
let tradesColFilters = {};        // { col: Set<string> }  — active value selections per column

// Signals table sort/filter state
let sigColFilters    = {};        // { col: Set<string> }  — active value selections per column
let sigSortCol       = 'timestamp';
let sigSortDir       = -1;        // -1 = desc, 1 = asc
let sigDateChecked   = new Set(); // Set of "Y-M-D-H" leaf strings; empty = no filter
let sigDatePending   = new Set(); // working copy while dropdown is open
let sigColDdActiveCol = null;
let _sigDdTh         = null;

/* ── Chart.js defaults ─────────────────────────────────────────────────────── */
Chart.defaults.font.family = "'IBM Plex Mono', monospace";
Chart.defaults.font.size = 11;
updateChartDefaults();  // sets color + borderColor based on current theme

function makeChart(id, config) {
  if (charts[id]) { charts[id].destroy(); delete charts[id]; }
  const canvas = document.getElementById(id);
  if (!canvas) return null;
  charts[id] = new Chart(canvas, config);
  return charts[id];
}

/* ── Utility ───────────────────────────────────────────────────────────────── */
function fmtUSD(v, decimals=2) {
  if (v == null) return '—';
  const abs = Math.abs(v);
  const fmt = abs >= 1000
    ? abs.toLocaleString('en-US', {minimumFractionDigits:0, maximumFractionDigits:0})
    : abs.toFixed(decimals);
  return (v < 0 ? '-' : v > 0 ? '+' : '') + '$' + fmt;
}

function fmtPct(v) {
  if (v == null) return '—';
  return (v >= 0 ? '+' : '') + v.toFixed(1) + '%';
}

function fmtNum(v, d=2) {
  if (v == null) return '—';
  return Number(v).toFixed(d);
}

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('en-US', {month:'short', day:'2-digit', hour:'2-digit', minute:'2-digit', hour12:false});
}

function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleDateString('en-US', {year:'numeric', month:'short', day:'2-digit'});
}

function pnlClass(v) {
  if (v == null) return '';
  return v > 0 ? 'pnl-pos' : v < 0 ? 'pnl-neg' : '';
}

function tierBadge(tier) {
  if (!tier) return '—';
  return `<span class="tier-badge tier-${tier}">${tier}</span>`;
}

function dirBadge(d) {
  if (!d) return '—';
  return `<span class="${d==='long'?'dir-long':'dir-short'}">${d.toUpperCase()}</span>`;
}

function scoreClass(s) {
  if (s == null) return 'score-low';
  if (s >= 75) return 'score-high';
  if (s >= 65) return 'score-mid';
  if (s >= 55) return 'score-entry';
  return 'score-low';
}

function voteCell(v) {
  if (v == null) return '<span class="vote-zero">—</span>';
  if (v === 2 || v === -2) return `<span class="vote-dbl">${v>0?'+2':'-2'}</span>`;
  if (v > 0)  return `<span class="vote-pos">+1</span>`;
  if (v < 0)  return `<span class="vote-neg">-1</span>`;
  return '<span class="vote-zero"> 0</span>';
}

function emptyState(msg='No data yet') {
  return `<div class="empty-state"><div class="empty-icon">◌</div>${msg}</div>`;
}

function sharpeBadge(v) {
  if (v == null) return '<span style="color:var(--text-muted)">—</span>';
  const cls = v >= 1.5 ? 'score-high' : v >= 0.5 ? 'score-entry' : v < 0 ? 'pnl-neg' : 'score-low';
  return `<span class="${cls}" style="font-family:var(--mono)">${v.toFixed(3)}</span>`;
}

/* ── API fetch ─────────────────────────────────────────────────────────────── */
async function apiFetch(path) {
  try {
    const r = await fetch(path);
    if (!r.ok) return null;
    return await r.json();
  } catch { return null; }
}

/* ── Router ────────────────────────────────────────────────────────────────── */
function navigate(page) {
  // Collapse the overview positions expander when leaving overview
  if (activePage === 'overview' && page !== 'overview') {
    document.getElementById('ov-pos-card')?.classList.remove('expanded');
    document.getElementById('ov-bottom-grid')?.classList.remove('pos-expanded');
    const lbl = document.querySelector('#ov-pos-expand-btn .expand-label');
    if (lbl) lbl.textContent = 'View full';
  }
  PAGES.forEach(p => {
    const sec = document.getElementById(`page-${p}`);
    if (sec) sec.style.display = p === page ? 'block' : 'none';
    const link = document.querySelector(`.nav-link[data-page="${p}"]`);
    if (link) link.classList.toggle('active', p === page);
  });
  activePage = page;
  refreshPage(page);
}

document.querySelectorAll('.nav-link[data-page]').forEach(link => {
  link.addEventListener('click', e => {
    e.preventDefault();
    navigate(link.dataset.page);
    closeNav(); // close hamburger menu on mobile
  });
});

/* ── Mobile hamburger nav ──────────────────────────────────────────────────── */
function openNav() {
  document.body.classList.add('nav-open');
  const btn = document.getElementById('nav-toggle');
  if (btn) btn.setAttribute('aria-expanded', 'true');
}
function closeNav() {
  document.body.classList.remove('nav-open');
  const btn = document.getElementById('nav-toggle');
  if (btn) btn.setAttribute('aria-expanded', 'false');
}
document.getElementById('nav-toggle')?.addEventListener('click', () => {
  document.body.classList.contains('nav-open') ? closeNav() : openNav();
});
document.getElementById('nav-backdrop')?.addEventListener('click', closeNav);
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeNav(); });

function refreshPage(page) {
  const fns = {
    overview:   renderOverview,
    positions:  renderPositions,
    signals:    renderSignals,
    trades:     renderTrades,
    costs:      renderCosts,
    strategies: renderStrategies,
    optimizer:  renderOptimizer,
    status:     renderStatus,
  };
  fns[page]?.();
  document.getElementById('last-update').textContent = new Date().toLocaleTimeString('en-US',
    {hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
}

setInterval(() => refreshPage(activePage), 30000);

/* ═══════════════════════════════════════════════════════════════════════════ */
/* OVERVIEW                                                                     */
/* ═══════════════════════════════════════════════════════════════════════════ */
async function renderOverview() {
  const data = await apiFetch('/api/overview');
  if (!data) return;

  // Mode pill
  const pill = document.getElementById('mode-pill');
  if (pill) {
    pill.textContent = data.trading_mode?.toUpperCase() || 'PAPER';
    pill.className = 'mode-pill' + (data.trading_mode === 'live' ? ' live' : '');
  }

  // Snapshot line
  const snapLine = document.getElementById('ov-snapshot-time');
  if (snapLine) {
    const age = data.last_snapshot_time ? `Last snapshot: ${fmtTime(data.last_snapshot_time)}` : 'No snapshot yet';
    const running = data.bot_running
      ? `<span class="bot-chip running" style="display:inline-flex;margin-left:10px">● Running</span>`
      : `<span class="bot-chip stopped" style="display:inline-flex;margin-left:10px">● Stopped</span>`;
    snapLine.innerHTML = age + running;
  }

  // Metrics
  const equity     = data.current_equity ?? data.total_capital;
  const realized   = data.realized_pnl   ?? 0;
  const unrealized = data.unrealized_pnl ?? 0;
  const dailyPnl   = data.daily_pnl      ?? 0;
  const totalPnl   = realized + unrealized;

  setText('ov-equity', fmtUSD(equity));
  setDelta('ov-equity-delta', totalPnl, `${fmtPct((totalPnl/data.total_capital)*100)} all-time`);
  setText('ov-realized', fmtUSD(realized));
  setDelta('ov-daily-pnl', dailyPnl, `today ${fmtUSD(dailyPnl, 2)}`);
  setText('ov-unrealized', fmtUSD(unrealized));
  document.getElementById('ov-unrealized-sub').textContent =
    data.open_positions ? `${data.open_positions} open position${data.open_positions !== 1 ? 's' : ''}` : 'no open positions';
  setText('ov-winrate', data.win_rate != null ? data.win_rate.toFixed(1) + '%' : '—');
  document.getElementById('ov-ntrades').textContent = `based on closed trades`;
  setText('ov-openpos', String(data.open_positions ?? 0));
  setDelta('ov-drawdown', data.drawdown_pct,
    data.drawdown_pct != null ? `drawdown ${fmtPct(data.drawdown_pct)}` : 'drawdown —');

  const pdt    = data.pdt_used  ?? 0;
  const pdtLim = data.pdt_limit ?? 3;
  setText('ov-pdt', `${pdtLim - pdt}/${pdtLim}`);
  document.getElementById('ov-pdt-sub').textContent = `${pdt} used of ${pdtLim}`;

  // Equity curve
  const curve = data.equity_curve || [];
  if (curve.length > 1) {
    makeChart('equity-chart', {
      type: 'line',
      data: {
        datasets: [{
          data: curve.map(p => ({ x: p.t, y: p.v })),
          borderColor: cLine(),
          backgroundColor: cFill(),
          borderWidth: 1.5,
          tension: 0.3,
          pointRadius: 0,
          pointHoverRadius: 4,
          fill: true,
        }]
      },
      options: {
        animation: false,
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        scales: {
          x: {
            type: 'time',
            time: {
              tooltipFormat: 'MMM d, HH:mm',
              displayFormats: {
                minute: 'HH:mm',
                hour:   'MMM d HH:mm',
                day:    'MMM d',
                week:   'MMM d',
                month:  'MMM yyyy',
              },
            },
            ticks: { maxRotation: 0, maxTicksLimit: 8 },
            grid: { display: false },
          },
          y: {
            ticks: { callback: v => '$' + v.toLocaleString() },
            grid: { color: cGrid() },
          },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            ...cTooltip(),
            callbacks: {
              title: items => {
                const d = new Date(items[0].parsed.x);
                return d.toLocaleString('en-US', { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
              },
              label: ctx => ' Equity  ' + fmtUSD(ctx.parsed.y),
            },
          },
          zoom: cZoom(3600000),
        },
      },
    });
  } else {
    const wrap = document.getElementById('equity-chart')?.parentElement;
    if (wrap) wrap.innerHTML = emptyState('No closed trades yet — equity curve will appear after first close');
  }

  // Capital allocation table
  const alloc = data.capital_allocation;
  const allocTbody = document.querySelector('#ov-alloc-table tbody');
  if (allocTbody && alloc) {
    allocTbody.innerHTML = [
      buildAllocRow('IBKR', alloc.ibkr),
      buildAllocRow('OANDA', alloc.oanda),
      buildAllocRow('TOTAL', alloc.total),
    ].join('');
  }

  // Open positions — populate both mini and full tables
  const openPos = await apiFetch('/api/positions');
  _renderOvPositionsMini(openPos);
  _renderOvPositionsFull(openPos);

  // Broker sync bar
  renderBrokerSync();
}

/* ── Broker sync bar ───────────────────────────────────────────────────────── */
async function renderBrokerSync(force = false) {
  const btn = document.getElementById('bsb-sync-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⟳ Syncing…'; }

  const url = force ? '/api/broker-sync?force=true' : '/api/broker-sync';
  const d = await apiFetch(url);

  if (btn) { btn.disabled = false; btn.textContent = '⟳ Sync'; }
  if (!d) return;

  const isLiveMode = d.trading_mode === 'live';
  function applyPill(elId, valElId, info) {
    const pill = document.getElementById(elId);
    const valEl = document.getElementById(valElId);
    if (!pill || !valEl) return;
    const live = info?.status === 'live';
    pill.classList.toggle('live', live);
    pill.classList.toggle('offline', !live);
    if (live) {
      if (isLiveMode) {
        const nav = info.nav ?? info.balance;
        valEl.textContent = nav != null ? fmtUSD(nav) : '—';
      } else {
        valEl.textContent = 'Paper ✓';
      }
    } else {
      valEl.textContent = '—';
      if (info?.error) valEl.title = info.error;
    }
  }

  applyPill('bsb-oanda', 'bsb-oanda-val', d.oanda);
  applyPill('bsb-ibkr',  'bsb-ibkr-val',  d.ibkr);

  const ts = document.getElementById('bsb-synced-at');
  if (ts && d.synced_at) {
    ts.textContent = 'synced ' + fmtTime(d.synced_at);
  }

  const bar = document.getElementById('broker-sync-bar');
  if (bar) {
    const anyLive = d.oanda?.status === 'live' || d.ibkr?.status === 'live';
    bar.style.display = anyLive ? '' : 'none';
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const syncBtn = document.getElementById('bsb-sync-btn');
  if (syncBtn) syncBtn.addEventListener('click', () => renderBrokerSync(true));
});

function _renderOvPositionsMini(data) {
  const tbody = document.querySelector('#ov-open-table tbody');
  if (!tbody) return;
  if (!data?.length) {
    tbody.innerHTML = `<tr><td colspan="8"><div class="empty-state" style="padding:20px">No open positions</div></td></tr>`;
    return;
  }
  tbody.innerHTML = data.map(p => {
    const stopCls = p.dist_stop_pct != null && p.dist_stop_pct < 0.5 ? 'pnl-neg'
                  : p.dist_stop_pct != null && p.dist_stop_pct > 0  ? 'pnl-pos' : '';
    const tpCls   = p.dist_tp_pct   != null && p.dist_tp_pct   < 0  ? 'pnl-neg'
                  : p.dist_tp_pct   != null                          ? 'pnl-pos' : '';
    return `<tr>
      <td style="font-weight:600">${esc(p.symbol)}</td>
      <td>${dirBadge(p.direction)}</td>
      <td style="font-family:var(--mono);font-size:11px">${fmtNum(p.entry_price, 4)}</td>
      <td style="font-family:var(--mono);font-size:11px">${p.current_price ? fmtNum(p.current_price, 4) : '—'}</td>
      <td class="${stopCls}" style="font-size:11px">${fmtPct(p.dist_stop_pct)}</td>
      <td class="${tpCls}"   style="font-size:11px">${fmtPct(p.dist_tp_pct)}</td>
      <td class="${pnlClass(p.unrealized_pnl)}">${fmtUSD(p.unrealized_pnl)}</td>
      <td>${phaseBadge(p.phase)}</td>
    </tr>`;
  }).join('');
}

function _renderOvPositionsFull(data) {
  const tbody = document.querySelector('#ov-open-table-full tbody');
  if (!tbody) return;
  if (!data?.length) {
    tbody.innerHTML = `<tr><td colspan="16"><div class="empty-state" style="padding:20px">No open positions</div></td></tr>`;
    return;
  }
  tbody.innerHTML = data.map(p => {
    const prog        = p.tp_progress_pct;
    const progCapped  = prog != null ? Math.max(0, Math.min(100, prog)) : 0;
    const progClass   = prog == null ? 'dim' : prog >= 100 ? 'gold' : prog >= 0 ? 'green' : 'red';
    const stopCls     = p.dist_stop_pct != null && p.dist_stop_pct < 0.5 ? 'pnl-neg' : '';
    const daysLeftCls = p.days_left != null && p.days_left <= 1 ? 'pnl-neg' : '';
    return `<tr>
      <td style="font-weight:600">${esc(p.symbol)}</td>
      <td>${dirBadge(p.direction)}</td>
      <td>${phaseBadge(p.phase)}</td>
      <td style="font-family:var(--mono)">${fmtNum(p.entry_price, 4)}</td>
      <td style="font-family:var(--mono)">${p.current_price ? fmtNum(p.current_price, 4) : '—'}</td>
      <td style="font-family:var(--mono);color:var(--red)">${p.stop_price ? fmtNum(p.stop_price, 4) : '—'}</td>
      <td style="font-family:var(--mono);color:var(--amber)">${p.take_profit_price ? fmtNum(p.take_profit_price, 4) : '—'}</td>
      <td class="${stopCls}">${fmtPct(p.dist_stop_pct)}</td>
      <td>${fmtPct(p.dist_tp_pct)}</td>
      <td style="min-width:100px">
        <div style="display:flex;align-items:center;gap:6px">
          <div class="prog-wrap" style="flex:1">
            <div class="prog-fill ${progClass}" style="width:${progCapped}%"></div>
          </div>
          <span style="font-family:var(--mono);font-size:11px;white-space:nowrap;color:var(--text-sub)">${prog != null ? prog.toFixed(0)+'%' : '—'}</span>
        </div>
      </td>
      <td class="${pnlClass(p.unrealized_pnl)}">${fmtUSD(p.unrealized_pnl)}</td>
      <td>
        <span style="font-family:var(--mono)">${fmtNum(p.quantity, 4)}</span>
        ${p.size_usd != null ? `<br><span style="font-size:10px;color:var(--text-sub)">${fmtUSD(p.size_usd)}</span>` : ''}
      </td>
      <td>${p.days_held ?? '—'}</td>
      <td class="${daysLeftCls}">${p.days_left ?? '—'}</td>
      <td>${tierBadge(p.position_tier)}</td>
      <td style="font-family:var(--mono)">${p.confidence != null ? p.confidence.toFixed(1) : '—'}</td>
    </tr>`;
  }).join('');
}

function toggleOvPositions() {
  const card  = document.getElementById('ov-pos-card');
  const grid  = document.getElementById('ov-bottom-grid');
  const btn   = document.getElementById('ov-pos-expand-btn');
  const label = btn?.querySelector('.expand-label');
  const isExpanded = card.classList.toggle('expanded');
  grid.classList.toggle('pos-expanded', isExpanded);
  if (label) label.textContent = isExpanded ? 'Collapse' : 'View full';
  btn?.setAttribute('title', isExpanded ? 'Collapse to compact view' : 'Expand to full view');
}

function buildAllocRow(label, a) {
  if (!a) return '';
  const util = a.utilization_pct ?? 0;
  const barW = Math.min(100, util);
  return `<tr>
    <td style="font-weight:600;color:var(--text)">${label}</td>
    <td>${fmtUSD(a.pool, 0)}</td>
    <td>${fmtUSD(a.deployed)}</td>
    <td>${fmtUSD(a.available)}</td>
    <td>
      <div style="display:flex;align-items:center;gap:6px">
        <div class="alloc-bar-wrap"><div class="alloc-bar-fill" style="width:${barW}%"></div></div>
        <span style="font-family:var(--mono);font-size:11px;color:var(--text-sub)">${util.toFixed(0)}%</span>
      </div>
    </td>
    <td>${a.positions ?? '—'}</td>
  </tr>`;
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function setDelta(id, val, text) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text;
  el.className = 'm-delta' + (val > 0 ? ' pos' : val < 0 ? ' neg' : '');
}

function phaseBadge(phase) {
  if (!phase) return '—';
  if (phase === '1')           return '<span class="phase-badge phase-1">Phase 1</span>';
  if (phase === '2-past-tp')   return '<span class="phase-badge phase-2-past">Phase 2 — past TP</span>';
  if (phase === '2-trailing')  return '<span class="phase-badge phase-2-trail">Phase 2 — trailing</span>';
  return `<span class="phase-badge phase-1">${phase}</span>`;
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/* POSITIONS                                                                    */
/* ═══════════════════════════════════════════════════════════════════════════ */
async function renderPositions() {
  const data   = await apiFetch('/api/positions');
  const tbody  = document.querySelector('#pos-table tbody');
  if (!tbody) return;

  if (!data?.length) {
    tbody.innerHTML = `<tr><td colspan="16">${emptyState('No open positions')}</td></tr>`;
    return;
  }

  tbody.innerHTML = data.map(p => {
    const prog       = p.tp_progress_pct;
    const progCapped = prog != null ? Math.max(0, Math.min(100, prog)) : 0;
    const progClass  = prog == null ? 'dim' : prog >= 100 ? 'gold' : prog >= 0 ? 'green' : 'red';
    const stopCls    = p.dist_stop_pct != null && p.dist_stop_pct < 0.5 ? 'pnl-neg' : '';
    const daysLeftCls= p.days_left != null && p.days_left <= 1 ? 'pnl-neg' : '';

    return `<tr>
      <td style="font-weight:600">${esc(p.symbol)}</td>
      <td>${dirBadge(p.direction)}</td>
      <td>${phaseBadge(p.phase)}</td>
      <td style="font-family:var(--mono)">${fmtNum(p.entry_price, 4)}</td>
      <td style="font-family:var(--mono)">${p.current_price ? fmtNum(p.current_price, 4) : '—'}</td>
      <td style="font-family:var(--mono);color:var(--red)">${p.stop_price ? fmtNum(p.stop_price, 4) : '—'}</td>
      <td style="font-family:var(--mono);color:var(--amber)">${p.take_profit_price ? fmtNum(p.take_profit_price, 4) : '—'}</td>
      <td class="${stopCls}">${fmtPct(p.dist_stop_pct)}</td>
      <td>${fmtPct(p.dist_tp_pct)}</td>
      <td style="min-width:100px">
        <div style="display:flex;align-items:center;gap:6px">
          <div class="prog-wrap" style="flex:1">
            <div class="prog-fill ${progClass}" style="width:${progCapped}%"></div>
          </div>
          <span style="font-family:var(--mono);font-size:11px;white-space:nowrap;color:var(--text-sub)">${prog != null ? prog.toFixed(0)+'%' : '—'}</span>
        </div>
      </td>
      <td class="${pnlClass(p.unrealized_pnl)}">${fmtUSD(p.unrealized_pnl)}</td>
      <td>
        <span style="font-family:var(--mono)">${fmtNum(p.quantity, 4)}</span>
        ${p.size_usd != null ? `<br><span style="font-size:10px;color:var(--text-sub)">${fmtUSD(p.size_usd)}</span>` : ''}
      </td>
      <td>${p.days_held ?? '—'}</td>
      <td class="${daysLeftCls}">${p.days_left ?? '—'}</td>
      <td>${tierBadge(p.position_tier)}</td>
      <td style="font-family:var(--mono)">${p.confidence != null ? p.confidence.toFixed(1) : '—'}</td>
    </tr>`;
  }).join('');
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/* SIGNALS                                                                      */
/* ═══════════════════════════════════════════════════════════════════════════ */
async function renderSignals() {
  const symFilter     = document.getElementById('sig-sym-filter')?.value   || '';
  const tierFilter    = document.getElementById('sig-tier-filter')?.value  || '';
  const tradeableOnly = document.getElementById('sig-tradeable-only')?.checked || false;

  const params = new URLSearchParams();
  if (symFilter)     params.set('symbol',        symFilter);
  if (tierFilter)    params.set('tier',           tierFilter);
  if (tradeableOnly) params.set('tradeable_only', 'true');

  params.set('days', '5');
  const data = await apiFetch('/api/signals?' + params);
  if (!data) return;
  const minEntry = data.min_confidence ?? 62;

  // Populate symbol filter on first load
  const symSel = document.getElementById('sig-sym-filter');
  if (symSel && symSel.options.length <= 1 && data.symbols?.length) {
    symSel.innerHTML = '<option value="">All</option>' +
      data.symbols.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
    if (symFilter) symSel.value = symFilter;
  }

  const tbody = document.querySelector('#sig-table tbody');
  if (!tbody) return;

  if (!data.signals?.length) {
    tbody.innerHTML = `<tr><td colspan="15">${emptyState('No signals yet — appears after first scan cycle')}</td></tr>`;
    document.getElementById('sig-pagination').innerHTML = '';
    return;
  }

  sigAllSignals = data.signals;
  sigPage = 1;
  renderSigTable();

  // ── Score Distribution ─────────────────────────────────────────────────────
  const scores    = data.signals.filter(s => s.score != null).map(s => s.score);
  const sigTotal  = scores.length;
  const sigMean   = sigTotal ? scores.reduce((a, b) => a + b, 0) / sigTotal : 0;
  const tradeable = data.signals.filter(s => ['SMALL','MEDIUM','LARGE','FULL'].includes(s.tier)).length;
  const pctTrad   = sigTotal ? Math.round(tradeable / sigTotal * 100) : 0;
  const bySymbol  = {};
  data.signals.forEach(s => {
    if (s.score == null) return;
    if (!bySymbol[s.symbol]) bySymbol[s.symbol] = [];
    bySymbol[s.symbol].push(s.score);
  });
  const topSym = Object.entries(bySymbol)
    .map(([sym, arr]) => ({ sym, avg: arr.reduce((a, b) => a + b, 0) / arr.length }))
    .sort((a, b) => b.avg - a.avg)[0];

  setText('sdist-total',     sigTotal);
  setText('sdist-mean',      sigMean.toFixed(1));
  setText('sdist-tradeable', pctTrad + '%');
  setText('sdist-top',       topSym ? topSym.sym : '—');
  setText('sdist-min-entry', minEntry);

  // Histogram buckets (step = 5, range 0–100)
  const buckets = {};
  for (let b = 0; b <= 100; b += 5) buckets[b] = 0;
  data.signals.forEach(s => {
    if (s.score == null) return;
    const b = Math.min(Math.floor(s.score / 5) * 5, 100);
    if (b in buckets) buckets[b]++;
  });
  const bKeys = Object.keys(buckets).map(Number);
  const bVals = bKeys.map(k => buckets[k]);

  // Smooth density line: 3-point rolling average
  const lineData = bVals.map((_v, i) => {
    const w = bVals.slice(Math.max(0, i - 1), i + 2);
    return w.reduce((a, b) => a + b, 0) / w.length;
  });

  // Custom plugin: tier zone backgrounds + threshold dashed lines
  const tierZonesPlugin = {
    id: 'tierZones',
    beforeDatasetsDraw(chart) {
      const { ctx, chartArea: { left, right, top, bottom } } = chart;
      const w  = right - left;
      const n  = bKeys.length;   // 21 buckets
      const bw = w / n;

      const zones = [
        { from: 0,   to: 55,  color: 'rgba(176,168,152,0.07)' },
        { from: 55,  to: 65,  color: 'rgba(180,92,0,0.06)'    },
        { from: 65,  to: 75,  color: 'rgba(26,51,86,0.06)'    },
        { from: 75,  to: 85,  color: 'rgba(20,82,41,0.08)'    },
        { from: 85,  to: 105, color: 'rgba(20,82,41,0.14)'    },
      ];
      ctx.save();
      zones.forEach(z => {
        const x0 = left + (z.from / 5) * bw;
        const x1 = left + (z.to   / 5) * bw;
        ctx.fillStyle = z.color;
        ctx.fillRect(x0, top, Math.min(x1, right) - x0, bottom - top);
      });

      const thresholds = [
        { score: 55, label: 'S', color: 'rgba(180,92,0,0.55)'  },
        { score: 65, label: 'M', color: 'rgba(26,51,86,0.5)'   },
        { score: 75, label: 'L', color: 'rgba(20,82,41,0.55)'  },
        { score: 85, label: 'F', color: 'rgba(20,82,41,0.70)'  },
      ];
      thresholds.forEach(t => {
        const x = left + (t.score / 5) * bw;
        ctx.strokeStyle = t.color;
        ctx.lineWidth   = 1;
        ctx.setLineDash([3, 4]);
        ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = t.color;
        ctx.font      = 'bold 8px "IBM Plex Mono", monospace';
        ctx.fillText(t.label, x + 3, top + 11);
      });

      // Min-entry threshold line (solid, distinct colour)
      const minX = left + (minEntry / 5) * bw;
      ctx.strokeStyle = 'rgba(196,90,82,0.85)';
      ctx.lineWidth   = 1.5;
      ctx.setLineDash([5, 3]);
      ctx.beginPath(); ctx.moveTo(minX, top); ctx.lineTo(minX, bottom); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = 'rgba(196,90,82,0.85)';
      ctx.font      = 'bold 8px "IBM Plex Mono", monospace';
      ctx.fillText('MIN', minX + 3, top + 22);
      ctx.restore();
    },
  };

  const monoFont = { family: '"IBM Plex Mono", monospace', size: 10 };
  const axisTick = { color: 'rgba(107,96,80,0.75)', font: monoFont };

  makeChart('score-dist-chart', {
    type: 'bar',
    plugins: [tierZonesPlugin],
    data: {
      labels: bKeys.map(b => [0, 25, 50, 75, 100].includes(b) ? String(b) : ''),
      datasets: [
        {
          type: 'bar',
          data: bVals,
          backgroundColor: bKeys.map(b => {
            if (b >= 85) return 'rgba(20,82,41,0.82)';
            if (b >= 75) return 'rgba(20,82,41,0.60)';
            if (b >= 65) return 'rgba(26,51,86,0.58)';
            if (b >= 55) return 'rgba(180,92,0,0.58)';
            return 'rgba(176,168,152,0.32)';
          }),
          borderRadius: 2,
          borderWidth: 0,
          barPercentage: 0.92,
          categoryPercentage: 0.92,
          order: 2,
        },
        {
          type: 'line',
          data: lineData,
          borderColor: isDark() ? 'rgba(237,232,223,0.30)' : 'rgba(26,22,20,0.22)',
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.45,
          fill: false,
          order: 1,
        },
      ],
    },
    options: {
      animation: { duration: 500, easing: 'easeOutQuart' },
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          ...cTooltip(),
          callbacks: {
            title: items => `Score ${bKeys[items[0].dataIndex]}–${bKeys[items[0].dataIndex] + 5}`,
            label: item  => item.datasetIndex === 0
              ? ` ${item.raw} signal${item.raw !== 1 ? 's' : ''}`
              : `Density: ${item.raw.toFixed(1)}`,
          },
        },
      },
      scales: {
        x: {
          grid:  { display: false },
          ticks: axisTick,
        },
        y: {
          grid:   { color: cGrid(), borderDash: [2, 4] },
          ticks:  { ...axisTick, precision: 0 },
          border: { display: false },
        },
      },
    },
  });
}

function renderSigTable() {
  const tbody = document.querySelector('#sig-table tbody');
  if (!tbody) return;

  // Apply client-side column filters
  let rows = [...sigAllSignals];

  // Date filter
  if (sigDateChecked.size > 0) {
    rows = rows.filter(s => {
      if (!s.timestamp) return false;
      const dt = new Date(String(s.timestamp).replace(' ', 'T'));
      const k  = `${dt.getFullYear()}-${dt.getMonth()+1}-${dt.getDate()}-${dt.getHours()}`;
      return sigDateChecked.has(k);
    });
  }

  // Column value filters
  for (const [col, sel] of Object.entries(sigColFilters)) {
    if (!sel || sel.size === 0) continue;
    rows = rows.filter(s => {
      const v = String(sigColVal(s, col) ?? '').toLowerCase();
      return sel.has(v);
    });
  }

  // Sort
  rows.sort((a, b) => {
    const av = sigColVal(a, sigSortCol);
    const bv = sigColVal(b, sigSortCol);
    if (av < bv) return -sigSortDir;
    if (av > bv) return  sigSortDir;
    return 0;
  });

  const total = rows.length;
  const pages = Math.max(1, Math.ceil(total / SIG_PAGE_SIZE));
  sigPage = Math.min(sigPage, pages);
  const start = (sigPage - 1) * SIG_PAGE_SIZE;
  const slice = rows.slice(start, start + SIG_PAGE_SIZE);

  tbody.innerHTML = slice.map(s => `
    <tr>
      <td style="white-space:nowrap;color:var(--text-sub)">${fmtTime(s.timestamp)}</td>
      <td style="font-weight:600">${esc(s.symbol)}</td>
      <td>${dirBadge(s.direction)}</td>
      <td>${voteCell(s.c1)}</td><td>${voteCell(s.c2)}</td><td>${voteCell(s.c3)}</td>
      <td>${voteCell(s.c4)}</td><td>${voteCell(s.c5)}</td><td>${voteCell(s.c6)}</td>
      <td>${voteCell(s.c7)}</td><td>${voteCell(s.c8)}</td>
      <td class="${scoreClass(s.score)}" style="font-weight:600">${s.score ?? '—'}</td>
      <td>${tierBadge(s.tier)}</td>
      <td style="color:var(--text-sub);font-size:11px">${esc(s.regime) || '—'}</td>
      <td>${riskBadge(s.macro_risk)}</td>
    </tr>`).join('');

  renderPagination('sig-pagination', sigPage, pages, total, p => { sigPage = p; renderSigTable(); });
  updateSigColDdIndicators();
}

function riskBadge(risk) {
  if (!risk) return '—';
  const cls = risk === 'HIGH' ? 'pnl-neg' : risk === 'MEDIUM' ? '' : 'score-high';
  return `<span class="${cls}" style="font-size:11px">${risk}</span>`;
}

document.getElementById('sig-sym-filter')?.addEventListener('change', renderSignals);
document.getElementById('sig-tier-filter')?.addEventListener('change', renderSignals);
document.getElementById('sig-tradeable-only')?.addEventListener('change', renderSignals);

/* ═══════════════════════════════════════════════════════════════════════════ */
/* TRADE LEDGER — sortable + per-column filter                                  */
/* ═══════════════════════════════════════════════════════════════════════════ */

// Column value extractor for sorting / filtering
function tradeColVal(row, col) {
  switch (col) {
    case 'time':               return row.time || '';
    case 'type':               return row.type || '';
    case 'symbol':             return row.symbol || '';
    case 'direction':          return row.direction || '';
    case 'asset_type':         return row.asset_type || '';
    case 'price':              return row.price ?? -Infinity;
    case 'stop_price':         return row.stop_price ?? -Infinity;
    case 'take_profit_price':  return row.take_profit_price ?? -Infinity;
    case 'quantity':           return row.quantity ?? -Infinity;
    case 'pnl_usd':            return row.pnl_usd ?? -Infinity;
    case 'reason':             return row.reason || '';
    case 'tier':               return row.tier || '';
    case 'confidence':         return row.confidence ?? -Infinity;
    case 'trade_id':           return row.trade_id ?? -Infinity;
    default:                   return '';
  }
}

function sigColVal(s, col) {
  switch (col) {
    case 'timestamp':  return s.timestamp || '';
    case 'symbol':     return s.symbol || '';
    case 'direction':  return s.direction || '';
    case 'c1':         return s.c1 ?? -Infinity;
    case 'c2':         return s.c2 ?? -Infinity;
    case 'c3':         return s.c3 ?? -Infinity;
    case 'c4':         return s.c4 ?? -Infinity;
    case 'c5':         return s.c5 ?? -Infinity;
    case 'c6':         return s.c6 ?? -Infinity;
    case 'c7':         return s.c7 ?? -Infinity;
    case 'c8':         return s.c8 ?? -Infinity;
    case 'score':      return s.score ?? -Infinity;
    case 'tier':       return s.tier || '';
    case 'regime':     return s.regime || '';
    case 'macro_risk': return s.macro_risk || '';
    default:           return '';
  }
}

function applyTradesFiltersAndSort() {
  let rows = [...tradesAllRows];

  // Date filter (multi-select: Set of "Y-M-D-H" leaf keys; empty = show all)
  if (tradesDateChecked.size > 0) {
    rows = rows.filter(r => {
      if (!r.time) return false;
      const dt = new Date(String(r.time).replace(' ', 'T'));
      const k = `${dt.getFullYear()}-${dt.getMonth()+1}-${dt.getDate()}-${dt.getHours()}`;
      return tradesDateChecked.has(k);
    });
  }

  // Symbol dropdown filter
  const sym = document.getElementById('tr-sym-filter')?.value || '';
  if (sym) rows = rows.filter(r => r.symbol === sym);

  // Column value filters (each col has a Set of selected values; empty Set = no filter)
  for (const [col, sel] of Object.entries(tradesColFilters)) {
    if (!sel || sel.size === 0) continue;
    rows = rows.filter(r => {
      const v = String(tradeColVal(r, col) ?? '').toLowerCase();
      return sel.has(v);
    });
  }

  // Sort
  rows.sort((a, b) => {
    const av = tradeColVal(a, tradesSortCol);
    const bv = tradeColVal(b, tradesSortCol);
    if (av < bv) return -tradesSortDir;
    if (av > bv) return  tradesSortDir;
    return 0;
  });

  // Compute summary from filtered rows
  const closes       = rows.filter(r => r.type === 'CLOSE');
  const partialRows  = rows.filter(r => r.type && r.type.includes('PARTIAL'));
  const wins         = closes.filter(r => (r.pnl_usd || 0) > 0).length;
  const partials     = partialRows.length;
  const closedPnl    = closes.reduce((s, r) => s + (r.pnl_usd || 0), 0);
  const partialPnl   = partialRows.reduce((s, r) => s + (r.pnl_usd || 0), 0);
  setText('tr-realized', fmtUSD(closedPnl + partialPnl));
  setText('tr-winrate',  closes.length ? (wins / closes.length * 100).toFixed(1) + '%' : '—');
  setText('tr-closes',   String(closes.length));
  setText('tr-partials', String(partials));

  // Client-side pagination
  const total = rows.length;
  const pages = Math.max(1, Math.ceil(total / TRADES_PAGE_SIZE));
  if (tradesPage > pages) tradesPage = 1;
  const slice = rows.slice((tradesPage - 1) * TRADES_PAGE_SIZE, tradesPage * TRADES_PAGE_SIZE);

  renderTradesRows(slice);
  renderPagination('trades-pagination', tradesPage, pages, total, p => {
    tradesPage = p; applyTradesFiltersAndSort();
  });
  updateColDdIndicators();
}

function updateSortIcons() {
  // Legacy stub — replaced by updateColDdIndicators()
}

function updateColDdIndicators() {
  document.querySelectorAll('#trades-sort-row th[data-col]').forEach(th => {
    const col = th.dataset.col;
    const btn = th.querySelector('.col-dd-btn');
    if (!btn) return;
    const hasFilter = col === 'time'
      ? tradesDateChecked.size > 0
      : tradesColFilters[col]?.size > 0;
    const isSort = col === tradesSortCol;
    btn.classList.toggle('col-dd-active', hasFilter || isSort);
    if (isSort) {
      btn.textContent = tradesSortDir === -1 ? '↓' : '↑';
    } else if (!hasFilter) {
      btn.textContent = '⌄';
    }
    if (hasFilter && !isSort) btn.textContent = '▾';
  });
}

function updateSigColDdIndicators() {
  document.querySelectorAll('#sig-sort-row th[data-col]').forEach(th => {
    const col = th.dataset.col;
    const btn = th.querySelector('.sig-col-dd-btn');
    if (!btn) return;
    const hasFilter = col === 'timestamp'
      ? sigDateChecked.size > 0
      : sigColFilters[col]?.size > 0;
    const isSort = col === sigSortCol;
    btn.classList.toggle('col-dd-active', hasFilter || isSort);
    if (isSort) {
      btn.textContent = sigSortDir === -1 ? '↓' : '↑';
    } else if (!hasFilter) {
      btn.textContent = '⌄';
    }
    if (hasFilter && !isSort) btn.textContent = '▾';
  });
}

function assetTypeBadge(at) {
  if (!at) return '—';
  const cls = at === 'forex' ? 'at-forex' : at === 'crypto' ? 'at-crypto' : 'at-stock';
  return `<span class="at-badge ${cls}">${esc(at)}</span>`;
}

function brokerBadge(b) {
  if (!b) return '—';
  const name = b.toLowerCase();
  const cls  = name === 'oanda' ? 'broker-oanda' : 'broker-ibkr';
  return `<span class="broker-badge ${cls}">${esc(b.toUpperCase())}</span>`;
}

function reasonBadge(reason, type) {
  if (!reason || reason === 'entry') {
    return type === 'OPEN'
      ? '<span class="reason-entry">entry</span>'
      : '<span style="color:var(--text-muted)">—</span>';
  }
  const r = reason.toLowerCase();
  if (r === 'stop_loss')   return '<span class="reason-stop">stop loss</span>';
  if (r === 'take_profit') return '<span class="reason-tp">take profit</span>';
  if (r === 'signal_exit') return '<span class="reason-sig">signal exit</span>';
  if (r.includes('partial')) return '<span class="reason-partial">phase-2</span>';
  return `<span class="reason-other">${esc(reason)}</span>`;
}

function holdTime(entryIso, exitIso) {
  if (!entryIso || !exitIso) return '—';
  // SQLite may store "YYYY-MM-DD HH:MM:SS" (space); normalize to T for reliable parsing
  const parse = s => new Date(String(s).replace(' ', 'T'));
  const ms = parse(exitIso) - parse(entryIso);
  if (isNaN(ms) || ms < 0) return '—';
  const h = Math.floor(ms / 3600000);
  const m = Math.floor((ms % 3600000) / 60000);
  if (h >= 48) return `${Math.floor(h/24)}d ${h%24}h`;
  if (h >= 1)  return `${h}h ${m}m`;
  return `${m}m`;
}

function renderTradesRows(rows) {
  const tbody = document.querySelector('#trades-table tbody');
  if (!tbody) return;

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="13">${emptyState('No matching trades')}</td></tr>`;
    return;
  }

  tbody.innerHTML = rows.map(r => {
    const isOpen    = r.type === 'OPEN';
    const isPartial = r.type?.includes('PARTIAL');
    const isClose   = r.type === 'CLOSE';
    const typeClass = isOpen ? 'tx-open' : isPartial ? 'tx-partial' : 'tx-close';

    // P&L cell: show USD + % for closes
    let pnlCell = '—';
    if (r.pnl_usd != null) {
      const pct = r.pnl_pct != null ? `<span style="font-size:10px;opacity:0.7;margin-left:4px">${fmtPct(r.pnl_pct)}</span>` : '';
      pnlCell = `<span class="${pnlClass(r.pnl_usd)}">${fmtUSD(r.pnl_usd)}${pct}</span>`;
    }

    // Price cell: show stop/tp inline for OPEN rows
    let priceCell = fmtNum(r.price, 4);
    if (isOpen && (r.stop_price != null || r.take_profit_price != null)) {
      const stop = r.stop_price != null ? `<span class="price-stop" title="Stop">▼${fmtNum(r.stop_price,4)}</span>` : '';
      const tp   = r.take_profit_price != null ? `<span class="price-tp" title="TP">▲${fmtNum(r.take_profit_price,4)}</span>` : '';
      priceCell += `<div class="price-levels">${stop}${tp}</div>`;
    }

    const hold = (isClose || isPartial) ? holdTime(r.entry_time, r.exit_time || r.time) : '—';

    return `<tr class="tr-${typeClass}">
      <td style="white-space:nowrap;color:var(--text-sub);font-size:11px">${fmtTime(r.time)}</td>
      <td><span class="type-badge ${typeClass}" style="white-space:nowrap">${esc(r.type)}</span></td>
      <td style="font-weight:600;letter-spacing:0.02em">${esc(r.symbol)}</td>
      <td>${dirBadge(r.direction)}</td>
      <td>${assetTypeBadge(r.asset_type)}</td>
      <td style="font-family:var(--mono);font-size:12px">${priceCell}</td>
      <td style="font-family:var(--mono);color:var(--text-sub)">${fmtNum(r.quantity, 2)}</td>
      <td>${pnlCell}</td>
      <td>${reasonBadge(r.reason, r.type)}</td>
      <td>${tierBadge(r.tier)}</td>
      <td style="font-family:var(--mono);color:var(--text-sub);font-size:11px">${r.confidence != null ? fmtNum(r.confidence, 1) : '—'}</td>
      <td style="font-family:var(--mono);color:var(--text-muted);font-size:11px;white-space:nowrap">${hold}</td>
      <td style="font-family:var(--mono);color:var(--text-muted);font-size:11px">${r.trade_id ?? '—'}</td>
    </tr>`;
  }).join('');
}

// Date filter state — committed (applied) and pending (while dropdown is open)
let tradesDateChecked = new Set(); // Set of "Y-M-D-H" leaf strings; empty = no filter
let tradesDatePending = new Set(); // working copy while dropdown is open

async function renderTrades() {
  // Fetch ALL rows once; all filtering & pagination done client-side
  const data = await apiFetch('/api/trades?page=1&page_size=9999');
  if (!data) return;

  tradesAllRows = data.rows || [];

  // Populate symbol filter once from all rows
  const symSel = document.getElementById('tr-sym-filter');
  if (symSel && symSel.options.length <= 1 && tradesAllRows.length) {
    const syms = [...new Set(tradesAllRows.map(r => r.symbol).filter(Boolean))].sort();
    symSel.innerHTML = '<option value="">All</option>' +
      syms.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
  }

  applyTradesFiltersAndSort();
  renderTradesCumChart();
}

function renderTradesCumChart() {
  // Collect events with PnL: CLOSE and PARTIAL rows only
  const events = tradesAllRows
    .filter(r => r.pnl_usd != null && r.time)
    .sort((a, b) => (a.time < b.time ? -1 : 1));

  if (events.length === 0) {
    const wrap = document.getElementById('trades-cum-chart')?.parentElement;
    if (wrap) wrap.innerHTML = emptyState('No closed trades yet');
    return;
  }

  let cum = 0;
  const points = events.map(r => {
    cum += r.pnl_usd;
    return { x: r.time, y: round2(cum) };
  });

  const isPos = cum >= 0;
  makeChart('trades-cum-chart', {
    type: 'line',
    data: {
      datasets: [{
        data: points,
        borderColor: isPos ? '#145229' : '#9B1C1C',
        backgroundColor: isPos ? 'rgba(20,82,41,0.06)' : 'rgba(155,28,28,0.06)',
        borderWidth: 1.5,
        tension: 0.2,
        pointRadius: points.length < 30 ? 3 : 0,
        pointHoverRadius: 5,
        fill: true,
      }],
    },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: {
          type: 'time',
          time: {
            tooltipFormat: 'MMM d, HH:mm',
            displayFormats: {
              minute: 'HH:mm',
              hour:   'MMM d HH:mm',
              day:    'MMM d',
              week:   'MMM d',
              month:  'MMM yyyy',
            },
          },
          ticks: { maxRotation: 0, maxTicksLimit: 8 },
          grid: { display: false },
        },
        y: {
          ticks: { callback: v => '$' + v.toFixed(2) },
          grid: { color: cGrid() },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          ...cTooltip(),
          callbacks: {
            title: items => {
              const d = new Date(items[0].parsed.x);
              return d.toLocaleString('en-US', { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
            },
            label: ctx => ' Cum. P&L  ' + fmtUSD(ctx.parsed.y),
          },
        },
        zoom: cZoom(86400000),  // min zoom = 1 day for trade-level chart
      },
    },
  });
}

function round2(v) { return Math.round(v * 100) / 100; }

/* ── Column dropdown (Excel-style AutoFilter) ─────────────────────────────── */
let colDdActiveCol = null;
let _ddTh = null; // TH element the dropdown is anchored to

function _positionDd(dd, th, w) {
  const rect = th.getBoundingClientRect();
  let left = rect.left;
  if (left + w > window.innerWidth - 8) left = window.innerWidth - w - 8;
  if (left < 4) left = 4;
  dd.style.left = left + 'px';
  dd.style.top  = (rect.bottom + 4) + 'px';
}

function _onScrollReposition() {
  const dd = document.getElementById('col-dd');
  if (!dd || dd.style.display === 'none' || !_ddTh) return;
  _positionDd(dd, _ddTh, parseInt(dd.style.width) || 210);
}
// Catch all scroll events (page, table horizontal, any container)
document.addEventListener('scroll', _onScrollReposition, { passive: true, capture: true });

function openColDd(col, triggerEl) {
  const dd = document.getElementById('col-dd');
  if (!dd) return;

  // Close if same col clicked again
  if (colDdActiveCol === col && dd.style.display !== 'none') {
    closeColDd(); return;
  }
  colDdActiveCol = col;
  _ddTh = triggerEl.closest('th');

  dd.style.display = 'block';
  const ddW = col === 'time' ? 230 : 210;
  dd.style.width = ddW + 'px';
  _positionDd(dd, _ddTh, ddW);

  // Sort buttons
  dd.querySelectorAll('.col-dd-sort-btn').forEach(btn => {
    btn.onclick = () => {
      tradesSortCol = col;
      tradesSortDir = btn.dataset.dir === 'asc' ? 1 : -1;
      applyTradesFiltersAndSort();
      closeColDd();
    };
  });

  const search  = document.getElementById('col-dd-search');
  const list    = document.getElementById('col-dd-list');
  const clearBtn   = document.getElementById('col-dd-clear');
  const selAllBtn  = document.getElementById('col-dd-selectall');

  // ── Excel-style date tree for the time column ─────────────────────────
  if (col === 'time') {
    if (search) { search.style.display = ''; search.value = ''; search.placeholder = 'Search date…'; }
    if (clearBtn)  { clearBtn.textContent  = 'Cancel'; }
    if (selAllBtn) { selAllBtn.textContent = 'OK'; selAllBtn.style.display = ''; }

    tradesDatePending = new Set(tradesDateChecked);

    const MONTHS = ['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

    // Build tree: { year: { month: { day: Set<hour> } } }
    const tree = {};
    tradesAllRows.forEach(r => {
      if (!r.time) return;
      const dt = new Date(String(r.time).replace(' ', 'T'));
      const y = dt.getFullYear(), m = dt.getMonth()+1, d = dt.getDate(), h = dt.getHours();
      if (!tree[y]) tree[y] = {};
      if (!tree[y][m]) tree[y][m] = {};
      if (!tree[y][m][d]) tree[y][m][d] = new Set();
      tree[y][m][d].add(h);
    });

    function lk(y, m, d, h) { return `${y}-${m}-${d}-${h}`; }

    function leavesFor(y, m, d) {
      const keys = [];
      const ys = y != null ? [y] : Object.keys(tree).map(Number);
      ys.forEach(yr => {
        const ms = m != null ? [m] : Object.keys(tree[yr]||{}).map(Number);
        ms.forEach(mo => {
          const ds = d != null ? [d] : Object.keys((tree[yr]||{})[mo]||{}).map(Number);
          ds.forEach(dy => {
            ((tree[yr]||{})[mo]||{})[dy]?.forEach(h => keys.push(lk(yr, mo, dy, h)));
          });
        });
      });
      return keys;
    }

    function pState(leaves) {
      const n = leaves.filter(k => tradesDatePending.has(k)).length;
      if (n === 0) return 'none';
      if (n === leaves.length) return 'all';
      return 'some';
    }

    function setCb(el, state) {
      if (!el) return;
      el.checked = state === 'all';
      el.indeterminate = state === 'some';
    }

    function updateCbStates() {
      if (!list) return;
      list.querySelectorAll('input.ddt-cb[data-h]').forEach(cb => {
        cb.checked = tradesDatePending.has(lk(+cb.dataset.y, +cb.dataset.m, +cb.dataset.d, +cb.dataset.h));
        cb.indeterminate = false;
      });
      list.querySelectorAll('input.ddt-cb[data-d]:not([data-h])').forEach(cb =>
        setCb(cb, pState(leavesFor(+cb.dataset.y, +cb.dataset.m, +cb.dataset.d))));
      list.querySelectorAll('input.ddt-cb[data-m]:not([data-d])').forEach(cb =>
        setCb(cb, pState(leavesFor(+cb.dataset.y, +cb.dataset.m, null))));
      list.querySelectorAll('input.ddt-cb[data-y]:not([data-m])').forEach(cb =>
        setCb(cb, pState(leavesFor(+cb.dataset.y, null, null))));
      setCb(list.querySelector('#ddt-sa'), pState(leavesFor(null, null, null)));
    }

    // Restore expanded state: auto-open ancestors of any committed selection
    const ddtExp = new Set();
    tradesDateChecked.forEach(key => {
      const [y, m, d] = key.split('-');
      ddtExp.add(`ddt-y${y}`);
      ddtExp.add(`ddt-m${y}-${m}`);
      ddtExp.add(`ddt-d${y}-${m}-${d}`);
    });

    function buildDdtTree(q) {
      if (!list) return;
      q = (q || '').toLowerCase().trim();
      const years = Object.keys(tree).map(Number).sort((a,b) => b-a);
      let html = '';

      if (q) {
        // ── Flat search results ──
        const matches = [];
        years.forEach(y => {
          Object.keys(tree[y]).map(Number).sort((a,b)=>a-b).forEach(m => {
            Object.keys(tree[y][m]).map(Number).sort((a,b)=>a-b).forEach(d => {
              [...tree[y][m][d]].sort((a,b)=>a-b).forEach(h => {
                const label = `${y} › ${MONTHS[m]} › ${String(d).padStart(2,'0')} › ${String(h).padStart(2,'0')}:00`;
                if (label.toLowerCase().includes(q)) matches.push({y,m,d,h,label});
              });
            });
          });
        });

        if (!matches.length) {
          list.innerHTML = '<div class="ddt-empty">No matches</div>';
          return;
        }

        const matchKeys = matches.map(({y,m,d,h}) => lk(y,m,d,h));
        const saState = pState(matchKeys);
        html = `<div class="ddt-item">
          <input type="checkbox" class="ddt-cb" id="ddt-sa">
          <label for="ddt-sa" class="ddt-bold">Select All Results</label>
          <span class="ddt-x" id="ddt-ca" title="Clear filter">✕</span>
        </div><div class="ddt-sep"></div>` +
          matches.map(({y,m,d,h,label}) => {
            const k = lk(y,m,d,h);
            return `<div class="ddt-item ddt-flat">
              <input type="checkbox" class="ddt-cb" data-y="${y}" data-m="${m}" data-d="${d}" data-h="${h}" id="ddt-r${k}">
              <label for="ddt-r${k}" class="ddt-lbl">${label}</label>
            </div>`;
          }).join('');

        list.innerHTML = html;
        matches.forEach(({y,m,d,h}) => {
          const cb = list.querySelector(`#ddt-r${lk(y,m,d,h)}`);
          if (cb) { cb.checked = tradesDatePending.has(lk(y,m,d,h)); cb.indeterminate = false; }
        });
        const saEl = list.querySelector('#ddt-sa');
        setCb(saEl, saState);

        list.querySelector('#ddt-ca')?.addEventListener('click', () => {
          tradesDatePending.clear();
          list.querySelectorAll('input.ddt-cb[data-h]').forEach(cb => { cb.checked = false; cb.indeterminate = false; });
          setCb(saEl, 'none');
        });

        saEl?.addEventListener('change', () => {
          matchKeys.forEach(k => saEl.checked ? tradesDatePending.add(k) : tradesDatePending.delete(k));
          list.querySelectorAll('input.ddt-cb[data-h]').forEach(cb => {
            cb.checked = tradesDatePending.has(lk(+cb.dataset.y,+cb.dataset.m,+cb.dataset.d,+cb.dataset.h));
          });
          setCb(saEl, pState(matchKeys));
        });
        list.querySelectorAll('input.ddt-cb[data-h]').forEach(cb => {
          cb.addEventListener('change', () => {
            const k = lk(+cb.dataset.y,+cb.dataset.m,+cb.dataset.d,+cb.dataset.h);
            cb.checked ? tradesDatePending.add(k) : tradesDatePending.delete(k);
            setCb(saEl, pState(matchKeys));
          });
        });
        return;
      }

      // ── Tree mode ──
      const allLeaves = leavesFor(null, null, null);
      html = `<div class="ddt-item">
        <input type="checkbox" class="ddt-cb" id="ddt-sa">
        <label for="ddt-sa" class="ddt-bold">Select All</label>
        <span class="ddt-x" id="ddt-ca" title="Clear filter">✕</span>
      </div><div class="ddt-sep"></div>`;

      years.forEach(y => {
        const yId = `ddt-y${y}`;
        const yOpen = ddtExp.has(yId);
        html += `<div class="ddt-item">
          <input type="checkbox" class="ddt-cb" data-y="${y}" id="cb-y${y}">
          <span class="ddt-arr${yOpen?' open':''}" data-cid="${yId}">›</span>
          <label for="cb-y${y}" class="ddt-lbl">${y}</label>
        </div><div id="${yId}" class="ddt-ch"${yOpen?'':' style="display:none"'}>`;

        Object.keys(tree[y]).map(Number).sort((a,b)=>a-b).forEach(m => {
          const mId = `ddt-m${y}-${m}`;
          const mOpen = ddtExp.has(mId);
          html += `<div class="ddt-item">
            <input type="checkbox" class="ddt-cb" data-y="${y}" data-m="${m}" id="cb-m${y}-${m}">
            <span class="ddt-arr${mOpen?' open':''}" data-cid="${mId}">›</span>
            <label for="cb-m${y}-${m}" class="ddt-lbl">${MONTHS[m]}</label>
          </div><div id="${mId}" class="ddt-ch"${mOpen?'':' style="display:none"'}>`;

          Object.keys(tree[y][m]).map(Number).sort((a,b)=>a-b).forEach(d => {
            const dId = `ddt-d${y}-${m}-${d}`;
            const dOpen = ddtExp.has(dId);
            html += `<div class="ddt-item">
              <input type="checkbox" class="ddt-cb" data-y="${y}" data-m="${m}" data-d="${d}" id="cb-d${y}-${m}-${d}">
              <span class="ddt-arr${dOpen?' open':''}" data-cid="${dId}">›</span>
              <label for="cb-d${y}-${m}-${d}" class="ddt-lbl">${String(d).padStart(2,'0')}</label>
            </div><div id="${dId}" class="ddt-ch"${dOpen?'':' style="display:none"'}>`;

            [...tree[y][m][d]].sort((a,b)=>a-b).forEach(h => {
              html += `<div class="ddt-item">
                <input type="checkbox" class="ddt-cb" data-y="${y}" data-m="${m}" data-d="${d}" data-h="${h}" id="cb-h${y}-${m}-${d}-${h}">
                <label for="cb-h${y}-${m}-${d}-${h}" class="ddt-lbl">${String(h).padStart(2,'0')}:00</label>
              </div>`;
            });

            html += `</div>`; // close day children
          });
          html += `</div>`; // close month children
        });
        html += `</div>`; // close year children
      });

      list.innerHTML = html;
      updateCbStates();

      // Clear All
      list.querySelector('#ddt-ca')?.addEventListener('click', () => {
        tradesDatePending.clear();
        updateCbStates();
      });

      // Toggle arrows — CSS rotation, don't close dropdown
      list.querySelectorAll('.ddt-arr').forEach(arr => {
        arr.addEventListener('click', e => {
          e.preventDefault();
          e.stopPropagation();
          const cid = arr.dataset.cid;
          const child = document.getElementById(cid);
          if (!child) return;
          const open = child.style.display !== 'none';
          child.style.display = open ? 'none' : '';
          arr.classList.toggle('open', !open);
          open ? ddtExp.delete(cid) : ddtExp.add(cid);
        });
      });

      // Select All
      list.querySelector('#ddt-sa')?.addEventListener('change', e => {
        allLeaves.forEach(k => e.target.checked ? tradesDatePending.add(k) : tradesDatePending.delete(k));
        updateCbStates();
      });
      // Year
      list.querySelectorAll('input.ddt-cb[data-y]:not([data-m])').forEach(cb => {
        cb.addEventListener('change', () => {
          leavesFor(+cb.dataset.y, null, null)
            .forEach(k => cb.checked ? tradesDatePending.add(k) : tradesDatePending.delete(k));
          updateCbStates();
        });
      });
      // Month
      list.querySelectorAll('input.ddt-cb[data-m]:not([data-d])').forEach(cb => {
        cb.addEventListener('change', () => {
          leavesFor(+cb.dataset.y, +cb.dataset.m, null)
            .forEach(k => cb.checked ? tradesDatePending.add(k) : tradesDatePending.delete(k));
          updateCbStates();
        });
      });
      // Day
      list.querySelectorAll('input.ddt-cb[data-d]:not([data-h])').forEach(cb => {
        cb.addEventListener('change', () => {
          leavesFor(+cb.dataset.y, +cb.dataset.m, +cb.dataset.d)
            .forEach(k => cb.checked ? tradesDatePending.add(k) : tradesDatePending.delete(k));
          updateCbStates();
        });
      });
      // Hour
      list.querySelectorAll('input.ddt-cb[data-h]').forEach(cb => {
        cb.addEventListener('change', () => {
          const k = lk(+cb.dataset.y, +cb.dataset.m, +cb.dataset.d, +cb.dataset.h);
          cb.checked ? tradesDatePending.add(k) : tradesDatePending.delete(k);
          updateCbStates();
        });
      });
    }

    buildDdtTree('');
    if (search) search.oninput = () => buildDdtTree(search.value);

    // OK — commit pending and apply
    if (selAllBtn) selAllBtn.onclick = () => {
      tradesDateChecked = new Set(tradesDatePending);
      tradesPage = 1; applyTradesFiltersAndSort(); updateColDdIndicators(); closeColDd();
    };
    // Cancel — discard pending
    if (clearBtn) clearBtn.onclick = () => closeColDd();

    return;
  }

  // ── Generic checkbox filter for all other columns ───────────────────────
  if (search)   { search.style.display = ''; search.placeholder = 'Search…'; }
  if (selAllBtn) { selAllBtn.style.display = ''; selAllBtn.textContent = 'Select All'; }
  if (clearBtn)  { clearBtn.textContent = 'Clear'; }
  if (search) { search.value = ''; }

  const allVals = [...new Set(
    tradesAllRows.map(r => String(tradeColVal(r, col) ?? '').toLowerCase()).filter(v => v !== '' && v !== '-infinity')
  )].sort();

  const currentSel = tradesColFilters[col] || new Set();
  tradesColFilters[col] = currentSel;

  function renderList(filter) {
    const visible = filter ? allVals.filter(v => v.includes(filter.toLowerCase())) : allVals;
    if (!list) return;
    list.innerHTML = visible.map(v => `
      <label class="col-dd-item">
        <input type="checkbox" value="${esc(v)}" ${currentSel.has(v) ? 'checked' : ''}/>
        <span>${esc(v)}</span>
      </label>
    `).join('');
    list.querySelectorAll('input[type=checkbox]').forEach(cb => {
      cb.addEventListener('change', () => {
        if (cb.checked) currentSel.add(cb.value);
        else currentSel.delete(cb.value);
        tradesColFilters[col] = currentSel;
        applyTradesFiltersAndSort();
      });
    });
  }
  renderList('');
  if (search) {
    search.oninput = () => renderList(search.value);
    setTimeout(() => search.focus(), 50);
  }

  if (clearBtn) clearBtn.onclick = () => {
    tradesColFilters[col] = new Set();
    closeColDd();
    applyTradesFiltersAndSort();
  };

  if (selAllBtn) selAllBtn.onclick = () => {
    allVals.forEach(v => currentSel.add(v));
    tradesColFilters[col] = currentSel;
    applyTradesFiltersAndSort();
    closeColDd();
  };
}

function closeColDd() {
  const dd = document.getElementById('col-dd');
  if (dd) dd.style.display = 'none';
  colDdActiveCol = null;
  _ddTh = null;
}

// Open dropdown when trigger button is clicked
document.getElementById('trades-table')?.addEventListener('click', e => {
  const btn = e.target.closest('.col-dd-btn');
  if (!btn) return;
  e.stopPropagation();
  openColDd(btn.dataset.col, btn);
});

// Close on outside click
document.addEventListener('click', e => {
  const dd = document.getElementById('col-dd');
  if (!dd || dd.style.display === 'none') return;
  if (!dd.contains(e.target) && !e.target.closest('.col-dd-btn')) {
    closeColDd();
  }
});

// Close on Escape
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeColDd();
});

// Clicking the th label (not the ⌄ button) also opens the dropdown
document.querySelector('#trades-sort-row')?.addEventListener('click', e => {
  if (e.target.closest('.col-dd-btn')) return; // already handled by trades-table listener
  const th = e.target.closest('th[data-col]');
  if (!th) return;
  const btn = th.querySelector('.col-dd-btn');
  if (btn) openColDd(th.dataset.col, btn);
});

document.getElementById('tr-sym-filter')?.addEventListener('change',   () => { tradesPage = 1; renderTrades(); });
document.getElementById('tr-year-filter')?.addEventListener('change',  () => { tradesPage = 1; renderTrades(); });
document.getElementById('tr-month-filter')?.addEventListener('change', () => { tradesPage = 1; renderTrades(); });
document.getElementById('tr-day-filter')?.addEventListener('change',   () => { tradesPage = 1; renderTrades(); });

/* ═══════════════════════════════════════════════════════════════════════════ */
/* SIGNALS — Column dropdown (Excel-style AutoFilter)                           */
/* ═══════════════════════════════════════════════════════════════════════════ */

function _positionSigDd(dd, th, w) {
  const rect = th.getBoundingClientRect();
  let left = rect.left;
  if (left + w > window.innerWidth - 8) left = window.innerWidth - w - 8;
  if (left < 4) left = 4;
  dd.style.left = left + 'px';
  dd.style.top  = (rect.bottom + 4) + 'px';
}

document.addEventListener('scroll', () => {
  const dd = document.getElementById('sig-col-dd');
  if (!dd || dd.style.display === 'none' || !_sigDdTh) return;
  _positionSigDd(dd, _sigDdTh, parseInt(dd.style.width) || 210);
}, { passive: true, capture: true });

function openSigColDd(col, triggerEl) {
  const dd = document.getElementById('sig-col-dd');
  if (!dd) return;

  // Close if same col clicked again
  if (sigColDdActiveCol === col && dd.style.display !== 'none') {
    closeSigColDd(); return;
  }
  sigColDdActiveCol = col;
  _sigDdTh = triggerEl.closest('th');

  dd.style.display = 'block';
  const ddW = col === 'timestamp' ? 230 : 210;
  dd.style.width = ddW + 'px';
  _positionSigDd(dd, _sigDdTh, ddW);

  // Sort buttons
  dd.querySelectorAll('.sig-col-dd-sort-btn').forEach(btn => {
    btn.onclick = () => {
      sigSortCol = col;
      sigSortDir = btn.dataset.dir === 'asc' ? 1 : -1;
      sigPage = 1;
      renderSigTable();
      closeSigColDd();
    };
  });

  const search    = document.getElementById('sig-col-dd-search');
  const list      = document.getElementById('sig-col-dd-list');
  const clearBtn  = document.getElementById('sig-col-dd-clear');
  const selAllBtn = document.getElementById('sig-col-dd-selectall');

  // ── Excel-style date tree for the timestamp column ─────────────────────
  if (col === 'timestamp') {
    if (search)   { search.style.display = ''; search.value = ''; search.placeholder = 'Search date…'; }
    if (clearBtn)  { clearBtn.textContent  = 'Cancel'; }
    if (selAllBtn) { selAllBtn.textContent = 'OK'; selAllBtn.style.display = ''; }

    sigDatePending = new Set(sigDateChecked);

    const MONTHS = ['','Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

    // Build tree: { year: { month: { day: Set<hour> } } }
    const tree = {};
    sigAllSignals.forEach(s => {
      if (!s.timestamp) return;
      const dt = new Date(String(s.timestamp).replace(' ', 'T'));
      const y = dt.getFullYear(), m = dt.getMonth()+1, d = dt.getDate(), h = dt.getHours();
      if (!tree[y]) tree[y] = {};
      if (!tree[y][m]) tree[y][m] = {};
      if (!tree[y][m][d]) tree[y][m][d] = new Set();
      tree[y][m][d].add(h);
    });

    function lk(y, m, d, h) { return `${y}-${m}-${d}-${h}`; }

    function leavesFor(y, m, d) {
      const keys = [];
      const ys = y != null ? [y] : Object.keys(tree).map(Number);
      ys.forEach(yr => {
        const ms = m != null ? [m] : Object.keys(tree[yr]||{}).map(Number);
        ms.forEach(mo => {
          const ds = d != null ? [d] : Object.keys((tree[yr]||{})[mo]||{}).map(Number);
          ds.forEach(dy => {
            ((tree[yr]||{})[mo]||{})[dy]?.forEach(h => keys.push(lk(yr, mo, dy, h)));
          });
        });
      });
      return keys;
    }

    function pState(leaves) {
      const n = leaves.filter(k => sigDatePending.has(k)).length;
      if (n === 0) return 'none';
      if (n === leaves.length) return 'all';
      return 'some';
    }

    function setCb(el, state) {
      if (!el) return;
      el.checked = state === 'all';
      el.indeterminate = state === 'some';
    }

    function updateCbStates() {
      if (!list) return;
      list.querySelectorAll('input.ddt-cb[data-h]').forEach(cb => {
        cb.checked = sigDatePending.has(lk(+cb.dataset.y, +cb.dataset.m, +cb.dataset.d, +cb.dataset.h));
        cb.indeterminate = false;
      });
      list.querySelectorAll('input.ddt-cb[data-d]:not([data-h])').forEach(cb =>
        setCb(cb, pState(leavesFor(+cb.dataset.y, +cb.dataset.m, +cb.dataset.d))));
      list.querySelectorAll('input.ddt-cb[data-m]:not([data-d])').forEach(cb =>
        setCb(cb, pState(leavesFor(+cb.dataset.y, +cb.dataset.m, null))));
      list.querySelectorAll('input.ddt-cb[data-y]:not([data-m])').forEach(cb =>
        setCb(cb, pState(leavesFor(+cb.dataset.y, null, null))));
      setCb(list.querySelector('#sddt-sa'), pState(leavesFor(null, null, null)));
    }

    const ddtExp = new Set();
    sigDateChecked.forEach(key => {
      const [y, m, d] = key.split('-');
      ddtExp.add(`sddt-y${y}`);
      ddtExp.add(`sddt-m${y}-${m}`);
      ddtExp.add(`sddt-d${y}-${m}-${d}`);
    });

    function buildDdtTree(q) {
      if (!list) return;
      q = (q || '').toLowerCase().trim();
      const years = Object.keys(tree).map(Number).sort((a,b) => b-a);
      let html = '';

      if (q) {
        const matches = [];
        years.forEach(y => {
          Object.keys(tree[y]).map(Number).sort((a,b)=>a-b).forEach(m => {
            Object.keys(tree[y][m]).map(Number).sort((a,b)=>a-b).forEach(d => {
              [...tree[y][m][d]].sort((a,b)=>a-b).forEach(h => {
                const label = `${y} › ${MONTHS[m]} › ${String(d).padStart(2,'0')} › ${String(h).padStart(2,'0')}:00`;
                if (label.toLowerCase().includes(q)) matches.push({y,m,d,h,label});
              });
            });
          });
        });

        if (!matches.length) { list.innerHTML = '<div class="ddt-empty">No matches</div>'; return; }

        const matchKeys = matches.map(({y,m,d,h}) => lk(y,m,d,h));
        html = `<div class="ddt-item">
          <input type="checkbox" class="ddt-cb" id="sddt-sa">
          <label for="sddt-sa" class="ddt-bold">Select All Results</label>
          <span class="ddt-x" id="sddt-ca" title="Clear filter">✕</span>
        </div><div class="ddt-sep"></div>` +
          matches.map(({y,m,d,h,label}) => {
            const k = lk(y,m,d,h);
            return `<div class="ddt-item ddt-flat">
              <input type="checkbox" class="ddt-cb" data-y="${y}" data-m="${m}" data-d="${d}" data-h="${h}" id="sddt-r${k}">
              <label for="sddt-r${k}" class="ddt-lbl">${label}</label>
            </div>`;
          }).join('');
        list.innerHTML = html;
        matches.forEach(({y,m,d,h}) => {
          const cb = list.querySelector(`#sddt-r${lk(y,m,d,h)}`);
          if (cb) { cb.checked = sigDatePending.has(lk(y,m,d,h)); cb.indeterminate = false; }
        });
        const saEl = list.querySelector('#sddt-sa');
        setCb(saEl, pState(matchKeys));
        list.querySelector('#sddt-ca')?.addEventListener('click', () => {
          sigDatePending.clear();
          list.querySelectorAll('input.ddt-cb[data-h]').forEach(cb => { cb.checked = false; cb.indeterminate = false; });
          setCb(saEl, 'none');
        });
        saEl?.addEventListener('change', () => {
          matchKeys.forEach(k => saEl.checked ? sigDatePending.add(k) : sigDatePending.delete(k));
          list.querySelectorAll('input.ddt-cb[data-h]').forEach(cb => {
            cb.checked = sigDatePending.has(lk(+cb.dataset.y,+cb.dataset.m,+cb.dataset.d,+cb.dataset.h));
          });
          setCb(saEl, pState(matchKeys));
        });
        list.querySelectorAll('input.ddt-cb[data-h]').forEach(cb => {
          cb.addEventListener('change', () => {
            const k = lk(+cb.dataset.y,+cb.dataset.m,+cb.dataset.d,+cb.dataset.h);
            cb.checked ? sigDatePending.add(k) : sigDatePending.delete(k);
            setCb(saEl, pState(matchKeys));
          });
        });
        return;
      }

      // Tree mode
      const allLeaves = leavesFor(null, null, null);
      html = `<div class="ddt-item">
        <input type="checkbox" class="ddt-cb" id="sddt-sa">
        <label for="sddt-sa" class="ddt-bold">Select All</label>
        <span class="ddt-x" id="sddt-ca" title="Clear filter">✕</span>
      </div><div class="ddt-sep"></div>`;

      years.forEach(y => {
        const yId = `sddt-y${y}`;
        const yOpen = ddtExp.has(yId);
        html += `<div class="ddt-item">
          <input type="checkbox" class="ddt-cb" data-y="${y}" id="scb-y${y}">
          <span class="ddt-arr${yOpen?' open':''}" data-cid="${yId}">›</span>
          <label for="scb-y${y}" class="ddt-lbl">${y}</label>
        </div><div id="${yId}" class="ddt-ch"${yOpen?'':' style="display:none"'}>`;

        Object.keys(tree[y]).map(Number).sort((a,b)=>a-b).forEach(m => {
          const mId = `sddt-m${y}-${m}`;
          const mOpen = ddtExp.has(mId);
          html += `<div class="ddt-item">
            <input type="checkbox" class="ddt-cb" data-y="${y}" data-m="${m}" id="scb-m${y}-${m}">
            <span class="ddt-arr${mOpen?' open':''}" data-cid="${mId}">›</span>
            <label for="scb-m${y}-${m}" class="ddt-lbl">${MONTHS[m]}</label>
          </div><div id="${mId}" class="ddt-ch"${mOpen?'':' style="display:none"'}>`;

          Object.keys(tree[y][m]).map(Number).sort((a,b)=>a-b).forEach(d => {
            const dId = `sddt-d${y}-${m}-${d}`;
            const dOpen = ddtExp.has(dId);
            html += `<div class="ddt-item">
              <input type="checkbox" class="ddt-cb" data-y="${y}" data-m="${m}" data-d="${d}" id="scb-d${y}-${m}-${d}">
              <span class="ddt-arr${dOpen?' open':''}" data-cid="${dId}">›</span>
              <label for="scb-d${y}-${m}-${d}" class="ddt-lbl">${String(d).padStart(2,'0')}</label>
            </div><div id="${dId}" class="ddt-ch"${dOpen?'':' style="display:none"'}>`;

            [...tree[y][m][d]].sort((a,b)=>a-b).forEach(h => {
              html += `<div class="ddt-item">
                <input type="checkbox" class="ddt-cb" data-y="${y}" data-m="${m}" data-d="${d}" data-h="${h}" id="scb-h${y}-${m}-${d}-${h}">
                <label for="scb-h${y}-${m}-${d}-${h}" class="ddt-lbl">${String(h).padStart(2,'0')}:00</label>
              </div>`;
            });
            html += `</div>`;
          });
          html += `</div>`;
        });
        html += `</div>`;
      });

      list.innerHTML = html;
      updateCbStates();

      list.querySelector('#sddt-ca')?.addEventListener('click', () => {
        sigDatePending.clear();
        updateCbStates();
      });
      list.querySelectorAll('.ddt-arr').forEach(arr => {
        arr.addEventListener('click', e => {
          e.preventDefault(); e.stopPropagation();
          const cid = arr.dataset.cid;
          const child = document.getElementById(cid);
          if (!child) return;
          const open = child.style.display !== 'none';
          child.style.display = open ? 'none' : '';
          arr.classList.toggle('open', !open);
          open ? ddtExp.delete(cid) : ddtExp.add(cid);
        });
      });
      list.querySelector('#sddt-sa')?.addEventListener('change', e => {
        allLeaves.forEach(k => e.target.checked ? sigDatePending.add(k) : sigDatePending.delete(k));
        updateCbStates();
      });
      list.querySelectorAll('input.ddt-cb[data-y]:not([data-m])').forEach(cb => {
        cb.addEventListener('change', () => {
          leavesFor(+cb.dataset.y, null, null)
            .forEach(k => cb.checked ? sigDatePending.add(k) : sigDatePending.delete(k));
          updateCbStates();
        });
      });
      list.querySelectorAll('input.ddt-cb[data-m]:not([data-d])').forEach(cb => {
        cb.addEventListener('change', () => {
          leavesFor(+cb.dataset.y, +cb.dataset.m, null)
            .forEach(k => cb.checked ? sigDatePending.add(k) : sigDatePending.delete(k));
          updateCbStates();
        });
      });
      list.querySelectorAll('input.ddt-cb[data-d]:not([data-h])').forEach(cb => {
        cb.addEventListener('change', () => {
          leavesFor(+cb.dataset.y, +cb.dataset.m, +cb.dataset.d)
            .forEach(k => cb.checked ? sigDatePending.add(k) : sigDatePending.delete(k));
          updateCbStates();
        });
      });
      list.querySelectorAll('input.ddt-cb[data-h]').forEach(cb => {
        cb.addEventListener('change', () => {
          const k = lk(+cb.dataset.y, +cb.dataset.m, +cb.dataset.d, +cb.dataset.h);
          cb.checked ? sigDatePending.add(k) : sigDatePending.delete(k);
          updateCbStates();
        });
      });
    }

    buildDdtTree('');
    if (search) search.oninput = () => buildDdtTree(search.value);

    // OK — commit pending and apply
    if (selAllBtn) selAllBtn.onclick = () => {
      sigDateChecked = new Set(sigDatePending);
      sigPage = 1; renderSigTable(); closeSigColDd();
    };
    // Cancel — discard pending
    if (clearBtn) clearBtn.onclick = () => closeSigColDd();
    return;
  }

  // ── Generic checkbox filter for all other columns ───────────────────────
  if (search)   { search.style.display = ''; search.placeholder = 'Search…'; }
  if (selAllBtn) { selAllBtn.style.display = ''; selAllBtn.textContent = 'Select All'; }
  if (clearBtn)  { clearBtn.textContent = 'Clear'; }
  if (search)    { search.value = ''; }

  const allVals = [...new Set(
    sigAllSignals.map(s => String(sigColVal(s, col) ?? '').toLowerCase())
      .filter(v => v !== '' && v !== '-infinity')
  )].sort();

  const currentSel = sigColFilters[col] || new Set();
  sigColFilters[col] = currentSel;

  function renderList(filter) {
    const visible = filter ? allVals.filter(v => v.includes(filter.toLowerCase())) : allVals;
    if (!list) return;
    list.innerHTML = visible.map(v => `
      <label class="col-dd-item">
        <input type="checkbox" value="${esc(v)}" ${currentSel.has(v) ? 'checked' : ''}/>
        <span>${esc(v)}</span>
      </label>
    `).join('');
    list.querySelectorAll('input[type=checkbox]').forEach(cb => {
      cb.addEventListener('change', () => {
        if (cb.checked) currentSel.add(cb.value);
        else currentSel.delete(cb.value);
        sigColFilters[col] = currentSel;
        sigPage = 1;
        renderSigTable();
      });
    });
  }
  renderList('');
  if (search) {
    search.oninput = () => renderList(search.value);
    setTimeout(() => search.focus(), 50);
  }

  if (clearBtn) clearBtn.onclick = () => {
    sigColFilters[col] = new Set();
    closeSigColDd();
    sigPage = 1;
    renderSigTable();
  };

  if (selAllBtn) selAllBtn.onclick = () => {
    allVals.forEach(v => currentSel.add(v));
    sigColFilters[col] = currentSel;
    sigPage = 1;
    renderSigTable();
    closeSigColDd();
  };
}

function closeSigColDd() {
  const dd = document.getElementById('sig-col-dd');
  if (dd) dd.style.display = 'none';
  sigColDdActiveCol = null;
  _sigDdTh = null;
}

// Open signals dropdown when trigger button is clicked
document.getElementById('sig-table')?.addEventListener('click', e => {
  const btn = e.target.closest('.sig-col-dd-btn');
  if (!btn) return;
  e.stopPropagation();
  openSigColDd(btn.dataset.col, btn);
});

// Close signals dropdown on outside click
document.addEventListener('click', e => {
  const dd = document.getElementById('sig-col-dd');
  if (!dd || dd.style.display === 'none') return;
  if (!dd.contains(e.target) && !e.target.closest('.sig-col-dd-btn')) {
    closeSigColDd();
  }
});

// Close signals dropdown on Escape (appended to existing keydown listener)
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeSigColDd();
});

// Clicking the th label also opens the signals dropdown
document.querySelector('#sig-sort-row')?.addEventListener('click', e => {
  if (e.target.closest('.sig-col-dd-btn')) return;
  const th = e.target.closest('th[data-col]');
  if (!th) return;
  const btn = th.querySelector('.sig-col-dd-btn');
  if (btn) openSigColDd(th.dataset.col, btn);
});

function renderPagination(containerId, page, pages, total, onNavigate) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = `
    <button class="page-btn" id="${containerId}-prev" ${page <= 1 ? 'disabled' : ''}>← Prev</button>
    <span class="page-info">Page ${page} / ${pages}  (${total} rows)</span>
    <button class="page-btn" id="${containerId}-next" ${page >= pages ? 'disabled' : ''}>Next →</button>
  `;
  document.getElementById(`${containerId}-prev`)?.addEventListener('click',
    () => onNavigate(Math.max(1, page - 1)));
  document.getElementById(`${containerId}-next`)?.addEventListener('click',
    () => onNavigate(Math.min(pages, page + 1)));
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/* COSTS                                                                        */
/* ═══════════════════════════════════════════════════════════════════════════ */
async function renderCosts() {
  const data = await apiFetch('/api/costs');
  if (!data) return;

  const summ = data.summary || {};
  setText('cost-gross', fmtUSD(summ.total_gross_pnl));
  setText('cost-total', fmtUSD(summ.total_cost));
  setText('cost-net',   fmtUSD(summ.total_net_pnl));
  setText('cost-open',  fmtUSD(summ.open_entry_cost));

  const allRows    = (data.rows || []).sort((a, b) =>
    (a.exit_time||'') < (b.exit_time||'') ? -1 : 1);
  // Charts show only closed/partial rows (open entries have no realized P&L)
  const rows = allRows.filter(r => r.type !== 'open entry');

  makeChart('cost-line-chart', {
    type: 'line',
    data: {
      labels: rows.map((_, i) => i + 1),
      datasets: [
        { label:'Gross', data: rows.map(r => r.gross_pnl),
          borderColor:'#B45C00', backgroundColor:'transparent', borderWidth:1.5, pointRadius:2, tension:0.2 },
        { label:'Net',   data: rows.map(r => r.net_pnl),
          borderColor: cNavy(), backgroundColor:'transparent', borderWidth:1.5, pointRadius:2, tension:0.2 },
      ],
    },
    options: {
      animation: false, responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position:'top', labels: { boxWidth:10, font:{size:11} } },
        tooltip: { ...cTooltip({ displayColors: true }),
          callbacks: { label: ctx => ` ${ctx.dataset.label}: ${fmtUSD(ctx.parsed.y)}` } },
      },
      scales: {
        x: { grid:{display:false}, ticks:{display:false} },
        y: { grid:{color: cGrid()}, ticks:{callback: v => '$'+v.toFixed(0)} },
      },
    },
  });

  let cumCost = 0;
  makeChart('cost-area-chart', {
    type: 'line',
    data: {
      labels: rows.map((_, i) => i + 1),
      datasets: [{
        label:'Cumulative Cost',
        data: rows.map(r => { cumCost += r.estimated_cost; return cumCost; }),
        borderColor:'#9B1C1C', backgroundColor:'rgba(155,28,28,0.06)',
        borderWidth:1.5, pointRadius:0, fill:true, tension:0.3,
      }],
    },
    options: {
      animation: false, responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend:{display:false},
        tooltip: { ...cTooltip(),
          callbacks: { label: ctx => ` Cumulative cost: ${fmtUSD(ctx.parsed.y)}` } },
      },
      scales: {
        x: { grid:{display:false}, ticks:{display:false} },
        y: { grid:{color: cGrid()}, ticks:{callback: v => '$'+v.toFixed(2)} },
      },
    },
  });

  const tbody = document.querySelector('#cost-table tbody');
  if (tbody) {
    if (!allRows.length) {
      tbody.innerHTML = `<tr><td colspan="6">${emptyState('No trades yet')}</td></tr>`;
    } else {
      tbody.innerHTML = [...allRows].reverse().map(r => {
        const isOpen = r.type === 'open entry';
        const rowStyle = isOpen ? ' style="opacity:0.7"' : '';
        const timeLabel = isOpen
          ? `<span style="font-size:9px;color:var(--amber);font-family:var(--sans);font-weight:700;letter-spacing:0.06em;text-transform:uppercase;margin-left:4px">OPEN</span>`
          : '';
        return `<tr${rowStyle}>
          <td style="white-space:nowrap;color:var(--text-sub)">${fmtTime(r.exit_time)}${timeLabel}</td>
          <td style="font-weight:600">${esc(r.symbol)}</td>
          <td style="font-size:11px;color:var(--text-sub)">${esc(r.type)}</td>
          <td class="${pnlClass(r.gross_pnl)}">${isOpen ? '—' : fmtUSD(r.gross_pnl)}</td>
          <td style="color:var(--red)">${fmtUSD(r.estimated_cost)}</td>
          <td class="${pnlClass(r.net_pnl)}">${fmtUSD(r.net_pnl)}</td>
        </tr>`;
      }).join('');
    }
  }
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/* STRATEGY REGISTRY                                                            */
/* ═══════════════════════════════════════════════════════════════════════════ */
async function renderStrategies() {
  const data = await apiFetch('/api/strategies');
  if (!Array.isArray(data)) return;

  const active   = data.filter(s =>  s.is_active);
  const inactive = data.filter(s => !s.is_active);

  setText('strat-total',    String(data.length));
  setText('strat-active',   String(active.length));
  setText('strat-inactive', String(inactive.length));

  // Strategy cards
  const grid = document.getElementById('strat-cards-grid');
  if (grid) {
    if (!data.length) {
      grid.innerHTML = emptyState('No strategies registered');
    } else {
      grid.innerHTML = data.map(s => {
        const paramsStr = s.params ? JSON.stringify(s.params, null, 2) : '';
        return `<div class="strat-card ${s.is_active ? 'strat-active-card' : 'strat-inactive-card'}">
          <div class="strat-card-header">
            <div>
              <div class="strat-name">${esc(s.name)}</div>
              <div class="strat-version">v${esc(s.version) || '—'} &nbsp;·&nbsp; ${fmtDate(s.created_at)}</div>
            </div>
            <span class="tier-badge ${s.is_active ? 'tier-SMALL' : 'tier-NO_TRADE'}">${s.is_active ? 'ACTIVE' : 'INACTIVE'}</span>
          </div>
          ${s.description ? `<div class="strat-desc">${esc(s.description)}</div>` : ''}
          ${paramsStr ? `<div class="strat-params-label">Parameters</div><pre class="strat-params">${esc(paramsStr)}</pre>` : ''}
        </div>`;
      }).join('');
    }
  }

  // Table
  const tbody = document.querySelector('#strat-table tbody');
  if (tbody) {
    if (!data.length) {
      tbody.innerHTML = `<tr><td colspan="5">${emptyState('No strategies registered')}</td></tr>`;
    } else {
      tbody.innerHTML = data.map(s => `<tr>
        <td style="font-weight:600">${esc(s.name)}</td>
        <td style="font-family:var(--mono);color:var(--text-sub)">v${esc(s.version) || '—'}</td>
        <td><span class="tier-badge ${s.is_active ? 'tier-SMALL' : 'tier-NO_TRADE'}">${s.is_active ? 'ACTIVE' : 'INACTIVE'}</span></td>
        <td style="color:var(--text-sub);white-space:nowrap">${fmtDate(s.created_at)}</td>
        <td style="color:var(--text-sub);font-size:11.5px;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(s.description) || '—'}</td>
      </tr>`).join('');
    }
  }
}

/* ═══════════════════════════════════════════════════════════════════════════ */
/* OPTIMIZER                                                                    */
/* ═══════════════════════════════════════════════════════════════════════════ */
async function runOptimizer() {
  const btn = document.getElementById('run-opt-btn');
  const status = document.getElementById('run-opt-status');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = 'Running…';
  status.style.color = 'var(--text-sub)';
  status.textContent = 'Running optimization pipeline — this may take a minute…';
  try {
    const res = await fetch('/api/run-optimizer', { method: 'POST' });
    const data = await res.json();
    if (data.status === 'ok') {
      status.style.color = 'var(--green)';
      status.textContent = '✓ ' + data.message + ' Refreshing…';
      setTimeout(() => renderOptimizer(), 1500);
    } else {
      status.style.color = 'var(--red)';
      status.textContent = '✗ ' + (data.message || 'Unknown error');
    }
  } catch (e) {
    status.style.color = 'var(--red)';
    status.textContent = '✗ Request failed: ' + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Run Optimizer Now';
  }
}

async function renderOptimizer() {
  const stratFilter = document.getElementById('opt-strategy-filter')?.value || '';
  const params = new URLSearchParams({ page: optPage, page_size: OPT_PAGE_SIZE });
  if (stratFilter) params.set('strategy', stratFilter);

  const data = await apiFetch('/api/optimization?' + params);
  if (!data) return;

  // Populate strategy filter once
  const stratSel = document.getElementById('opt-strategy-filter');
  if (stratSel && stratSel.options.length <= 1 && data.strategies?.length) {
    stratSel.innerHTML = '<option value="">All</option>' +
      data.strategies.map(s => `<option value="${esc(s)}"${s===stratFilter?' selected':''}>${esc(s)}</option>`).join('');
  }

  const rows     = data.rows || [];
  const accepted = rows.filter(r => r.accepted === true);
  const rejected = rows.filter(r => r.accepted === false);
  const oosSharpes = rows.map(r => r.oos_sharpe).filter(v => v != null);
  const avgOOS   = oosSharpes.length
    ? oosSharpes.reduce((a, b) => a + b, 0) / oosSharpes.length
    : null;

  setText('opt-total',      String(data.total ?? rows.length));
  setText('opt-accepted',   String(accepted.length));
  setText('opt-rejected',   String(rejected.length));
  setText('opt-avg-sharpe', avgOOS != null ? avgOOS.toFixed(3) : '—');

  // Scatter: IS vs OOS Sharpe
  const sharpeRows = rows.filter(r => r.in_sample_sharpe != null && r.oos_sharpe != null);
  if (sharpeRows.length) {
    makeChart('opt-sharpe-chart', {
      type: 'scatter',
      data: {
        datasets: [
          { label:'Accepted',
            data: sharpeRows.filter(r =>  r.accepted).map(r => ({x:r.in_sample_sharpe, y:r.oos_sharpe})),
            backgroundColor:'rgba(20,82,41,0.7)', pointRadius:5 },
          { label:'Rejected',
            data: sharpeRows.filter(r => r.accepted === false).map(r => ({x:r.in_sample_sharpe, y:r.oos_sharpe})),
            backgroundColor:'rgba(155,28,28,0.55)', pointRadius:5 },
          { label:'Pending',
            data: sharpeRows.filter(r => r.accepted == null).map(r => ({x:r.in_sample_sharpe, y:r.oos_sharpe})),
            backgroundColor:'rgba(176,168,152,0.55)', pointRadius:4 },
        ],
      },
      options: {
        animation: false, responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { position:'top', labels:{boxWidth:10, font:{size:11}} },
          tooltip: {
            ...cTooltip({ displayColors: true }),
            callbacks: {
              label: ctx => ` IS: ${ctx.parsed.x.toFixed(3)}  →  OOS: ${ctx.parsed.y.toFixed(3)}`,
            },
          },
        },
        scales: {
          x: { title:{display:true, text:'In-Sample Sharpe', font:{size:10}},
               grid:{color: cGrid()} },
          y: { title:{display:true, text:'OOS Sharpe', font:{size:10}},
               grid:{color: cGrid()} },
        },
      },
    });
  } else {
    const wrap = document.getElementById('opt-sharpe-chart')?.parentElement;
    if (wrap) wrap.innerHTML = emptyState('No optimization cycles yet');
  }

  // Bar: acceptance by strategy
  const byStrat = {};
  rows.forEach(r => {
    if (!r.strategy_name) return;
    byStrat[r.strategy_name] ??= { acc:0, rej:0 };
    if (r.accepted === true)  byStrat[r.strategy_name].acc++;
    if (r.accepted === false) byStrat[r.strategy_name].rej++;
  });
  const stratNames = Object.keys(byStrat);
  if (stratNames.length) {
    makeChart('opt-accept-chart', {
      type: 'bar',
      data: {
        labels: stratNames,
        datasets: [
          { label:'Accepted', data: stratNames.map(s => byStrat[s].acc),
            backgroundColor:'rgba(20,82,41,0.65)', borderRadius:2, borderWidth:0 },
          { label:'Rejected', data: stratNames.map(s => byStrat[s].rej),
            backgroundColor:'rgba(155,28,28,0.5)',  borderRadius:2, borderWidth:0 },
        ],
      },
      options: {
        animation: false, responsive: true, maintainAspectRatio: false,
        plugins: {
          legend:{position:'top', labels:{boxWidth:10, font:{size:11}}},
          tooltip: { ...cTooltip({ displayColors: true }),
            callbacks: { label: ctx => ` ${ctx.dataset.label}: ${ctx.parsed.y}` } },
        },
        scales: {
          x: { grid:{display:false} },
          y: { grid:{color: cGrid()}, ticks:{precision:0} },
        },
      },
    });
  } else {
    const wrap = document.getElementById('opt-accept-chart')?.parentElement;
    if (wrap) wrap.innerHTML = emptyState('No data');
  }

  // Table
  const tbody = document.querySelector('#opt-table tbody');
  if (tbody) {
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="9">${emptyState('No optimization cycles yet')}</td></tr>`;
    } else {
      tbody.innerHTML = rows.map(r => {
        const resultBadge = r.accepted === true
          ? '<span class="tier-badge tier-SMALL">ACCEPTED</span>'
          : r.accepted === false
          ? '<span class="tier-badge tier-FULL">REJECTED</span>'
          : '<span class="tier-badge tier-WATCH">PENDING</span>';
        const diff = (r.in_sample_sharpe != null && r.oos_sharpe != null)
          ? r.oos_sharpe - r.in_sample_sharpe : null;
        return `<tr>
          <td style="white-space:nowrap;color:var(--text-sub)">${fmtDate(r.started_at)}</td>
          <td style="font-weight:600">${esc(r.strategy_name) || '—'}</td>
          <td>${sharpeBadge(r.in_sample_sharpe)}</td>
          <td>
            ${sharpeBadge(r.oos_sharpe)}
            ${diff != null ? `<span style="font-size:10px;color:${diff>=0?'var(--green)':'var(--red)'};margin-left:4px">(${diff>=0?'+':''}${diff.toFixed(3)})</span>` : ''}
          </td>
          <td style="font-family:var(--mono)">${r.in_sample_trades ?? '—'}</td>
          <td style="font-family:var(--mono)">${r.oos_trades ?? '—'}</td>
          <td style="font-family:var(--mono);color:var(--text-sub)">${r.p_value != null ? r.p_value.toFixed(4) : '—'}</td>
          <td>${resultBadge}</td>
          <td style="font-size:11px;color:var(--text-sub);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.notes) || '—'}</td>
        </tr>`;
      }).join('');
    }
  }

  renderPagination('opt-pagination', data.page, data.pages, data.total, p => {
    optPage = p; renderOptimizer();
  });
}

document.getElementById('opt-strategy-filter')?.addEventListener('change', () => {
  optPage = 1; renderOptimizer();
});

/* ═══════════════════════════════════════════════════════════════════════════ */
/* STATUS                                                                       */
/* ═══════════════════════════════════════════════════════════════════════════ */
async function renderStatus() {
  const data = await apiFetch('/api/status');
  if (!data) return;

  const botLine = document.getElementById('st-bot-line');
  if (botLine) {
    const chip = data.bot_running
      ? `<span class="bot-chip running">● Running</span>`
      : `<span class="bot-chip stopped">● Stopped</span>`;
    const snap = data.last_snapshot ? `Last snapshot: ${fmtTime(data.last_snapshot)}` : 'No snapshot yet';
    botLine.innerHTML = `${chip} &nbsp; ${snap}`;
  }

  const brokerEl = document.getElementById('st-broker-cards');
  if (brokerEl && data.brokers) {
    brokerEl.innerHTML = Object.entries(data.brokers).map(([name, b]) => {
      const detail = name === 'ibkr' ? 'IBKR Gateway' : esc(b.environment);
      return `<div class="broker-card status-${esc(b.status)}" style="flex:1;min-width:160px">
        <span class="status-dot"></span>
        <div>
          <div class="bk-name">${esc(name.toUpperCase())}</div>
          <div class="bk-detail">${detail} · ${esc(b.status)}</div>
        </div>
      </div>`;
    }).join('');
  }

  const cbEl = document.getElementById('st-cb-card');
  if (cbEl && data.circuit_breaker) {
    const cb  = data.circuit_breaker;
    const prog = Math.min(100, Math.abs(cb.daily_pnl) / cb.daily_loss_limit * 100);
    cbEl.innerHTML = `<div class="cb-card ${cb.tripped ? 'tripped' : 'clear'}">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
        <span style="font-weight:600;font-size:13px">${cb.tripped ? '⚡ TRIPPED' : '✓ CLEAR'}</span>
        <span class="tier-badge ${cb.tripped ? 'tier-FULL' : 'tier-SMALL'}">${cb.tripped ? 'ACTIVE' : 'OK'}</span>
      </div>
      ${cb.reason ? `<div style="font-size:12px;color:var(--red);margin-bottom:8px">${esc(cb.reason)}</div>` : ''}
      <div style="font-size:11.5px;color:var(--text-sub);margin-bottom:8px">
        Daily P&L: <span class="${pnlClass(cb.daily_pnl)}" style="font-family:var(--mono)">${fmtUSD(cb.daily_pnl)}</span>
        &nbsp;/&nbsp; Limit: <span style="font-family:var(--mono);color:var(--red)">-${fmtUSD(cb.daily_loss_limit)}</span>
      </div>
      <div class="prog-wrap"><div class="prog-fill ${cb.tripped ? 'red' : 'green'}" style="width:${prog}%"></div></div>
    </div>`;
  }

  const univEl = document.getElementById('st-universe');
  if (univEl) {
    univEl.innerHTML = data.active_universe?.length
      ? data.active_universe.map(u =>
          `<div class="u-chip"><span class="u-sym">${esc(u.symbol)}</span><span class="u-meta">${esc(u.broker)} · ${esc(u.asset_type)}</span></div>`
        ).join('')
      : '<span style="color:var(--text-muted);font-size:13px">No active universe set</span>';
  }

  const schedTbody = document.querySelector('#st-sched-table tbody');
  if (schedTbody && data.scheduler) {
    schedTbody.innerHTML = Object.entries(data.scheduler).map(([k, v]) => `<tr>
      <td style="color:var(--text-sub);font-size:11.5px">${k.replace(/_/g,' ')}</td>
      <td style="font-family:var(--mono);font-size:12px">${v}</td>
    </tr>`).join('');
  }

  const stratEl = document.getElementById('st-strategies');
  if (stratEl) {
    stratEl.innerHTML = data.strategies?.length
      ? data.strategies.map(s => `
          <div style="display:flex;align-items:center;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)">
            <div>
              <div style="font-weight:600;font-size:13px">${esc(s.name)} <span style="font-family:var(--mono);font-size:11px;color:var(--text-sub)">v${esc(s.version)}</span></div>
              <div style="font-size:11px;color:var(--text-muted)">${fmtTime(s.created_at)}</div>
            </div>
            <span class="tier-badge ${s.is_active ? 'tier-SMALL' : 'tier-NO_TRADE'}">${s.is_active ? 'ACTIVE' : 'INACTIVE'}</span>
          </div>`).join('')
      : '<div style="color:var(--text-muted);font-size:12px;padding:8px 0">No strategies registered</div>';
  }

  const evTbody = document.querySelector('#st-events-table tbody');
  if (evTbody) {
    evTbody.innerHTML = data.recent_events?.length
      ? data.recent_events.map(ev => `<tr>
          <td style="white-space:nowrap;color:var(--text-sub)">${fmtTime(ev.timestamp)}</td>
          <td>${evBadge(ev.event_type)}</td>
          <td style="font-family:var(--mono);font-weight:600">${esc(ev.symbol) || '—'}</td>
          <td style="font-size:12px;color:var(--text-sub);max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(ev.description) || '—'}</td>
        </tr>`).join('')
      : `<tr><td colspan="4">${emptyState('No events logged')}</td></tr>`;
  }
}

function evBadge(type) {
  const map = {
    system:'ev-system', partial_close:'ev-partial', earnings:'ev-earnings',
    fomc:'ev-fomc', trade_open:'ev-trade', trade_close:'ev-trade',
  };
  return `<span class="ev-badge ${map[type]||'ev-default'}">${type}</span>`;
}

/* ── Init ───────────────────────────────────────────────────────────────────── */
initSettings();
navigate('overview');
