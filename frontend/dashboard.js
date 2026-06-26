const API_BASE = 'http://localhost:8000';

// API key loaded dynamically from backend at startup — never hardcoded in source
let _API_KEY = '';
(async () => {
    try {
        const r = await fetch(`${API_BASE}/dashboard/config`);
        if (r.ok) { const d = await r.json(); _API_KEY = d.api_key || ''; }
    } catch (e) { /* silent — no key = local dev mode */ }
})();
function _authHeaders() {
    const h = { 'Content-Type': 'application/json' };
    if (_API_KEY) h['X-API-Key'] = _API_KEY;
    return h;
}

// ── Frontend cache ────────────────────────────────────────────────────────────
let _lastFetch = 0;
let _cachedData = null;
let _futuresUnrealPnl = 0;  // Updated by renderFuturesPositions each cycle
let _cachedFuturesPositions = [];  // Updated by renderFuturesPositions each cycle
let _cachedFuturesClosed = [];     // Updated by renderFuturesClosed each cycle
const CACHE_TTL_MS = 15000;   // 15 s frontend cache
const POLL_INTERVAL_MS = 30000; // 30 s poll (was 5 s → 6× less traffic)
let _fetchInProgress = false;

async function fetchDashboardData() {
    const now = Date.now();
    if (_fetchInProgress) return _cachedData;
    if (_cachedData && (now - _lastFetch) < CACHE_TTL_MS) return _cachedData;
    _fetchInProgress = true;
    try {
        const res = await fetch(`${API_BASE}/dashboard/summary`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        _cachedData = await res.json();
        _lastFetch = Date.now();
        return _cachedData;
    } catch (err) {
        console.error('Dashboard fetch error:', err);
        return _cachedData;
    } finally {
        _fetchInProgress = false;
    }
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmt(n, dec = 2) { return parseFloat(n || 0).toFixed(dec); }
function pnlClass(v) { return parseFloat(v || 0) >= 0 ? 'positive' : 'negative'; }

function fmtPrediction(pred, showDetails = false) {
    if (!pred) return '<span class="no-pred">Sin predicción</span>';
    const ep = parseFloat(pred.expected_pnl || 0);
    const sp = parseFloat(pred.success_probability || 0);
    const isBootstrap = pred.bootstrap_mode === true;
    const badge = isBootstrap ? ' <span class="badge-bootstrap" title="Modelo en aprendizaje">📚</span>' : '';
    
    if (!showDetails) {
        return `<span class="pred-badge ${ep >= 0 ? 'positive' : 'negative'}">${badge}
            ${ep >= 0 ? '+' : ''}${ep.toFixed(2)} USDT <small>(${(sp * 100).toFixed(0)}%)</small>
        </span>`;
    }
    
    // Detailed view
    const priceRange = pred.price_range || {};
    const minPrice = priceRange.min ? `$${parseFloat(priceRange.min).toFixed(4)}` : '—';
    const maxPrice = priceRange.max ? `$${parseFloat(priceRange.max).toFixed(4)}` : '—';
    
    return `<div class="pred-detail">
        <div class="pred-main ${ep >= 0 ? 'positive' : 'negative'}">${badge} Esperado: ${ep >= 0 ? '+' : ''}${ep.toFixed(2)} USDT</div>
        <div class="pred-sub">Probabilidad: ${(sp * 100).toFixed(1)}% | Rango: ${minPrice} — ${maxPrice}</div>
    </div>`;
}

// ── Accuracy badge helper ────────────────────────────────────────────────────
function fmtAccuracy(actual, expected) {
    if (!expected || expected === 0) return '<span class="accuracy-none">—</span>';
    const ratio = (actual / expected) * 100;
    const absRatio = Math.abs(ratio);
    let badgeClass = 'accuracy-missed';
    let icon = '❌';
    
    if (absRatio >= 80) { badgeClass = 'accuracy-precise'; icon = '🎯'; }
    else if (absRatio >= 50) { badgeClass = 'accuracy-good'; icon = '⚡'; }
    
    return `<span class="accuracy-badge ${badgeClass}">${icon} ${ratio.toFixed(0)}%</span>`;
}

function fmtAccuracyFromField(accuracyPct) {
    if (accuracyPct == null) return '<span class="accuracy-none">—</span>';
    const absVal = Math.abs(accuracyPct);
    let badgeClass = 'accuracy-missed';
    let icon = '❌';
    
    if (absVal >= 80) { badgeClass = 'accuracy-precise'; icon = '🎯'; }
    else if (absVal >= 50) { badgeClass = 'accuracy-good'; icon = '⚡'; }
    
    return `<span class="accuracy-badge ${badgeClass}">${icon} ${accuracyPct.toFixed(0)}%</span>`;
}

// ── Progress bar toward targets ──────────────────────────────────────────────
function fmtProgressBar(current, entry, target, isTp = true) {
    if (!target || !entry) return '<span class="progress-none">—</span>';
    
    const totalDistance = Math.abs(target - entry);
    const currentDistance = Math.abs(current - entry);
    const percent = Math.min(100, Math.max(0, (currentDistance / totalDistance) * 100));
    
    const direction = isTp ? (target > entry ? 'up' : 'down') : (target < entry ? 'down' : 'up');
    const progressClass = isTp ? 'progress-tp' : 'progress-sl';
    const remainingPct = Math.abs((target - current) / entry * 100);
    
    return `<div class="progress-container" title="${remainingPct.toFixed(1)}% ${isTp ? 'para TP' : 'hasta SL'}">
        <div class="progress-bar ${progressClass}" style="width: ${percent}%"></div>
        <span class="progress-text">${remainingPct.toFixed(1)}%</span>
    </div>`;
}

// ── Expected vs Actual comparison ───────────────────────────────────────────
function fmtExpectedVsActual(actual, expected) {
    if (expected == null || expected === undefined) return '<span class="no-pred">—</span>';
    
    const act = parseFloat(actual || 0);
    const exp = parseFloat(expected || 0);
    const diff = act - exp;
    const diffPct = exp !== 0 ? (diff / Math.abs(exp)) * 100 : 0;
    
    const diffClass = diff >= 0 ? 'positive' : 'negative';
    const arrow = diff >= 0 ? '↑' : '↓';
    
    return `<div class="expected-actual">
        <div class="actual-line ${act >= 0 ? 'positive' : 'negative'}">Real: ${act >= 0 ? '+' : ''}${act.toFixed(2)}</div>
        <div class="expected-line">Esper: ${exp >= 0 ? '+' : ''}${exp.toFixed(2)}</div>
        <div class="diff-line ${diffClass}">${arrow} ${Math.abs(diff).toFixed(2)} (${diffPct >= 0 ? '+' : ''}${diffPct.toFixed(0)}%)</div>
    </div>`;
}

// ── Close reason → human-readable label ──────────────────────────────────────
const _CLOSE_REASON_MAP = {
    'stop_loss':                   ['🔴 Stop Loss',             'reason-sl'],
    'take_profit':                 ['🟢 Take Profit',           'reason-tp'],
    'trailing_stop':               ['🟢 Trailing Stop',         'reason-tp'],
    'smart_tp_close':              ['🟢 SmartTP Close',         'reason-tp'],
    'smart_tp_reversal':           ['🟡 SmartTP Reversal',      'reason-neutral'],
    'prediction_exit_deteriorated':['🤖 PredExit',              'reason-sl'],
    'binance_safety_close':        ['⚡ Binance Safety',        'reason-sl'],
    'scale_out':                   ['✂️ Scale-Out parcial',     'reason-tp'],
    'momentum_reversal':           ['⚡ Momentum Rev.',          'reason-tp'],
    'trailing_sl_binance':         ['🟢 Trailing (Binance)',     'reason-tp'],
    'regime_change_to_short':      ['⚡ Régimen → SHORT',        'reason-neutral'],
    'portfolio_rebalancing':       ['🔄 Rebalanceo técnico',    'reason-rebal'],
    'prediction_reallocation':     ['🔄 Reemplazo por EV',      'reason-rebal'],
    'reallocation_slot_pressure':  ['🔄 Presión de slots',      'reason-rebal'],
    'reallocation_rsi_overbought': ['🔄 Reemplazo: RSI overbought + mejor EV', 'reason-rebal'],
    'reallocation_ema9_bearish':   ['🔄 Reemplazo: tendencia bajista + mejor EV', 'reason-rebal'],
    'orphan_asset_cleanup':        ['🧹 Activo sin registro',   'reason-neutral'],
    'prediction_realloc':          ['🔄 Reemplazo por EV',      'reason-rebal'],
    'regime_change':               ['⚡ Cambio de régimen',     'reason-neutral'],
    'manual':                      ['✋ Manual',                 'reason-neutral'],
    'sso_stagnant_profit':         ['⏱️ SSO Estancada',          'reason-tp'],
    'sso_momentum_exhausted':      ['📉 SSO Momentum Agotado',   'reason-tp'],
    'sso_multilevel_l1':           ['📊 SSO Nivel 1',            'reason-tp'],
    'sso_multilevel_l2':           ['📊 SSO Nivel 2',            'reason-tp'],
    'sso_multilevel_l3':           ['📊 SSO Nivel 3',            'reason-tp'],
    'sso_target_contribution':     ['🎯 SSO Target',             'reason-tp'],
    'sso_reentry_upgrade':         ['🔄 SSO Upgrade',            'reason-tp'],
    'sso_bounce_rescue':           ['🔄 Bounce Rescue',          'reason-rescue'],
};

// VIS-3: SL/TP lifecycle helpers
function fmtSlTpCell(p) {
    const sl = parseFloat(p.stop_loss || 0);
    const tp = parseFloat(p.take_profit || 0);
    const slDist = p.sl_dist_pct;
    const tpDist = p.tp_dist_pct;
    if (!sl && !tp) return '—';
    let html = '';
    if (sl) html += `<span class="sl-val">SL $${fmt(sl)} <small>(${slDist != null ? (slDist >= 0 ? '+' : '') + slDist.toFixed(1) + '%' : '—'})</small></span>`;
    if (tp) html += `<br><span class="tp-val">TP $${fmt(tp)} <small>(${tpDist != null ? (tpDist >= 0 ? '+' : '') + tpDist.toFixed(1) + '%' : '—'})</small></span>`;
    return html;
}

function fmtSlTpBadges(p) {
    let badges = '';
    if (p.breakeven_done)  badges += '<span class="sl-badge be" title="Breakeven activado">🛡BE</span>';
    if (p.trailing_active) badges += '<span class="sl-badge trail" title="Trailing activo">📈T</span>';
    if (p.tp_extensions > 0) badges += `<span class="sl-badge ext" title="SmartTP extendido ${p.tp_extensions}×">🔄${p.tp_extensions}</span>`;
    if (p.scale_out_done) {
        const soTip = p.scale_out_pnl ? `Scale-out: +$${fmt(p.scale_out_pnl)} locked` : 'Scale-out ejecutado';
        badges += `<span class="sl-badge scale" title="${soTip}">✂️$${fmt(p.scale_out_pnl || 0)}</span>`;
    }
    if (p.peak_pnl_pct > 0) badges += `<span class="sl-badge peak" title="Peak PnL alcanzado: ${fmt(p.peak_pnl_pct, 1)}%">📊${fmt(p.peak_pnl_pct, 1)}%</span>`;
    if (p.sso_levels_done && p.sso_levels_done.length > 0) {
        const lvls = p.sso_levels_done.join(',');
        badges += `<span class="sl-badge sso" title="SSO Levels: ${lvls} | ${p.sso_last_trigger || ''}">🎯L${lvls}</span>`;
    }
    if (p.bounce_entry) {
        const btcChg = p.btc_chg_since_entry_pct != null ? ` | BTC: ${p.btc_chg_since_entry_pct >= 0 ? '+' : ''}${p.btc_chg_since_entry_pct.toFixed(1)}% desde entrada` : '';
        const _gBlocked = (p.bounce_gate_blocked_count || 0) > 0;
        const _gSig = p.bounce_gate_last_signal ? ` | Señal: ${p.bounce_gate_last_signal} ${p.bounce_gate_last_signal_strength != null ? '(' + (p.bounce_gate_last_signal_strength * 100).toFixed(0) + '%)' : ''}` : '';
        const _gEv = p.bounce_gate_last_ev != null ? ` | EV: ${p.bounce_gate_last_ev >= 0 ? '+' : ''}$${p.bounce_gate_last_ev.toFixed(2)}` : '';
        const _gVel = p.bounce_gate_last_velocity != null ? ` | vel: ${(p.bounce_gate_last_velocity * 100).toFixed(3)}%/h` : '';
        const _gCount = _gBlocked ? ` | Bloqueado ${p.bounce_gate_blocked_count}x` : '';
        const _gTip = `Rescue Mode${_gBlocked ? ' ⏸️GATE ACTIVO' : ''} — cierra al acercarse a breakeven${btcChg}${_gSig}${_gEv}${_gVel}${_gCount}`;
        badges += `<span class="sl-badge bounce${_gBlocked ? ' bounce-gated' : ''}" title="${_gTip}">🔄RESCUE${_gBlocked ? '⏸️' : ''}</span>`;
    }
    return badges || '<span class="sl-badge none">—</span>';
}
function fmtCloseReason(r) {
    if (!r) return '—';
    const entry = _CLOSE_REASON_MAP[r];
    if (entry) return `<span class="close-reason ${entry[1]}">${entry[0]}</span>`;
    return `<span class="close-reason reason-neutral">${r}</span>`;
}

// ── Category helpers (must be before any render function) ───────────────────
const CAT_ICON  = { 'LARGE_CAP': '🏦', 'MIDCAP_ALT': '📊', 'NARRATIVE': '🤖', 'MEME': '🎭' };
const CAT_LABEL = { 'LARGE_CAP': 'LARGE CAP', 'MIDCAP_ALT': 'MID ALT', 'NARRATIVE': 'NARRATIVE', 'MEME': 'MEME' };
function catIcon(cat) {
    const icon  = CAT_ICON[cat]  || '📊';
    const label = CAT_LABEL[cat] || cat;
    return `<span class="cat-icon" title="${label}">${icon}</span>`;
}

// ── Trading Mode config (global — used by renderExecutiveSummary and renderTradingMode*) ──
const MODE_CFG = {
    'SPOT_BULL':    { icon: '📈', label: 'SPOT BULL',   color: '#4ecca3' },
    'RANGE_SCALP':  { icon: '↔️',  label: 'LATERAL',    color: '#f39c12' },
    'PRESERVATION': { icon: '🛡',  label: 'PRESERVAR',  color: '#e74c3c' },
    'FUTURES_SHORT':{ icon: '📉',  label: 'SHORT 2×',   color: '#9b59b6' },
};

// ── Tab switching ─────────────────────────────────────────────────────────────
function switchTab(tabId) {
    document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tabId);
    });
    const target = document.getElementById(tabId);
    if (target) target.classList.add('active');
}

// ── Collapsible sections ─────────────────────────────────────────────────────
function toggleSection(bodyId, chevronId) {
    const body = document.getElementById(bodyId);
    const chev = document.getElementById(chevronId);
    if (!body) return;
    const isCollapsed = body.classList.toggle('collapsed');
    if (chev) chev.textContent = isCollapsed ? '▶' : '▼';
}

// ── Executive Summary ─────────────────────────────────────────────────────────
function renderExecutiveSummary(data) {
    const el = document.getElementById('executiveSummaryData');
    if (!el || !data) return;

    const balance  = data.balance       || {};
    const regime   = data.market_regime || {};
    const ctx      = data.market_context|| {};
    const openPos  = data.open_positions|| {};
    const perf     = data.performance   || {};
    const signals  = data.signals       || {};
    const cycle    = data.next_cycle    || {};
    const pnlH     = data.pnl_history   || {};

    const totalUsdt   = parseFloat(balance.total_usdt || 0);
    const liquidUsdt  = parseFloat(balance.usdt_available || balance.total_usdt || 0);
    const positions   = openPos.positions || [];
    const invested    = positions.reduce((s, p) => s + parseFloat(p.trade_type === 'futures' ? (p.margin_usdt || p.invested_usdt || 0) : (p.invested_usdt || 0)), 0);
    const exposurePct = totalUsdt > 0 ? ((invested / totalUsdt) * 100).toFixed(1) : '0.0';
    const unrealPnl   = positions.reduce((s, p) => s + parseFloat(p.unrealized_pnl || 0), 0);
    const lockedPnl   = positions.reduce((s, p) => s + (p.scale_out_done ? parseFloat(p.scale_out_pnl || 0) : 0), 0);
    const pnlToday    = parseFloat((pnlH.day && pnlH.day.total_pnl) || 0);
    const mode        = regime.trading_mode || 'SPOT_BULL';
    const modeInfo    = MODE_CFG[mode] || MODE_CFG['SPOT_BULL'];
    const fng         = ctx.fear_greed_value ?? 50;
    const fngLabel    = ctx.fear_greed_label || 'Neutral';
    const btc7d       = regime.btc_7d_change_pct != null ? regime.btc_7d_change_pct : null;
    const btc4h       = regime.btc_trend || '?';
    const m           = perf.metrics || {};
    const wr          = parseFloat(m.win_rate || 0);
    const pf          = parseFloat(m.profit_factor || 0);
    const cycleMins   = cycle.interval_minutes;
    const cycleSecsLeft = cycle.seconds_until_next;

    // Recent actions from signals
    const signalList = (Array.isArray(signals) ? signals : (signals.signals || []));
    const executed   = signalList.filter(s => s.decision === 'executed').slice(0, 4);
    const lastActHtml = executed.length
        ? executed.map(s => {
            const isBuy = (s.action || '').toUpperCase() === 'BUY';
            const badge = isBuy ? '<span class="exec-buy">BUY</span>' : '<span class="exec-sell">SELL</span>';
            const ago   = s.received_at ? relativeTime(s.received_at) : '';
            return `${badge} <strong>${s.symbol}</strong>${ago ? ' <span class="exec-ago">' + ago + '</span>' : ''}`;
        }).join(' &nbsp;·&nbsp; ')
        : '<span style="color:#555">Sin acciones recientes</span>';

    // Alerts
    const alerts = [];
    const maxExpPct = 75;
    const expNum = parseFloat(exposurePct);
    if (expNum >= maxExpPct * 0.90) alerts.push(`⚠️ Exposición al ${expNum.toFixed(0)}% del cap máximo (${maxExpPct}%)`);
    const closeSL = positions.filter(p => {
        const cur = parseFloat(p.current_price || 0);
        const sl  = parseFloat(p.stop_loss     || 0);
        return sl > 0 && cur > 0 && Math.abs((sl - cur) / cur * 100) < 1.0;
    });
    if (closeSL.length > 0) alerts.push(`🔴 ${closeSL.length} posición(es) con <1% al SL: ${closeSL.map(p => p.symbol).join(', ')}`);
    if (fng < 20) alerts.push(`😰 Fear &amp; Greed: ${fng} — Miedo Extremo (señales más difíciles de aprobar)`);
    const alertHtml = alerts.length
        ? `<div class="exec-alerts">${alerts.map(a => `<div class="exec-alert">${a}</div>`).join('')}</div>`
        : '';

    // BTC change display
    const btc7dHtml = btc7d != null
        ? `BTC 7d <span class="${btc7d >= 0 ? 'positive' : 'negative'}">${btc7d >= 0 ? '+' : ''}${btc7d.toFixed(1)}%</span>`
        : '';

    const wrCls   = wr >= 50 ? 'positive' : wr >= 40 ? 'exec-warn' : 'negative';
    const pnlHCls = pnlToday >= 0 ? 'positive' : 'negative';
    const unrealCls = unrealPnl >= 0 ? 'positive' : 'negative';

    // Cycle bar
    const maxSecs = (cycleMins || 20) * 60;
    const progPct = cycleSecsLeft != null ? Math.max(0, Math.min(100, ((maxSecs - cycleSecsLeft) / maxSecs * 100))).toFixed(0) : 0;
    const cycleBarColor = cycleSecsLeft != null && cycleSecsLeft < 60 ? '#e74c3c' : '#4ecca3';

    el.innerHTML = `
        <div class="exec-grid">
            <div class="exec-section exec-market">
                <div class="exec-section-title">📊 MERCADO</div>
                <div class="exec-line">
                    <span class="exec-badge" style="background:${modeInfo.color}22;color:${modeInfo.color};border:1px solid ${modeInfo.color}44">${modeInfo.icon} ${modeInfo.label}</span>
                    &nbsp; BTC 4h <span class="${btc4h === 'UP' ? 'positive' : btc4h === 'DOWN' ? 'negative' : ''}">${btc4h === 'UP' ? '⬆ UP' : btc4h === 'DOWN' ? '⬇ DOWN' : '→ FLAT'}</span>
                    &nbsp; ${btc7dHtml}
                    &nbsp; F&amp;G <span style="color:${fng < 25 ? '#e74c3c' : fng < 45 ? '#ff7c4d' : fng < 56 ? '#aaa' : '#4ecca3'}">${fng} ${fngLabel}</span>
                </div>
            </div>
            <div class="exec-section exec-portfolio">
                <div class="exec-section-title">💼 SISTEMA</div>
                <div class="exec-line">
                    <span class="exec-badge exec-badge-green">✅ ${positions.length} posiciones</span>
                    &nbsp; Exp: <strong>${exposurePct}%</strong> / ${maxExpPct}%
                    &nbsp; Líquido: <strong>$${fmt(liquidUsdt)}</strong>
                    &nbsp; Total: <strong>$${fmt(totalUsdt)}</strong>
                </div>
            </div>
            <div class="exec-section exec-actions">
                <div class="exec-section-title">⚡ ÚLTIMAS ACCIONES</div>
                <div class="exec-line">${lastActHtml}</div>
            </div>
            <div class="exec-section exec-pnl">
                <div class="exec-section-title">📈 PnL</div>
                <div class="exec-line">
                    Abierto: <span class="${unrealCls}"><strong>${unrealPnl >= 0 ? '+' : ''}$${fmt(unrealPnl)} USDT</strong></span>
                    ${lockedPnl > 0 ? `&nbsp; <span class="positive" title="Ganancia asegurada por scale-out en posiciones abiertas">✂️+$${fmt(lockedPnl)}</span>` : ''}
                    &nbsp; Hoy: <span class="${pnlHCls}">${pnlToday >= 0 ? '+' : ''}$${fmt(pnlToday)}</span>
                    &nbsp; WR: <span class="${wrCls}">${fmt(wr, 1)}%</span>
                    &nbsp; PF: <span class="${pf >= 1.5 ? 'positive' : pf >= 1.0 ? 'exec-warn' : 'negative'}">${fmt(pf)}</span>
                </div>
            </div>
            <div class="exec-section exec-cycle">
                <div class="exec-section-title">⏱ PRÓXIMO CICLO</div>
                <div class="exec-line exec-cycle-line">
                    <span class="exec-cycle-val countdown" id="execCountdown">${cycleSecsLeft != null ? Math.floor(cycleSecsLeft/60)+':'+(String(cycleSecsLeft%60).padStart(2,'0')) : '—:——'}</span>
                    <div class="exec-cycle-bar-bg"><div class="exec-cycle-bar" style="width:${progPct}%;background:${cycleBarColor}"></div></div>
                    <span style="color:#555;font-size:11px">${cycleMins ? cycleMins + ' min / ' + mode : ''}</span>
                </div>
            </div>
        </div>
        ${alertHtml}
    `;
}

// ── Apply adaptive mode theming ───────────────────────────────────────────────
function applyModeTheme(mode) {
    const cfg = MODE_CFG[mode] || MODE_CFG['SPOT_BULL'];
    const root = document.documentElement;
    root.style.setProperty('--mode-color', cfg.color);
    // RGB breakdown for rgba() usage
    const hex = cfg.color.replace('#','');
    const r = parseInt(hex.substring(0,2),16);
    const g = parseInt(hex.substring(2,4),16);
    const b = parseInt(hex.substring(4,6),16);
    root.style.setProperty('--mode-color-rgb', `${r},${g},${b}`);
    root.style.setProperty('--mode-bg', `rgba(${r},${g},${b},0.08)`);
    // Update mode pill
    const pill = document.getElementById('modePillText');
    const dot  = document.getElementById('modePillDot');
    if (pill) pill.textContent = cfg.icon + ' ' + cfg.label;
    if (dot)  { dot.style.background = cfg.color; dot.style.boxShadow = `0 0 6px ${cfg.color}`; }
}

