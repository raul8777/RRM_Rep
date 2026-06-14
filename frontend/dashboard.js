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

function fmtPrediction(pred) {
    if (!pred) return '<span class="no-pred">Sin predicción</span>';
    const ep = parseFloat(pred.expected_pnl || 0);
    const sp = parseFloat(pred.success_probability || 0);
    return `<span class="pred-badge ${ep >= 0 ? 'positive' : 'negative'}">
        Pred: ${ep.toFixed(3)} USDT (${(sp * 100).toFixed(0)}% éxito)
    </span>`;
}

// ── Category helpers (must be before any render function) ───────────────────
const CAT_ICON  = { 'LARGE_CAP': '🏦', 'MIDCAP_ALT': '📊', 'NARRATIVE': '🤖', 'MEME': '🎭' };
const CAT_LABEL = { 'LARGE_CAP': 'LARGE CAP', 'MIDCAP_ALT': 'MID ALT', 'NARRATIVE': 'NARRATIVE', 'MEME': 'MEME' };
function catIcon(cat) {
    const icon  = CAT_ICON[cat]  || '📊';
    const label = CAT_LABEL[cat] || cat;
    return `<span class="cat-icon" title="${label}">${icon}</span>`;
}

// ── Render functions (pure – no fetch) ───────────────────────────────────────
function renderSystemStatus(d) {
    if (!d) return;
    const dot = document.querySelector('.status-dot');
    const txt = document.querySelector('.status-text');
    if (dot) dot.className = 'status-dot ' + (d.state || 'unknown').toLowerCase();
    if (txt) txt.textContent = d.state || '—';
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
            <h3>Desglose por Activo:</h3>
            <div class="balances-scroll">
            ${Object.entries(d.balances_usdt || {}).filter(([a, v]) => parseFloat(v) >= 5).map(([a, v]) => `
                <div class="data-item">
                    <span class="data-label">${a}</span>
                    <span class="data-value">${fmt(v, 4)} USDT / ${fmt((d.balances_mxn || {})[a])} MXN</span>
                </div>`).join('')}
            </div>
        </div>
    `;
}

function renderProfitProjection(pp) {
    const el = document.getElementById('profitProjectionData');
    if (!el) return;
    if (!pp || pp.error) {
        el.innerHTML = `<p class="loading">${pp && pp.error === 'insufficient_trades' ? 'Insuficientes trades (' + (pp.n_trades||0) + '< 5)' : 'Sin datos'}</p>`;
        return;
    }
    const rate = parseFloat(pp.usd_mxn_rate || 17.8);
    const pRow = (label, obj, objMxn) => {
        const cls = parseFloat(obj.mean) >= 0 ? 'positive' : 'negative';
        return `<tr>
            <td>${label}</td>
            <td class="${cls}">${fmt(obj.mean, 4)} USD</td>
            <td class="${cls}">${fmt(objMxn.mean)} MXN</td>
            <td style="color:#888;font-size:0.82em">${fmt(objMxn.p5)} / ${fmt(objMxn.p95)}</td>
        </tr>`;
    };
    // Progress bar toward $30k MXN/month
    const t30 = pp.target_30k_mxn || {};
    const prog = Math.min(parseFloat(t30.current_progress_pct || 0), 100);
    const s100 = pp.scenario_100k_mxn || {};
    el.innerHTML = `
        <div class="proj-stats">
            <span class="proj-stat">WR <b>${fmt(pp.win_rate, 1)}%</b></span>
            <span class="proj-stat">Trades/mes <b>${fmt(pp.trades_per_month, 1)}</b></span>
            <span class="proj-stat">Avg win <b class="positive">+$${fmt(pp.avg_win_usd, 3)}</b></span>
            <span class="proj-stat">Avg loss <b class="negative">$${fmt(pp.avg_loss_usd, 3)}</b></span>
        </div>
        <table class="proj-table">
            <thead><tr><th>Período</th><th>Esperado</th><th>En MXN</th><th>Rango P5/P95 MXN</th></tr></thead>
            <tbody>
                ${pRow('📅 Hoy',    pp.day,   pp.day_mxn)}
                ${pRow('📆 Semana', pp.week,  pp.week_mxn)}
                ${pRow('🗓️ Mes',    pp.month, pp.month_mxn)}
            </tbody>
        </table>
        <div class="proj-scenario">
            <div class="proj-scenario-title">💰 Con $100,000 MXN (~$${fmt(s100.capital_usd)} USD)</div>
            <div class="proj-scenario-row">
                <span>Ganancia mensual esperada:</span>
                <span class="${parseFloat(s100.monthly_mean_mxn||0)>=0?'positive':'negative'}"><b>$${fmt(s100.monthly_mean_mxn)} MXN</b></span>
            </div>
            <div class="proj-scenario-row">
                <span>Rango P5–P95:</span>
                <span style="color:#aaa">$${fmt(s100.monthly_p5_mxn)} – $${fmt(s100.monthly_p95_mxn)} MXN</span>
            </div>
            <div class="proj-scenario-row">
                <span>Ganancia anual esperada:</span>
                <span class="${parseFloat(s100.annual_mean_mxn||0)>=0?'positive':'negative'}"><b>$${fmt(s100.annual_mean_mxn)} MXN</b></span>
            </div>
        </div>
        <div class="proj-target">
            <div class="proj-target-title">🎯 Objetivo: $30,000 MXN/mes</div>
            <div class="proj-target-row">
                <span>Capital requerido:</span>
                <span><b>$${fmt(t30.capital_required_mxn)} MXN</b> (~$${fmt(t30.capital_required_usd)} USD)</span>
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

function renderOpenPositions(d) {
    if (!d) return;
    const el = document.getElementById('openPositionsData');
    if (!d.positions || d.positions.length === 0) {
        el.innerHTML = '<p class="loading">Sin posiciones abiertas</p>';
        return;
    }
    el.innerHTML = `
        <div class="table-scroll-10">
        <table class="positions-table">
            <thead>
                <tr>
                    <th>#</th>
                    <th>Símbolo</th>
                    <th>Cat</th>
                    <th>Lado</th>
                    <th>Qty</th>
                    <th>Entrada</th>
                    <th>Actual</th>
                    <th>Invertido (MXN)</th>
                    <th>Valor actual (MXN)</th>
                    <th>PnL No Real.</th>
                    <th>PnL Pred.</th>
                    <th>SL / TP</th>
                    <th>%→SL</th>
                    <th>%→TP</th>
                    <th>Horas</th>
                </tr>
            </thead>
            <tbody>
                ${d.positions.map(p => {
                    const rowCls = parseFloat(p.unrealized_pnl||0) > 0 ? 'row-pnl-pos' : parseFloat(p.unrealized_pnl||0) < -0.01 ? 'row-pnl-neg' : '';
                    const curP = parseFloat(p.current_price || 0);
                    const slDistPct = p.stop_loss && curP > 0
                        ? ((parseFloat(p.stop_loss) - curP) / curP * 100)
                        : null;
                    const tpDistPct = p.take_profit && curP > 0
                        ? ((parseFloat(p.take_profit) - curP) / curP * 100)
                        : null;
                    const slCls = slDistPct != null && Math.abs(slDistPct) < 1.0 ? 'negative' : '';
                    const tpCls = tpDistPct != null && Math.abs(tpDistPct) < 1.0 ? 'positive' : '';
                    return `
                    <tr class="${rowCls}">
                        <td title="ID: ${p.position_id || ''}">E${p.entry_number || 1}</td>
                        <td>${p.symbol}</td>
                        <td>${catIcon(p.category || 'MIDCAP_ALT')}</td>
                        <td>${p.side}</td>
                        <td>${fmt(p.quantity, 4)}</td>
                        <td>$${fmt(p.entry_price)}</td>
                        <td>$${fmt(p.current_price)}</td>
                        <td>$${fmt(p.invested_mxn ?? (p.invested_usdt * 17.5), 2)} MXN</td>
                        <td>$${fmt(p.current_value_mxn ?? 0, 2)} MXN</td>
                        <td class="${pnlClass(p.unrealized_pnl)}">${fmt(p.unrealized_pnl)} USDT<br><small class="${pnlClass(p.unrealized_pnl_mxn)}">${fmt(p.unrealized_pnl_mxn ?? 0, 2)} MXN</small></td>
                        <td>${fmtPrediction(p.pnl_prediction)}</td>
                        <td class="sl-tp">${p.stop_loss ? '$' + fmt(p.stop_loss) : '—'} / ${p.take_profit ? '$' + fmt(p.take_profit) : '—'}</td>
                        <td class="${slCls}" title="Distancia al Stop-Loss">${slDistPct != null ? (slDistPct >= 0 ? '+' : '') + slDistPct.toFixed(2) + '%' : '—'}</td>
                        <td class="${tpCls}" title="Distancia al Take-Profit">${tpDistPct != null ? (tpDistPct >= 0 ? '+' : '') + tpDistPct.toFixed(2) + '%' : '—'}</td>
                        <td>${p.hours_held ?? '—'}h</td>
                    </tr>`;
                }).join('')}
            </tbody>
        </table>
        </div>
        <div class="positions-summary">
            Total invertido: <strong>$${fmt(d.positions.reduce((s, p) => s + parseFloat(p.invested_mxn || 0), 0), 2)} MXN</strong>
            &nbsp;|&nbsp;
            Total PnL No Realizado:
            <span class="${pnlClass(d.positions.reduce((s, p) => s + parseFloat(p.unrealized_pnl || 0), 0))}">
                ${fmt(d.positions.reduce((s, p) => s + parseFloat(p.unrealized_pnl || 0), 0))} USDT
                / $${fmt(d.positions.reduce((s, p) => s + parseFloat(p.unrealized_pnl_mxn || 0), 0), 2)} MXN
            </span>
        </div>
    `;
}

function renderClosedPositions(d) {
    if (!d) return;
    const el = document.getElementById('closedPositionsData');
    if (!d.positions || d.positions.length === 0) {
        el.innerHTML = '<p class="loading">Sin posiciones cerradas</p>';
        return;
    }
    el.innerHTML = `
        <div class="table-scroll-10">
        <table class="positions-table">
            <thead>
                <tr>
                    <th>Símbolo</th>
                    <th>Lado</th>
                    <th>PnL Real</th>
                    <th>PnL %</th>
                    <th>PnL Pred.</th>
                    <th>Precisión</th>
                    <th>Razón</th>
                    <th>Cierre</th>
                </tr>
            </thead>
            <tbody>
                ${d.positions.map(p => {
                    const acc = p.prediction_accuracy != null
                        ? `<span class="${parseFloat(p.prediction_accuracy) >= 0 ? 'positive' : 'negative'}">${fmt(p.prediction_accuracy)}%</span>`
                        : '—';
                    const rowCls = parseFloat(p.pnl||0) > 0 ? 'row-pnl-pos' : parseFloat(p.pnl||0) < -0.01 ? 'row-pnl-neg' : '';
                    return `
                    <tr class="${rowCls}">
                        <td>${catIcon(p.category || 'MIDCAP_ALT')} ${p.symbol}</td>
                        <td>${p.side}</td>
                        <td class="${pnlClass(p.pnl)}">${fmt(p.pnl)} USDT</td>
                        <td class="${pnlClass(p.pnl_percentage)}">${fmt(p.pnl_percentage)}%</td>
                        <td>${fmtPrediction(p.pnl_prediction)}</td>
                        <td>${acc}</td>
                        <td>${p.close_reason || '—'}</td>
                        <td>${p.exit_time ? new Date(p.exit_time).toLocaleString('es-MX') : '—'}</td>
                    </tr>`;
                }).join('')}
            </tbody>
        </table>
        </div>
    `;
}

function renderNextCycle(d) {
    if (!d || d.error) return;
    const min = Math.floor(d.seconds_until_next / 60);
    const sec = d.seconds_until_next % 60;
    document.getElementById('nextCycleData').innerHTML = `
        <div class="data-item"><span class="data-label">Intervalo</span><span class="data-value">${d.interval_minutes} min</span></div>
        <div class="data-item"><span class="data-label">Tiempo Restante</span><span class="data-value countdown">${min}:${String(sec).padStart(2, '0')}</span></div>
        <div class="data-item"><span class="data-label">Próximo Ciclo</span><span class="data-value">${d.next_cycle_time ? new Date(d.next_cycle_time).toLocaleTimeString('es-MX') : '—'}</span></div>
    `;
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
        if (key === 'trade_cycle')  detail = c.last_cycle_at ? `Último: ${new Date(c.last_cycle_at).toLocaleTimeString('es-MX')} (${c.age_minutes}min ago)` : (c.detail || 'Sin datos');
        if (key === 'position_sync')detail = `DB: ${c.db_open_positions ?? '?'} | Binance: ${c.binance_holdings ?? '?'}${c.mismatch ? ` | ⚠ Dif: ${c.mismatch}` : ''}`;
        if (key === 'futures_api')  detail = c.latency_ms != null ? `${c.latency_ms} ms${c.open_shorts != null ? ` | ${c.open_shorts} shorts` : ''}` : (c.detail || '');
        if (key === 'social_score') detail = c.enabled ? `✅ activo — ${c.trending_count ?? 0} tokens trending` : '⚪ sin API key';
        if (key === 'errors')       detail = `${c.errors_last_5min || 0} err / ${c.warnings_last_5min || 0} warn (5min) | Total: ${c.total_records || 0}`;
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
    el.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:0.82rem">
        <thead><tr style="color:#aaa;text-align:left">
            <th style="padding:2px 8px">Hora</th>
            <th style="padding:2px 8px">Nivel</th>
            <th style="padding:2px 8px">Fuente</th>
            <th style="padding:2px 8px">Mensaje</th>
        </tr></thead>
        <tbody>${rows}</tbody>
    </table>`;
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
const MODE_CFG = {
    'SPOT_BULL':    { icon: '📈', label: 'SPOT BULL',   color: '#4ecca3' },
    'RANGE_SCALP':  { icon: '↔️',  label: 'LATERAL',    color: '#f39c12' },
    'PRESERVATION': { icon: '🛡',  label: 'PRESERVAR',  color: '#e74c3c' },
    'FUTURES_SHORT':{ icon: '📉',  label: 'SHORT 2×',   color: '#9b59b6' },
};

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

    el.innerHTML = `
        <div class="mode-banner" style="background:${cfg.color}15; border-color:${cfg.color}40">
            <div class="mode-banner-icon">${cfg.icon}</div>
            <div>
                <div class="mode-banner-title" style="color:${cfg.color}">${cfg.label}</div>
                <div class="mode-banner-reason">${r.mode_reason || ''}</div>
            </div>
        </div>
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
            <div class="mode-ind-item">
                <div class="mode-ind-label">Confirmado</div>
                <div class="mode-ind-val ${confirmed ? 'up' : 'flat'}">${confirmed ? '✅ Sí' : '⏳ Pendiente'}</div>
            </div>
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

    const breakdown = mc.monthly_breakdown || [];
    const cap0      = mc.initial_capital  || 346.13;
    const btcFinal  = mc.btc_hodl_final   || cap0;
    const btcPct    = mc.btc_hodl_pct     != null ? mc.btc_hodl_pct : (mc.btc_hodl_180d_pct ?? 0);
    const totalPnl  = mc.total_pnl        != null ? mc.total_pnl    : (mc.total_pnl_post ?? 0);
    const p5        = mc.p5               ?? mc.p5_post  ?? cap0;
    const p50       = mc.p50              ?? cap0;
    const p95       = mc.p95              ?? mc.p95_post ?? cap0;
    const winRate   = mc.win_rate         ?? mc.win_rate_improved ?? 51.5;
    const nTrades   = mc.n_trades_used    ?? 0;
    const genAt     = mc.generated_at     ?? '—';

    const rows = breakdown.map(m => {
        // Support both old format (pnl_pre/pnl_post) and new live format (pnl/capital)
        const cap    = m.capital  ?? m.capital_post ?? (cap0 + (m.pnl ?? 0));
        const pnl    = m.pnl      ?? m.pnl_post     ?? 0;
        const pnlCls = pnl >= 0 ? 'mc-pnl-pos' : 'mc-pnl-neg';
        return `<tr>
            <td>Mes ${m.month}</td>
            <td class="right">${mc.trades_per_month}</td>
            <td class="right">$${cap.toFixed(2)}</td>
            <td class="right ${pnlCls}">${pnl >= 0 ? '+' : ''}$${pnl.toFixed(4)}</td>
        </tr>`;
    }).join('');

    const totalCls = totalPnl >= 0 ? 'mc-pnl-pos' : 'mc-pnl-neg';

    // Progress bar: system final vs BTC final
    const rangeMin = Math.min(btcFinal, cap0 * 0.85);
    const rangeMax = p95 || cap0 * 1.10;
    const rangeDiff = rangeMax - rangeMin;
    const pctBtc  = rangeDiff > 0 ? ((btcFinal  - rangeMin) / rangeDiff * 100).toFixed(1) : 0;
    const pctPost = rangeDiff > 0 ? (((totalPnl + cap0) - rangeMin) / rangeDiff * 100).toFixed(1) : 50;

    el.innerHTML = `
        <div class="mc-header">
            <span class="mc-subtitle">10,000 sims · WR: ${winRate}% · ${nTrades} trades reales · ${(mc.trades_per_month||11)} trades/mes</span>
            <span class="mc-subtitle" style="display:flex;align-items:center;gap:8px">
                <span>Generado: ${genAt}</span>
                <button id="mcRecalcBtn" class="btn btn-primary btn-sm" onclick="recalculateMonteCarlo()">🔄 Recalcular</button>
            </span>
        </div>
        <div style="overflow-x:auto">
        <table class="mc-table">
            <thead><tr>
                <th>Mes</th>
                <th class="right">Trades</th>
                <th class="right">Capital esperado</th>
                <th class="right">PnL mes</th>
            </tr></thead>
            <tbody>${rows}</tbody>
            <tfoot><tr class="mc-total-row">
                <td>TOTAL ${mc.months || 6} meses</td>
                <td class="right">${(mc.trades_per_month || 11) * (mc.months || 6)}</td>
                <td class="right">$${(cap0 + totalPnl).toFixed(2)}</td>
                <td class="right ${totalCls}">${totalPnl >= 0 ? '+' : ''}$${totalPnl.toFixed(4)}</td>
            </tr></tfoot>
        </table>
        </div>
        <div class="mc-summary">
            <div class="mc-summary-item">
                <div class="mc-summary-label">Peor caso P5</div>
                <div class="mc-summary-val" style="color:#e74c3c">$${p5.toFixed(2)}</div>
            </div>
            <div class="mc-summary-item">
                <div class="mc-summary-label">Mediana P50</div>
                <div class="mc-summary-val" style="color:#f39c12">$${p50.toFixed(2)}</div>
            </div>
            <div class="mc-summary-item">
                <div class="mc-summary-label">Mejor caso P95</div>
                <div class="mc-summary-val" style="color:#4ecca3">$${p95.toFixed(2)}</div>
            </div>
            <div class="mc-summary-item">
                <div class="mc-summary-label">HODL BTC (proyectado)</div>
                <div class="mc-summary-val" style="color:#e74c3c">$${btcFinal.toFixed(2)} (${btcPct >= 0 ? '+' : ''}${parseFloat(btcPct).toFixed(1)}%)</div>
            </div>
            <div class="mc-summary-item">
                <div class="mc-summary-label">Ventaja vs BTC</div>
                <div class="mc-summary-val" style="color:#4ecca3">+$${((cap0 + totalPnl) - btcFinal).toFixed(2)}</div>
            </div>
        </div>
        <div class="mc-progress-wrap">
            <div class="mc-progress-label">Comparativa capital final (escala relativa)</div>
            <div style="display:flex;flex-direction:column;gap:6px;margin-top:6px">
                <div style="display:flex;align-items:center;gap:10px;font-size:12px">
                    <span style="width:110px;color:#888">HODL BTC</span>
                    <div class="mc-progress-bar-bg" style="flex:1">
                        <div class="mc-progress-bar-fill" style="width:${pctBtc}%;background:#e74c3c"></div>
                    </div>
                    <span style="width:60px;text-align:right;color:#e74c3c">$${btcFinal.toFixed(0)}</span>
                </div>
                <div style="display:flex;align-items:center;gap:10px;font-size:12px">
                    <span style="width:110px;color:#888">Sist. mejorado</span>
                    <div class="mc-progress-bar-bg" style="flex:1">
                        <div class="mc-progress-bar-fill" style="width:${pctPost}%;background:#4ecca3"></div>
                    </div>
                    <span style="width:60px;text-align:right;color:#4ecca3">$${(cap0 + totalPnl).toFixed(0)}</span>
                </div>
            </div>
        </div>
        <div class="mc-note">* Basado en trades reales cerrados como distribución. El Monte Carlo no incluye compounding completo (Kelly dinámico → retorno real podría ser mayor).</div>
    `;
}

// ── Futures Positions Card (C1) ───────────────────────────────────────────────
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
                return `<tr>
                    <td style="color:#9b59b6;font-weight:600">${p.symbol}</td>
                    <td><span style="color:#9b59b6">▼ SHORT</span></td>
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
    renderSignals(data.signals);
    renderBalance(data.balance);
    renderPnlHistory(data.pnl_history);
    renderOpenPositions(data.open_positions);
    renderFuturesPositions();  // C1: async, fetches /futures/positions independently
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

    // Optimized params: separate lightweight call (cached 30s in backend)
    try {
        const sp = await fetch(`${API_BASE}/strategy/params`);
        if (sp.ok) renderOptimizedParams(await sp.json());
    } catch (e) { /* silent */ }
}

// ── Countdown ticker (local – no API call) ────────────────────────────────────
function tickCountdown() {
    const el = document.querySelector('.countdown');
    if (!el) return;
    const parts = el.textContent.split(':');
    let total = parseInt(parts[0]) * 60 + parseInt(parts[1]);
    if (total > 0) total--;
    const m = Math.floor(total / 60);
    const s = total % 60;
    el.textContent = `${m}:${String(s).padStart(2, '0')}`;
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

document.addEventListener('DOMContentLoaded', () => {
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

    updateDashboard();
    updateHealthAndErrors();
    pollBacktestResults();
    setInterval(updateDashboard, POLL_INTERVAL_MS);
    setInterval(updateHealthAndErrors, 60000);  // Health & errors refresh every 60s
    setInterval(tickCountdown, 1000);
});
