const API_BASE = 'http://localhost:8000';

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
        <span style="color:#666;font-size:11px">RAZÓN</span>
        <span style="color:#666;font-size:11px">PNL</span>
        <span style="color:#666;font-size:11px">HACE</span>
    </div>`;
    el.innerHTML = header + d.signals.slice(0, 15).map(s => {
        const isBuy = (s.action || '').toUpperCase() === 'BUY';
        const badge = isBuy
            ? '<span class="signal-badge signal-buy">BUY</span>'
            : '<span class="signal-badge signal-sell">SELL</span>';
        const price = s.price ? `$${parseFloat(s.price).toFixed(4)}` : '—';
        const reason = s.reason ? `<span class="signal-reason">${s.reason}</span>` : '';
        const pnl = s.pnl_pct != null
            ? `<span class="${s.pnl_pct >= 0 ? 'positive' : 'negative'}">${s.pnl_pct > 0 ? '+' : ''}${s.pnl_pct.toFixed(2)}%</span>`
            : '';
        const ts = s.received_at ? relativeTime(s.received_at) : '';
        return `
        <div class="signal-row">
            <span class="signal-symbol">${s.symbol || '—'}</span>
            ${badge}
            <span class="signal-price">${price}</span>
            ${reason}
            ${pnl}
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
    document.getElementById('balanceData').innerHTML = `
        <div class="data-item"><span class="data-label">Total USDT</span><span class="data-value">$${fmt(d.total_usdt)}</span></div>
        <div class="data-item"><span class="data-label">Total MXN</span><span class="data-value">$${fmt(d.total_mxn)}</span></div>
        <div class="data-item"><span class="data-label">USD/MXN</span><span class="data-value">${fmt(d.usd_to_mxn_rate)}</span></div>
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
                    <th>Lado</th>
                    <th>Qty</th>
                    <th>Entrada</th>
                    <th>Actual</th>
                    <th>Invertido (MXN)</th>
                    <th>Valor actual (MXN)</th>
                    <th>PnL No Real.</th>
                    <th>PnL Pred.</th>
                    <th>SL / TP</th>
                    <th>Horas</th>
                </tr>
            </thead>
            <tbody>
                ${d.positions.map(p => `
                    <tr>
                        <td title="ID: ${p.position_id || ''}">E${p.entry_number || 1}</td>
                        <td>${p.symbol}</td>
                        <td>${p.side}</td>
                        <td>${fmt(p.quantity, 4)}</td>
                        <td>$${fmt(p.entry_price)}</td>
                        <td>$${fmt(p.current_price)}</td>
                        <td>$${fmt(p.invested_mxn ?? (p.invested_usdt * 17.5), 2)} MXN</td>
                        <td>$${fmt(p.current_value_mxn ?? 0, 2)} MXN</td>
                        <td class="${pnlClass(p.unrealized_pnl)}">${fmt(p.unrealized_pnl)} USDT<br><small class="${pnlClass(p.unrealized_pnl_mxn)}">${fmt(p.unrealized_pnl_mxn ?? 0, 2)} MXN</small></td>
                        <td>${fmtPrediction(p.pnl_prediction)}</td>
                        <td class="sl-tp">${p.stop_loss ? '$' + fmt(p.stop_loss) : '—'} / ${p.take_profit ? '$' + fmt(p.take_profit) : '—'}</td>
                        <td>${p.hours_held ?? '—'}h</td>
                    </tr>`).join('')}
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
                    return `
                    <tr>
                        <td>${p.symbol}</td>
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

function renderAnalysisSummary(d) {
    if (!d || d.error) return;
    const m = d.metrics || {};
    document.getElementById('analysisSummaryData').innerHTML = `
        <div class="data-item"><span class="data-label">Win Rate</span><span class="data-value">${fmt(m.win_rate)}%</span></div>
        <div class="data-item"><span class="data-label">Profit Factor</span><span class="data-value">${fmt(m.profit_factor)}</span></div>
        <div class="data-item"><span class="data-label">Max Drawdown</span><span class="data-value ${m.max_drawdown <= 0 ? 'positive' : 'negative'}">${fmt(m.max_drawdown)}%</span></div>
        <div class="data-item"><span class="data-label">Total Trades</span><span class="data-value">${m.total_trades ?? 0}</span></div>
        <div class="data-item"><span class="data-label">Ganadores</span><span class="data-value positive">${m.winning_trades ?? 0}</span></div>
        <div class="data-item"><span class="data-label">Perdedores</span><span class="data-value negative">${m.losing_trades ?? 0}</span></div>
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
    const lastRun = d.last_run ? new Date(d.last_run).toLocaleString('es-MX') : '—';

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
function renderStrategy(d) {
    const el = document.getElementById('strategyData');
    if (!d) { el.innerHTML = '<p class="loading">Sin datos de estrategia</p>'; return; }
    if (d.error) { el.innerHTML = `<p class="loading">${d.error}</p>`; return; }

    const w = d.active_weights || {};
    const bias = parseFloat(d.bias_correction || 1.0);
    const DEFAULTS = { rsi: 0.15, macd: 0.10, bb: 0.10, ema: 0.10, volume: 0.05 };

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
        </div>`;
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

// ── Main update loop (single fetch per cycle) ─────────────────────────────────
async function updateDashboard() {
    const data = await fetchDashboardData();
    if (!data) return;

    renderSystemStatus(data.system_status);
    renderPortfolio(data.portfolio_config);
    renderSignals(data.signals);
    renderHealth(data.health);
    renderBalance(data.balance);
    renderPnlHistory(data.pnl_history);
    renderOpenPositions(data.open_positions);
    renderClosedPositions(data.closed_positions);
    renderNextCycle(data.next_cycle);
    renderAnalysisSummary(data.performance);
    renderLearning(data.learning);
    renderStrategy(data.learning);

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
            headers: { 'Content-Type': 'application/json' },
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
                { method: 'POST' }
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
            const res = await fetch(`${API_BASE}/learning/run`, { method: 'POST' });
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