// ── KPI Row — updates header chips + status bar ───────────────────────────────
function renderKPIRow(data) {
    if (!data) return;
    const balance  = data.balance       || {};
    const openPos  = data.open_positions|| {};
    const perf     = data.performance   || {};
    const cycle    = data.next_cycle    || {};
    const pnlH     = data.pnl_history   || {};
    const regime   = data.market_regime || {};

    const totalUsdt  = parseFloat(balance.total_usdt || 0);
    const liquidUsdt = parseFloat(balance.usdt_available || balance.total_usdt || 0);
    const positions  = openPos.positions || [];
    const invested   = positions.reduce((s, p) => s + parseFloat(p.trade_type === 'futures' ? (p.margin_usdt || p.invested_usdt || 0) : (p.invested_usdt || 0)), 0);
    const expPct     = totalUsdt > 0 ? ((invested / totalUsdt) * 100) : 0;
    const maxExpPct  = 75;
    const unrealPnl  = positions.reduce((s, p) => s + parseFloat(p.unrealized_pnl || 0), 0);
    const pnlToday   = parseFloat((pnlH.day && pnlH.day.total_pnl) || 0);
    const m          = perf.metrics || {};
    const wr         = parseFloat(m.win_rate || 0);
    const pf         = parseFloat(m.profit_factor || 0);
    const cycleMins  = cycle.interval_minutes || '—';
    const secsLeft   = cycle.seconds_until_next;
    const mode       = regime.trading_mode || 'SPOT_BULL';
    // FIX-L6b: derive counts from unified open_positions array (not stale cache)
    const isFutShort = mode === 'FUTURES_SHORT';
    const maxSpot    = openPos.max_positions || 12;
    const spotPositions = positions.filter(p => p.trade_type !== 'futures');
    const futPositions  = positions.filter(p => p.trade_type === 'futures');
    const spotCount  = openPos.spot_count    ?? spotPositions.length;
    const futCount   = openPos.futures_count ?? futPositions.length;
    const futMax     = isFutShort ? (futCount > 0 ? Math.max(futCount, 5) : 7) : 0;
    const maxPos     = maxSpot;  // kept for legacy KPI tiles
    const ctx        = data.market_context || {};
    const fng        = ctx.fear_greed_value ?? 50;
    const fngLabel   = ctx.fear_greed_label || 'Neutral';

    const unrealCls  = unrealPnl >= 0 ? 'positive' : 'negative';
    const pnlHCls    = pnlToday  >= 0 ? 'positive' : 'negative';
    const wrCls      = wr >= 50  ? 'positive' : wr >= 40 ? '' : 'negative';
    const pfCls      = pf >= 1.5 ? 'positive' : pf >= 1.0 ? '' : 'negative';
    const expColor   = expPct >= maxExpPct ? 'var(--red)' : expPct >= maxExpPct*0.85 ? '#f39c12' : 'var(--green)';

    const countdownStr = secsLeft != null
        ? `${Math.floor(secsLeft/60)}:${String(secsLeft%60).padStart(2,'0')}`
        : '—:——';

    // ── Apply adaptive theme ──────────────────────────────────────────────────
    applyModeTheme(mode);

    // ── Header chips ──────────────────────────────────────────────────────────
    const hLiquid = document.getElementById('headerLiquid');
    const hTotal  = document.getElementById('headerTotal');
    const hCd     = document.getElementById('headerCountdownValue');
    const hFng    = document.getElementById('headerFng');
    const hState  = document.getElementById('headerState');
    if (hLiquid) hLiquid.textContent = `$${fmt(liquidUsdt)}`;
    if (hTotal)  hTotal.textContent  = `$${fmt(totalUsdt)}`;
    if (hCd)     hCd.textContent     = countdownStr;
    if (hFng)    hFng.textContent    = `${fng} ${fngLabel}`;

    // ── Status bar ────────────────────────────────────────────────────────────
    const setText = (id, txt, cls) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.textContent = txt;
        if (cls) el.className = 'sb-val ' + cls;
    };
    setText('sbPnlHoy',    `${pnlToday >= 0 ? '+' : ''}$${fmt(pnlToday)} USDT`, pnlHCls);
    setText('sbUnreal',    `${unrealPnl >= 0 ? '+' : ''}$${fmt(unrealPnl)} USDT`, unrealCls);
    setText('sbWr',        `${fmt(wr,1)}%`, wrCls);
    setText('sbPf',        fmt(pf), pfCls);
    // FIX-L6b: Multimodal position counter using unified counts
    const spotOverflow = spotCount > maxSpot;
    const futOverflow  = isFutShort && futCount > futMax;
    let posStr, posCls;
    if (isFutShort) {
        posStr = `Spot: ${spotCount}/${maxSpot} | Fut: ${futCount}/${futMax}`;
        posCls = (spotOverflow || futOverflow) ? 'negative' : 'positive';
    } else {
        posStr = `Spot: ${spotCount}/${maxSpot}`;
        posCls = spotOverflow ? 'negative' : 'positive';
    }
    setText('sbPositions', posStr, posCls);
    const sbExpEl = document.getElementById('sbExposure');
    if (sbExpEl) { sbExpEl.textContent = `${expPct.toFixed(1)}%`; sbExpEl.style.color = expColor; }

    // Alerts in status bar
    const sbAlerts = document.getElementById('sbAlerts');
    if (sbAlerts) {
        const alerts = [];
        if (expPct >= maxExpPct * 0.90) alerts.push(`Exposición ${expPct.toFixed(0)}%`);
        const closeSL = positions.filter(p => {
            const cur = parseFloat(p.current_price||0), sl = parseFloat(p.stop_loss||0);
            return sl>0 && cur>0 && Math.abs((sl-cur)/cur*100)<1.0;
        });
        if (closeSL.length) alerts.push(`${closeSL.length} pos cerca SL`);
        if (fng < 20) alerts.push(`F&G: ${fng} — Miedo Extremo`);
        sbAlerts.innerHTML = alerts.map(a => `<span class="sb-alert">⚠ ${a}</span>`).join('');
    }

    // ── Header unreal PnL chip ────────────────────────────────────────────────
    const hUnreal = document.getElementById('chipPnlUnreal');
    if (hUnreal) { hUnreal.textContent = `${unrealPnl>=0?'+':''}$${fmt(unrealPnl)}`; hUnreal.className = `chip-val ${unrealCls}`; }
    const hRegime = document.getElementById('chipRegime');
    if (hRegime) hRegime.textContent = mode;

    // ── Performance tab KPI cards ─────────────────────────────────────────────
    const setText2 = (id, txt, cls) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.textContent = txt;
        if (cls !== undefined) el.className = 'pkc-val ' + cls;
    };
    setText2('perfKpiPnlHoy',    `${pnlToday>=0?'+':''}$${fmt(pnlToday)}`, pnlHCls);
    setText2('perfKpiWr',        `${fmt(wr,1)}%`, wrCls);
    setText2('perfKpiPf',        fmt(pf), pfCls);

    // ── Legacy KPI tiles (if present in DOM) ──────────────────────────────────
    const kpiTotal    = document.getElementById('kpiTotal');
    const kpiLiquid   = document.getElementById('kpiLiquid');
    const kpiExp      = document.getElementById('kpiExposure');
    const kpiExpCap   = document.getElementById('kpiExposureCap');
    const kpiUnreal   = document.getElementById('kpiUnrealPnl');
    const kpiPnlHoy   = document.getElementById('kpiPnlHoy');
    const kpiWr       = document.getElementById('kpiWr');
    const kpiPf       = document.getElementById('kpiPf');
    const kpiPos      = document.getElementById('kpiPositions');
    const kpiPosSub   = document.getElementById('kpiPositionsSub');
    const kpiCd       = document.getElementById('kpiCountdown');
    const kpiCycleSub = document.getElementById('kpiCycleSub');

    if (kpiTotal)   { kpiTotal.textContent = `$${fmt(totalUsdt)}`; kpiTotal.className = 'kpi-value'; }
    if (kpiLiquid)  kpiLiquid.textContent  = `Líquido: $${fmt(liquidUsdt)}`;
    if (kpiExp)     { kpiExp.textContent = `${expPct.toFixed(1)}%`; kpiExp.style.color = expColor; }
    if (kpiExpCap)  kpiExpCap.textContent  = `Cap: ${maxExpPct}% | inv $${fmt(invested)}`;
    if (kpiUnreal)  { kpiUnreal.textContent = `${unrealPnl >= 0 ? '+' : ''}$${fmt(unrealPnl)}`; kpiUnreal.className = `kpi-value ${unrealCls}`; }
    if (kpiPnlHoy)  { kpiPnlHoy.textContent = `Hoy: ${pnlToday >= 0 ? '+' : ''}$${fmt(pnlToday)}`; kpiPnlHoy.className = `kpi-sub ${pnlHCls}`; }
    if (kpiWr)      { kpiWr.textContent = `${fmt(wr, 1)}%`; kpiWr.className = `kpi-value ${wrCls}`; }
    if (kpiPf)      { kpiPf.textContent = `PF: ${fmt(pf)}`; kpiPf.className = `kpi-sub ${pfCls}`; }
    if (kpiPos)     { kpiPos.textContent = isFutShort ? `${spotCount}s+${futCount}f` : `${spotCount}/${maxPos}`; kpiPos.className = `kpi-value ${(spotOverflow || futOverflow) ? 'negative' : 'positive'}`; }
    if (kpiPosSub)  kpiPosSub.textContent  = isFutShort ? `Spot: ${spotCount}/${maxSpot} | Fut: ${futCount}/${futMax}` : `Abiertas (max ${maxPos})`;
    if (kpiCd)      kpiCd.textContent      = countdownStr;
    if (kpiCycleSub)kpiCycleSub.textContent = `${mode} · ${cycleMins}min`;
}

// ── Ops unified card state ────────────────────────────────────────────────────
let _currentOpenFilter   = 'all';
let _currentClosedFilter = 'all';
let _lastOpenData   = null;
let _lastClosedData = null;
let _opsMainTab = 'open';  // 'open' | 'closed'
let _opsSubFilter = 'all'; // 'all' | 'spot' | 'fut'

function switchOpsMainTab(main) {
    _opsMainTab = main;
    // Level-1 button state
    document.getElementById('opsTabOpen')  ?.classList.toggle('active', main === 'open');
    document.getElementById('opsTabClosed')?.classList.toggle('active', main === 'closed');
    // Panel visibility
    const openPanel   = document.getElementById('openPositionsData');
    const closedPanel = document.getElementById('closedPositionsData');
    if (openPanel)   { openPanel.classList.toggle('active-panel', main === 'open');   openPanel.style.display   = main === 'open'   ? '' : 'none'; }
    if (closedPanel) { closedPanel.classList.toggle('active-panel', main === 'closed'); closedPanel.style.display = main === 'closed' ? '' : 'none'; }
    // Sync sub-tab filter to the active main tab
    if (main === 'open')   { _currentOpenFilter   = _opsSubFilter; if (_lastOpenData)   renderOpenPositions(_lastOpenData); }
    if (main === 'closed') { _currentClosedFilter = _opsSubFilter; if (_lastClosedData) renderClosedPositions(_lastClosedData); }
}

function switchOpsSubTab(sub) {
    _opsSubFilter = sub;
    // Level-2 button state
    document.querySelectorAll('[data-inner-tab^="ops-"]').forEach(b => {
        b.classList.toggle('active', b.dataset.innerTab === `ops-${sub}`);
    });
    if (_opsMainTab === 'open') {
        _currentOpenFilter = sub;
        if (_lastOpenData) renderOpenPositions(_lastOpenData);
    } else {
        _currentClosedFilter = sub;
        if (_lastClosedData) renderClosedPositions(_lastClosedData);
    }
}

// Legacy wrapper kept for any inline onclick references from old inner-tabs
function switchInnerTab(card, filter) {
    if (card === 'open')   { _opsMainTab = 'open';   switchOpsSubTab(filter); }
    else                   { _opsMainTab = 'closed';  switchOpsSubTab(filter); }
}

// ── Render functions (pure – no fetch) ───────────────────────────────────────
function renderSystemStatus(d) {
    if (!d) return;
    const state = (d.state || 'unknown').toLowerCase();
    const dot = document.querySelector('.status-dot');
    const txt = document.querySelector('.status-text');
    if (dot) dot.className = 'status-dot ' + state;
    if (txt) txt.textContent = d.state || '—';
    // Header health chip
    const hDot  = document.getElementById('healthDot');
    const hText = document.getElementById('healthText');
    const color = state === 'running' ? '#4ecca3' : state === 'paused' ? '#f39c12' : '#e74c3c';
    if (hDot)  { hDot.style.background = color; hDot.style.boxShadow = `0 0 5px ${color}`; }
    if (hText) hText.textContent = d.state || 'Unknown';
}

function renderPortfolio(d) {
    if (!d) return;
    document.getElementById('portfolioData').innerHTML = `
        <div class="data-item"><span class="data-label">Allocation %</span><span class="data-value">${d.portfolio_allocation_pct}%</span></div>
        <div class="data-item"><span class="data-label">Min Confidence</span><span class="data-value">${d.min_pair_confidence_score}</span></div>
        <div class="data-item"><span class="data-label">Max Risk/Par</span><span class="data-value">${d.max_risk_per_pair_pct}%</span></div>
        <div class="data-item"><span class="data-label">Diversificación</span><span class="data-value">${d.diversification_factor}x</span></div>
        <div class="data-item"><span class="data-label">Cierre Posición</span><span class="data-value">${d.enable_position_closure ? 'Activo' : 'Inactivo'}</span></div>
    `;
}

function renderSignals(d) {
    if (!d) return;
    const el = document.getElementById('signalsData');
    if (!d.signals || d.signals.length === 0) {
        el.innerHTML = '<p class="loading">Sin señales</p>';
        return;
    }
    const header = `<div class="signal-row signal-header">
        <span class="signal-symbol" style="color:#666;font-size:11px;font-weight:400">SÍMBOLO</span>
        <span style="color:#666;font-size:11px">ACCIÓN</span>
        <span style="color:#666;font-size:11px">PRECIO</span>
        <span style="color:#666;font-size:11px">FUERZA</span>
        <span style="color:#666;font-size:11px">DECISIÓN</span>
        <span style="color:#666;font-size:11px">RAZÓN SEÑAL</span>
        <span style="color:#666;font-size:11px">HACE</span>
    </div>`;
    el.innerHTML = header + d.signals.slice(0, 15).map(s => {
        const isBuy = (s.action || '').toUpperCase() === 'BUY';
        const badge = isBuy
            ? '<span class="signal-badge signal-buy">BUY</span>'
            : '<span class="signal-badge signal-sell">SELL</span>';
        const price = s.price ? `$${parseFloat(s.price).toFixed(4)}` : '—';
        const reason = s.reason ? `<span class="signal-reason">${s.reason}</span>` : '<span class="signal-reason" style="color:#555">—</span>';
        const ts = s.received_at ? relativeTime(s.received_at) : '';
        const catIco = catIcon(s.category || 'MIDCAP_ALT');
        const trendingBadge = s.trending_boost ? '<span class="signal-trending">🔥 CG</span>' : '';
        const socialBadge = (s.reason || '').includes('[+social trending]') ? '<span class="signal-social">📣 SOCIAL</span>' : '';
        // Decision badge
        let decisionBadge;
        if (s.decision === 'executed') {
            decisionBadge = '<span class="decision-badge decision-executed">✅ EJECUTADO</span>';
        } else if (s.decision === 'rejected') {
            const r = s.decision_reason || 'RECHAZADO';
            decisionBadge = `<span class="decision-badge decision-rejected">❌ ${r}</span>`;
        } else if (s.processed) {
            decisionBadge = '<span class="decision-badge decision-expired">⏱ EXPIRADA</span>';
        } else {
            decisionBadge = '<span class="decision-badge decision-pending">PENDIENTE</span>';
        }
        const str = parseFloat(s.strength || 0);
        const strCls = str >= 0.75 ? 'positive' : str >= 0.55 ? '' : 'negative';
        const strBar = Math.round(str * 100);
        const strengthBadge = str > 0
            ? `<span class="strength-badge ${strCls}" title="Fuerza de señal: ${strBar}%">${strBar}%</span>`
            : '<span class="strength-badge" style="color:#555">—</span>';
        return `
        <div class="signal-row">
            <span class="signal-symbol">${catIco} ${s.symbol || '—'}</span>
            ${badge}
            <span class="signal-price">${price}</span>
            ${strengthBadge}
            ${decisionBadge}
            <span style="display:flex;gap:4px;align-items:center;overflow:hidden">${reason}${trendingBadge}${socialBadge}</span>
            <span class="signal-ts">${ts}</span>
        </div>`;
    }).join('');
}

function relativeTime(isoStr) {
    try {
        const diff = Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000);
        if (diff < 60) return `${diff}s`;
        if (diff < 3600) return `${Math.floor(diff/60)}m`;
        if (diff < 86400) return `${Math.floor(diff/3600)}h`;
        return `${Math.floor(diff/86400)}d`;
    } catch { return ''; }
}

function renderHealth(d) {
    if (!d) return;
    document.getElementById('healthData').innerHTML = `
        <div class="data-item"><span class="data-label">Estado</span><span class="data-value">${d.system_state || '—'}</span></div>
        <div class="data-item"><span class="data-label">Ciclo de Trades</span><span class="data-value">${d.trade_cycle_running ? '✅ Activo' : '⛔ Detenido'}</span></div>
        <div class="data-item"><span class="data-label">Ciclo de Análisis</span><span class="data-value">${d.analysis_cycle_running ? '✅ Activo' : '⛔ Detenido'}</span></div>
        <div class="data-item"><span class="data-label">Portfolio Manager</span><span class="data-value">${d.portfolio_manager_active ? '✅ Activo' : '⛔ Inactivo'}</span></div>
        <div class="data-item"><span class="data-label">Position Manager</span><span class="data-value">${d.position_manager_active ? '✅ Activo' : '⛔ Inactivo'}</span></div>
    `;
}

function renderBalance(d) {
    if (!d || d.error) return;
    const spotPct    = parseFloat(d.spot_pct    || 100);
    const futPct     = parseFloat(d.futures_pct || 0);
    const spotUsdt   = parseFloat(d.spot_usdt   || d.total_usdt || 0);
    const futUsdt    = parseFloat(d.futures_usdt || 0);
    const rate       = parseFloat(d.usd_to_mxn_rate || 17.8);
    const walletHtml = futUsdt > 0 ? `
        <div class="wallet-split">
            <div class="wallet-row">
                <span class="wallet-icon">💼</span>
                <span class="wallet-label">Spot</span>
                <span class="wallet-amount">$${fmt(spotUsdt)} USDT</span>
                <span class="wallet-pct">${spotPct}%</span>
            </div>
            <div class="wallet-bar-wrap">
                <div class="wallet-bar-spot" style="width:${spotPct}%"></div>
                <div class="wallet-bar-fut"  style="width:${futPct}%"></div>
            </div>
            <div class="wallet-row">
                <span class="wallet-icon">⚡</span>
                <span class="wallet-label">Futuros</span>
                <span class="wallet-amount">$${fmt(futUsdt)} USDT</span>
                <span class="wallet-pct">${futPct}%</span>
            </div>
        </div>` : '';
    document.getElementById('balanceData').innerHTML = `
        <div class="data-item"><span class="data-label">Total USDT</span><span class="data-value">$${fmt(d.total_usdt)}</span></div>
        <div class="data-item"><span class="data-label">Total MXN</span><span class="data-value">$${fmt(d.total_mxn)}</span></div>
        <div class="data-item"><span class="data-label">USD/MXN</span><span class="data-value">${fmt(d.usd_to_mxn_rate)}</span></div>
        ${walletHtml}
        <div class="balances-breakdown">
            <h3>💼 SPOT — Activos:</h3>
            <div class="balances-scroll">
            ${Object.entries(d.balances_usdt || {}).filter(([a, v]) => parseFloat(v) >= 5).map(([a, v]) => `
                <div class="data-item">
                    <span class="data-label">${a}</span>
                    <span class="data-value">${fmt(v, 4)} USDT / ${fmt((d.balances_mxn || {})[a])} MXN</span>
                </div>`).join('')}
            </div>
        </div>
        ${_cachedFuturesPositions.length > 0 ? `
        <div class="balances-breakdown" style="margin-top:10px">
            <h3>⚡ FUTUROS — Posiciones abiertas:</h3>
            <div class="balances-scroll">
            ${_cachedFuturesPositions.map(p => {
                const pnl = parseFloat(p.unrealized_pnl || 0);
                const pnlCls = pnl >= 0 ? 'positive' : 'negative';
                const pnlSign = pnl >= 0 ? '+' : '';
                const sym = (p.symbol || '').replace('USDT', '');
                const side = p.side || '';
                const sideColor = side === 'SHORT' ? '#e74c3c' : '#4ecca3';
                const qty = parseFloat(p.qty || 0);
                const ep = parseFloat(p.entry_price || 0);
                const exposure = qty * ep;
                return `<div class="data-item">
                    <span class="data-label"><span style="color:${sideColor};font-weight:700">${sym} ${side}</span></span>
                    <span class="data-value">
                        ${fmt(qty,3)} @ $${fmt(ep)} = $${fmt(exposure)}
                        &nbsp;<span class="${pnlCls}" style="font-weight:700">${pnlSign}${fmt(pnl)} USDT</span>
                    </span>
                </div>`;
            }).join('')}
            </div>
        </div>` : ''}
    `;
}

function renderProfitProjection(pp) {
    const el = document.getElementById('profitProjectionData');
    if (!el) return;
    if (!pp || pp.error) {
        el.innerHTML = `<p class="loading">${pp && pp.error === 'insufficient_trades' ? 'Insuficientes trades (' + (pp.n_trades||0) + '< 5)' : 'Sin datos'}</p>`;
        return;
    }
    const rate = parseFloat(pp.usd_mxn_rate || 19.5);
    const pRow = (label, obj, objMxn) => {
        if (!obj || obj.mean == null) return '';
        const cls = parseFloat(obj.mean) >= 0 ? 'positive' : 'negative';
        const p5Mxn  = objMxn && objMxn.p5  != null ? fmt(objMxn.p5)  : '—';
        const p95Mxn = objMxn && objMxn.p95 != null ? fmt(objMxn.p95) : '—';
        return `<tr>
            <td>${label}</td>
            <td class="${cls}">${fmt(obj.mean, 2)} USD</td>
            <td class="${cls}"><b>${fmt(objMxn ? objMxn.mean : obj.mean * rate, 0)} MXN</b></td>
            <td style="color:#888;font-size:0.82em">${p5Mxn} / ${p95Mxn} MXN</td>
        </tr>`;
    };

    // Capital actual scenario
    const sca = pp.scenario_capital_actual || {};
    const scaCapMxn   = parseFloat(sca.capital_mxn || pp.capital_mxn || 0);
    const scaMonthMxn = parseFloat(sca.monthly_mean_mxn || 0);
    const scaP5Mxn    = parseFloat(sca.monthly_p5_mxn || 0);
    const scaP95Mxn   = parseFloat(sca.monthly_p95_mxn || 0);
    const scaYearMxn  = parseFloat(sca.annual_mean_mxn || 0);
    const scaMonthCls = scaMonthMxn >= 0 ? 'positive' : 'negative';
    const scaYearCls  = scaYearMxn  >= 0 ? 'positive' : 'negative';

    // Progress bar toward $30k MXN/month
    const t30  = pp.target_30k_mxn || {};
    const prog = Math.min(parseFloat(t30.current_progress_pct || 0), 100);

    // Meta 1,000 MXN/día
    const m1k = pp.meta_1000_mxn_day || {};
    const m1kPnlDay   = parseFloat(m1k.pnl_day_mxn ?? 0);
    const m1kTdCur    = parseFloat(m1k.trades_day_current ?? 0);
    const m1kTdNeed   = parseFloat(m1k.trades_needed_day ?? 999);
    const m1kPct      = parseFloat(m1k.pct_toward_goal ?? 0);
    const m1kEv       = parseFloat(m1k.ev_per_trade_usd ?? 0);
    const m1kPnlCls   = m1kPnlDay >= 0 ? 'positive' : 'negative';

    el.innerHTML = `
        <div class="proj-stats">
            <span class="proj-stat">WR <b>${fmt(pp.win_rate, 1)}%</b></span>
            <span class="proj-stat">Trades/mes <b>${fmt(pp.trades_per_month, 1)}</b></span>
            <span class="proj-stat">Avg win <b class="positive">+$${fmt(pp.avg_win_usd, 3)}</b></span>
            <span class="proj-stat">Avg loss <b class="negative">$${fmt(pp.avg_loss_usd, 3)}</b></span>
        </div>
        <table class="proj-table">
            <thead><tr><th>Período</th><th>Esperado USD</th><th>Esperado MXN</th><th>Rango P5/P95 MXN</th></tr></thead>
            <tbody>
                ${pRow('📅 Hoy',    pp.day,   pp.day_mxn)}
                ${pRow('📆 Semana', pp.week,  pp.week_mxn)}
                ${pRow('🗓️ Mes',    pp.month, pp.month_mxn)}
            </tbody>
        </table>

        <div class="proj-scenario">
            <div class="proj-scenario-title">💰 Con capital actual ($${fmt(scaCapMxn, 0)} MXN · $${fmt(sca.capital_usd || pp.capital_usd, 0)} USD)</div>
            <div class="proj-scenario-row">
                <span>Ganancia mensual esperada:</span>
                <span class="${scaMonthCls}"><b>${scaMonthMxn >= 0 ? '+' : ''}$${fmt(scaMonthMxn, 0)} MXN</b></span>
            </div>
            <div class="proj-scenario-row">
                <span>Rango P5–P95:</span>
                <span style="color:#aaa">$${fmt(scaP5Mxn, 0)} – $${fmt(scaP95Mxn, 0)} MXN</span>
            </div>
            <div class="proj-scenario-row">
                <span>Ganancia anual proyectada:</span>
                <span class="${scaYearCls}"><b>${scaYearMxn >= 0 ? '+' : ''}$${fmt(scaYearMxn, 0)} MXN</b></span>
            </div>
        </div>

        ${m1kEv > 0 ? `
        <div class="proj-target" style="border-color:rgba(78,204,163,0.3);background:rgba(78,204,163,0.05)">
            <div class="proj-target-title" style="color:#4ecca3">🎯 Meta: 1,000 MXN/día con capital actual</div>
            <div class="proj-target-row">
                <span>PnL/día actual (sistema Base):</span>
                <span class="${m1kPnlCls}"><b>${m1kPnlDay >= 0 ? '+' : ''}$${fmt(m1kPnlDay, 0)} MXN</b></span>
            </div>
            <div class="proj-target-row">
                <span>EV por trade:</span>
                <span><b>$${fmt(m1kEv, 3)} USDT</b></span>
            </div>
            <div class="proj-target-row">
                <span>Trades/día actuales vs necesarios:</span>
                <span><b>${fmt(m1kTdCur, 1)}</b> de <b style="color:#f39c12">${fmt(m1kTdNeed, 1)}</b> necesarios</span>
            </div>
            <div class="proj-progress-wrap" style="margin-top:8px">
                <div class="proj-progress-bar" style="width:${Math.min(m1kPct,100)}%;background:linear-gradient(90deg,#f39c12,#4ecca3)"></div>
            </div>
            <div class="proj-progress-label">${fmt(m1kPct, 1)}% hacia la meta de 1,000 MXN/día</div>
        </div>` : ''}

        <div class="proj-target">
            <div class="proj-target-title">📈 Objetivo: $30,000 MXN/mes</div>
            <div class="proj-target-row">
                <span>Capital requerido (con WR actual):</span>
                <span><b>$${fmt(t30.capital_required_mxn, 0)} MXN</b></span>
            </div>
            <div class="proj-progress-wrap">
                <div class="proj-progress-bar" style="width:${prog}%"></div>
            </div>
            <div class="proj-progress-label">Capital actual: ${fmt(prog, 1)}% del objetivo</div>
        </div>
    `;
}

function renderPnlHistory(d) {
    if (!d || d.error) return;
    const row = (label, p) => `
        <div class="data-item">
            <span class="data-label">${label}</span>
            <span class="data-value ${pnlClass(p.total_pnl)}">${fmt(p.total_pnl)} USDT (${p.total_trades} trades)</span>
        </div>`;
    document.getElementById('pnlHistoryData').innerHTML =
        row('PnL Hoy', d.day) + row('PnL Semana', d.week) + row('PnL Año', d.year);
}

// ── Profit Tracker card ──────────────────────────────────────────────────────
let _profitTrackerPeriod = 'today';
function switchProfitTrackerPeriod(period) {
    _profitTrackerPeriod = period;
    if (_cachedData && _cachedData.pnl_by_close_reason) renderProfitTracker(_cachedData.pnl_by_close_reason);
}
function renderProfitTracker(d) {
    const el = document.getElementById('profitTrackerData');
    if (!el || !d || d.error) return;

    const period = _profitTrackerPeriod === 'week' ? d.week : d.today;
    if (!period) return;
    const target = d.daily_target || 40.0;
    const totalPnl = period.total_pnl || 0;
    const totalTrades = period.total_trades || 0;
    const byReason = period.by_reason || [];
    const spotPnl = period.spot_pnl || 0;
    const futPnl = period.futures_pnl || 0;
    const spotC = period.spot_count || 0;
    const futC = period.futures_count || 0;

    // Progress bar
    const progressPct = Math.min(100, Math.max(0, (totalPnl / target) * 100));
    const barColor = totalPnl >= target ? '#4ecca3' : totalPnl >= target * 0.5 ? '#f39c12' : totalPnl > 0 ? '#3498db' : '#e74c3c';

    // Win rate for the period
    const wins = byReason.filter(r => r.pnl > 0).reduce((s, r) => s + r.count, 0);
    const wr = totalTrades > 0 ? ((wins / totalTrades) * 100).toFixed(1) : '0.0';

    // Reason icon map
    const reasonIcon = (r) => {
        const m = _CLOSE_REASON_MAP[r];
        return m ? m[0] : r;
    };

    const reasonRows = byReason.map(r => {
        const cls = r.pnl >= 0 ? 'positive' : 'negative';
        return `<div class="pt-reason-row">
            <span class="pt-reason-label">${reasonIcon(r.reason)}</span>
            <span class="pt-reason-count">${r.count} trade${r.count !== 1 ? 's' : ''}</span>
            <span class="pt-reason-pnl ${cls}">${r.pnl >= 0 ? '+' : ''}${fmt(r.pnl)} USDT</span>
        </div>`;
    }).join('');

    const periodLabel = _profitTrackerPeriod === 'week' ? '7 días' : 'Hoy';
    const periodBtnToday = _profitTrackerPeriod === 'today' ? 'active' : '';
    const periodBtnWeek = _profitTrackerPeriod === 'week' ? 'active' : '';

    el.innerHTML = `
        <div class="pt-toggle">
            <button class="pt-toggle-btn ${periodBtnToday}" onclick="switchProfitTrackerPeriod('today')">Hoy</button>
            <button class="pt-toggle-btn ${periodBtnWeek}" onclick="switchProfitTrackerPeriod('week')">7d</button>
        </div>
        <div class="pt-target-section">
            <div class="pt-target-label">
                <span>${periodLabel}:</span>
                <span class="${pnlClass(totalPnl)}"><strong>${totalPnl >= 0 ? '+' : ''}$${fmt(totalPnl)} USDT</strong></span>
                <span class="pt-target-meta"> / $${fmt(target)} meta</span>
            </div>
            <div class="pt-bar-bg">
                <div class="pt-bar-fill" style="width:${progressPct}%;background:${barColor}"></div>
                <span class="pt-bar-pct">${progressPct.toFixed(0)}%</span>
            </div>
        </div>
        <div class="pt-reasons">
            ${reasonRows || '<div class="pt-empty">Sin trades cerrados en este período</div>'}
        </div>
        <div class="pt-footer">
            <span>${totalTrades} trades</span>
            <span>WR: <strong>${wr}%</strong></span>
            <span>Spot: <span class="${pnlClass(spotPnl)}">${spotPnl >= 0 ? '+' : ''}$${fmt(spotPnl)}</span> (${spotC})</span>
            <span>Fut: <span class="${pnlClass(futPnl)}">${futPnl >= 0 ? '+' : ''}$${fmt(futPnl)}</span> (${futC})</span>
        </div>
    `;
}

// ── Set of expanded symbols in open positions table ───────────────────────────
const _expandedSymbols = new Set();

// Helper: format entry_time legibly — shows date if position is >24h old
function fmtEntryTime(isoStr, hoursHeld) {
    if (!isoStr) return '—';
    try {
        const d = new Date(isoStr);
        if (hoursHeld >= 24) {
            return d.toLocaleString('es-MX', {
                timeZone: 'America/Mexico_City',
                day: '2-digit', month: 'short',
                hour: '2-digit', minute: '2-digit', hour12: false
            });
        }
        return d.toLocaleString('es-MX', {
            timeZone: 'America/Mexico_City',
            hour: '2-digit', minute: '2-digit', hour12: false
        });
    } catch(e) { return isoStr.substring(11, 16) || isoStr; }
}

function toggleEscRow(symbol) {
    if (_expandedSymbols.has(symbol)) {
        _expandedSymbols.delete(symbol);
    } else {
        _expandedSymbols.add(symbol);
    }
    // Re-render only the tbody rows without full re-fetch
    const el = document.getElementById('openPositionsData');
    if (_lastOpenData) renderOpenPositions(_lastOpenData);
}

function renderOpenPositions(d) {
    if (!d) return;
    _lastOpenData = d;  // FIX-L6c: cache for sub-tab re-render
    const el = document.getElementById('openPositionsData');

    // FIX-L6c: update card header with live counts
    const _allPos = d.positions || [];
    const _spotC = d.spot_count    ?? _allPos.filter(p => p.trade_type !== 'futures').length;
    const _futC  = d.futures_count ?? _allPos.filter(p => p.trade_type === 'futures').length;
    const _titleEl = document.getElementById('unifiedPosCardTitle');
    if (_titleEl && _opsMainTab === 'open') {
        _titleEl.innerHTML = `📂 Operaciones — Abiertas <span class="pos-count-badge">Spot: ${_spotC} | Fut: ${_futC}</span>`;
    }

    // FIX-D4: filter by active inner sub-tab
    const _filter = _currentOpenFilter;
    let visiblePositions = _allPos;
    if (_filter === 'spot') visiblePositions = _allPos.filter(p => p.trade_type !== 'futures');
    else if (_filter === 'fut') visiblePositions = _allPos.filter(p => p.trade_type === 'futures');

    // Ordenar por Edad descendente (más antigua primero — edad real desde primera apertura)
    visiblePositions = [...visiblePositions].sort((a, b) => {
        const aHrs = a.entries?.length > 0 ? Math.max(...a.entries.map(e => e.age_h || 0)) : parseFloat(a.hours_held || 0);
        const bHrs = b.entries?.length > 0 ? Math.max(...b.entries.map(e => e.age_h || 0)) : parseFloat(b.hours_held || 0);
        return bHrs - aHrs;
    });

    if (visiblePositions.length === 0) {
        el.innerHTML = _allPos.length === 0
            ? '<p class="loading">Sin posiciones abiertas</p>'
            : `<p class="loading">Sin posiciones ${_filter === 'spot' ? 'SPOT' : 'FUTUROS'} abiertas</p>`;
        return;
    }

    const totalUnreal = visiblePositions.reduce((s, p) => s + parseFloat(p.unrealized_pnl || 0), 0);
    const totalInvested = visiblePositions.reduce((s, p) => s + parseFloat(p.invested_usdt || 0), 0);

    const predPositions = visiblePositions.filter(p => p.pnl_prediction && p.pnl_prediction.expected_pnl);
    const avgAccuracy = predPositions.length > 0
        ? predPositions.reduce((s, p) => s + (parseFloat(p.unrealized_pnl || 0) / parseFloat(p.pnl_prediction.expected_pnl || 1)), 0) / predPositions.length
        : 0;

    const slGlobalPnl = visiblePositions.reduce((s, p) => {
        const sl = parseFloat(p.stop_loss || 0);
        const entry = parseFloat(p.entry_price || 0);
        const qty = parseFloat(p.quantity || 0);
        if (!sl || !entry || !qty) return s;
        const isShort = (p.side || '').toUpperCase() === 'SHORT' || (p.side || '').toUpperCase() === 'SELL';
        return s + (isShort ? (entry - sl) * qty : (sl - entry) * qty);
    }, 0);

    const rows = [];
    for (const p of visiblePositions) {
        const _isBounce = !!p.bounce_entry;
        const rowCls = _isBounce ? 'row-bounce' : parseFloat(p.unrealized_pnl||0) > 0 ? 'row-pnl-pos' : parseFloat(p.unrealized_pnl||0) < -0.01 ? 'row-pnl-neg' : '';
        const curP   = parseFloat(p.current_price || 0);
        const entryP = parseFloat(p.entry_price || 0);
        const _escEntries = p.entries || [];
        // B1: edad real = mayor age_h entre escalamientos, o hours_held del campo
        const realHrs = _escEntries.length > 0
            ? Math.max(..._escEntries.map(e => e.age_h || 0))
            : parseFloat(p.hours_held || 0);
        const ageCls = realHrs < 2 ? 'age-fresh' : realHrs < 8 ? 'age-mid' : 'age-old';
        const ageStr = realHrs < 1 ? '<1h' : realHrs < 24 ? realHrs.toFixed(0) + 'h' : (realHrs/24).toFixed(1) + 'd';

        const overflowBadge = p.is_overflow ? ' <span class="badge-overflow" title="Posición extra por señal excepcional">⚡OVF</span>' : '';
        const _btcTip = p.btc_chg_since_entry_pct != null ? ` BTC ${p.btc_chg_since_entry_pct >= 0 ? '+' : ''}${p.btc_chg_since_entry_pct.toFixed(1)}% desde entrada` : '';
        const _bGateBlocked = _isBounce && (p.bounce_gate_blocked_count || 0) > 0;
        const _bGateSig = p.bounce_gate_last_signal ? ` Señal: ${p.bounce_gate_last_signal}` : '';
        const _bGateEv = p.bounce_gate_last_ev != null ? ` EV: ${p.bounce_gate_last_ev >= 0 ? '+' : ''}$${p.bounce_gate_last_ev.toFixed(2)}` : '';
        const _bGateVel = p.bounce_gate_last_velocity != null ? ` vel: ${(p.bounce_gate_last_velocity * 100).toFixed(3)}%/h` : '';
        const _bGateTip = _isBounce ? `Entrada en rebote — Rescue Mode${_bGateBlocked ? ' ⏸️GATE BLOQUEADO (' + p.bounce_gate_blocked_count + 'x)' : ' activo'}. Target: pnl ≥ -$0.50${_btcTip}${_bGateSig}${_bGateEv}${_bGateVel}` : '';
        const bounceBadge = _isBounce ? ` <span class="badge-bounce${_bGateBlocked ? ' badge-bounce-gated' : ''}" title="${_bGateTip}">🔄 REBOTE${_bGateBlocked ? '⏸️' : ''}</span>` : '';
        const typeBadge = p.trade_type === 'futures' ? '<span class="badge-futures">FUT</span>' : '<span class="badge-spot">SPOT</span>';
        const _escN = p.escalamientos || 0;
        // Max escalonamiento_index from entries (may be >1 even if only 1 entry remains after sync)
        const _maxEscIdx = _escEntries.length > 0
            ? Math.max(..._escEntries.map(e => e.escalonamiento_index || 1))
            : _escN;
        const _showEscBadge = _escN > 1 || _maxEscIdx >= 2;
        const isExpanded = _expandedSymbols.has(p.symbol);
        const escBadge = _showEscBadge
            ? ` <span class="badge-esc" style="cursor:pointer" onclick="event.stopPropagation();toggleEscRow('${p.symbol}')" title="Click para ver/ocultar entradas (${_escN} registradas, idx máx ${_maxEscIdx})">📊×${_maxEscIdx} ${isExpanded ? '▲' : '▼'}</span>`
            : '';

        const tp = parseFloat(p.take_profit || 0);
        let progressHtml = '—';
        if (tp && entryP) progressHtml = fmtProgressBar(curP, entryP, tp, true);

        // B7: entry_time legible con fecha si >24h
        const firstEntry = _escEntries.length > 0
            ? _escEntries.reduce((a, b) => (a.age_h||0) > (b.age_h||0) ? a : b)
            : null;
        const entryTimeStr = firstEntry
            ? fmtEntryTime(firstEntry.opened_at, realHrs)
            : fmtEntryTime(p.entry_time, realHrs);

        rows.push(`
        <tr class="${rowCls}" onclick="showPositionDetailModal('${p.symbol}', '${p.position_id}', 'open')" style="cursor:pointer" data-symbol="${p.symbol}">
            <td>${typeBadge} <strong>${p.symbol}</strong>${overflowBadge}${escBadge}${bounceBadge}</td>
            <td>$${fmt(entryP)}<br><small class="entry-time">${entryTimeStr}</small></td>
            <td class="sl-tp-cell">${fmtSlTpCell(p)}</td>
            <td class="sl-tp-badges">${fmtSlTpBadges(p)}</td>
            <td>${progressHtml}</td>
            <td class="${pnlClass(p.unrealized_pnl)}">${fmt(p.unrealized_pnl)} USDT</td>
            <td class="${ageCls}">${ageStr}</td>
        </tr>`);

        // Sub-filas de escalamiento expandibles
        // Mostrar incluso con 1 entrada si escalonamiento_index >= 2 (Esc1 purgado por sync)
        if (_showEscBadge && isExpanded) {
            // Si hay entradas faltantes (idx > posición en array), mostrar placeholder
            const _knownIndices = new Set(_escEntries.map(e => e.escalonamiento_index || 1));
            for (let _idx = 1; _idx < _maxEscIdx; _idx++) {
                if (!_knownIndices.has(_idx)) {
                    rows.push(`
        <tr class="esc-sub-row" style="opacity:0.45">
            <td style="padding-left:28px;font-size:12px">
                <span style="color:#9b59b6">↳</span>
                <span style="color:#888;font-weight:600">Esc${_idx}</span>
                <small style="color:#555;font-style:italic">&nbsp;(entrada anterior — no disponible tras reinicio)</small>
            </td>
            <td colspan="6" style="color:#555;font-size:11px;font-style:italic">Datos no recuperables: posición promediada por Binance al sincronizar</td>
        </tr>`);
                }
            }
            for (const esc of _escEntries) {
                const escPnl = parseFloat(esc.pnl_usdt || 0);
                const escPct = parseFloat(esc.pnl_pct || 0);
                const escAge = parseFloat(esc.age_h || 0);
                const escAgeStr = escAge < 1 ? '<1h' : escAge < 24 ? escAge.toFixed(0) + 'h' : (escAge/24).toFixed(1) + 'd';
                const escEntryFmt = fmtEntryTime(esc.opened_at, escAge);
                const escCost = round2(parseFloat(esc.entry_price||0) * parseFloat(esc.qty||0));
                rows.push(`
        <tr class="esc-sub-row ${escPnl >= 0 ? 'row-pnl-pos' : 'row-pnl-neg'}">
            <td style="padding-left:28px;color:#aaa;font-size:12px">
                <span style="color:#9b59b6">↳</span>
                <span style="color:#f39c12;font-weight:600">Esc${esc.escalonamiento_index}</span>
                &nbsp;$${fmt(esc.entry_price)} × ${fmt(esc.qty,4)}
                <small style="color:#666">(~$${fmt(escCost)})</small>
            </td>
            <td><small class="entry-time">${escEntryFmt}</small></td>
            <td colspan="3" style="color:#888;font-size:12px">
                PnL entrada: <span class="${escPnl>=0?'positive':'negative'}" style="font-weight:600">${escPnl>=0?'+':''}${fmt(escPnl)} USDT (${escPct>=0?'+':''}${fmt(escPct)}%)</span>
            </td>
            <td class="${escPnl>=0?'positive':'negative'}" style="font-weight:600">${escPnl>=0?'+':''}${fmt(escPnl)}</td>
            <td style="color:#888;font-size:12px">${escAgeStr}</td>
        </tr>`);
            }
        }
    }

    el.innerHTML = `
        <div class="table-scroll-10">
        <table class="positions-table">
            <thead>
                <tr>
                    <th>Símbolo</th>
                    <th>Entrada</th>
                    <th>SL / TP</th>
                    <th>Estado SL/TP</th>
                    <th>Progreso</th>
                    <th>PnL No Real.</th>
                    <th>Edad</th>
                </tr>
            </thead>
            <tbody>${rows.join('')}</tbody>
        </table>
        </div>
        <div class="positions-summary">
            ${visiblePositions.length} posiciones${_allPos.length !== visiblePositions.length ? ` (de ${_allPos.length} total)` : ''}
            ${(()=>{const _bc=visiblePositions.filter(p=>p.bounce_entry).length;return _bc>0?`&nbsp;|&nbsp;<span style="color:#e67e22;font-weight:700">⚠️ ${_bc} en rebote — Rescue Mode activo</span>`:'';})()} &nbsp;|&nbsp;
            Invertido: <strong>$${fmt(totalInvested)} USDT</strong>
            &nbsp;|&nbsp;
            PnL No Realizado:
            <span class="${pnlClass(totalUnreal)}">
                <strong>${totalUnreal >= 0 ? '+' : ''}${fmt(totalUnreal)} USDT</strong>
            </span>
            ${predPositions.length > 0 ? `&nbsp;|&nbsp;Precisión Pred. Promedio: <strong>${(avgAccuracy * 100).toFixed(0)}%</strong>` : ''}
            &nbsp;|&nbsp;SL Global:
            <span class="${pnlClass(slGlobalPnl)}">
                <strong>${slGlobalPnl >= 0 ? '+' : ''}${fmt(slGlobalPnl)} USDT</strong>
            </span>
        </div>
    `;
}

function round2(x) { return Math.round(x * 100) / 100; }

function renderClosedPositions(d) {
    if (!d) return;
    _lastClosedData = d;  // FIX-L6c: cache for sub-tab re-render
    const el = document.getElementById('closedPositionsData');
    const periodDays = d.period_days || 7;

    // FIX-L6c: update card header with live counts
    const _allClosed = d.positions || [];
    const _clSpotC = d.spot_count    ?? _allClosed.filter(p => p.trade_type !== 'futures').length;
    const _clFutC  = d.futures_count ?? _allClosed.filter(p => p.trade_type === 'futures').length;
    const _clTitleEl = document.getElementById('unifiedPosCardTitle');
    if (_clTitleEl && _opsMainTab === 'closed') {
        _clTitleEl.innerHTML = `✅ Operaciones — Cerradas <span class="pos-count-badge">${_allClosed.length} total · Spot: ${_clSpotC} | Fut: ${_clFutC}</span>`;
    }
    const _daysLabel = document.getElementById('closedDaysLabel');
    if (_daysLabel) _daysLabel.textContent = `${periodDays}d`;

    // FIX-D4: filter by active inner sub-tab
    const _clFilter = _currentClosedFilter;
    let filteredClosed = _allClosed;
    if (_clFilter === 'spot') filteredClosed = _allClosed.filter(p => p.trade_type !== 'futures');
    else if (_clFilter === 'fut') filteredClosed = _allClosed.filter(p => p.trade_type === 'futures');

    if (filteredClosed.length === 0) {
        el.innerHTML = _allClosed.length === 0
            ? `<p class="loading">Sin posiciones cerradas recientes (últimos ${periodDays} días)</p>`
            : `<p class="loading">Sin trades ${_clFilter === 'spot' ? 'SPOT' : 'FUTUROS'} cerrados recientemente</p>`;
        return;
    }

    // Ordenar por exit_time descendente (más reciente primero)
    const sortedPositions = [...filteredClosed].sort((a, b) => {
        const dateA = new Date(b.exit_time || 0);
        const dateB = new Date(a.exit_time || 0);
        return dateA - dateB;
    });

    // Helper para formatear fechas en local (México)
    const fmtDateLocal = (isoStr) => {
        if (!isoStr) return '—';
        try {
            const d = new Date(isoStr);
            return d.toLocaleString('es-MX', {
                timeZone: 'America/Mexico_City',
                day: '2-digit',
                month: 'short',
                hour: '2-digit',
                minute: '2-digit',
                hour12: false
            });
        } catch(e) { return isoStr; }
    };

    // Helper para badge de tipo de trading
    const tradeTypeBadge = (type) => {
        if (type === 'futures') return '<span class="badge-futures">FUT</span>';
        return '<span class="badge-spot">SPOT</span>';
    };

    // Calcular métricas de accuracy
    const withPred = sortedPositions.filter(p => p.prediction_accuracy != null);
    const avgAccuracy = withPred.length > 0
        ? withPred.reduce((s, p) => s + parseFloat(p.prediction_accuracy || 0), 0) / withPred.length
        : 0;

    el.innerHTML = `
        <div class="table-scroll-10">
        <table class="positions-table">
            <thead>
                <tr>
                    <th>Símbolo</th>
                    <th>Entrada → Salida</th>
                    <th>Resultado</th>
                    <th>Razón</th>
                    <th>SL/TP Lifecycle</th>
                    <th>Cerrado</th>
                </tr>
            </thead>
            <tbody>
                ${sortedPositions.map(p => {
                    const actualPnl = parseFloat(p.pnl || 0);
                    const _clIsBounce = !!p.bounce_entry || p.close_reason === 'sso_bounce_rescue';
                    const rowCls = _clIsBounce ? 'row-bounce-closed' : actualPnl > 0 ? 'row-pnl-pos' : actualPnl < -0.01 ? 'row-pnl-neg' : '';
                    const typeBadge = tradeTypeBadge(p.trade_type);
                    const exitTimeFmt = fmtDateLocal(p.exit_time);

                    // VIS-3b: SL/TP lifecycle badges for closed
                    let lifecycleBadges = '';
                    if (p.trailing_peak) lifecycleBadges += `<span class="sl-badge trail" title="Peak: $${fmt(p.trailing_peak)}">📈T</span>`;
                    if (p.tp_extensions > 0) lifecycleBadges += `<span class="sl-badge ext" title="SmartTP ×${p.tp_extensions}">🔄${p.tp_extensions}</span>`;
                    if (p.scale_out_done) {
                        const soTipCl = p.scale_out_pnl ? `Scale-out: +$${fmt(p.scale_out_pnl)} locked` : 'Scale-out ejecutado';
                        lifecycleBadges += `<span class="sl-badge scale" title="${soTipCl}">✂️$${fmt(p.scale_out_pnl || 0)}</span>`;
                    }
                    if (p.peak_pnl_pct > 0) lifecycleBadges += `<span class="sl-badge peak" title="Peak PnL: ${fmt(p.peak_pnl_pct, 1)}%">📊${fmt(p.peak_pnl_pct, 1)}%</span>`;
                    if (p.close_reason === 'momentum_reversal') lifecycleBadges += '<span class="sl-badge mr" title="Cerrado por momentum reversal">⚡MR</span>';
                    // Escalamiento badge for closed positions
                    const _cMeta = p.metadata || {};
                    const _cEscIdx = _cMeta.escalonamiento_index;
                    const _cEscTotal = _cMeta.total_escalonamientos;
                    const _cCgid = _cMeta.close_group_id;
                    if (_cEscTotal > 1 && _cEscIdx) {
                        lifecycleBadges += `<span class="sl-badge esc" title="Escalonamiento ${_cEscIdx}/${_cEscTotal}${_cCgid ? ' grp:' + _cCgid.substring(0,8) : ''}">📊${_cEscIdx}/${_cEscTotal}</span>`;
                    }
                    if (_clIsBounce) {
                        const _clMeta2 = typeof p.metadata === 'object' ? (p.metadata || {}) : {};
                        const _clBlockCount = _clMeta2.bounce_gate_blocked_count || 0;
                        const _clHeldMin = _clMeta2.bounce_gate_held_minutes || 0;
                        const _clBrTip = `Bounce Rescue${_clBlockCount > 0 ? ' — Gate bloqueó ' + _clBlockCount + 'x' : ''}${_clHeldMin > 0 ? ', mantuvo ' + _clHeldMin + ' min' : ''}`;
                        lifecycleBadges += `<span class="sl-badge bounce" title="${_clBrTip}">🔄BR${_clBlockCount > 0 ? '(' + _clBlockCount + ')' : ''}</span>`;
                    }
                    if (!lifecycleBadges) lifecycleBadges = '<span class="sl-badge none">—</span>';

                    return `
                    <tr class="${rowCls}" onclick="showPositionDetailModal('${p.symbol}', '${p.position_id || ''}', 'closed')" style="cursor:pointer">
                        <td>${typeBadge} ${catIcon(p.category || 'MIDCAP_ALT')} <strong>${p.symbol}</strong></td>
                        <td>$${fmt(p.entry_price)} → $${fmt(p.exit_price)}</td>
                        <td class="${pnlClass(actualPnl)}">${actualPnl >= 0 ? '+' : ''}${fmt(actualPnl)} USDT<br><small>${fmt(p.pnl_percentage)}%</small></td>
                        <td>${fmtCloseReason(p.close_reason)}</td>
                        <td class="sl-tp-badges">${lifecycleBadges}</td>
                        <td>${exitTimeFmt}</td>
                    </tr>`;
                }).join('')}
            </tbody>
        </table>
        </div>
        <div class="positions-summary">
            ${sortedPositions.length} trades${_allClosed.length !== filteredClosed.length ? ` (de ${_allClosed.length} total)` : ''} cerrados (últimos ${periodDays} días) &nbsp;|&nbsp;
            PnL Total: <strong class="${pnlClass(sortedPositions.reduce((s,p) => s + parseFloat(p.pnl||0), 0))}">${sortedPositions.reduce((s,p) => s + parseFloat(p.pnl||0), 0) >= 0 ? '+' : ''}${fmt(sortedPositions.reduce((s,p) => s + parseFloat(p.pnl||0), 0))} USDT</strong>
            ${withPred.length > 0 ? `&nbsp;|&nbsp;Precisión Pred. Promedio: ${fmtAccuracyFromField(avgAccuracy)}` : ''}
        </div>
    `;
}

// ── Card: Expectativa vs Realidad ────────────────────────────────────────────
function renderExpectativaVsRealidad(data) {
    const el = document.getElementById('expectativaVsRealidadData');
    if (!el) return;
    
    const openPos = data?.open_positions?.positions || [];
    const closedPos = data?.closed_positions?.positions || [];
    
    // Combinar últimas posiciones (abiertas + cerradas recientes)
    const recentOpen = openPos.slice(0, 5).map(p => ({...p, status: 'open'}));
    const recentClosed = closedPos.slice(0, 5).map(p => ({...p, status: 'closed'}));
    const allPositions = [...recentOpen, ...recentClosed].slice(0, 6);
    
    if (allPositions.length === 0) {
        el.innerHTML = '<p class="loading">Sin datos de trades recientes</p>';
        return;
    }
    
    // Calcular métricas consolidadas
    const withPred = allPositions.filter(p => p.pnl_prediction);
    const totalExpected = withPred.reduce((s, p) => s + parseFloat(p.pnl_prediction?.expected_pnl || 0), 0);
    const totalActual = allPositions.reduce((s, p) => s + parseFloat(p.unrealized_pnl || p.pnl || 0), 0);
    const accuracy = withPred.length > 0 ? (totalActual / totalExpected) * 100 : 0;
    
    el.innerHTML = `
        <div class="expectativa-stats">
            <div class="stat-box">
                <div class="stat-label">🎯 Predicciones</div>
                <div class="stat-value">${withPred.length}</div>
                <div class="stat-sub">${withPred.filter(p => (p.unrealized_pnl || p.pnl || 0) >= 0).length} ganando</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">💰 Esperado</div>
                <div class="stat-value ${totalExpected >= 0 ? 'positive' : 'negative'}">${totalExpected >= 0 ? '+' : ''}${fmt(totalExpected)}</div>
                <div class="stat-sub">USDT esperados</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">📈 Real</div>
                <div class="stat-value ${totalActual >= 0 ? 'positive' : 'negative'}">${totalActual >= 0 ? '+' : ''}${fmt(totalActual)}</div>
                <div class="stat-sub">USDT obtenidos</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">🎯 Accuracy</div>
                <div class="stat-value">${Math.abs(accuracy).toFixed(0)}%</div>
                <div class="stat-sub">${accuracy >= 80 ? 'Preciso 🎯' : accuracy >= 50 ? 'Bueno ⚡' : 'Aprendiendo 📚'}</div>
            </div>
        </div>
        <div class="expectativa-table-wrap">
            <table class="expectativa-table">
                <thead>
                    <tr>
                        <th>Símbolo</th>
                        <th>Esperado</th>
                        <th>Progreso</th>
                        <th>Real/Actual</th>
                        <th>Diff</th>
                    </tr>
                </thead>
                <tbody>
                    ${allPositions.map(p => {
                        const expected = parseFloat(p.pnl_prediction?.expected_pnl || 0);
                        const actual = parseFloat(p.unrealized_pnl || p.pnl || 0);
                        const diff = expected !== 0 ? actual - expected : 0;
                        const diffPct = expected !== 0 ? (diff / Math.abs(expected)) * 100 : 0;
                        
                        // Progreso visual
                        let progressBar = '';
                        if (p.status === 'open' && p.take_profit && p.entry_price) {
                            const tp = parseFloat(p.take_profit);
                            const entry = parseFloat(p.entry_price);
                            const cur = parseFloat(p.current_price || entry);
                            const pct = Math.min(100, Math.max(0, ((cur - entry) / (tp - entry)) * 100));
                            progressBar = `<div class="mini-progress"><div class="mini-fill" style="width:${pct}%"></div></div>`;
                        } else if (p.status === 'closed') {
                            progressBar = '<span class="badge-closed">✓ Cerrado</span>';
                        }
                        
                        const _evMeta = p.metadata || {};
                        const _evEscIdx = _evMeta.escalonamiento_index;
                        const _evEscTotal = _evMeta.total_escalonamientos;
                        const _evEscBadge = (_evEscIdx > 1)
                            ? ` <span class="badge-esc" title="Escalamiento ${_evEscIdx}/${_evEscTotal || _evEscIdx}">+${_evEscIdx}</span>`
                            : '';
                        return `<tr>
                            <td><strong>${p.symbol}</strong>${_evEscBadge} <small class="status-${p.status}">${p.status === 'open' ? '🟢' : '⚫'}</small></td>
                            <td class="${expected >= 0 ? 'positive' : 'negative'}">${expected >= 0 ? '+' : ''}${fmt(expected)}</td>
                            <td>${progressBar}</td>
                            <td class="${actual >= 0 ? 'positive' : 'negative'}">${actual >= 0 ? '+' : ''}${fmt(actual)}</td>
                            <td class="${diff >= 0 ? 'positive' : 'negative'}">${diff >= 0 ? '↑' : '↓'} ${Math.abs(diff).toFixed(2)}</td>
                        </tr>`;
                    }).join('')}
                </tbody>
            </table>
        </div>
        <div class="expectativa-explain">
            <small>🧠 <strong>¿Qué es "PnL Pred."?</strong> Es lo que el modelo de IA esperaba ganar/perder al momento de comprar, basado en indicadores técnicos. Compara con el resultado real para medir precisión.</small>
        </div>
    `;
}

// ── Card: Auditoría VolSpike Override ─────────────────────────────────────────
async function renderVolSpikeAudit() {
    const el = document.getElementById('volSpikeAuditData');
    if (!el) return;
    
    try {
        const res = await fetch(`${API_BASE}/metrics/override-performance`);
        if (!res.ok) throw new Error('Failed to fetch');
        const d = await res.json();
        
        if (d.error || !d.summary) {
            el.innerHTML = `<p class="loading">Sin datos de auditoría</p>`;
            return;
        }
        
        const s = d.summary;
        const bySym = d.by_symbol || {};
        const recent = d.recent_trades || [];
        const recs = d.recommendations || [];
        
        // Determinar estado visual
        const statusClass = s.win_rate >= 50 ? 'positive' : s.win_rate >= 35 ? 'neutral' : 'negative';
        const statusIcon = s.win_rate >= 50 ? '✅' : s.win_rate >= 35 ? '⚡' : '🚨';
        
        el.innerHTML = `
            <div class="volspike-header">
                <div class="volspike-status ${statusClass}">
                    <span class="vs-icon">${statusIcon}</span>
                    <span class="vs-winrate">${s.win_rate}% WR</span>
                    <span class="vs-sub">${s.total_trades} trades override</span>
                </div>
                <div class="volspike-limits">
                    <span class="vs-limit ${s.active_positions >= s.positions_limit ? 'at-limit' : ''}">
                        Pos: ${s.active_positions}/${s.positions_limit}
                    </span>
                    <span class="vs-limit ${s.todays_count >= s.daily_limit ? 'at-limit' : ''}">
                        Hoy: ${s.todays_count}/${s.daily_limit}
                    </span>
                </div>
            </div>
            
            ${Object.keys(bySym).length > 0 ? `
            <div class="volspike-symbols">
                <table class="volspike-table">
                    <thead>
                        <tr><th>Símbolo</th><th>Trades</th><th>WR</th><th>PnL</th><th>Status</th></tr>
                    </thead>
                    <tbody>
                        ${Object.entries(bySym).map(([sym, stats]) => {
                            const symStatus = stats.win_rate >= 50 ? '✅' : stats.win_rate >= 30 ? '⚡' : '⚠️';
                            const symClass = stats.win_rate >= 50 ? 'positive' : stats.win_rate >= 30 ? 'neutral' : 'negative';
                            return `<tr class="${symClass}">
                                <td><strong>${sym}</strong></td>
                                <td>${stats.trades}</td>
                                <td>${stats.win_rate}%</td>
                                <td class="${stats.total_pnl >= 0 ? 'positive' : 'negative'}">${stats.total_pnl >= 0 ? '+' : ''}${fmt(stats.total_pnl)}</td>
                                <td>${symStatus}</td>
                            </tr>`;
                        }).join('')}
                    </tbody>
                </table>
            </div>
            ` : '<p class="loading">Sin trades override registrados todavía</p>'}
            
            ${recent.length > 0 ? `
            <div class="volspike-recent">
                <h4>Últimos Overrides</h4>
                <div class="vs-recent-list">
                    ${recent.slice(0, 5).map(t => `
                        <div class="vs-recent-item ${t.actual >= 0 ? 'positive' : 'negative'}">
                            <span class="vs-sym">${t.symbol}</span>
                            <span class="vs-exp">Esp: ${t.expected >= 0 ? '+' : ''}${fmt(t.expected)}</span>
                            <span class="vs-act">Real: ${t.actual >= 0 ? '+' : ''}${fmt(t.actual)}</span>
                            <span class="vs-acc">${fmtAccuracyFromField(t.accuracy)}</span>
                        </div>
                    `).join('')}
                </div>
            </div>
            ` : ''}
            
            ${recs.length > 0 ? `
            <div class="volspike-recs">
                <h4>Recomendaciones</h4>
                <ul>
                    ${recs.map(r => `<li>${r}</li>`).join('')}
                </ul>
            </div>
            ` : ''}
        `;
    } catch (e) {
        el.innerHTML = `<p class="loading">Error cargando auditoría: ${e.message}</p>`;
    }
}

// ── Modal: Detalle de Posición ────────────────────────────────────────────────
function showPositionDetailModal(symbol, positionId, status) {
    // Obtener datos del cache
    const data = _cachedData;
    if (!data) return;
    
    const positions = status === 'open' 
        ? (data.open_positions?.positions || [])
        : (data.closed_positions?.positions || []);
    
    const position = positions.find(p => p.symbol === symbol && (p.position_id === positionId || !positionId));
    if (!position) return;
    
    const pred = position.pnl_prediction || {};
    const indicators = pred.indicators_used || {};
    
    // Crear modal
    const modalId = 'positionDetailModal';
    let modal = document.getElementById(modalId);
    if (!modal) {
        modal = document.createElement('div');
        modal.id = modalId;
        modal.className = 'modal-detail';
        document.body.appendChild(modal);
    }
    
    const expectedPnl = parseFloat(pred.expected_pnl || 0);
    const actualPnl = parseFloat(position.unrealized_pnl || position.pnl || 0);
    const diff = actualPnl - expectedPnl;
    
    // Helper: format ISO date in a readable local format for modal
    const fmtModalDate = (isoStr) => {
        if (!isoStr) return '—';
        try {
            return new Date(isoStr).toLocaleString('es-MX', {
                timeZone: 'America/Mexico_City',
                day: '2-digit', month: 'short', year: 'numeric',
                hour: '2-digit', minute: '2-digit', hour12: false
            });
        } catch(e) { return isoStr; }
    };

    // Build escalamientos table if entries exist
    const _modalEntries = position.entries || [];
    const _modalMaxIdx = _modalEntries.length > 0
        ? Math.max(..._modalEntries.map(e => e.escalonamiento_index || 1)) : 1;
    const _showEscTable = _modalEntries.length > 1 || _modalMaxIdx >= 2;
    const _escTableHtml = _showEscTable ? (() => {
        const _knownModalIdx = new Set(_modalEntries.map(e => e.escalonamiento_index || 1));
        const totalCost = _modalEntries.reduce((s, e) => s + parseFloat(e.entry_price||0)*parseFloat(e.qty||0), 0);
        const totalPnlEsc = _modalEntries.reduce((s, e) => s + parseFloat(e.pnl_usdt||0), 0);
        // Rows: placeholder for missing indices + real entries
        let rows = '';
        for (let _mi = 1; _mi < _modalMaxIdx; _mi++) {
            if (!_knownModalIdx.has(_mi)) {
                rows += `<tr style="opacity:0.4">
                <td style="color:#888;font-weight:600">Esc ${_mi}</td>
                <td colspan="7" style="color:#555;font-style:italic;font-size:11px">Entrada anterior — no disponible tras reinicio (Binance promedia posición)</td>
            </tr>`;
            }
        }
        rows += _modalEntries.map(e => {
            const ep = parseFloat(e.entry_price||0), qty = parseFloat(e.qty||0);
            const cost = ep * qty;
            const pnl = parseFloat(e.pnl_usdt||0);
            const pct = parseFloat(e.pnl_pct||0);
            const age = parseFloat(e.age_h||0);
            const ageStr = age < 1 ? '<1h' : age < 24 ? age.toFixed(0)+'h' : (age/24).toFixed(1)+'d';
            return `<tr>
                <td style="color:#f39c12;font-weight:600">Esc ${e.escalonamiento_index}</td>
                <td>$${fmt(ep)}</td>
                <td>${fmt(qty,4)}</td>
                <td style="color:#888">~$${fmt(cost)}</td>
                <td class="${pnl>=0?'positive':'negative'}" style="font-weight:600">${pnl>=0?'+':''}${fmt(pnl)}</td>
                <td class="${pct>=0?'positive':'negative'}">${pct>=0?'+':''}${fmt(pct)}%</td>
                <td style="color:#888;font-size:11px">${fmtEntryTime(e.opened_at, age)}</td>
                <td style="color:#888">${ageStr}</td>
            </tr>`;
        }).join('');
        return `<div class="modal-section">
            <h4>📊 ESCALAMIENTOS (${_modalEntries.length} activas, idx máx ${_modalMaxIdx})</h4>
            <div style="overflow-x:auto">
            <table style="width:100%;font-size:12px;border-collapse:collapse">
                <thead><tr style="color:#888;border-bottom:1px solid #333">
                    <th style="text-align:left;padding:4px 6px">Entrada</th>
                    <th style="text-align:left;padding:4px 6px">Precio</th>
                    <th style="text-align:left;padding:4px 6px">Qty</th>
                    <th style="text-align:left;padding:4px 6px">Costo</th>
                    <th style="text-align:left;padding:4px 6px">PnL USDT</th>
                    <th style="text-align:left;padding:4px 6px">PnL %</th>
                    <th style="text-align:left;padding:4px 6px">Apertura</th>
                    <th style="text-align:left;padding:4px 6px">Edad</th>
                </tr></thead>
                <tbody>${rows}</tbody>
                <tfoot><tr style="border-top:1px solid #444;font-weight:600;color:#ccc">
                    <td colspan="3" style="padding:4px 6px">TOTAL (entradas disponibles)</td>
                    <td style="padding:4px 6px;color:#888">~$${fmt(totalCost)}</td>
                    <td style="padding:4px 6px" class="${totalPnlEsc>=0?'positive':'negative'}">${totalPnlEsc>=0?'+':''}${fmt(totalPnlEsc)}</td>
                    <td colspan="3"></td>
                </tr></tfoot>
            </table>
            </div>
        </div>`;
    })() : '';

    modal.innerHTML = `
        <div class="modal-overlay" onclick="closePositionDetailModal()">
            <div class="modal-content" onclick="event.stopPropagation()">
                <div class="modal-header">
                    <h3>${symbol} — Detalle de ${status === 'open' ? 'Posición Abierta' : 'Trade Cerrado'}</h3>
                    <button class="modal-close" onclick="closePositionDetailModal()">×</button>
                </div>
                <div class="modal-body">
                    <div class="modal-section">
                        <h4>📍 ENTRADA</h4>
                        <div class="detail-grid">
                            <div><label>Precio:</label><span>$${fmt(position.entry_price)}</span></div>
                            <div><label>Cantidad:</label><span>${fmt(position.quantity, 4)}</span></div>
                            <div><label>Invertido:</label><span>$${fmt(position.invested_usdt || (position.entry_price * position.quantity))} USDT</span></div>
                            <div><label>Apertura:</label><span>${fmtModalDate(position.entry_time)}</span></div>
                        </div>
                    </div>
                    ${_escTableHtml}
                    
                    <div class="modal-section">
                        <h4>🎯 PREDICCIÓN AL ENTRAR</h4>
                        <div class="detail-grid">
                            <div><label>PnL Esperado:</label><span class="${expectedPnl >= 0 ? 'positive' : 'negative'}">${expectedPnl >= 0 ? '+' : ''}${fmt(expectedPnl)} USDT</span></div>
                            <div><label>Probabilidad:</label><span>${(parseFloat(pred.success_probability || 0) * 100).toFixed(0)}%</span></div>
                            <div><label>Rango Precio:</label><span>$${fmt(pred.price_range?.min)} — $${fmt(pred.price_range?.max)}</span></div>
                            <div><label>Modo:</label><span>${pred.bootstrap_mode ? '📚 Aprendizaje' : '🎯 Entrenado'}</span></div>
                        </div>
                    </div>
                    
                    <div class="modal-section">
                        <h4>📊 INDICADORES AL ENTRAR</h4>
                        <div class="detail-grid">
                            <div><label>RSI 4h:</label><span>${fmt(indicators.rsi, 1)}</span></div>
                            <div><label>EMA9 > EMA21:</label><span>${indicators.ema9 > indicators.ema21 ? '✓ Sí' : '✗ No'}</span></div>
                            <div><label>Vol Spike:</label><span>${fmt(indicators.vol_spike, 1)}x</span></div>
                            <div><label>BB Position:</label><span>${fmt(indicators.bb_position, 1)}%</span></div>
                        </div>
                    </div>
                    
                    <div class="modal-section">
                        <h4>📊 COMPORTAMIENTO DEL PRECIO</h4>
                        <div class="modal-price-chart" id="spotChartWrap">
                            <div style="text-align:center;color:#888;padding:20px;">Cargando datos históricos...</div>
                        </div>
                    </div>

                    <div class="modal-section">
                        <h4>🛡️ SL/TP LIFECYCLE</h4>
                        <div class="detail-grid sl-tp-lifecycle">
                            <div><label>Stop Loss Actual:</label><span class="negative">$${fmt(position.stop_loss)} (${position.sl_dist_pct != null ? (position.sl_dist_pct >= 0 ? '+' : '') + position.sl_dist_pct.toFixed(1) + '%' : '—'})</span></div>
                            <div><label>Take Profit Actual:</label><span class="positive">$${fmt(position.take_profit)} (${position.tp_dist_pct != null ? (position.tp_dist_pct >= 0 ? '+' : '') + position.tp_dist_pct.toFixed(1) + '%' : '—'})</span></div>
                            ${pred.stop_loss_initial ? `<div><label>SL Inicial:</label><span class="negative">$${fmt(pred.stop_loss_initial)}${position.stop_loss && pred.stop_loss_initial ? (position.stop_loss > pred.stop_loss_initial ? ' → <span class="positive">↑ mejoró</span>' : position.stop_loss < pred.stop_loss_initial ? ' → <span class="negative">↓</span>' : ' → sin cambio') : ''}</span></div>` : ''}
                            ${pred.take_profit_initial ? `<div><label>TP Inicial:</label><span class="positive">$${fmt(pred.take_profit_initial)}</span></div>` : ''}
                        </div>
                        <div class="detail-grid sl-tp-lifecycle" style="margin-top:8px;border-top:1px solid #333;padding-top:8px;">
                            <div><label>Trailing:</label><span class="${position.trailing_active ? 'positive' : ''}">${position.trailing_active ? '📈 Activo' : '⏸️ Inactivo'}${position.trailing_peak ? ' · Peak: $' + fmt(position.trailing_peak) : ''}</span></div>
                            <div><label>Breakeven:</label><span class="${position.breakeven_done ? 'positive' : ''}">${position.breakeven_done ? '🛡️ Activado' : '—'}</span></div>
                            <div><label>SmartTP Ext.:</label><span>${position.tp_extensions > 0 ? '🔄 ×' + position.tp_extensions : '—'}</span></div>
                            <div><label>Scale-Out:</label><span>${position.scale_out_done ? '✂️ Ejecutado' : '—'}</span></div>
                            ${position.dynamic_trail_dist_pct ? `<div><label>Trail Dist. Dinámica:</label><span>${position.dynamic_trail_dist_pct}%</span></div>` : ''}
                        </div>
                        ${(position.tp_decisions && position.tp_decisions.length > 0) ? `
                        <div style="margin-top:8px;border-top:1px solid #333;padding-top:8px;">
                            <label style="font-weight:600;color:#ccc;">Decisiones TP:</label>
                            <div class="tp-decisions-list">
                                ${position.tp_decisions.map(d => `<div class="tp-decision-item">Nivel ${d.tp_level || '?'}: ${d.action || '?'} @ $${fmt(d.price || 0)} <small>(score: ${d.momentum_score ?? '—'})</small></div>`).join('')}
                            </div>
                        </div>` : ''}
                    </div>
                    
                    <div class="modal-section">
                        <h4>💰 ${status === 'open' ? 'RESULTADO ACTUAL' : 'RESULTADO FINAL'}</h4>
                        <div class="detail-grid">
                            <div><label>Precio ${status === 'open' ? 'Actual' : 'Salida'}:</label><span>$${fmt(status === 'open' ? position.current_price : position.exit_price)}</span></div>
                            <div><label>PnL Real:</label><span class="${actualPnl >= 0 ? 'positive' : 'negative'}">${actualPnl >= 0 ? '+' : ''}${fmt(actualPnl)} USDT</span></div>
                            <div><label>vs Esperado:</label><span class="${diff >= 0 ? 'positive' : 'negative'}">${diff >= 0 ? '↑' : '↓'} ${Math.abs(diff).toFixed(2)} USDT</span></div>
                            <div><label>Accuracy:</label><span>${fmtAccuracy(actualPnl, expectedPnl)}</span></div>
                        </div>
                    </div>
                    
                    ${position.bounce_entry ? `
                    <div class="modal-section modal-bounce-rescue">
                        <h4>🔄 BOUNCE RESCUE STATUS</h4>
                        <div class="detail-grid">
                            ${ (position.bounce_gate_blocked_count || 0) > 0
                                ? `<div><label>Estado Gate:</label><span style="color:#e67e22;font-weight:700">⏸️ BLOQUEADO (${position.bounce_gate_blocked_count}x)</span></div>`
                                : `<div><label>Estado Gate:</label><span style="color:#2ecc71">✅ Listo para rescue</span></div>` }
                            ${ position.bounce_gate_last_signal ? `<div><label>Última señal:</label><span>${position.bounce_gate_last_signal}${ position.bounce_gate_last_signal_strength != null ? ' — ' + (position.bounce_gate_last_signal_strength * 100).toFixed(0) + '%' : ''}</span></div>` : '' }
                            ${ position.bounce_gate_last_ev != null ? `<div><label>EV predicción:</label><span class="${ position.bounce_gate_last_ev >= 0 ? 'positive' : 'negative'}">${position.bounce_gate_last_ev >= 0 ? '+' : ''}$${position.bounce_gate_last_ev.toFixed(2)} USDT</span></div>` : '' }
                            ${ position.bounce_gate_last_velocity != null ? `<div><label>Velocidad PnL:</label><span>${(position.bounce_gate_last_velocity * 100).toFixed(3)}%/h</span></div>` : '' }
                            ${ position.bounce_gate_first_qualified_at ? `<div><label>Primer calificado:</label><span>${fmtModalDate(position.bounce_gate_first_qualified_at)}</span></div>` : '' }
                            ${ position.bounce_gate_blocked_reason ? `<div style="grid-column:1/-1"><label>Motivo bloqueo:</label><span style="font-size:11px;color:#aaa">${position.bounce_gate_blocked_reason}</span></div>` : '' }
                        </div>
                    </div>
                    ` : ''}
                    ${status === 'closed' ? `
                    <div class="modal-section">
                        <h4>🔚 CIERRE</h4>
                        <div class="detail-grid">
                            <div><label>Razón:</label><span>${position.close_reason || '—'}</span></div>
                            <div><label>Fecha salida:</label><span>${fmtModalDate(position.exit_time)}</span></div>
                        </div>
                    </div>
                    ` : ''}
                </div>
            </div>
        </div>
    `;
    
    modal.style.display = 'block';
    document.body.style.overflow = 'hidden';
    
    // Load time series chart for closed positions or use range chart for open
    const chartWrap = document.getElementById('spotChartWrap');
    if (status === 'closed' && position.entry_time && position.exit_time) {
        // Load historical data for closed positions
        loadPriceHistory(symbol, position.entry_time, position.exit_time).then(priceData => {
            if (priceData && priceData.data && chartWrap) {
                const chart = buildTimeSeriesChart(
                    priceData.data,
                    position.entry_price,
                    position.exit_price,
                    pred.stop_loss_initial || position.stop_loss,
                    pred.take_profit_initial || position.take_profit,
                    position.stop_loss,
                    position.take_profit,
                    position.quantity
                );
                chartWrap.innerHTML = chart.html;
                attachTimeSeriesChartHover(chart);
            } else if (chartWrap) {
                // Fallback to range chart
                const _sc = buildPriceRangeChart(
                    position.entry_price,
                    position.exit_price,
                    position.stop_loss, position.take_profit, position.side || 'LONG',
                    position.entries || []);
                chartWrap.innerHTML = _sc.html;
                attachChartHover(_sc.uid, _sc.entry, _sc.isShort, _sc.rangeMin, _sc.rangeSpan, _sc.chartL, _sc.chartW, position.quantity);
            }
        });
    } else {
        // Use range chart for open positions or if no timestamps
        const _sc = buildPriceRangeChart(
            position.entry_price,
            status === 'open' ? position.current_price : position.exit_price,
            position.stop_loss, position.take_profit, position.side || 'LONG',
            position.entries || []);
        if (chartWrap && _sc.html) {
            chartWrap.innerHTML = _sc.html;
            attachChartHover(_sc.uid, _sc.entry, _sc.isShort, _sc.rangeMin, _sc.rangeSpan, _sc.chartL, _sc.chartW, position.quantity);
        }
    }
}

function closePositionDetailModal() {
    const modal = document.getElementById('positionDetailModal');
    if (modal) {
        modal.style.display = 'none';
        document.body.style.overflow = '';
    }
}

// ── Time Series Chart SVG (with hover tooltip) ───────────────────────────────
let _chartUid = 0;

function buildTimeSeriesChart(priceData, entryPrice, exitPrice, slInitial, tpInitial, slFinal, tpFinal, qty) {
    const uid = ++_chartUid;
    const W = 620;
    const H = 180;
    const padding = { top: 20, right: 60, bottom: 30, left: 60 };
    const chartW = W - padding.left - padding.right;
    const chartH = H - padding.top - padding.bottom;
    
    if (!priceData || priceData.length === 0) {
        return {
            html: `<div style="width:${W}px;height:${H}px;display:flex;align-items:center;justify-content:center;color:#888;">
                <span>Sin datos históricos disponibles</span>
            </div>`,
            uid, entry: entryPrice, isShort: false, rangeMin: 0, rangeSpan: 0, chartL: 0, chartW: 0
        };
    }
    
    // Extract price data
    const prices = priceData.map(d => d.close);
    const minPrice = Math.min(...prices, entryPrice, exitPrice, slInitial || entryPrice, tpInitial || entryPrice, slFinal || entryPrice, tpFinal || entryPrice);
    const maxPrice = Math.max(...prices, entryPrice, exitPrice, slInitial || entryPrice, tpInitial || entryPrice, slFinal || entryPrice, tpFinal || entryPrice);
    const priceRange = maxPrice - minPrice || 1;
    
    // Time range
    const minTime = priceData[0].timestamp;
    const maxTime = priceData[priceData.length - 1].timestamp;
    const timeRange = maxTime - minTime || 1;
    
    // Scale functions
    const xScale = (t) => padding.left + ((t - minTime) / timeRange) * chartW;
    const yScale = (p) => padding.top + chartH - ((p - minPrice) / priceRange) * chartH;
    
    // Build SVG path for price line
    let pathData = priceData.map((d, i) => {
        const x = xScale(d.timestamp);
        const y = yScale(d.close);
        return `${i === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${y.toFixed(1)}`;
    }).join(' ');
    
    // Find closest data points for markers
    const findClosestDataPoint = (targetPrice, targetTime) => {
        let closest = priceData[0];
        let minDist = Infinity;
        
        for (const d of priceData) {
            const priceDist = Math.abs(d.close - targetPrice);
            const timeDist = Math.abs(d.timestamp - targetTime);
            const dist = priceDist + (timeDist / timeRange) * priceRange * 0.1; // Weight time less
            
            if (dist < minDist) {
                minDist = dist;
                closest = d;
            }
        }
        return closest;
    };
    
    const entryPoint = findClosestDataPoint(entryPrice, minTime);
    const exitPoint = findClosestDataPoint(exitPrice, maxTime);
    
    // Generate SVG
    const svg = `
    <svg id="prc-svg-${uid}" width="${W}" height="${H}" style="background:#1a1a1a;border-radius:8px;">
        <!-- Grid lines -->
        ${[0, 25, 50, 75, 100].map(pct => {
            const y = padding.top + (pct / 100) * chartH;
            const price = maxPrice - (pct / 100) * priceRange;
            return `
                <line x1="${padding.left}" y1="${y}" x2="${W - padding.right}" y2="${y}" 
                      stroke="#333" stroke-width="0.5"/>
                <text x="${padding.left - 5}" y="${y + 3}" text-anchor="end" 
                      fill="#666" font-size="10">$${price.toFixed(entryPrice > 10 ? 2 : 4)}</text>
            `;
        }).join('')}
        
        <!-- Price line -->
        <path d="${pathData}" fill="none" stroke="#4ecca3" stroke-width="1.5" opacity="0.8"/>
        
        <!-- Entry marker -->
        <circle cx="${xScale(entryPoint.timestamp).toFixed(1)}" cy="${yScale(entryPrice).toFixed(1)}" 
                r="4" fill="#888" stroke="white" stroke-width="1">
            <title>Entrada: $${fmt(entryPrice)}</title>
        </circle>
        
        <!-- Exit/Current marker -->
        <circle cx="${xScale(exitPoint.timestamp).toFixed(1)}" cy="${yScale(exitPrice).toFixed(1)}" 
                r="4" fill="${exitPrice >= entryPrice ? '#4ecca3' : '#e74c3c'}" stroke="white" stroke-width="1">
            <title>${exitPrice >= entryPrice ? 'Salida' : 'Actual'}: $${fmt(exitPrice)}</title>
        </circle>
        
        <!-- SL markers -->
        ${slInitial && slInitial !== entryPrice ? `
            <line x1="${padding.left}" y1="${yScale(slInitial).toFixed(1)}" 
                  x2="${W - padding.right}" y2="${yScale(slInitial).toFixed(1)}" 
                  stroke="#e74c3c" stroke-width="1" stroke-dasharray="5,5" opacity="0.6"/>
            <text x="${W - padding.right + 5}" y="${yScale(slInitial).toFixed(1) + 3}" 
                  fill="#e74c3c" font-size="10">SL Inicial</text>
        ` : ''}
        
        ${slFinal && slFinal !== slInitial ? `
            <line x1="${padding.left}" y1="${yScale(slFinal).toFixed(1)}" 
                  x2="${W - padding.right}" y2="${yScale(slFinal).toFixed(1)}" 
                  stroke="#e74c3c" stroke-width="1.5" opacity="0.8"/>
            <text x="${W - padding.right + 5}" y="${yScale(slFinal).toFixed(1) + 3}" 
                  fill="#e74c3c" font-size="10">SL Final</text>
        ` : ''}
        
        <!-- TP markers -->
        ${tpInitial && tpInitial !== entryPrice ? `
            <line x1="${padding.left}" y1="${yScale(tpInitial).toFixed(1)}" 
                  x2="${W - padding.right}" y2="${yScale(tpInitial).toFixed(1)}" 
                  stroke="#4ecca3" stroke-width="1" stroke-dasharray="5,5" opacity="0.6"/>
            <text x="${W - padding.right + 5}" y="${yScale(tpInitial).toFixed(1) + 3}" 
                  fill="#4ecca3" font-size="10">TP Inicial</text>
        ` : ''}
        
        ${tpFinal && tpFinal !== tpInitial ? `
            <line x1="${padding.left}" y1="${yScale(tpFinal).toFixed(1)}" 
                  x2="${W - padding.right}" y2="${yScale(tpFinal).toFixed(1)}" 
                  stroke="#4ecca3" stroke-width="1.5" opacity="0.8"/>
            <text x="${W - padding.right + 5}" y="${yScale(tpFinal).toFixed(1) + 3}" 
                  fill="#4ecca3" font-size="10">TP Final</text>
        ` : ''}
        
        <!-- Hover line (initially hidden) -->
        <line id="prc-ln-${uid}" x1="0" y1="0" x2="0" y2="${H}" 
              stroke="#ffd700" stroke-width="1" opacity="0.8" style="display:none"/>
        
        <!-- Tooltip -->
        <div id="prc-tip-${uid}" class="price-tooltip" style="display:none;"></div>
    </svg>`;
    
    return {
        html: svg,
        uid, 
        entry: entryPrice, 
        isShort: false, 
        rangeMin: minPrice, 
        rangeSpan: priceRange, 
        chartL: padding.left, 
        chartW: chartW,
        priceData: priceData,
        exitPrice: exitPrice,
        slInitial: slInitial,
        tpInitial: tpInitial,
        slFinal: slFinal,
        tpFinal: tpFinal,
        qty: qty
    };
}

// ── Price Range Chart SVG (legacy, kept for compatibility) ───────────────────────

// Load price history data from API
async function loadPriceHistory(symbol, startTime, endTime) {
    try {
        const response = await fetch(`${API_BASE}/price-history/${symbol}?start_time=${startTime}&end_time=${endTime}`, {
            headers: _authHeaders()
        });
        if (!response.ok) return null;
        const data = await response.json();
        return data.error ? null : data;
    } catch (e) {
        console.error('Error loading price history:', e);
        return null;
    }
}

// Enhanced attachChartHover for time series charts
function attachTimeSeriesChartHover(chartData) {
    const { uid, entry, priceData, exitPrice, slInitial, tpInitial, slFinal, tpFinal, qty } = chartData;
    console.log('[attachTimeSeriesChartHover] uid=' + uid + ' entry=' + entry + ' dataPoints=' + (priceData?.length || 0));
    
    const svg = document.getElementById('prc-svg-' + uid);
    const tip = document.getElementById('prc-tip-' + uid);
    const ln  = document.getElementById('prc-ln-'  + uid);
    
    if (!svg || !tip || !ln || !priceData || priceData.length === 0) {
        console.warn('[attachTimeSeriesChartHover] Missing elements or data');
        return;
    }
    
    const W = 620;
    const padding = { top: 20, right: 60, bottom: 30, left: 60 };
    const chartW = W - padding.left - padding.right;
    
    // Time range for scaling
    const minTime = priceData[0].timestamp;
    const maxTime = priceData[priceData.length - 1].timestamp;
    const timeRange = maxTime - minTime || 1;
    
    svg.addEventListener('mousemove', function(e) {
        const rect = svg.getBoundingClientRect();
        const scale = (rect.width || W) / W;
        const mx = (e.clientX - rect.left) / scale;
        
        // Check if within chart area
        if (mx < padding.left || mx > padding.left + chartW) {
            tip.style.display = 'none'; ln.style.display = 'none'; return;
        }
        
        // Find corresponding time and price
        const timeRatio = (mx - padding.left) / chartW;
        const targetTime = minTime + (timeRatio * timeRange);
        
        // Find closest data point
        let closestPoint = priceData[0];
        let minDist = Math.abs(priceData[0].timestamp - targetTime);
        
        for (let i = 1; i < priceData.length; i++) {
            const dist = Math.abs(priceData[i].timestamp - targetTime);
            if (dist < minDist) {
                minDist = dist;
                closestPoint = priceData[i];
            }
        }
        
        const hoverPrice = closestPoint.close;
        const pnlEstUnit = exitPrice >= entry ? (hoverPrice - entry) : (entry - hoverPrice);
        const pnlEst = pnlEstUnit * qty;
        const pnlPct = entry > 0 ? (pnlEstUnit / entry * 100) : 0;
        const col = pnlEst >= 0 ? '#4ecca3' : '#e74c3c';
        
        // Format time
        const timeStr = new Date(closestPoint.timestamp).toLocaleString('es-MX', {
            month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit'
        });
        
        tip.style.display = 'block';
        tip.style.left = (e.clientX + 12) + 'px';
        tip.style.top = (e.clientY - 45) + 'px';
        tip.style.borderColor = col;
        tip.innerHTML = `
            <span style="color:#aaa;font-size:10px">${timeStr}</span><br>
            <strong style="color:#fff">$${hoverPrice.toFixed(hoverPrice > 10 ? 2 : 4)}</strong><br>
            <span style="color:${col};font-weight:700">
                ${pnlEst >= 0 ? '+' : ''}${pnlEst.toFixed(4)} USDT (${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%)
            </span>
        `;
        
        ln.setAttribute('x1', mx); ln.setAttribute('x2', mx);
        ln.style.display = 'block';
    });
    
    svg.addEventListener('mouseleave', function() {
        tip.style.display = 'none'; ln.style.display = 'none';
    });
}

// Legacy attachChartHover for compatibility
function attachChartHover(uid, entry, isShort, rangeMin, rangeSpan, cL, cW, qty) {
    console.log('[attachChartHover] uid=' + uid + ' entry=' + entry + ' isShort=' + isShort + ' qty=' + qty);
    const svg = document.getElementById('prc-svg-' + uid);
    const tip = document.getElementById('prc-tip-' + uid);
    const ln  = document.getElementById('prc-ln-'  + uid);
    console.log('[attachChartHover] elements:', {svg: !!svg, tip: !!tip, ln: !!ln, 
        svgId: 'prc-svg-' + uid, tipId: 'prc-tip-' + uid, lnId: 'prc-ln-' + uid});
    if (!svg || !tip || !ln) {
        console.warn('[attachChartHover] Missing elements, aborting');
        return;
    }
    const W = 620;
    svg.addEventListener('mousemove', function(e) {
        const rect  = svg.getBoundingClientRect();
        const scale = (rect.width || W) / W;
        const mx    = (e.clientX - rect.left) / scale;
        if (mx < cL || mx > cL + cW) { tip.style.display = 'none'; ln.style.display = 'none'; return; }
        const hoverPrice = rangeMin + ((mx - cL) / cW) * rangeSpan;
        const pnlEstUnit = isShort ? (entry - hoverPrice) : (hoverPrice - entry);
        const pnlEst     = pnlEstUnit * qty;  // PnL real en USD
        const pnlPct     = entry > 0 ? (pnlEstUnit / entry * 100) : 0;  // Porcentaje basado en unit price
        const col        = pnlEst >= 0 ? '#4ecca3' : '#e74c3c';
        tip.style.display    = 'block';
        tip.style.left       = (e.clientX + 12) + 'px';
        tip.style.top        = (e.clientY - 38) + 'px';
        tip.style.borderColor = col;
        tip.innerHTML = '<span style="color:#aaa;font-size:10px">Si precio: </span>' +
            '<strong style="color:#fff">$' + hoverPrice.toFixed(hoverPrice > 10 ? 2 : 4) + '</strong><br>' +
            '<span style="color:' + col + ';font-weight:700">' +
            (pnlEst >= 0 ? '+' : '') + pnlEst.toFixed(4) + ' USDT (' +
            (pnlPct >= 0 ? '+' : '') + pnlPct.toFixed(2) + '%)</span>';
        ln.setAttribute('x1', mx); ln.setAttribute('x2', mx);
        ln.style.display = 'block';
    });
    svg.addEventListener('mouseleave', function() {
        tip.style.display = 'none'; ln.style.display = 'none';
    });
}

function buildPriceRangeChart(entry, current, sl, tp, side, entries = []) {
    entry   = parseFloat(entry)   || 0;
    current = parseFloat(current) || 0;
    sl      = parseFloat(sl)      || 0;
    tp      = parseFloat(tp)      || 0;
    if (!entry || !sl || !tp) return { html: '', uid: 0 };

    const isShort = side === 'SHORT';
    // Include escalamiento prices in range calculation
    const _escPrices = entries.map(e => parseFloat(e.entry_price || 0)).filter(p => p > 0);
    const lo  = Math.min(...[sl, tp, entry, current, ..._escPrices].filter(v => v > 0));
    const hi  = Math.max(...[sl, tp, entry, current, ..._escPrices]);
    const span = hi - lo || 1;
    const pad  = span * 0.10;
    const rangeMin = lo - pad;
    const rangeMax = hi + pad;
    const rangeSpan = rangeMax - rangeMin;

    const hasEsc = entries.length > 0 && (entries.length > 1 || (entries[0] && (entries[0].escalonamiento_index || 1) >= 2));
    const W = 620;
    // Increase height when escalamientos are present to fit labels
    const H = hasEsc ? 148 : 110;
    const chartL = 16, chartR = W - 16;
    const chartW = chartR - chartL;
    const trackY  = hasEsc ? 58 : 38;   // push track down to make room for Esc labels above
    const labelY  = trackY + 20;
    const priceY  = trackY + 34;

    function xOf(price) {
        return chartL + ((price - rangeMin) / rangeSpan) * chartW;
    }
    function priceOfX(x) {
        return rangeMin + ((x - chartL) / chartW) * rangeSpan;
    }

    const xSL  = xOf(sl);
    const xTP  = xOf(tp);
    const xEnt = xOf(entry);
    const xCur = xOf(current);

    const profitable = isShort ? current < entry : current > entry;
    const curColor   = profitable ? '#4ecca3' : '#e74c3c';
    const fillColor  = profitable ? 'rgba(78,204,163,0.18)' : 'rgba(231,76,60,0.18)';
    const fillX1 = Math.min(xEnt, xCur);
    const fillX2 = Math.max(xEnt, xCur);

    const fmtP = (v) => {
        if (v >= 10000) return '$' + (v/1000).toFixed(1) + 'k';
        if (v >= 1000)  return '$' + v.toFixed(0);
        if (v >= 100)   return '$' + v.toFixed(1);
        if (v >= 1)     return '$' + v.toFixed(2);
        return '$' + v.toFixed(4);
    };

    const slAnchor = xSL < W / 2 ? 'start' : 'end';
    const tpAnchor = xTP < W / 2 ? 'start' : 'end';
    const uid = ++_chartUid;
    const svgId  = `prc-svg-${uid}`;
    const tipId  = `prc-tip-${uid}`;
    const lineId = `prc-ln-${uid}`;

    // Escalamiento markers — one per entry, sorted by escalonamiento_index
    const _escColors = ['#f39c12','#e67e22','#d35400','#c0392b','#8e44ad'];
    const _sortedEntries = [...entries].sort((a, b) => (a.escalonamiento_index||1) - (b.escalonamiento_index||1));
    const escMarkersHtml = _sortedEntries.map((e, i) => {
        const ep = parseFloat(e.entry_price || 0);
        if (!ep) return '';
        const xE = xOf(ep);
        const col = _escColors[i % _escColors.length];
        const idx = e.escalonamiento_index || (i + 1);
        const labelAbove = trackY - 10;
        const pnl = parseFloat(e.pnl_usdt || 0);
        const pnlSign = pnl >= 0 ? '+' : '';
        const pnlTip = `Esc${idx}: $${fmtP(ep)} | PnL: ${pnlSign}${pnl.toFixed(2)} USDT`;
        return `
  <line x1="${xE.toFixed(1)}" y1="${trackY - 12}" x2="${xE.toFixed(1)}" y2="${trackY + 12}"
        stroke="${col}" stroke-width="1.5" stroke-dasharray="3,2" opacity="0.9"/>
  <circle cx="${xE.toFixed(1)}" cy="${trackY}" r="4" fill="${col}" stroke="#1a1a2e" stroke-width="1">
    <title>${pnlTip}</title>
  </circle>
  <text x="${xE.toFixed(1)}" y="${(trackY - 16).toFixed(1)}" text-anchor="middle"
        fill="${col}" font-size="9" font-weight="700">Esc${idx}</text>
  <text x="${xE.toFixed(1)}" y="${(trackY - 26).toFixed(1)}" text-anchor="middle"
        fill="${col}" font-size="8" opacity="0.8">${fmtP(ep)}</text>`;
    }).join('');

    // Entry label: show "Entrada (avg)" when multiple escalamientos exist
    const entryLabel = hasEsc ? 'Avg' : 'Entrada';

    const html = `<div style="position:relative">
<svg id="${svgId}" class="modal-chart-svg" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" style="cursor:crosshair">
  <line x1="${chartL}" y1="${trackY}" x2="${chartR}" y2="${trackY}" stroke="#333" stroke-width="3" stroke-linecap="round"/>
  <rect x="${fillX1}" y="${trackY - 6}" width="${fillX2 - fillX1}" height="12" fill="${fillColor}" rx="3"/>
  ${escMarkersHtml}
  <line x1="${xSL}" y1="${trackY - 10}" x2="${xSL}" y2="${trackY + 10}" stroke="#e74c3c" stroke-width="2"/>
  <circle cx="${xSL}" cy="${trackY}" r="6" fill="#e74c3c"/>
  <text x="${xSL}" y="${labelY}" text-anchor="${slAnchor}" fill="#e74c3c" font-size="10" font-weight="600">SL</text>
  <text x="${xSL}" y="${priceY}" text-anchor="${slAnchor}" fill="#e74c3c" font-size="9">${fmtP(sl)}</text>
  <line x1="${xTP}" y1="${trackY - 10}" x2="${xTP}" y2="${trackY + 10}" stroke="#4ecca3" stroke-width="2"/>
  <circle cx="${xTP}" cy="${trackY}" r="6" fill="#4ecca3"/>
  <text x="${xTP}" y="${labelY}" text-anchor="${tpAnchor}" fill="#4ecca3" font-size="10" font-weight="600">TP</text>
  <text x="${xTP}" y="${priceY}" text-anchor="${tpAnchor}" fill="#4ecca3" font-size="9">${fmtP(tp)}</text>
  <line x1="${xEnt}" y1="${trackY - 13}" x2="${xEnt}" y2="${trackY + 13}" stroke="#ffffff" stroke-width="2" stroke-dasharray="3,2"/>
  <circle cx="${xEnt}" cy="${trackY}" r="5" fill="#fff" stroke="#888" stroke-width="1"/>
  <text x="${xEnt}" y="${labelY}" text-anchor="middle" fill="#ccc" font-size="10" font-weight="600">${entryLabel}</text>
  <text x="${xEnt}" y="${priceY}" text-anchor="middle" fill="#ccc" font-size="9">${fmtP(entry)}</text>
  <circle cx="${xCur}" cy="${trackY}" r="7" fill="${curColor}" stroke="#1a1a2e" stroke-width="2"/>
  <text x="${xCur}" y="${trackY - 16}" text-anchor="middle" fill="${curColor}" font-size="10" font-weight="700">▼ Actual</text>
  <text x="${xCur}" y="${priceY}" text-anchor="middle" fill="${curColor}" font-size="9" font-weight="600">${fmtP(current)}</text>
  <line id="${lineId}" x1="0" y1="${trackY - 14}" x2="0" y2="${trackY + 14}" stroke="rgba(255,255,255,0.25)" stroke-width="1" stroke-dasharray="3,2" style="display:none"/>
  <text x="${W/2}" y="${H - 4}" text-anchor="middle" fill="#444" font-size="9">Mueve el mouse sobre la barra para ver PnL estimado</text>
</svg>
<div id="${tipId}" style="display:none;position:fixed;background:#1e1e2e;border:1px solid #4ecca3;border-radius:6px;padding:5px 9px;font-size:12px;pointer-events:none;z-index:9999;white-space:nowrap;box-shadow:0 4px 12px rgba(0,0,0,0.5)"></div>
</div>`;

    return { html, uid, entry, isShort, rangeMin, rangeSpan, chartL, chartW };
}

// ── Modal: Detalle Futuros ─────────────────────────────────────────────────────
function showFuturesDetailModal(symbol, side) {
    const pos = _cachedFuturesPositions.find(p => p.symbol === symbol && p.side === side);
    if (!pos) return;

    const modalId = 'positionDetailModal';
    let modal = document.getElementById(modalId);
    if (!modal) {
        modal = document.createElement('div');
        modal.id = modalId;
        modal.className = 'modal-detail';
        document.body.appendChild(modal);
    }

    const upnl     = parseFloat(pos.unrealized_pnl || 0);
    const upnlCls  = upnl >= 0 ? 'positive' : 'negative';
    const isShort  = side === 'SHORT';
    const sideLabel = isShort
        ? '<span style="color:#9b59b6;font-weight:700">▼ SHORT</span>'
        : '<span style="color:#27ae60;font-weight:700">▲ LONG</span>';
    modal.innerHTML = `
        <div class="modal-overlay" onclick="closePositionDetailModal()">
            <div class="modal-content" onclick="event.stopPropagation()">
                <div class="modal-header">
                    <h3>📉 ${symbol} — Futuro ${side} Abierto <span style="font-size:11px;color:#888">2× Leverage</span></h3>
                    <button class="modal-close" onclick="closePositionDetailModal()">×</button>
                </div>
                <div class="modal-body">
                    <div class="modal-section">
                        <h4>📍 POSICIÓN</h4>
                        <div class="detail-grid">
                            <div><label>Símbolo:</label><span style="font-weight:700">${symbol}</span></div>
                            <div><label>Lado:</label><span>${sideLabel}</span></div>
                            <div><label>Cantidad:</label><span>${parseFloat(pos.qty || 0).toFixed(4)}</span></div>
                            <div><label>Abierto:</label><span>${pos.opened_at ? relativeTime(pos.opened_at) : '—'}</span></div>
                        </div>
                    </div>
                    <div class="modal-section">
                        <h4>💵 PRECIOS</h4>
                        <div class="detail-grid">
                            <div><label>Entrada:</label><span>$${fmt(pos.entry_price)}</span></div>
                            <div><label>Actual:</label><span class="${upnlCls}">$${fmt(pos.current_price)}</span></div>
                        </div>
                    </div>
                    <div class="modal-section">
                        <h4>📊 COMPORTAMIENTO DEL PRECIO</h4>
                        <div class="modal-price-chart" id="futChartWrap"></div>
                    </div>
                    <div class="modal-section">
                        <h4>🛡️ SL/TP LIFECYCLE</h4>
                        <div class="detail-grid sl-tp-lifecycle">
                            <div><label>Stop Loss:</label><span class="negative">$${fmt(pos.sl_price)} (${pos.sl_dist_pct != null ? (pos.sl_dist_pct >= 0 ? '+' : '') + parseFloat(pos.sl_dist_pct).toFixed(1) + '%' : '—'})</span></div>
                            <div><label>Take Profit:</label><span class="positive">$${fmt(pos.tp_price)} (${pos.tp_dist_pct != null ? (pos.tp_dist_pct >= 0 ? '+' : '') + parseFloat(pos.tp_dist_pct).toFixed(1) + '%' : '—'})</span></div>
                        </div>
                        <div class="detail-grid sl-tp-lifecycle" style="margin-top:8px;border-top:1px solid #333;padding-top:8px;">
                            <div><label>Breakeven:</label><span class="${pos.breakeven_done ? 'positive' : ''}">${pos.breakeven_done ? '🛡️ Activado' : '⏸️ Pendiente'}</span></div>
                            <div><label>Trailing:</label><span class="${pos.trailing_active ? 'positive' : ''}">${pos.trailing_active ? '📈 Activo' : '⏸️ Inactivo'}${pos.trail_best_price ? ' · Best: $' + fmt(pos.trail_best_price) : ''}</span></div>
                            <div><label>Trail Dist. Dinámica:</label><span>${pos.dynamic_trail_dist_pct ? pos.dynamic_trail_dist_pct + '%' : '—'}</span></div>
                            <div><label>SmartTP Ext.:</label><span>${pos.tp_extensions > 0 ? '🔄 ×' + pos.tp_extensions : '—'}</span></div>
                        </div>
                        <div class="sl-regla-oro" style="margin-top:6px;padding:4px 8px;background:#1a2332;border-radius:4px;font-size:11px;">
                            ⚖️ <strong>REGLA DE ORO:</strong> SL nunca retrocede — ${isShort ? 'solo baja' : 'solo sube'}
                        </div>
                    </div>
                    <div class="modal-section">
                        <h4>💰 RESULTADO ACTUAL</h4>
                        <div class="detail-grid">
                            <div><label>PnL No Realizado:</label><span class="${upnlCls}" style="font-size:16px;font-weight:700">${upnl >= 0 ? '+' : ''}${fmt(upnl, 4)} USDT</span></div>
                            <div><label>Movimiento %:</label><span class="${upnlCls}">${upnl >= 0 ? '+' : ''}${fmt(upnl * 100 / (pos.entry_price * pos.qty))}%</span></div>
                            <div><label>Dirección:</label><span>${isShort ? '📉 SHORT — ganancia al bajar' : '📈 LONG — ganancia al subir'}</span></div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `;

    modal.style.display = 'block';
    document.body.style.overflow = 'hidden';
    // Attach chart hover AFTER innerHTML is set
    const _fc = buildPriceRangeChart(pos.entry_price, pos.current_price, pos.sl_price, pos.tp_price, side, pos.entries || []);
    const _fw = document.getElementById('futChartWrap');
    if (_fw && _fc.html) {
        _fw.innerHTML = _fc.html;
        attachChartHover(_fc.uid, _fc.entry, _fc.isShort, _fc.rangeMin, _fc.rangeSpan, _fc.chartL, _fc.chartW, pos.qty);
    }
}

function renderNextCycle(d) {
    if (!d || d.error) return;
    const min = Math.floor((d.seconds_until_next || 0) / 60);
    const sec = (d.seconds_until_next || 0) % 60;
    const countdownStr = `${min}:${String(sec).padStart(2, '0')}`;
    // Update ALL countdown elements
    document.querySelectorAll('.countdown').forEach(el => { el.textContent = countdownStr; });
    const hCd = document.getElementById('headerCountdownValue');
    if (hCd) hCd.textContent = countdownStr;
}

function renderAnalysisSummary(d, closedPositions) {
    if (!d || d.error) return;
    const m = d.metrics || {};
    const wr = parseFloat(m.win_rate || 0);
    const wrCls = wr >= 50 ? 'positive' : wr >= 40 ? '' : 'negative';

    // Win-rate by category from closed positions
    const catOrder = ['LARGE_CAP','MIDCAP_ALT','NARRATIVE','MEME'];
    const catTotals = {};
    const catWins = {};
    ((closedPositions && closedPositions.positions) || []).forEach(p => {
        const cat = p.category || 'MIDCAP_ALT';
        catTotals[cat] = (catTotals[cat] || 0) + 1;
        if (parseFloat(p.pnl || 0) > 0) catWins[cat] = (catWins[cat] || 0) + 1;
    });
    const hasCatData = Object.keys(catTotals).length > 0;
    const catRows = hasCatData ? catOrder.filter(c => catTotals[c] > 0).map(c => {
        const n   = catTotals[c] || 0;
        const w   = catWins[c]   || 0;
        const pct = n > 0 ? Math.round(w / n * 100) : 0;
        const barCls = pct >= 50 ? 'wr-good' : pct >= 40 ? 'wr-warn' : 'wr-bad';
        const pctCls = pct >= 50 ? 'positive' : pct >= 40 ? '' : 'negative';
        return `<div class="category-wr-row">
            <span class="category-wr-icon">${CAT_ICON[c] || '📊'}</span>
            <span class="category-wr-name">${CAT_LABEL[c] || c}</span>
            <div class="category-wr-bar-wrap"><div class="category-wr-bar ${barCls}" style="width:${pct}%"></div></div>
            <span class="category-wr-pct ${pctCls}">${pct}%</span>
            <span class="category-wr-n">${n} trades</span>
        </div>`;
    }).join('') : '<span style="color:#555;font-size:11px">Sin suficientes trades por categoría</span>';

    document.getElementById('analysisSummaryData').innerHTML = `
        <div class="data-item"><span class="data-label">Win Rate Global</span><span class="data-value ${wrCls}">${fmt(m.win_rate)}%</span></div>
        <div class="data-item"><span class="data-label">Profit Factor</span><span class="data-value">${fmt(m.profit_factor)}</span></div>
        <div class="data-item"><span class="data-label">Max Drawdown</span><span class="data-value ${m.max_drawdown <= 0 ? 'positive' : 'negative'}">${fmt(m.max_drawdown)}%</span></div>
        <div class="data-item"><span class="data-label">Total Trades</span><span class="data-value">${m.total_trades ?? 0}</span></div>
        <div class="data-item"><span class="data-label">Ganadores</span><span class="data-value positive">${m.winning_trades ?? 0}</span></div>
        <div class="data-item"><span class="data-label">Perdedores</span><span class="data-value negative">${m.losing_trades ?? 0}</span></div>
        <div class="category-wr-section">
            <div class="category-wr-title">Win Rate por Categoría</div>
            ${catRows}
        </div>
    `;
}

// ── Quality badge helper ───────────────────────────────────────────────────────
function qualityBadge(score) {
    const pct = parseFloat(score || 0);
    if (pct >= 0.60) return `<span class="quality-badge quality-good">🟢 Bueno (${(pct*100).toFixed(0)}%)</span>`;
    if (pct >= 0.40) return `<span class="quality-badge quality-warn">🟡 Aceptable (${(pct*100).toFixed(0)}%)</span>`;
    return `<span class="quality-badge quality-bad">🔴 Requiere ajuste (${(pct*100).toFixed(0)}%)</span>`;
}

function weightBar(label, val, def) {
    const pct = Math.min(100, Math.round((parseFloat(val || 0)) * 200));
    const diff = parseFloat(val || 0) - parseFloat(def || 0);
    const arrow = diff > 0.005 ? '▲' : diff < -0.005 ? '▼' : '–';
    const cls = diff > 0.005 ? 'positive' : diff < -0.005 ? 'negative' : '';
    return `
        <div class="weight-row">
            <span class="weight-label">${label}</span>
            <div class="weight-bar-wrap"><div class="weight-bar" style="width:${pct}%"></div></div>
            <span class="weight-value">${(parseFloat(val||0)*100).toFixed(1)}% <span class="${cls}">${arrow}</span></span>
        </div>`;
}

// ── Render: Aprendizaje del Sistema ───────────────────────────────────────────
function renderLearning(d) {
    const el = document.getElementById('learningData');
    if (!d) { el.innerHTML = '<p class="loading">Sin datos de aprendizaje</p>'; return; }
    if (d.error) { el.innerHTML = `<p class="loading">${d.error}</p>`; return; }

    const acc = parseFloat(d.prediction_accuracy || 0);
    const err = parseFloat(d.avg_prediction_error || 0);
    const lastRun = d.last_run ? new Date(d.last_run).toLocaleString('es-MX', { timeZone: 'America/Mexico_City', hour12: false }) : '—';

    const biasRows = Object.entries(d.bias_by_symbol || {}).slice(0, 5)
        .map(([sym, b]) => {
            const bVal = parseFloat(b);
            const cls = bVal > 0.05 ? 'positive' : bVal < -0.05 ? 'negative' : '';
            const label = bVal > 0.05 ? 'subestima' : bVal < -0.05 ? 'sobreestima' : 'preciso';
            return `<div class="data-item"><span class="data-label">${sym}</span><span class="data-value ${cls}">${label} ${Math.abs(bVal*100).toFixed(1)}%</span></div>`;
        }).join('') || '<p class="loading">Sin datos de sesgo aún</p>';

    el.innerHTML = `
        <div class="data-item"><span class="data-label">Última ejecución</span><span class="data-value">${lastRun}</span></div>
        <div class="data-item"><span class="data-label">Trades analizados</span><span class="data-value">${d.trades_analyzed || 0}</span></div>
        <div class="data-item"><span class="data-label">Precisión predictor</span><span class="data-value ${acc >= 0.6 ? 'positive' : acc >= 0.4 ? '' : 'negative'}">${(acc*100).toFixed(1)}%</span></div>
        <div class="data-item"><span class="data-label">Error promedio</span><span class="data-value">${(err*100).toFixed(1)}%</span></div>
        <div class="data-item"><span class="data-label">Calidad señal</span><span class="data-value">${qualityBadge(d.signal_quality_score)}</span></div>
        ${d.recommendation ? `<div class="learning-rec">${d.recommendation}</div>` : ''}
        <div class="learning-section-title">Sesgo por símbolo:</div>
        ${biasRows}`;
}

// ── Render: Estrategia Activa ──────────────────────────────────────────────────
function renderStrategy(d, effectiveThresholds) {
    const el = document.getElementById('strategyData');
    if (!d) { el.innerHTML = '<p class="loading">Sin datos de estrategia</p>'; return; }
    if (d.error) { el.innerHTML = `<p class="loading">${d.error}</p>`; return; }

    const w = d.active_weights || {};
    const bias = parseFloat(d.bias_correction || 1.0);
    const DEFAULTS = { rsi: 0.15, macd: 0.10, bb: 0.10, ema: 0.10, volume: 0.05 };

    // Effective thresholds section
    const th = effectiveThresholds || {};
    const catOrder = ['LARGE_CAP','MIDCAP_ALT','NARRATIVE','MEME'];
    const thRows = catOrder.filter(c => th[c]).map(c => {
        const t = th[c];
        const base  = parseFloat(t.base_prob  || 0);
        const eff   = parseFloat(t.effective_prob || 0);
        const delta = parseFloat(t.delta || 0);
        const deltaCls = delta < 0 ? 't-delta-good' : delta > 0 ? 't-delta-bad' : 't-delta-warn';
        const deltaStr = delta !== 0 ? `${delta > 0 ? '+' : ''}${(delta*100).toFixed(0)}%` : '—';
        const effCls   = eff < 0.42 ? 't-eff-aggressive' : eff > 0.55 ? 't-eff-strict' : 't-eff-normal';
        return `<tr>
            <td class="threshold-cat">${CAT_ICON[c] || ''} ${CAT_LABEL[c] || c}</td>
            <td class="threshold-base">${(base*100).toFixed(0)}%</td>
            <td class="threshold-arrow">→</td>
            <td class="threshold-eff ${effCls}">${(eff*100).toFixed(0)}%</td>
            <td class="threshold-delta ${deltaCls}">${deltaStr}</td>
        </tr>`;
    }).join('');

    const thSection = thRows ? `
        <div class="threshold-section">
            <div class="threshold-title">Umbrales efectivos hoy (con F&amp;G)</div>
            <table class="threshold-table">${thRows}</table>
        </div>` : '';

    el.innerHTML = `
        <div class="weight-section">
            ${weightBar('RSI',    w.rsi,    DEFAULTS.rsi)}
            ${weightBar('MACD',   w.macd,   DEFAULTS.macd)}
            ${weightBar('BB',     w.bb,     DEFAULTS.bb)}
            ${weightBar('EMA',    w.ema,    DEFAULTS.ema)}
            ${weightBar('Volumen',w.volume, DEFAULTS.volume)}
        </div>
        <div class="data-item" style="margin-top:8px">
            <span class="data-label">Corrección sesgo</span>
            <span class="data-value ${bias > 1.05 ? 'positive' : bias < 0.95 ? 'negative' : ''}">${bias.toFixed(3)}x</span>
        </div>
        ${thSection}`;
}

// ── Render: Backtesting Results ───────────────────────────────────────────────
function renderBacktestResults(data) {
    const el = document.getElementById('backtestData');
    const results = (data && data.results) ? data.results : [];
    const pending = (data && data.pending) ? data.pending : [];

    const pendingHtml = pending.map(p => `
        <tr class="bt-pending">
            <td>${p.symbol}</td>
            <td colspan="7"><span class="quality-badge quality-warn">⏳ Corriendo... (${p.started_at ? new Date(p.started_at).toLocaleTimeString('es-MX') : ''})</span></td>
        </tr>`).join('');

    if (!results.length && !pending.length) {
        el.innerHTML = '<p class="loading">Sin backtests. Usa ▶ Ejecutar para iniciar uno.</p>';
        return;
    }

    const rows = results.map(r => {
        const wr = parseFloat(r.win_rate || 0);
        const sh = parseFloat(r.sharpe_ratio || 0);
        const pnl = parseFloat(r.total_pnl || 0);
        const badge = r.status === 'completed'
            ? `<span class="quality-badge ${sh >= 1.0 ? 'quality-good' : sh >= 0.5 ? 'quality-warn' : 'quality-bad'}">✅</span>`
            : `<span class="quality-badge quality-bad">❌</span>`;
        return `
            <tr>
                <td>${badge} ${r.symbol}</td>
                <td>${r.interval || '1h'} / ${r.days_back || 30}d</td>
                <td>${r.total_trades || 0}</td>
                <td class="${wr >= 0.5 ? 'positive' : 'negative'}">${(wr*100).toFixed(1)}%</td>
                <td class="${sh >= 1 ? 'positive' : sh >= 0.5 ? '' : 'negative'}">${sh.toFixed(2)}</td>
                <td>${parseFloat(r.sortino_ratio||0).toFixed(2)}</td>
                <td class="${pnl >= 0 ? 'positive' : 'negative'}">${pnl.toFixed(2)} USDT</td>
                <td>${parseFloat(r.max_drawdown||0).toFixed(1)}%</td>
                <td>${r.created_at ? new Date(r.created_at).toLocaleString('es-MX') : '—'}</td>
            </tr>`;
    }).join('');

    el.innerHTML = `
        <table class="positions-table">
            <thead>
                <tr>
                    <th>Símbolo</th><th>Intervalo</th><th>Trades</th>
                    <th>Win Rate</th><th>Sharpe</th><th>Sortino</th>
                    <th>PnL Sim.</th><th>Max DD</th><th>Fecha</th>
                </tr>
            </thead>
            <tbody>${pendingHtml}${rows}</tbody>
        </table>`;
}

// ── Render: Parámetros Optimizados ────────────────────────────────────────────
function renderOptimizedParams(d) {
    const el = document.getElementById('optimizedParamsData');
    if (!d) { el.innerHTML = '<p class="loading">Sin datos</p>'; return; }
    if (d.error) { el.innerHTML = `<p class="loading">${d.error}</p>`; return; }

    const active = d.active_params;
    if (!active) {
        el.innerHTML = '<p class="loading">Sin optimización ejecutada aún.</p>';
        return;
    }
    el.innerHTML = `
        <div class="data-item"><span class="data-label">RSI Period</span><span class="data-value">${active.rsi_period}</span></div>
        <div class="data-item"><span class="data-label">MACD</span><span class="data-value">${active.macd_fast}/${active.macd_slow}/${active.macd_signal}</span></div>
        <div class="data-item"><span class="data-label">BB</span><span class="data-value">${active.bb_period} / std ${active.bb_std}</span></div>
        <div class="data-item"><span class="data-label">EMA cross</span><span class="data-value">${active.ema_fast_period}/${active.ema_slow_period}</span></div>
        <div class="data-item"><span class="data-label">Buy threshold</span><span class="data-value">≥ ${active.buy_score_threshold}</span></div>
        <div class="data-item"><span class="data-label">Sharpe</span><span class="data-value ${parseFloat(active.sharpe_ratio||0) >= 1 ? 'positive' : ''}">${parseFloat(active.sharpe_ratio||0).toFixed(2)}</span></div>
        <div class="data-item"><span class="data-label">Win Rate</span><span class="data-value">${(parseFloat(active.win_rate||0)*100).toFixed(1)}%</span></div>
        <div class="data-item"><span class="data-label">Actualizado</span><span class="data-value">${active.created_at ? new Date(active.created_at).toLocaleString('es-MX') : '—'}</span></div>`;
}

// ── Health & Error rendering ──────────────────────────────────────

const _healthIcon = s => s === 'ok' ? '✅' : s === 'warning' ? '⚠️' : s === 'error' ? '❌' : '❓';
const _healthColor = s => s === 'ok' ? '#4caf50' : s === 'warning' ? '#ff9800' : s === 'error' ? '#f44336' : '#888';
const _levelColor = l => l === 'ERROR' ? '#f44336' : l === 'WARNING' ? '#ff9800' : '#aaa';

function updateHealthBadge(overall, summary) {
    const dot = document.getElementById('healthDot');
    const txt = document.getElementById('healthText');
    if (!dot || !txt) return;
    dot.style.background = _healthColor(overall);
    const e = summary ? summary.errors_last_5min || 0 : 0;
    const w = summary ? summary.warnings_last_5min || 0 : 0;
    txt.textContent = overall === 'ok'
        ? 'Sistema OK'
        : `${e} err / ${w} warn (5min)`;
    txt.style.color = _healthColor(overall);
}

function renderHealthChecks(data) {
    const el = document.getElementById('healthChecksData');
    if (!data || data.error) { el.innerHTML = '<p class="loading">No disponible</p>'; return; }
    const checks = data.checks || {};
    const rows = Object.entries(checks).map(([key, c]) => {
        if (!c || typeof c !== 'object') return '';
        let detail = '';
        if (key === 'db')           detail = c.latency_ms != null ? `${c.latency_ms} ms` : (c.detail || '');
        if (key === 'binance')      detail = c.latency_ms != null ? `${c.latency_ms} ms${c.testnet ? ' (testnet)' : ''}` : (c.detail || '');
        if (key === 'trade_cycle')  {
            const lastAt = c.last_trade_cycle_at ? new Date(c.last_trade_cycle_at).toLocaleTimeString('es-MX') : '-';
            const ovfStr = c.overflow_slots_used != null ? ` | ⚡ OVF: ${c.overflow_slots_used}/${c.overflow_max_extra ?? 3}` : '';
            detail = `Ciclo: ${c.current_cycle_minutes ?? '?'} min | Max pos: ${c.max_positions_current ?? '?'} (${c.regime ?? '?'})${ovfStr} | Último: ${lastAt}`;
        }
        if (key === 'position_sync')detail = `DB: ${c.db_open_positions ?? '?'} | Binance: ${c.binance_holdings ?? '?'}${c.mismatch ? ` | ⚠ Dif: ${c.mismatch}` : ''}`;
        if (key === 'futures_api')  detail = c.latency_ms != null ? `${c.latency_ms} ms${c.open_shorts != null ? ` | ${c.open_shorts} S / ${c.open_longs ?? 0} L` : ''}` : (c.detail || '');
        if (key === 'social_score') detail = c.enabled ? `✅ activo — ${c.trending_count ?? 0} tokens trending` : '⚪ sin API key';
        if (key === 'errors')       detail = `${c.errors_last_5min || 0} err / ${c.warnings_last_5min || 0} warn (5min) | Total: ${c.total_records || 0}`;
        if (key === 'circuit_breaker') {
            const cb = c.consecutive_failures ?? 0;
            const thr = c.threshold ?? 5;
            detail = c.status === 'paused'
                ? `🛑 PAUSED hasta ${c.paused_until ? new Date(c.paused_until).toLocaleTimeString('es-MX') : '?'}`
                : `✅ OK — fallos consecutivos: ${cb}/${thr}`;
        }
        if (key === 'portfolio_exposure') {
            const expPct = c.exposure_pct ?? 0;
            const maxPct = c.max_exposure_pct ?? 65;
            const expColor = expPct >= maxPct ? '#f44336' : expPct >= maxPct * 0.85 ? '#ff9800' : '#4caf50';
            const barWidth = Math.min(100, (expPct / maxPct) * 100).toFixed(0);
            detail = `<span style="color:${expColor};font-weight:bold">${expPct}%</span> / ${maxPct}% cap` +
                ` | inv: $${c.invested_usdt ?? '?'} | liq: $${c.liquid_usdt ?? '?'}` +
                `<div style="margin-top:3px;height:4px;background:#333;border-radius:2px;width:100px;display:inline-block;vertical-align:middle;margin-left:6px">` +
                `<div style="height:4px;width:${barWidth}%;background:${expColor};border-radius:2px"></div></div>`;
        }
        if (key === 'disk_storage') {
            const usedPct = c.used_pct ?? 0;
            const freePct = 100 - usedPct;
            const diskColor = c.status === 'critical' ? '#e74c3c' : c.status === 'warning' ? '#f39c12' : '#4ecca3';
            const barW = Math.min(100, usedPct).toFixed(0);
            detail = `💾 ${c.used_gb ?? '?'} / ${c.total_gb ?? '?'} GB usado` +
                ` — libre: <span style="color:${diskColor};font-weight:700">${c.free_gb ?? '?'} GB (${freePct.toFixed(1)}%)</span>` +
                `<div style="margin-top:3px;height:4px;background:#333;border-radius:2px;width:120px;display:inline-block;vertical-align:middle;margin-left:6px">` +
                `<div style="height:4px;width:${barW}%;background:${diskColor};border-radius:2px"></div></div>`;
        }
        return `<div class="data-item">
            <span class="data-label">${_healthIcon(c.status || c.health)} ${key}</span>
            <span class="data-value" style="color:${_healthColor(c.status || c.health)}">${detail || c.status || c.health}</span>
        </div>`;
    }).join('');
    el.innerHTML = rows || '<p class="loading">Sin checks</p>';
}

function renderErrors(data) {
    const el = document.getElementById('errorsData');
    if (!data) { el.innerHTML = '<p class="loading">Cargando...</p>'; return; }
    const errors = data.errors || [];
    if (!errors.length) { el.innerHTML = '<p class="loading" style="color:#4caf50">✅ Sin errores recientes</p>'; return; }
    const rows = errors.map(e => {
        const ts = e.timestamp ? new Date(e.timestamp).toLocaleTimeString('es-MX') : '';
        const ctx = e.context && Object.keys(e.context).length ? `<small style="color:#888"> ${JSON.stringify(e.context)}</small>` : '';
        return `<tr>
            <td style="white-space:nowrap;color:#aaa">${ts}</td>
            <td><span style="color:${_levelColor(e.level)};font-weight:bold">${e.level}</span></td>
            <td><span style="color:#7ec8e3">${e.source}</span></td>
            <td style="word-break:break-word">${e.message}${ctx}</td>
        </tr>`;
    }).join('');
    el.innerHTML = `<table>
        <thead><tr>
            <th>Hora</th>
            <th>Nivel</th>
            <th>Fuente</th>
            <th>Mensaje</th>
        </tr></thead>
        <tbody>${rows}</tbody>
    </table>`;
}

function renderFullHealthCheck(data) {
    const el = document.getElementById('fullHealthData');
    const verEl = document.getElementById('fullHealthVersion');
    if (!data) { el.innerHTML = '<p class="loading" style="color:#f44">Sin datos del servidor</p>'; return; }

    const STATUS_COLOR = { pass: '#4caf50', fail: '#f44336', warn: '#ff9800' };
    const STATUS_ICON  = { pass: '✅', fail: '❌', warn: '⚠️' };
    const s = data.summary || {};
    const overallColor = data.overall === 'ok' ? '#4caf50' : data.overall === 'warn' ? '#ff9800' : '#f44336';
    const overallIcon  = data.overall === 'ok' ? '✅' : data.overall === 'warn' ? '⚠️' : '❌';

    if (verEl) verEl.textContent = `v${data.version || '?'} · ${new Date(data.timestamp).toLocaleTimeString('es-MX')}`;

    const summary = `<div class="health-summary" style="display:flex;gap:16px;align-items:center;padding:10px 0 14px;flex-wrap:wrap">
        <span style="font-size:22px;font-weight:bold;color:${overallColor}">${overallIcon} ${(data.overall || '?').toUpperCase()}</span>
        <span class="hc-pill" style="background:#2a2a2a;padding:4px 10px;border-radius:20px">
            🔢 Total: <b>${s.total || 0}</b>
        </span>
        <span class="hc-pill" style="background:#1a3a1a;padding:4px 10px;border-radius:20px;color:#4caf50">
            ✅ Passed: <b>${s.passed || 0}</b>
        </span>
        <span class="hc-pill" style="background:#3a1a1a;padding:4px 10px;border-radius:20px;color:#f44336">
            ❌ Failed: <b>${s.failed || 0}</b>
        </span>
        <span class="hc-pill" style="background:#3a2a1a;padding:4px 10px;border-radius:20px;color:#ff9800">
            ⚠️ Warned: <b>${s.warned || 0}</b>
        </span>
    </div>`;

    const cats = (data.categories || []).map(cat => {
        const checks = (cat.checks || []);
        const catFailed = checks.filter(c => c.status === 'fail').length;
        const catWarned = checks.filter(c => c.status === 'warn').length;
        const catOk     = checks.filter(c => c.status === 'pass').length;
        const catColor  = catFailed ? '#f44336' : catWarned ? '#ff9800' : '#4caf50';
        const rows = checks.map(c => {
            const color = STATUS_COLOR[c.status] || '#888';
            const icon  = STATUS_ICON[c.status]  || '•';
            const detail = c.detail ? `<small style="color:#aaa;display:block;margin-top:2px;word-break:break-word">${c.detail}</small>` : '';
            return `<div class="hc-check-row" style="padding:4px 0;border-bottom:1px solid #1a1a1a">
                <span style="color:${color};margin-right:8px">${icon}</span>
                <span style="font-family:monospace;font-size:12px">${c.name}</span>
                ${detail}
            </div>`;
        }).join('');
        return `<div class="hc-category" style="margin-bottom:12px">
            <div class="hc-cat-header" style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:2px solid ${catColor}30;margin-bottom:4px">
                <span style="color:${catColor};font-weight:bold;font-size:13px">${cat.label || cat.name}</span>
                <span style="color:#4caf50;font-size:11px">✅ ${catOk}</span>
                ${catFailed ? `<span style="color:#f44336;font-size:11px">❌ ${catFailed}</span>` : ''}
                ${catWarned ? `<span style="color:#ff9800;font-size:11px">⚠️ ${catWarned}</span>` : ''}
            </div>
            ${rows}
        </div>`;
    }).join('');

    el.innerHTML = summary + `<div class="hc-categories" style="margin-top:4px">${cats}</div>`;
}

async function updateHealthAndErrors() {
    try {
        const [hRes, eRes] = await Promise.all([
            fetch(`${API_BASE}/health/status`),
            fetch(`${API_BASE}/errors/recent?limit=50`)
        ]);
        const hData = hRes.ok ? await hRes.json() : null;
        const eData = eRes.ok ? await eRes.json() : null;
        renderHealthChecks(hData);
        renderErrors(eData);
        if (hData) updateHealthBadge(hData.overall, hData.checks && hData.checks.errors);
    } catch (e) {
        console.error('Health fetch error:', e);
    }
}

// ── Backtest polling ───────────────────────────────────────────────────────────
let _pendingBacktests = [];

async function pollBacktestResults() {
    try {
        const res = await fetch(`${API_BASE}/backtest/results?limit=10`);
        if (!res.ok) return;
        const data = await res.json();
        renderBacktestResults(data);
        _pendingBacktests = (data.pending || []).filter(p => p.status === 'running');
    } catch (e) { /* silent */ }
}

// ── F&G header badge ──────────────────────────────────────────────────────────────
function renderFngBadge(ctx) {
    const badge = document.getElementById('fngBadge');
    const dot   = document.getElementById('fngDot');
    const val   = document.getElementById('fngValue');
    if (!badge || !ctx) return;
    const v = ctx.fear_greed_value ?? 50;
    const label = ctx.fear_greed_label || 'Neutral';
    // Color & class
    let cls, dotColor, shortLabel;
    if (v < 25)      { cls = 'fng-extreme-fear';  dotColor = '#e74c3c'; shortLabel = 'MIEDO EXT.'; }
    else if (v < 45) { cls = 'fng-fear';           dotColor = '#ff7c4d'; shortLabel = 'MIEDO'; }
    else if (v < 56) { cls = 'fng-neutral';        dotColor = '#888';    shortLabel = 'NEUTRAL'; }
    else if (v < 75) { cls = 'fng-greed';          dotColor = '#a8d5c4'; shortLabel = 'CODICIA'; }
    else             { cls = 'fng-extreme-greed';  dotColor = '#ffc83c'; shortLabel = 'COD. EXT.'; }
    badge.className = `fng-badge ${cls}`;
    badge.title = `Fear & Greed: ${v} — ${label}`;
    dot.style.background = dotColor;
    dot.style.boxShadow = `0 0 6px ${dotColor}80`;
    val.textContent = `${v} ${shortLabel}`;
    val.style.color = dotColor;
}

// ── Market Context card ───────────────────────────────────────────────────────────
function renderMarketContext(ctx) {
    const el = document.getElementById('marketContextData');
    if (!el || !ctx) return;
    const v   = ctx.fear_greed_value ?? 50;
    const lbl = ctx.fear_greed_label || 'Neutral';
    const mod = parseFloat(ctx.fear_greed_modifier ?? 0);
    const pct = Math.round(v);
    // Effect text
    let effectCls, effectText;
    if (mod < -0.04)      { effectCls = 'relax';   effectText = `↓ ${Math.abs(mod*100).toFixed(0)}% más fácil entrar — oportunidad de compra`; }
    else if (mod < 0)     { effectCls = 'relax';   effectText = `↓ ${Math.abs(mod*100).toFixed(0)}% más fácil entrar`; }
    else if (mod === 0)   { effectCls = 'neutral';  effectText = 'Sin cambio en umbrales'; }
    else if (mod < 0.05)  { effectCls = 'tighten'; effectText = `↑ ${(mod*100).toFixed(0)}% más difícil entrar`; }
    else                  { effectCls = 'tighten'; effectText = `↑ ${(mod*100).toFixed(0)}% más difícil — evitar comprar en techo`; }
    // Trending chips
    const trending   = ctx.trending_symbols || [];
    const inSystem   = new Set(ctx.analysis_symbols_base || []);
    const chips = trending.map(s => {
        const inSys = inSystem.has(s);
        const check = inSys ? ' <span class="trending-check">✓</span>' : '';
        return `<span class="trending-chip ${inSys ? 'in-system' : 'out-system'}" title="${inSys ? 'En nuestro análisis' : 'Fuera del sistema'}">${s}${check}</span>`;
    }).join('');
    const trendingAge = trending.length > 0 ? ' <small style="color:#555">(CoinGecko)</small>' : '';
    // News sentiment section (Fase 3)
    const ns = ctx.news_sentiment || {};
    const nsPos = ns.positive || 0;
    const nsNeg = ns.negative || 0;
    const nsNeu = ns.neutral  || 0;
    const nsTotal = nsPos + nsNeg + nsNeu;
    let sentimentHtml = '';
    if (nsTotal > 0) {
        const sentColor = nsNeg >= 2 ? '#e74c3c' : nsPos > nsNeg ? '#4ecca3' : '#aaa';
        const sentLabel = nsNeg >= 2 ? '⚠️ Noticias negativas' : nsPos > nsNeg ? '✅ Noticias positivas' : '➖ Noticias neutras';
        sentimentHtml = `<div class="news-sentiment-row" style="margin-top:8px;font-size:12px;display:flex;gap:10px;align-items:center">
            <span style="color:#555;font-size:11px">📰 Noticias 24h:</span>
            <span style="color:#4ecca3">+${nsPos}</span>
            <span style="color:#e74c3c">-${nsNeg}</span>
            <span style="color:#555">${nsNeu} neu</span>
            <span style="color:${sentColor};font-weight:600">${sentLabel}</span>
        </div>`;
    }
    el.innerHTML = `
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
            <span style="font-size:22px;font-weight:800;color:${ v<25?'#e74c3c':v<45?'#ff7c4d':v<56?'#aaa':v<75?'#a8d5c4':'#ffc83c' }">${v}</span>
            <span style="font-size:13px;font-weight:600;color:#aaa">${lbl}</span>
        </div>
        <div class="fng-bar-wrap">
            <div class="fng-bar-cursor" style="left:${pct}%"></div>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:10px;color:#444;margin-bottom:6px">
            <span>Miedo Ext.</span><span>Miedo</span><span>Neutral</span><span>Codicia</span><span>Cod. Ext.</span>
        </div>
        <div class="fng-effect ${effectCls}">${effectText}</div>
        <div class="trending-section-title">🔥 Trending CoinGecko${trendingAge}</div>
        <div class="trending-chips">${chips || '<span style="color:#555;font-size:12px">Sin datos</span>'}</div>
        ${sentimentHtml}
    `;
}

// ── Trading Mode Badge (header) ─────────────────────────────────────────────────
function renderTradingModeBadge(r) {
    const dot  = document.getElementById('tradingModeDot');
    const text = document.getElementById('tradingModeText');
    const badge = document.getElementById('tradingModeBadge');
    if (!r || !dot || !text) return;
    const mode = r.trading_mode || 'SPOT_BULL';
    const cfg  = MODE_CFG[mode] || MODE_CFG['SPOT_BULL'];
    dot.style.background  = cfg.color;
    dot.style.boxShadow   = `0 0 6px ${cfg.color}80`;
    text.textContent      = `${cfg.icon} ${cfg.label}`;
    text.style.color      = cfg.color;
    badge.title           = r.mode_reason || '';
}

// ── Trading Mode Card ─────────────────────────────────────────────────────────────
function renderTradingModeCard(r) {
    const el = document.getElementById('tradingModeData');
    if (!el || !r) return;
    const mode    = r.trading_mode || 'SPOT_BULL';
    const cfg     = MODE_CFG[mode] || MODE_CFG['SPOT_BULL'];
    const chg7    = r.btc_7d_change_pct != null ? r.btc_7d_change_pct : 0;
    const chgDay  = r.day_change_pct    != null ? r.day_change_pct    : 0;
    const vol     = r.vol_ratio         != null ? r.vol_ratio         : 0;
    const trend   = r.btc_trend         || '?';
    const desc    = r.mode_desc         || '';

    const chgCls7   = chg7  >= 0 ? 'up'   : 'down';
    const chgClsDay = chgDay >= 0 ? 'up'   : 'down';
    const volCls    = vol   >= 1 ? 'up'   : vol >= 0.6 ? 'flat' : 'down';
    const trendCls  = trend === 'UP' ? 'up' : trend === 'DOWN' ? 'down' : 'flat';
    const bb        = r.bb_bandwidth != null ? r.bb_bandwidth : null;
    const bbCls     = bb != null ? (bb < 0.04 ? 'down' : bb < 0.07 ? 'flat' : 'up') : '';
    const bbAlert   = bb != null && bb < 0.04 ? ' ⚡' : '';
    const confirmed = r.mode_confirmed !== false;

    const futuresWarn = (mode === 'FUTURES_SHORT')
        ? `<div class="mode-warn" style="color:#9b59b6;border-left-color:#9b59b6">⚠️ FUTURES SHORT activo — Posiciones con apalancamiento 2× abiertas. SL: ${r.regime_sl_pct ?? 3.0}% | TP: ${r.regime_tp_pct ?? 5.0}% | Kelly: ${r.regime_kelly ?? 0.3}×</div>`
        : '';

    const sl   = r.regime_sl_pct    != null ? r.regime_sl_pct    : '—';
    const tp   = r.regime_tp_pct    != null ? r.regime_tp_pct    : '—';
    const kel  = r.regime_kelly     != null ? r.regime_kelly     : '—';

    const modeReason = r.mode_reason || '';

    el.innerHTML = `
        <div class="mode-banner" style="background:${cfg.color}15; border-color:${cfg.color}40">
            <div class="mode-banner-icon">${cfg.icon}</div>
            <div style="flex:1;min-width:0">
                <div class="mode-banner-title" style="color:${cfg.color}">${cfg.label}</div>
                <div class="mode-banner-conf" style="font-size:11px;margin-top:2px;color:${confirmed ? '#4ecca3' : '#f39c12'}">${confirmed ? '✅ Confirmado' : '⏳ Pendiente confirmación'}</div>
            </div>
            <div style="display:flex;gap:20px;flex-shrink:0;text-align:center;padding-left:12px;border-left:1px solid rgba(255,255,255,0.1)">
                <div><div style="font-size:10px;color:#888;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:3px">SL</div><div style="font-size:20px;font-weight:700;color:#e74c3c">${sl}%</div></div>
                <div><div style="font-size:10px;color:#888;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:3px">TP</div><div style="font-size:20px;font-weight:700;color:#4ecca3">${tp}%</div></div>
                <div><div style="font-size:10px;color:#888;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:3px">Kelly</div><div style="font-size:20px;font-weight:700;color:${cfg.color}">${kel}×</div></div>
            </div>
        </div>
        ${modeReason ? `<div class="mode-reason-row" title="${modeReason}">📋 ${modeReason}</div>` : ''}
        <div class="mode-indicators">
            <div class="mode-ind-item">
                <div class="mode-ind-label">BTC 7 días</div>
                <div class="mode-ind-val ${chgCls7}">${chg7 >= 0 ? '+' : ''}${chg7.toFixed(1)}%</div>
            </div>
            <div class="mode-ind-item">
                <div class="mode-ind-label">BTC hoy</div>
                <div class="mode-ind-val ${chgClsDay}">${chgDay >= 0 ? '+' : ''}${chgDay.toFixed(2)}%</div>
            </div>
            <div class="mode-ind-item">
                <div class="mode-ind-label">Vol ratio</div>
                <div class="mode-ind-val ${volCls}">${vol.toFixed(2)}×</div>
            </div>
            <div class="mode-ind-item">
                <div class="mode-ind-label">Tendencia 4h</div>
                <div class="mode-ind-val ${trendCls}">${trend === 'UP' ? '⬆ UP' : trend === 'DOWN' ? '⬇ DOWN' : '→ FLAT'}</div>
            </div>
            ${bb != null ? `<div class="mode-ind-item">
                <div class="mode-ind-label">BB Bandwidth${bbAlert}</div>
                <div class="mode-ind-val ${bbCls}" title="${bb < 0.04 ? 'COMPRESIÓN — breakout inminente' : bb < 0.07 ? 'Moderado' : 'Expandido'}">${(bb * 100).toFixed(2)}%</div>
            </div>` : ''}
        </div>
        <div class="mode-strategy-box" style="border-left-color:${cfg.color}">
            <strong>Estrategia activa:</strong> ${desc}
        </div>
        ${futuresWarn}
    `;
}

// ── Monte Carlo Projection Card ────────────────────────────────────────────────────
async function recalculateMonteCarlo() {
    const btn = document.getElementById('mcRecalcBtn');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Calculando...'; }
    try {
        const res = await apiFetch('/monte-carlo/recalculate', { method: 'POST' });
        if (res && res.monte_carlo_projection) renderMonteCarlo(res.monte_carlo_projection);
    } catch(e) { console.error('MC recalc error:', e); }
    finally { if (btn) { btn.disabled = false; btn.textContent = '🔄 Recalcular'; } }
}

function renderMonteCarlo(mc) {
    const el = document.getElementById('monteCarloData');
    if (!el || !mc) return;

    const cap0      = mc.initial_capital  || 0;
    const nTrades   = mc.n_trades_used    ?? 0;
    const nNormal   = mc.n_normal_trades  ?? '?';
    const genAt     = mc.generated_at     ?? '—';
    const btcFinal  = mc.btc_hodl_final   || cap0;
    const btcPct    = parseFloat(mc.btc_hodl_pct ?? 0);
    const months    = mc.months || 3;
    const meta      = mc.meta_1000_mxn_day || {};

    // ── 3-scenario table ───────────────────────────────────────────────────────
    const scenarios = mc.scenarios || {};
    const scA = scenarios.A || {};
    const scB = scenarios.B || {};
    const scC = scenarios.C || {};

    function scRow(sc, color, emoji) {
        if (!sc || !sc.label) return '';
        const ret = sc.return_pct != null ? sc.return_pct : ((sc.mean_final - cap0) / cap0 * 100);
        const retStr = (ret >= 0 ? '+' : '') + parseFloat(ret).toFixed(1) + '%';
        const retCls = ret >= 0 ? 'mc-pnl-pos' : 'mc-pnl-neg';
        const pnlDayMxn = sc.total_pnl != null ? (sc.total_pnl / 90 * 19.5) : null;
        const pnlDayStr = pnlDayMxn != null
            ? `<span style="color:#aaa;font-size:0.85em"> (${pnlDayMxn >= 0 ? '+' : ''}$${pnlDayMxn.toFixed(0)} MXN/día)</span>`
            : '';
        return `<tr>
            <td style="color:${color};font-weight:600">${emoji} ${sc.label}${pnlDayStr}</td>
            <td class="right" style="color:#e74c3c">$${parseFloat(sc.p5 || cap0).toFixed(0)}</td>
            <td class="right" style="color:#f39c12">$${parseFloat(sc.p50 || cap0).toFixed(0)}</td>
            <td class="right" style="color:#4ecca3">$${parseFloat(sc.p95 || cap0).toFixed(0)}</td>
            <td class="right ${retCls}">${retStr}</td>
            <td class="right" style="color:#888">${sc.win_rate ?? '?'}%</td>
        </tr>`;
    }

    const scenariosTable = (scA.label || scB.label || scC.label) ? `
        <div style="overflow-x:auto;margin-top:14px">
        <table class="mc-table">
            <thead><tr>
                <th>Escenario (${months} meses)</th>
                <th class="right">P5 peor</th>
                <th class="right">Mediana</th>
                <th class="right">P95 mejor</th>
                <th class="right">Return</th>
                <th class="right">WR</th>
            </tr></thead>
            <tbody>
                ${scRow(scA, '#e74c3c', '🔴')}
                ${scRow(scB, '#f39c12', '🟡')}
                ${scRow(scC, '#4ecca3', '🟢')}
            </tbody>
        </table>
        </div>` : '';

    // ── Monthly breakdown for scenario Base ───────────────────────────────────
    const breakdown = (scB.monthly_breakdown || mc.monthly_breakdown || []);
    const rows = breakdown.map(m => {
        const capM  = m.capital ?? (cap0 + (m.pnl ?? 0));
        const pnl   = m.pnl ?? 0;
        const pnlMxn = pnl * 19.5;
        const pnlCls = pnl >= 0 ? 'mc-pnl-pos' : 'mc-pnl-neg';
        return `<tr>
            <td>Mes ${m.month}</td>
            <td class="right">${scB.trades_per_month ?? mc.trades_per_month ?? '?'}</td>
            <td class="right">$${parseFloat(capM).toFixed(0)}</td>
            <td class="right ${pnlCls}">${pnl >= 0 ? '+' : ''}$${parseFloat(pnl).toFixed(2)}</td>
            <td class="right ${pnlCls}" style="font-size:0.85em">${pnlMxn >= 0 ? '+' : ''}$${pnlMxn.toFixed(0)} MXN</td>
        </tr>`;
    }).join('');

    // ── Progress bars Bear / Base / Bull / BTC ────────────────────────────────
    const allFinals = [scA.p50, scB.p50, scC.p50, btcFinal, cap0].filter(Boolean).map(Number);
    const rangeMin = Math.min(...allFinals) * 0.97;
    const rangeMax = Math.max(...allFinals) * 1.03;
    const rangeDiff = rangeMax - rangeMin || 1;
    function barPct(val) { return Math.max(2, Math.min(98, ((val - rangeMin) / rangeDiff * 100))).toFixed(1); }

    const progressBars = `
        <div class="mc-progress-wrap" style="margin-top:16px">
            <div class="mc-progress-label">Comparativa capital final mediana — ${months} meses · Capital inicial: $${parseFloat(cap0).toFixed(0)}</div>
            <div style="display:flex;flex-direction:column;gap:7px;margin-top:8px">
                <div style="display:flex;align-items:center;gap:10px;font-size:12px">
                    <span style="width:120px;color:#e74c3c">🔴 Bear</span>
                    <div class="mc-progress-bar-bg" style="flex:1">
                        <div class="mc-progress-bar-fill" style="width:${barPct(scA.p50 || cap0)}%;background:#e74c3c"></div>
                    </div>
                    <span style="width:65px;text-align:right;color:#e74c3c">$${parseFloat(scA.p50 || cap0).toFixed(0)}</span>
                </div>
                <div style="display:flex;align-items:center;gap:10px;font-size:12px">
                    <span style="width:120px;color:#f39c12">🟡 Base</span>
                    <div class="mc-progress-bar-bg" style="flex:1">
                        <div class="mc-progress-bar-fill" style="width:${barPct(scB.p50 || cap0)}%;background:#f39c12"></div>
                    </div>
                    <span style="width:65px;text-align:right;color:#f39c12">$${parseFloat(scB.p50 || cap0).toFixed(0)}</span>
                </div>
                <div style="display:flex;align-items:center;gap:10px;font-size:12px">
                    <span style="width:120px;color:#4ecca3">🟢 Bull</span>
                    <div class="mc-progress-bar-bg" style="flex:1">
                        <div class="mc-progress-bar-fill" style="width:${barPct(scC.p50 || cap0)}%;background:#4ecca3"></div>
                    </div>
                    <span style="width:65px;text-align:right;color:#4ecca3">$${parseFloat(scC.p50 || cap0).toFixed(0)}</span>
                </div>
                <div style="display:flex;align-items:center;gap:10px;font-size:12px">
                    <span style="width:120px;color:#f7931a">₿ HODL BTC</span>
                    <div class="mc-progress-bar-bg" style="flex:1">
                        <div class="mc-progress-bar-fill" style="width:${barPct(btcFinal)}%;background:#f7931a"></div>
                    </div>
                    <span style="width:65px;text-align:right;color:#f7931a">$${btcFinal.toFixed(0)}</span>
                </div>
            </div>
        </div>`;

    // ── Meta 1,000 MXN/día ────────────────────────────────────────────────────
    const evTrade    = parseFloat(meta.ev_per_trade_usd ?? 0);
    const tdCurrent  = parseFloat(meta.trades_day_current ?? 0);
    const tdNeeded   = parseFloat(meta.trades_needed_day ?? 999);
    const pnlDayMxn  = parseFloat(meta.pnl_day_current_mxn ?? 0);
    const pctGoal    = parseFloat(meta.pct_toward_goal ?? 0);
    const bullDay    = parseFloat(meta.bull_trades_day ?? 0);
    const bullPnlMxn = parseFloat(meta.bull_pnl_day_mxn ?? 0);
    const pnlDayCls  = pnlDayMxn >= 0 ? '#4ecca3' : '#e74c3c';
    const metaBadge = evTrade > 0 ? `
        <div style="background:rgba(78,204,163,0.08);border:1px solid rgba(78,204,163,0.25);border-radius:10px;padding:12px 16px;margin-top:14px">
            <div style="font-size:13px;font-weight:700;color:#4ecca3;margin-bottom:8px">🎯 Meta: 1,000 MXN/día con capital actual</div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px 20px;font-size:12px">
                <div>EV por trade: <b style="color:#fff">$${evTrade.toFixed(3)} USDT</b></div>
                <div>Trades/día actuales: <b style="color:#fff">${tdCurrent}</b></div>
                <div>Trades/día necesarios: <b style="color:#f39c12">${tdNeeded}</b></div>
                <div>PnL/día actual: <b style="color:${pnlDayCls}">${pnlDayMxn >= 0 ? '+' : ''}$${pnlDayMxn.toFixed(0)} MXN</b></div>
            </div>
            <div style="margin-top:10px">
                <div style="display:flex;justify-content:space-between;font-size:11px;color:#888;margin-bottom:3px">
                    <span>Progreso hacia 1,000 MXN/día</span>
                    <span>${pctGoal.toFixed(1)}% (en mercado Base)</span>
                </div>
                <div style="background:#1a1a2e;border-radius:4px;height:8px;overflow:hidden">
                    <div style="height:100%;width:${Math.min(pctGoal,100)}%;background:linear-gradient(90deg,#f39c12,#4ecca3);border-radius:4px;transition:width 0.5s"></div>
                </div>
            </div>
            ${bullPnlMxn > 0 ? `<div style="margin-top:8px;font-size:11px;color:#aaa">🟢 En mercado Bull (F&G>50): ~<b style="color:#4ecca3">$${bullPnlMxn.toFixed(0)} MXN/día</b> con ${bullDay} trades/día estimados</div>` : ''}
        </div>` : '';

    el.innerHTML = `
        <div class="mc-header">
            <span class="mc-subtitle">1,000 sims · Capital: $${parseFloat(cap0).toFixed(0)} · ${nTrades} trades reales · ${months} meses</span>
            <span class="mc-subtitle" style="display:flex;align-items:center;gap:8px">
                <span style="color:#666">Generado: ${genAt}</span>
                <button id="mcRecalcBtn" class="btn btn-primary btn-sm" onclick="recalculateMonteCarlo()">🔄 Recalcular</button>
            </span>
        </div>

        ${scenariosTable}
        ${metaBadge}
        ${progressBars}

        ${breakdown.length > 0 ? `
        <div style="margin-top:16px">
            <div class="mc-progress-label">Desglose mensual — Escenario Base (sistema actual)</div>
            <div style="overflow-x:auto;margin-top:8px">
            <table class="mc-table">
                <thead><tr>
                    <th>Mes</th>
                    <th class="right">Trades</th>
                    <th class="right">Capital esperado</th>
                    <th class="right">PnL mes (USD)</th>
                    <th class="right">PnL mes (MXN)</th>
                </tr></thead>
                <tbody>${rows}</tbody>
            </table>
            </div>
        </div>` : ''}

        <div class="mc-note">* 1,000 simulaciones Monte Carlo sobre trades reales cerrados. Bear=WR histórico real, Base=solo trades SL/TP (sin rebalanceos), Bull=WR 55% + señales M1-M5 activas.</div>
    `;
}

// ── Futures Positions sync (keeps _futuresUnrealPnl updated, no separate card) ─
async function _syncFuturesUnrealPnl() {
    try {
        const res = await fetch(`${API_BASE}/futures/positions`);
        if (!res.ok) return;
        const data = await res.json();
        if (!data.enabled) return;
        _futuresUnrealPnl = (data.positions || []).reduce((s, p) => s + (p.unrealized_pnl || 0), 0);
        _cachedFuturesPositions = data.positions || [];
    } catch (e) { /* silent */ }
}

// ── Futures Positions Card (C1) — kept for backwards compatibility ─────────────
async function renderFuturesPositions() {
    const el = document.getElementById('futuresPositionsData');
    const card = document.getElementById('futuresCard');
    if (!el) return;
    try {
        const res = await fetch(`${API_BASE}/futures/positions`);
        if (!res.ok) { el.innerHTML = '<p class="loading">No disponible</p>'; return; }
        const data = await res.json();
        if (!data.enabled) {
            card && (card.style.display = 'none');
            return;
        }
        card && (card.style.display = '');
        if (!data.positions || data.positions.length === 0) {
            el.innerHTML = '<p class="loading" style="color:#9b59b6">Sin posiciones de futuros abiertas</p>';
            return;
        }
        const totalPnl = data.positions.reduce((s, p) => s + (p.unrealized_pnl || 0), 0);
        _futuresUnrealPnl = totalPnl;  // Update global so KPI row can include it
        _cachedFuturesPositions = data.positions;  // Cache for modal click
        const pnlCls = totalPnl >= 0 ? 'positive' : 'negative';
        el.innerHTML = `
        <div class="table-scroll-10">
        <table class="positions-table">
            <thead><tr>
                <th>Símbolo</th><th>Lado</th><th>Qty</th>
                <th>Entrada</th><th>Actual</th>
                <th>SL</th><th>TP</th>
                <th>%→SL</th><th>%→TP</th>
                <th>PnL No Real.</th><th>Abierto</th>
            </tr></thead>
            <tbody>
            ${data.positions.map(p => {
                const upnlCls = p.unrealized_pnl >= 0 ? 'positive' : 'negative';
                const slCls = Math.abs(p.sl_dist_pct) < 1.0 ? 'negative' : '';
                const tpCls = Math.abs(p.tp_dist_pct) < 1.0 ? 'positive' : '';
                const age = p.opened_at ? relativeTime(p.opened_at) : '—';
                const isLong = p.side === 'LONG';
                const sideLabel = isLong
                    ? `<span style="color:#27ae60">▲ LONG</span>`
                    : `<span style="color:#9b59b6">▼ SHORT</span>`;
                const _fEsc = p.escalamientos || 0;
                const _fEscTip = (_fEsc > 1 && p.entries) ? p.entries.map(e => `Esc${e.escalonamiento_index}: $${fmt(e.entry_price)} \u00d7 ${e.qty} (${e.age_h}h)`).join('&#10;') : '';
                const _fEscBadge = _fEsc > 1 ? ` <span class="badge-esc" title="${_fEscTip}">\u{1F4CA}\u00d7${_fEsc}</span>` : '';
                return `<tr style="cursor:pointer" onclick="showFuturesDetailModal('${p.symbol}','${p.side}')">
                    <td style="color:#9b59b6;font-weight:600">${p.symbol}${_fEscBadge}</td>
                    <td>${sideLabel}</td>
                    <td>${parseFloat(p.qty || 0).toFixed(4)}</td>
                    <td>$${fmt(p.entry_price)}</td>
                    <td>$${fmt(p.current_price)}</td>
                    <td class="${slCls}">$${fmt(p.sl_price)}</td>
                    <td class="${tpCls}">$${fmt(p.tp_price)}</td>
                    <td class="${slCls}">${p.sl_dist_pct >= 0 ? '+' : ''}${fmt(p.sl_dist_pct)}%</td>
                    <td class="${tpCls}">${p.tp_dist_pct >= 0 ? '+' : ''}${fmt(p.tp_dist_pct)}%</td>
                    <td class="${upnlCls}">${fmt(p.unrealized_pnl, 4)} USDT</td>
                    <td>${age}</td>
                </tr>`;
            }).join('')}
            </tbody>
        </table>
        </div>
        <div class="positions-summary">
            Total PnL No Realizado (Futuros):
            <span class="${pnlCls}"><strong>${fmt(totalPnl, 4)} USDT</strong></span>
            &nbsp;|&nbsp; Posiciones: <strong>${data.count}</strong>
        </div>`;
    } catch (e) {
        el.innerHTML = '<p class="loading">Error cargando futuros</p>';
    }
}

// ── Futures Closed Positions Card ───────────────────────────────────────────────
async function renderFuturesClosed() {
    const el = document.getElementById('futuresClosedData');
    const card = document.getElementById('futuresClosedCard');
    if (!el) return;

    try {
        const res = await fetch(`${API_BASE}/futures/closed?days=7`, { headers: _authHeaders() });
        if (!res.ok) {
            el.innerHTML = '<p class="loading">No disponible</p>';
            if (card) card.style.display = 'none';
            return;
        }
        const d = await res.json();

        if (!d.enabled) {
            if (card) card.style.display = 'none';
            return;
        }

        const periodDays = d.period_days || 7;

        if (!d.positions || d.positions.length === 0) {
            el.innerHTML = `<p class="loading" style="color:#9b59b6">Sin futuros cerrados recientes (últimos ${periodDays} días)</p>`;
            if (card) card.style.display = '';
            return;
        }

        if (card) card.style.display = '';
        _cachedFuturesClosed = d.positions;  // Cache for modal click

        // Helper para formatear fechas en local (México)
        const fmtDateLocal = (isoStr) => {
            if (!isoStr) return '—';
            try {
                const date = new Date(isoStr);
                return date.toLocaleString('es-MX', {
                    timeZone: 'America/Mexico_City',
                    day: '2-digit',
                    month: 'short',
                    hour: '2-digit',
                    minute: '2-digit',
                    hour12: false
                });
            } catch(e) { return isoStr; }
        };

        const totalPnl = d.positions.reduce((s, p) => s + (p.pnl || 0), 0);
        const pnlCls = totalPnl >= 0 ? 'positive' : 'negative';

        el.innerHTML = `
        <div class="table-scroll-10">
        <table class="positions-table">
            <thead><tr>
                <th>Símbolo</th>
                <th>Lado</th>
                <th>Entrada → Salida</th>
                <th>PnL Realizado</th>
                <th>Cerrado</th>
            </tr></thead>
            <tbody>
            ${d.positions.map((p, i) => {
                const pnlCls = (p.pnl || 0) >= 0 ? 'positive' : 'negative';
                const closedAt = fmtDateLocal(p.closed_at || p.opened_at);
                const _fcMeta = p.metadata || {};
                const _fcEscIdx = _fcMeta.escalonamiento_index;
                const _fcEscTotal = _fcMeta.total_escalonamientos;
                const _fcCgid = _fcMeta.close_group_id;
                const _fcEscBadge = (_fcEscTotal > 1 && _fcEscIdx)
                    ? ` <span class="sl-badge esc" title="Escalonamiento ${_fcEscIdx}/${_fcEscTotal}${_fcCgid ? ' grp:' + _fcCgid.substring(0,8) : ''}">📊${_fcEscIdx}/${_fcEscTotal}</span>`
                    : '';
                return `<tr style="cursor:pointer" onclick="showFuturesClosedDetailModal('${p.symbol}','${p.side}',${i})">
                    <td><span class="badge-futures">FUT</span> <strong style="color:#9b59b6">${p.symbol}</strong>${_fcEscBadge}</td>
                    <td><span style="color:#9b59b6">${p.side === 'SHORT' ? '▼ SHORT' : '▲ LONG'}</span></td>
                    <td>$${fmt(p.entry_price)} → $${fmt(p.exit_price)}</td>
                    <td class="${pnlCls}">${p.pnl >= 0 ? '+' : ''}${fmt(p.pnl, 4)} USDT</td>
                    <td>${closedAt}</td>
                </tr>`;
            }).join('')}
            </tbody>
        </table>
        </div>
        <div class="positions-summary">
            ${d.positions.length} futuros cerrados (últimos ${periodDays} días) &nbsp;|&nbsp;
            PnL Total: <span class="${pnlCls}"><strong>${totalPnl >= 0 ? '+' : ''}${fmt(totalPnl, 4)} USDT</strong></span>
        </div>`;
    } catch (e) {
        el.innerHTML = '<p class="loading">Error cargando futuros cerrados</p>';
        if (card) card.style.display = 'none';
    }
}

// ── Modal: Detalle Futuros Cerrados ──────────────────────────────────────────────
function showFuturesClosedDetailModal(symbol, side, idx) {
    const pos = _cachedFuturesClosed[idx] ||
                _cachedFuturesClosed.find(p => p.symbol === symbol && p.side === side);
    if (!pos) return;

    const modalId = 'positionDetailModal';
    let modal = document.getElementById(modalId);
    if (!modal) {
        modal = document.createElement('div');
        modal.id = modalId;
        modal.className = 'modal-detail';
        document.body.appendChild(modal);
    }

    const pnl      = parseFloat(pos.pnl || 0);
    const pnlCls   = pnl >= 0 ? 'positive' : 'negative';
    const isShort  = (pos.side || '').toUpperCase() === 'SHORT';
    const sideLabel = isShort
        ? '<span style="color:#9b59b6;font-weight:700">▼ SHORT</span>'
        : '<span style="color:#27ae60;font-weight:700">▲ LONG</span>';

    const entryP  = parseFloat(pos.entry_price || 0);
    const exitP   = parseFloat(pos.exit_price  || 0);
    const pnlPct  = entryP > 0 ? ((exitP - entryP) / entryP * 100 * (isShort ? -1 : 1)) : 0;

    // Simplified entry→exit bar (no SL/TP range needed since position is closed)
    const lo = Math.min(entryP, exitP);
    const hi = Math.max(entryP, exitP);
    const range = hi - lo || 1;
    const entryPct = ((entryP - lo) / range * 100).toFixed(1);
    const exitPct  = ((exitP  - lo) / range * 100).toFixed(1);
    const slInfo   = pos.sl_price  ? `$${fmt(pos.sl_price)}`  : '—';
    const tpInfo   = pos.tp_price  ? `$${fmt(pos.tp_price)}`  : '—';

    const fmtDateLocal = (isoStr) => {
        if (!isoStr) return '—';
        try {
            return new Date(isoStr).toLocaleString('es-MX', {
                timeZone: 'America/Mexico_City',
                day: '2-digit', month: 'short',
                hour: '2-digit', minute: '2-digit', hour12: false
            });
        } catch(e) { return isoStr; }
    };

    modal.innerHTML = `
        <div class="modal-overlay" onclick="closePositionDetailModal()">
            <div class="modal-content" onclick="event.stopPropagation()">
                <div class="modal-header">
                    <h3>📦 ${symbol} — Futuro ${side} Cerrado</h3>
                    <button class="modal-close" onclick="closePositionDetailModal()">×</button>
                </div>
                <div class="modal-body">
                    <div class="modal-section">
                        <h4>📍 POSICIÓN</h4>
                        <div class="detail-grid">
                            <div><label>Símbolo:</label><span style="font-weight:700">${symbol}</span></div>
                            <div><label>Lado:</label><span>${sideLabel}</span></div>
                            <div><label>Cantidad:</label><span>${parseFloat(pos.qty || 0).toFixed(4)}</span></div>
                            <div><label>Cerrado:</label><span>${fmtDateLocal(pos.closed_at || pos.opened_at)}</span></div>
                        </div>
                    </div>
                    
                    <div class="modal-section">
                        <h4>📊 COMPORTAMIENTO DEL PRECIO</h4>
                        <div class="modal-price-chart" id="futChartWrap">
                            <div style="text-align:center;color:#888;padding:20px;">Cargando datos históricos...</div>
                        </div>
                    </div>
                    
                    <div class="modal-section">
                        <h4>💵 PRECIOS</h4>
                        <div class="detail-grid">
                            <div><label>Entrada:</label><span>$${fmt(entryP)}</span></div>
                            <div><label>Salida:</label><span class="${pnlCls}">$${fmt(exitP)}</span></div>
                        </div>
                    </div>
                    
                    <div class="modal-section">
                        <h4>🛡️ SL/TP LIFECYCLE</h4>
                        <div class="detail-grid sl-tp-lifecycle">
                            <div><label>Stop Loss Final:</label><span class="negative">${slInfo}</span></div>
                            <div><label>Take Profit Final:</label><span class="positive">${tpInfo}</span></div>
                            <div><label>Razón de Cierre:</label><span>${fmtCloseReason(pos.close_reason)}</span></div>
                        </div>
                    </div>
                    
                    <div class="modal-section">
                        <h4>💰 RESULTADO FINAL</h4>
                        <div class="detail-grid">
                            <div><label>PnL Realizado:</label><span class="${pnlCls}" style="font-size:16px;font-weight:700">${pnl >= 0 ? '+' : ''}${fmt(pnl, 4)} USDT</span></div>
                            <div><label>Movimiento %:</label><span class="${pnlCls}">${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%</span></div>
                            <div><label>Dirección:</label><span>${isShort ? '📉 SHORT — ganancia al bajar' : '📈 LONG — ganancia al subir'}</span></div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `;

    modal.style.display = 'block';
    document.body.style.overflow = 'hidden';
    
    // Load time series chart for closed futures
    const chartWrap = document.getElementById('futChartWrap');
    if (pos.opened_at && pos.closed_at) {
        loadPriceHistory(symbol, pos.opened_at, pos.closed_at).then(priceData => {
            if (priceData && priceData.data && chartWrap) {
                const chart = buildTimeSeriesChart(
                    priceData.data,
                    entryP,
                    exitP,
                    pos.sl_price_initial || pos.sl_price,
                    pos.tp_price_initial || pos.tp_price,
                    pos.sl_price,
                    pos.tp_price,
                    pos.qty
                );
                chartWrap.innerHTML = chart.html;
                attachTimeSeriesChartHover(chart);
            } else if (chartWrap) {
                // Fallback to simple range display
                chartWrap.innerHTML = `
                    <div style="position:relative;height:32px;background:#1a1a1a;border-radius:6px;margin:8px 0">
                        <div style="position:absolute;left:0;top:0;bottom:0;width:100%;display:flex;align-items:center;padding:0 8px;box-sizing:border-box">
                            <span style="font-size:10px;color:#aaa">$${fmt(lo)}</span>
                            <div style="flex:1;position:relative;height:6px;background:#333;border-radius:3px;margin:0 8px">
                                <div style="position:absolute;left:${entryPct}%;top:-3px;width:2px;height:12px;background:#888;border-radius:1px" title="Entrada"></div>
                                <div style="position:absolute;left:${exitPct}%;top:-3px;width:2px;height:12px;background:${pnl >= 0 ? '#4caf50' : '#f44336'};border-radius:1px" title="Salida"></div>
                            </div>
                            <span style="font-size:10px;color:#aaa">$${fmt(hi)}</span>
                        </div>
                    </div>
                    <div style="display:flex;gap:16px;font-size:11px;color:#888;margin-top:4px">
                        <span>● Entrada: <strong style="color:#ccc">$${fmt(entryP)}</strong></span>
                        <span style="color:${pnl >= 0 ? '#4caf50' : '#f44336'}">● Salida: <strong>$${fmt(exitP)}</strong></span>
                    </div>
                `;
            }
        });
    }
}

// ── Market Regime Badge ─────────────────────────────────────────────────────────
function renderMarketRegime(r) {
    const dot  = document.getElementById('marketRegimeDot');
    const text = document.getElementById('marketRegimeText');
    const badge = document.getElementById('marketRegime');
    if (!dot || !text || !r) return;

    const color = r.color || '#888';
    const regime = r.regime || '?';
    const chg = r.day_change_pct != null ? r.day_change_pct : 0;
    const vol = r.vol_ratio != null ? r.vol_ratio : 0;
    const trend = r.btc_trend || '?';

    // Emoji prefix
    const emoji = { 'ALCISTA': '🟢', 'BAJISTA': '🔴', 'LATERAL': '🟡', 'DÉBIL ALZA': '🟡', 'DÉBIL BAJA': '🟠' }[regime] || '⚪';

    dot.style.background = color;
    dot.style.boxShadow = `0 0 6px ${color}80`;
    text.textContent = `${emoji} ${regime}`;
    text.style.color = color;

    badge.title = `BTC 4h: ${trend} | Cambio día: ${chg >= 0 ? '+' : ''}${chg.toFixed(2)}% | Vol ratio: ${vol.toFixed(2)}x`;
}

// ── Ticker Bar ──────────────────────────────────────────────────────────────────
function renderTickerBar(items) {
    const track = document.getElementById('tickerTrack');
    if (!track) return;
    if (!items || items.length === 0) {
        track.innerHTML = '<span class="ticker-loading">Sin datos de precios</span>';
        return;
    }

    function fmtPrice(p, sym) {
        const n = parseFloat(p);
        if (sym === 'BTCUSDT') return '$' + n.toLocaleString('en-US', {minimumFractionDigits: 0, maximumFractionDigits: 0});
        if (sym === 'ETHUSDT') return '$' + n.toLocaleString('en-US', {minimumFractionDigits: 0, maximumFractionDigits: 0});
        if (n >= 100) return '$' + n.toFixed(2);
        if (n >= 1)   return '$' + n.toFixed(3);
        return '$' + n.toFixed(5);
    }

    function buildItem(t) {
        const label = (t.symbol || '').replace('USDT', '');
        const chg = parseFloat(t.change_pct || 0);
        const cls = chg > 0.05 ? 'up' : chg < -0.05 ? 'down' : 'flat';
        const arrow = chg > 0.05 ? '▲' : chg < -0.05 ? '▼' : '▸';
        return `<div class="ticker-item">
            <span class="ticker-symbol">${label}</span>
            <span class="ticker-open">8AM: ${fmtPrice(t.open_price, t.symbol)}</span>
            <span class="ticker-price">${fmtPrice(t.current_price, t.symbol)}</span>
            <span class="ticker-change ${cls}">${arrow} ${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</span>
        </div>`;
    }

    // Duplicate content for seamless infinite loop
    const html = items.map(buildItem).join('');
    track.innerHTML = html + html;
    track.style.width = 'max-content';
}

// ── Main update loop (single fetch per cycle) ─────────────────────────────────
async function updateDashboard() {
    const data = await fetchDashboardData();
    if (!data) return;

    renderSystemStatus(data.system_status);
    renderExecutiveSummary(data);
    renderKPIRow(data);
    renderSignals(data.signals);
    renderBalance(data.balance);
    renderPnlHistory(data.pnl_history);
    renderProfitTracker(data.pnl_by_close_reason);
    renderOpenPositions(data.open_positions);
    renderExpectativaVsRealidad(data);
    renderVolSpikeAudit();  // Auditoría VolSpike Override
    _syncFuturesUnrealPnl();  // Keep _futuresUnrealPnl fresh for KPI (no separate card)
    renderClosedPositions(data.closed_positions);
    renderNextCycle(data.next_cycle);
    renderAnalysisSummary(data.performance, data.closed_positions);
    renderLearning(data.learning);
    renderStrategy(data.learning, data.effective_thresholds);
    renderTradingModeBadge(data.market_regime);
    renderTradingModeCard(data.market_regime);
    renderMonteCarlo(data.monte_carlo_projection);
    renderProfitProjection(data.profit_projection);
    renderMarketRegime(data.market_regime);
    renderFngBadge(data.market_context);
    renderMarketContext(data.market_context);
    renderTickerBar(data.daily_ticker);

    // ── New tab renders ────────────────────────────────────────────────────────
    renderBTCContext(data.market_regime);
    renderPairScanner(data.pair_screener);
    renderPredEvalMarket(data.open_positions);
    renderSignalsMarket(data.signals);
    renderMarketHeatmap(data.daily_ticker);
    renderPnlSvgChart('pnlChartSvg', data.pnl_history);
    renderPnlSvgChart('pnlFullChart', data.pnl_history);
    renderPredVsRealChart('predVsRealChart', data.expectativa_vs_realidad);

    // Optimized params: separate lightweight call (cached 30s in backend)
    try {
        const sp = await fetch(`${API_BASE}/strategy/params`);
        if (sp.ok) renderOptimizedParams(await sp.json());
    } catch (e) { /* silent */ }

    // Futures closed card: refresh every cycle alongside the rest of the dashboard
    renderFuturesClosed();
}

// ── Countdown ticker (local – no API call) ────────────────────────────────────
function tickCountdown() {
    const els = document.querySelectorAll('.countdown');
    if (!els.length) return;
    const first = els[0];
    const parts = first.textContent.split(':');
    let total = parseInt(parts[0] || 0) * 60 + parseInt(parts[1] || 0);
    if (total > 0) total--;
    const m = Math.floor(total / 60);
    const s = total % 60;
    const str = `${m}:${String(s).padStart(2, '0')}`;
    els.forEach(el => { el.textContent = str; });
    const hCd = document.getElementById('headerCountdownValue');
    if (hCd) hCd.textContent = str;
}

// ── Control buttons ───────────────────────────────────────────────────────────
async function controlAction(path) {
    try {
        await fetch(`${API_BASE}${path}`, {
            method: 'POST',
            headers: _authHeaders(),
            body: JSON.stringify({ reason: 'Manual action from dashboard' })
        });
        _cachedData = null;
        await updateDashboard();
    } catch (e) { console.error('Control error:', e); }
}

// ── SVG PnL Chart (with hover tooltip + crosshair) ────────────────────────────
function renderPnlSvgChart(containerId, pnlHistory) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const pnlH = pnlHistory || {};
    const days = pnlH.last_30_days || pnlH.daily_breakdown || [];
    if (!days.length) {
        container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:160px;color:#444;font-size:12px">Sin datos de historial</div>';
        return;
    }
    const W = container.clientWidth || 600, H = 160;
    const PAD = { t:12, r:16, b:28, l:52 };
    const chartW = W - PAD.l - PAD.r;
    const chartH = H - PAD.t - PAD.b;

    // cumulative PnL
    let cum = 0;
    const points = days.map((d, i) => {
        const v = parseFloat(d.pnl ?? d.total_pnl ?? 0);
        cum += v;
        return { x: i, y: cum, v, date: d.date || String(i) };
    });

    const minY = Math.min(0, ...points.map(p => p.y));
    const maxY = Math.max(0, ...points.map(p => p.y));
    const rangeY = maxY - minY || 1;
    const scaleX = chartW / Math.max(points.length - 1, 1);
    const scaleY = chartH / rangeY;

    const toX = i  => PAD.l + i * scaleX;
    const toY = yv => PAD.t + chartH - (yv - minY) * scaleY;
    const zeroY = toY(0);

    // Build SVG path
    const pts = points.map((p, i) => `${i === 0 ? 'M' : 'L'}${toX(i).toFixed(1)},${toY(p.y).toFixed(1)}`).join(' ');
    const fillPts = `${pts} L${toX(points.length-1).toFixed(1)},${zeroY.toFixed(1)} L${toX(0).toFixed(1)},${zeroY.toFixed(1)} Z`;

    const lastY = points[points.length-1]?.y ?? 0;
    const lineColor = lastY >= 0 ? '#4ecca3' : '#e74c3c';

    // Y-axis labels
    const yTicks = 3;
    const yLabels = Array.from({length: yTicks+1}, (_, i) => {
        const v = minY + (rangeY / yTicks) * i;
        return `<text x="${PAD.l-6}" y="${toY(v).toFixed(1)}" text-anchor="end" fill="#555" font-size="10" dominant-baseline="middle">${v>=0?'+':''}${v.toFixed(1)}</text>`;
    }).join('');

    // X-axis labels (every ~7 days)
    const xLabels = points.filter((_, i) => i % 7 === 0 || i === points.length-1).map(p => {
        const label = p.date ? p.date.slice(5) : p.x;
        return `<text x="${toX(p.x).toFixed(1)}" y="${H-6}" text-anchor="middle" fill="#555" font-size="9">${label}</text>`;
    }).join('');

    // Invisible hit circles for hover
    const hitCircles = points.map((p, i) =>
        `<circle cx="${toX(i).toFixed(1)}" cy="${toY(p.y).toFixed(1)}" r="6" fill="transparent" class="pnl-hit-dot" data-i="${i}"/>`
    ).join('');

    const svgId = `pnl-svg-${containerId}`;
    const crossId = `pnl-cross-${containerId}`;
    const tipId = `pnl-tip-${containerId}`;
    const dotId = `pnl-dot-${containerId}`;

    container.innerHTML = `<div style="position:relative">
<svg id="${svgId}" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" style="cursor:crosshair;display:block">
    <defs>
        <linearGradient id="pnlGrad${containerId}" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="${lineColor}" stop-opacity="0.4"/>
            <stop offset="100%" stop-color="${lineColor}" stop-opacity="0.02"/>
        </linearGradient>
    </defs>
    <line x1="${PAD.l}" y1="${zeroY.toFixed(1)}" x2="${W-PAD.r}" y2="${zeroY.toFixed(1)}" stroke="#333" stroke-width="1" stroke-dasharray="3,3"/>
    <path d="${fillPts}" fill="url(#pnlGrad${containerId})"/>
    <path d="${pts}" fill="none" stroke="${lineColor}" stroke-width="1.5" stroke-linejoin="round"/>
    ${yLabels}
    ${xLabels}
    <line id="${crossId}" x1="0" y1="${PAD.t}" x2="0" y2="${H-PAD.b}" stroke="rgba(255,255,255,0.25)" stroke-width="1" stroke-dasharray="3,2" style="display:none;pointer-events:none"/>
    <circle id="${dotId}" cx="0" cy="0" r="4" fill="${lineColor}" stroke="#1a1a2e" stroke-width="2" style="display:none;pointer-events:none"/>
    ${hitCircles}
</svg>
<div id="${tipId}" style="display:none;position:absolute;top:8px;left:60px;background:#1e1e2e;border:1px solid ${lineColor};border-radius:6px;padding:5px 10px;font-size:12px;pointer-events:none;z-index:50;white-space:nowrap;box-shadow:0 4px 12px rgba(0,0,0,0.5)"></div>
</div>`;

    // Attach hover events
    const svg = document.getElementById(svgId);
    const cross = document.getElementById(crossId);
    const tipEl = document.getElementById(tipId);
    const dotEl = document.getElementById(dotId);
    if (!svg) return;

    svg.addEventListener('mousemove', (e) => {
        const rect = svg.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        // Find nearest point
        let best = 0, bestDist = Infinity;
        points.forEach((p, i) => {
            const px = toX(i);
            const d = Math.abs(mx - px);
            if (d < bestDist) { bestDist = d; best = i; }
        });
        const p = points[best];
        const px = toX(best), py = toY(p.y);
        cross.setAttribute('x1', px); cross.setAttribute('x2', px);
        cross.style.display = 'block';
        dotEl.setAttribute('cx', px); dotEl.setAttribute('cy', py);
        dotEl.style.display = 'block';
        const dateStr = p.date ? p.date.slice(5) : `Día ${best+1}`;
        const daySign = p.v >= 0 ? '+' : '';
        const cumSign = p.y >= 0 ? '+' : '';
        const dayColor = p.v >= 0 ? '#4ecca3' : '#e74c3c';
        const cumColor = p.y >= 0 ? '#4ecca3' : '#e74c3c';
        tipEl.innerHTML = `<span style="color:#888;font-size:11px">${dateStr}</span><br>
            Día: <span style="color:${dayColor};font-weight:700">${daySign}${p.v.toFixed(2)} USDT</span><br>
            Acum: <span style="color:${cumColor};font-weight:700">${cumSign}${p.y.toFixed(2)} USDT</span>`;
        // Position tip to not overflow right edge
        const tipLeft = px + 10 > W - 120 ? px - 130 : px + 10;
        tipEl.style.left = tipLeft + 'px';
        tipEl.style.display = 'block';
    });
    svg.addEventListener('mouseleave', () => {
        cross.style.display = 'none';
        dotEl.style.display = 'none';
        tipEl.style.display = 'none';
    });
}

// ── BTC Context render ────────────────────────────────────────────────────────
function renderBTCContext(regime) {
    const el = document.getElementById('btcContextData');
    if (!el || !regime) return;
    // Fallback: get BTC price from daily_ticker if regime.btc_price not populated
    const _btcTicker = (_cachedData?.daily_ticker || []).find(t => t.symbol === 'BTCUSDT');
    const btcPrice = parseFloat(regime.btc_price || _btcTicker?.current_price || 0);
    const btc24h   = parseFloat(regime.btc_24h_change_pct ?? regime.btc_7d_change_pct ?? 0);
    const btc7d    = parseFloat(regime.btc_7d_change_pct ?? 0);
    const btc30d   = parseFloat(regime.btc_30d_change_pct ?? 0);
    const vol      = parseFloat(regime.btc_volume_24h_usd ?? 0);
    const trend    = regime.btc_trend || regime.btc_4h_trend || '—';
    const rsi      = parseFloat(regime.btc_rsi ?? 0);
    const priceCls = btc24h >= 0 ? 'positive' : 'negative';
    const trendCls = trend === 'UP' ? 'positive' : trend === 'DOWN' ? 'negative' : '';

    el.innerHTML = `
        <div style="margin-bottom:10px">
            <span class="btc-price-big">$${btcPrice ? btcPrice.toLocaleString('en-US',{minimumFractionDigits:0,maximumFractionDigits:0}) : '—'}</span>
            <span class="btc-change ${priceCls}">${btc24h>=0?'+':''}${btc24h.toFixed(2)}% (24h)</span>
        </div>
        <div class="btc-metrics">
            <div class="btc-metric">
                <div class="btc-metric-label">Tendencia 4h</div>
                <div class="btc-metric-val ${trendCls}">${trend === 'UP' ? '⬆ UP' : trend === 'DOWN' ? '⬇ DOWN' : '→ FLAT'}</div>
            </div>
            <div class="btc-metric">
                <div class="btc-metric-label">RSI 4h</div>
                <div class="btc-metric-val ${rsi>70?'negative':rsi<30?'positive':''}">${rsi ? rsi.toFixed(1) : '—'}</div>
            </div>
            <div class="btc-metric">
                <div class="btc-metric-label">7d</div>
                <div class="btc-metric-val ${btc7d>=0?'positive':'negative'}">${btc7d>=0?'+':''}${btc7d.toFixed(2)}%</div>
            </div>
            <div class="btc-metric">
                <div class="btc-metric-label">30d</div>
                <div class="btc-metric-val ${btc30d>=0?'positive':'negative'}">${btc30d>=0?'+':''}${btc30d.toFixed(2)}%</div>
            </div>
        </div>
        ${vol ? `<div style="font-size:11px;color:#555;margin-top:6px">Vol 24h: $${(vol/1e9).toFixed(2)}B</div>` : ''}
    `;
}

// ── Pair Scanner render ───────────────────────────────────────────────────────
function renderPairScanner(pairScreener) {
    const el = document.getElementById('pairScannerData');
    if (!el) return;
    const pairs = Array.isArray(pairScreener) ? pairScreener : (pairScreener?.pairs || pairScreener?.candidates || []);
    if (!pairs.length) {
        el.innerHTML = '<div class="loading">Sin datos del screener</div>';
        return;
    }
    const header = `<div class="scanner-row scanner-header">
        <span class="scanner-sym">PAR</span>
        <span>SCORE</span>
        <span>RSI</span>
        <span>Vol</span>
        <span>24h%</span>
        <span>Trend</span>
        <span>BIAS</span>
    </div>`;
    el.innerHTML = header + pairs.slice(0, 20).map(p => {
        const score = parseFloat(p.composite_score ?? p.score ?? 0);
        const rsi   = parseFloat(p.rsi_4h ?? p.rsi ?? 0);
        const vol   = parseFloat(p.vol_spike ?? p.volume_spike_ratio ?? 0);
        const chg24 = parseFloat(p.change_24h ?? p.price_change_24h ?? 0);
        const trend = p.trend_4h ?? p.trend ?? '—';
        const bias  = p.bias ?? (score >= 0.6 ? 'LONG' : score <= 0.35 ? 'SHORT' : 'NEUTRAL');
        const bCls  = bias === 'LONG' ? 'scanner-long' : bias === 'SHORT' ? 'scanner-short' : 'scanner-neutral';
        const chgCls = chg24 >= 0 ? 'scanner-up' : 'scanner-down';
        const trendCls = trend === 'UP' ? 'scanner-up' : trend === 'DOWN' ? 'scanner-down' : 'scanner-flat';
        const scoreCls = score >= 0.65 ? 'positive' : score >= 0.50 ? '' : 'negative';
        return `<div class="scanner-row">
            <span class="scanner-sym">${p.symbol || '—'}</span>
            <span class="${scoreCls}">${(score*100).toFixed(0)}%</span>
            <span style="color:${rsi>70?'#e74c3c':rsi<30?'#4ecca3':'#aaa'}">${rsi.toFixed(1)}</span>
            <span style="color:${vol>1.5?'#4ecca3':'#888'}">${vol.toFixed(1)}x</span>
            <span class="${chgCls}">${chg24>=0?'+':''}${chg24.toFixed(2)}%</span>
            <span class="${trendCls}">${trend}</span>
            <span><span class="scanner-badge ${bCls}">${bias}</span></span>
        </div>`;
    }).join('');
}

// ── PredEval render (Tab 2) ───────────────────────────────────────────────────
function renderPredEvalMarket(openPositions) {
    const el = document.getElementById('predEvalMarketData');
    if (!el) return;
    const positions = openPositions?.positions || [];
    const withPred = positions.filter(p => p.pnl_prediction);
    if (!withPred.length) {
        el.innerHTML = '<div class="loading">Sin predicciones activas</div>';
        return;
    }
    el.innerHTML = withPred.map(p => {
        const pred = p.pnl_prediction || {};
        const ep = parseFloat(pred.expected_pnl || 0);
        const sp = parseFloat(pred.success_probability || 0);
        const ev = parseFloat(pred.ev_score ?? ep);
        const evCls = ev >= 0 ? 'positive' : 'negative';
        const statusLabel = ev >= 0.5 ? '🟢 Mantener' : ev >= 0 ? '🟡 Vigilar' : '🔴 Riesgo';
        return `<div class="predeval-item">
            <span class="predeval-sym">${p.symbol}</span>
            <span class="predeval-status">${statusLabel} <span style="color:#555;font-size:10px">(${(sp*100).toFixed(0)}%)</span></span>
            <span class="predeval-ev ${evCls}">${ev>=0?'+':''}${ev.toFixed(3)}</span>
        </div>`;
    }).join('');
}

// ── Market Heatmap (cambio 24h de todos los pares del ticker) ─────────────────
function renderMarketHeatmap(tickerItems) {
    const el = document.getElementById('marketHeatmapData');
    if (!el) return;
    const items = Array.isArray(tickerItems) ? tickerItems : [];
    if (!items.length) {
        el.innerHTML = '<div class="loading">Sin datos del ticker</div>';
        return;
    }
    // Sort by change_pct desc
    const sorted = [...items].sort((a, b) => parseFloat(b.change_pct || 0) - parseFloat(a.change_pct || 0));
    const cells = sorted.map(t => {
        const sym = (t.symbol || '').replace('USDT', '');
        const chg = parseFloat(t.change_pct || 0);
        const vol = parseFloat(t.volume_usdt || t.volume || 0);
        const volStr = vol > 1e9 ? `${(vol/1e9).toFixed(1)}B` : vol > 1e6 ? `${(vol/1e6).toFixed(0)}M` : '';
        let cls;
        if (chg >= 3)       cls = 'heatmap-strong-up';
        else if (chg >= 0.5) cls = 'heatmap-mild-up';
        else if (chg > -0.5) cls = 'heatmap-flat';
        else if (chg > -3)   cls = 'heatmap-mild-down';
        else                  cls = 'heatmap-strong-down';
        const sign = chg >= 0 ? '+' : '';
        return `<div class="heatmap-cell ${cls}" title="${t.symbol}: ${sign}${chg.toFixed(2)}%${volStr ? ' | Vol '+volStr : ''}">
            <span class="heatmap-symbol">${sym}</span>
            <span class="heatmap-chg">${sign}${chg.toFixed(2)}%</span>
            ${volStr ? `<span class="heatmap-vol">${volStr}</span>` : ''}
        </div>`;
    }).join('');
    el.innerHTML = `<div class="heatmap-grid">${cells}</div>`;
}

// ── Signals mirror for Market tab ─────────────────────────────────────────────
function renderSignalsMarket(d) {
    const targetId = 'signalsDataMarket';
    const elM = document.getElementById(targetId);
    if (!elM || !d) return;
    const elMain = document.getElementById('signalsData');
    if (elMain) elM.innerHTML = elMain.innerHTML;
}

// ── Performance SVG: Expectativa vs Realidad scatter (with hover tooltip) ─────
function renderPredVsRealChart(containerId, expectativaData) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const data = expectativaData || {};
    const items = (data.details || data.items || []).filter(d => d.prediction_pnl != null && d.actual_pnl != null);
    if (!items.length) {
        container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:200px;color:#444;font-size:12px">Sin datos de predicción</div>';
        return;
    }
    const W = container.clientWidth || 400, H = 220;
    const PAD = 40;
    const chartW = W - PAD*2, chartH = H - PAD*2;

    const xs = items.map(d => parseFloat(d.prediction_pnl));
    const ys = items.map(d => parseFloat(d.actual_pnl));
    const minX = Math.min(...xs), maxX = Math.max(...xs);
    const minY = Math.min(...ys), maxY = Math.max(...ys);
    const padR = Math.max(Math.abs(maxX - minX), Math.abs(maxY - minY)) * 0.08 || 0.5;
    const rx = (maxX - minX + padR*2) || 1, ry = (maxY - minY + padR*2) || 1;
    const toX = v => PAD + (v - (minX - padR)) / rx * chartW;
    const toY = v => PAD + chartH - (v - (minY - padR)) / ry * chartH;

    // Diagonal (perfect prediction line)
    const dx1 = Math.min(minX, minY) - padR, dx2 = Math.max(maxX, maxY) + padR;
    const diag = `M${toX(dx1).toFixed(1)},${toY(dx1).toFixed(1)} L${toX(dx2).toFixed(1)},${toY(dx2).toFixed(1)}`;

    // Y axis labels
    const yTicks = 4;
    const yLabels = Array.from({length: yTicks+1}, (_, i) => {
        const v = (minY - padR) + (ry / yTicks) * i;
        return `<text x="${PAD-5}" y="${toY(v).toFixed(1)}" text-anchor="end" fill="#444" font-size="9" dominant-baseline="middle">${v>=0?'+':''}${v.toFixed(1)}</text>`;
    }).join('');
    const xTicks = 4;
    const xLabels = Array.from({length: xTicks+1}, (_, i) => {
        const v = (minX - padR) + (rx / xTicks) * i;
        return `<text x="${toX(v).toFixed(1)}" y="${H-PAD+12}" text-anchor="middle" fill="#444" font-size="9">${v>=0?'+':''}${v.toFixed(1)}</text>`;
    }).join('');

    // Zero axis lines
    const zeroX = toX(0), zeroY2 = toY(0);
    const zeroLines = `
        <line x1="${zeroX.toFixed(1)}" y1="${PAD}" x2="${zeroX.toFixed(1)}" y2="${H-PAD}" stroke="#2a2a40" stroke-width="1"/>
        <line x1="${PAD}" y1="${zeroY2.toFixed(1)}" x2="${W-PAD}" y2="${zeroY2.toFixed(1)}" stroke="#2a2a40" stroke-width="1"/>`;

    const scatterSvgId = `scatter-svg-${containerId}`;
    const scatterTipId = `scatter-tip-${containerId}`;

    // Dots with data attributes for hover
    const dots = items.map((d, idx) => {
        const cx = toX(parseFloat(d.prediction_pnl)).toFixed(1);
        const cy = toY(parseFloat(d.actual_pnl)).toFixed(1);
        const win = parseFloat(d.actual_pnl) >= 0;
        const color = win ? '#4ecca3' : '#e74c3c';
        const sym = d.symbol || '?';
        const pred = parseFloat(d.prediction_pnl).toFixed(2);
        const real = parseFloat(d.actual_pnl).toFixed(2);
        const acc = d.prediction_accuracy != null ? parseFloat(d.prediction_accuracy).toFixed(0) + '%' : '—';
        const side = d.side || '';
        return `<circle cx="${cx}" cy="${cy}" r="5" fill="${color}" fill-opacity="0.8" stroke="${color}" stroke-width="1"
            class="scatter-dot" style="cursor:pointer"
            data-sym="${sym}" data-pred="${pred}" data-real="${real}" data-acc="${acc}" data-side="${side}" data-idx="${idx}"/>`;
    }).join('');

    container.innerHTML = `<div style="position:relative">
<svg id="${scatterSvgId}" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" style="display:block">
    <line x1="${PAD}" y1="${PAD}" x2="${PAD}" y2="${H-PAD}" stroke="#333" stroke-width="1"/>
    <line x1="${PAD}" y1="${H-PAD}" x2="${W-PAD}" y2="${H-PAD}" stroke="#333" stroke-width="1"/>
    ${zeroLines}
    <path d="${diag}" stroke="#2a3a50" stroke-width="1.5" stroke-dasharray="4,4" fill="none" opacity="0.8"/>
    ${yLabels}
    ${xLabels}
    ${dots}
    <text x="${W/2}" y="${H-2}" text-anchor="middle" font-size="9" fill="#444">← Predicción USDT →</text>
    <text x="10" y="${H/2}" font-size="9" fill="#444" transform="rotate(-90,10,${H/2})">← Real USDT →</text>
    <text x="${W-PAD+2}" y="${PAD-8}" font-size="8" fill="#444" text-anchor="end">línea ideal ↗</text>
</svg>
<div id="${scatterTipId}" style="display:none;position:absolute;background:#1e1e2e;border:1px solid #4ecca3;border-radius:6px;padding:6px 10px;font-size:12px;pointer-events:none;z-index:50;white-space:nowrap;box-shadow:0 4px 12px rgba(0,0,0,0.6)"></div>
</div>`;

    // Hover events on dots
    const svgEl = document.getElementById(scatterSvgId);
    const tipEl = document.getElementById(scatterTipId);
    if (!svgEl || !tipEl) return;

    svgEl.querySelectorAll('.scatter-dot').forEach(dot => {
        dot.addEventListener('mouseenter', (e) => {
            const sym  = dot.dataset.sym;
            const pred = parseFloat(dot.dataset.pred);
            const real = parseFloat(dot.dataset.real);
            const acc  = dot.dataset.acc;
            const side = dot.dataset.side ? ` <span style="color:#888">${dot.dataset.side}</span>` : '';
            const realColor = real >= 0 ? '#4ecca3' : '#e74c3c';
            const predColor = pred >= 0 ? '#4ecca3' : '#e74c3c';
            tipEl.innerHTML = `<strong style="color:#ddd">${sym}${side}</strong><br>
                Predicción: <span style="color:${predColor};font-weight:700">${pred>=0?'+':''}${pred} USDT</span><br>
                Real: <span style="color:${realColor};font-weight:700">${real>=0?'+':''}${real} USDT</span><br>
                Accuracy: <span style="color:#f39c12">${acc}</span>`;
            const rect = svgEl.getBoundingClientRect();
            const dx = e.clientX - rect.left;
            const dy = e.clientY - rect.top;
            tipEl.style.left = (dx + 12 > W - 160 ? dx - 170 : dx + 12) + 'px';
            tipEl.style.top  = (dy - 10) + 'px';
            tipEl.style.display = 'block';
            dot.setAttribute('r', '7');
            dot.style.fillOpacity = '1';
        });
        dot.addEventListener('mouseleave', () => {
            tipEl.style.display = 'none';
            dot.setAttribute('r', '5');
            dot.style.fillOpacity = '0.8';
        });
    });
}

document.addEventListener('DOMContentLoaded', () => {
    // ── Tab navigation ────────────────────────────────────────────────────────
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const tabId = btn.dataset.tab;
            if (tabId) switchTab(tabId);
        });
    });

    document.getElementById('pauseBtn').addEventListener('click', () => controlAction('/control/pause'));
    document.getElementById('stopBtn').addEventListener('click', () => controlAction('/control/stop'));
    document.getElementById('startBtn').addEventListener('click', () => controlAction('/control/start'));

    // ── Backtest run button ──────────────────────────────────────────────────
    document.getElementById('runBacktestBtn').addEventListener('click', async () => {
        const btn = document.getElementById('runBacktestBtn');
        const symbol = document.getElementById('backtestSymbol').value;
        const days = document.getElementById('backtestDays').value;
        btn.disabled = true;
        btn.textContent = '⏳ Iniciando...';
        try {
            const res = await fetch(
                `${API_BASE}/backtest/run?symbol=${symbol}&days_back=${days}`,
                { method: 'POST', headers: _authHeaders() }
            );
            if (res.ok) {
                btn.textContent = '⏳ Corriendo...';
                // Poll every 5s until done
                const pollId = setInterval(async () => {
                    await pollBacktestResults();
                    if (!_pendingBacktests.length) {
                        clearInterval(pollId);
                        btn.disabled = false;
                        btn.textContent = '▶ Ejecutar';
                    }
                }, 5000);
                await pollBacktestResults();
            } else {
                btn.textContent = '❌ Error';
                setTimeout(() => { btn.disabled = false; btn.textContent = '▶ Ejecutar'; }, 3000);
            }
        } catch (e) {
            btn.textContent = '❌ Error';
            setTimeout(() => { btn.disabled = false; btn.textContent = '▶ Ejecutar'; }, 3000);
        }
    });

    // ── Trigger learning button ──────────────────────────────────────────────
    document.getElementById('triggerLearningBtn').addEventListener('click', async () => {
        const btn = document.getElementById('triggerLearningBtn');
        btn.disabled = true;
        btn.textContent = '⏳ Aprendiendo...';
        try {
            const res = await fetch(`${API_BASE}/learning/run`, { method: 'POST', headers: _authHeaders() });
            if (res.ok) {
                setTimeout(async () => {
                    _cachedData = null;
                    await updateDashboard();
                    btn.disabled = false;
                    btn.textContent = '⚡ Re-Aprender';
                }, 4000);
            } else {
                btn.textContent = '❌ Error';
                setTimeout(() => { btn.disabled = false; btn.textContent = '⚡ Re-Aprender'; }, 3000);
            }
        } catch (e) {
            btn.textContent = '❌ Error';
            setTimeout(() => { btn.disabled = false; btn.textContent = '⚡ Re-Aprender'; }, 3000);
        }
    });

    // ── Sync Balance button ──────────────────────────────────────
    document.getElementById('syncBalanceBtn').addEventListener('click', async () => {
        const btn = document.getElementById('syncBalanceBtn');
        btn.disabled = true;
        btn.textContent = '⏳ Sincronizando...';
        try {
            const res = await fetch(`${API_BASE}/positions/sync`, { method: 'POST' });
            const data = await res.json();
            if (res.ok) {
                const msg = data.count > 0
                    ? `✅ ${data.count} importado(s): ${data.imported.join(', ')}`
                    : '✅ Sin activos huérfanos';
                btn.textContent = msg;
                await updateDashboard();
            } else {
                btn.textContent = '❌ Error';
            }
        } catch (e) {
            btn.textContent = '❌ Error';
        }
        setTimeout(() => { btn.disabled = false; btn.textContent = '⇄ Sincronizar Saldo'; }, 4000);
    });

    // ── Bootstrap Predictor button ───────────────────────────────
    document.getElementById('bootstrapBtn').addEventListener('click', async () => {
        const btn = document.getElementById('bootstrapBtn');
        btn.disabled = true;
        btn.textContent = '⏳ Cargando 90d...';
        try {
            const res = await fetch(`${API_BASE}/predictor/bootstrap?days=90`, { method: 'POST' });
            const data = await res.json();
            if (res.ok && data.status === 'completed') {
                const wr = data.global_win_rate != null ? (data.global_win_rate * 100).toFixed(1) : '?';
                btn.textContent = `✅ ${data.total_simulated_trades} trades, WR=${wr}%`;
            } else if (res.ok && data.status === 'skipped') {
                btn.textContent = '⚠️ Sin señales';
            } else {
                btn.textContent = '❌ Error';
            }
        } catch (e) {
            btn.textContent = '❌ Error de red';
        }
        setTimeout(() => { btn.disabled = false; btn.textContent = '📊 Bootstrap Predictor (90d)'; }, 6000);
    });

    // ── Refresh Health button ────────────────────────────────────
    document.getElementById('refreshHealthBtn').addEventListener('click', () => updateHealthAndErrors());

    // ── Clear Errors button ──────────────────────────────────────
    document.getElementById('clearErrorsBtn').addEventListener('click', async () => {
        await fetch(`${API_BASE}/errors/clear`, { method: 'POST' });
        await updateHealthAndErrors();
    });

    // ── Full Health Check button ──────────────────────────────────
    document.getElementById('runFullHealthBtn').addEventListener('click', async () => {
        const btn = document.getElementById('runFullHealthBtn');
        const card = document.getElementById('fullHealthCard');
        const dataEl = document.getElementById('fullHealthData');
        btn.disabled = true;
        btn.textContent = '⏳ Ejecutando...';
        card.style.display = 'block';
        dataEl.innerHTML = '<p class="loading">Ejecutando 80+ checks del sistema...</p>';
        try {
            const res = await fetch(`${API_BASE}/health/full-check`);
            const data = res.ok ? await res.json() : null;
            renderFullHealthCheck(data);
        } catch (e) {
            dataEl.innerHTML = `<p class="loading" style="color:#f44">Error: ${e.message}</p>`;
        } finally {
            btn.disabled = false;
            btn.textContent = '🩺 Full Health';
        }
    });

    // ── Close Full Health panel ───────────────────────────────────
    document.getElementById('closeFullHealthBtn').addEventListener('click', () => {
        document.getElementById('fullHealthCard').style.display = 'none';
    });

    updateDashboard();
    updateHealthAndErrors();
    pollBacktestResults();
    setInterval(updateDashboard, POLL_INTERVAL_MS);
    setInterval(updateHealthAndErrors, 60000);  // Health & errors refresh every 60s
    // Toggle MXN re-renders open positions
    const toggleMxnEl = document.getElementById('toggleMxn');
    if (toggleMxnEl) toggleMxnEl.addEventListener('change', () => {
        if (_cachedData) renderOpenPositions(_cachedData.open_positions);
    });

    setInterval(tickCountdown, 1000);
});
