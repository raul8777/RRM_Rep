/**
 * Super MIMIC dashboard controller
 *
 * Three-stage pipeline:
 *   1. Design   → POST /super-mimic/experiment
 *   2. Execute  → POST /super-mimic/experiment/{id}/start + polling
 *   3. Validate → review recommended config and (manual) deploy
 */
(function () {
  "use strict";

  // ── State ─────────────────────────────────────────────────────────────────
  let activeExperimentId = null;
  let pollTimer = null;

  // ── Helpers ───────────────────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);
  const api = async (method, path, body) => {
    const opts = {
      method,
      headers: { "Content-Type": "application/json" },
    };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(path, opts);
    if (!res.ok && res.status !== 404) {
      throw new Error(`HTTP ${res.status}`);
    }
    return res.json();
  };

  const fmtEta = (sec) => {
    if (sec == null || sec < 0) return "ETA: —";
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return `ETA: ${m}m ${s}s`;
  };

  const setStatusDot = (color) => {
    const dot = $("superMimicStatusDot");
    if (dot) dot.style.background = color;
  };

  const setCardVisibility = (id, show) => {
    const el = $(id);
    if (el) el.style.display = show ? "block" : "none";
  };

  // ── Pipeline actions ────────────────────────────────────────────────────
  window.superMimicCreate = async function () {
    const windows = $("smDesignWindows").value
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean);
    const maxCycles = parseInt($("smDesignMaxCycles").value, 10) || null;

    const payload = {
      mode: $("smDesignMode").value,
      profile: $("smDesignProfile").value,
      windows: windows,
      engine: $("smDesignEngine").value,
      strategy: $("smDesignStrategy").value,
      max_cycles: maxCycles,
      max_workers: 1,
    };

    try {
      const res = await api("POST", "/super-mimic/experiment", payload);
      if (!res.ok) throw new Error(res.error || "Create failed");
      activeExperimentId = res.experiment_id;
      $("smActiveId").textContent = activeExperimentId;
      $("smActiveStatus").textContent = "diseñado";
      $("smProgressBar").style.width = "0%";
      $("smEta").textContent = "ETA: —";
      setCardVisibility("superMimicDesignCard", false);
      setCardVisibility("superMimicExecuteCard", true);
      setCardVisibility("superMimicDeployCard", false);
      setStatusDot("#0d8f3c");
      superMimicRefreshList();
    } catch (e) {
      alert("Error creando experimento: " + e.message);
    }
  };

  window.superMimicStart = async function () {
    if (!activeExperimentId) return;
    try {
      const res = await api(
        "POST",
        `/super-mimic/experiment/${activeExperimentId}/start`
      );
      if (!res.ok) throw new Error(res.error || "Start failed");
      $("smActiveStatus").textContent = "en ejecución";
      setStatusDot("#1976d2");
      startPolling();
    } catch (e) {
      alert("Error iniciando experimento: " + e.message);
    }
  };

  window.superMimicCancel = async function () {
    if (!activeExperimentId) return;
    try {
      const res = await api(
        "POST",
        `/super-mimic/experiment/${activeExperimentId}/cancel`
      );
      if (!res.ok) throw new Error(res.error || "Cancel failed");
      stopPolling();
      $("smActiveStatus").textContent = "cancelado";
      setStatusDot("#888");
      superMimicRefreshList();
    } catch (e) {
      alert("Error cancelando: " + e.message);
    }
  };

  window.superMimicDeploy = async function () {
    if (!activeExperimentId) return;
    if (!confirm("¿Desplegar la config recomendada? Se aplicará merge sobre kelly_config_override.")) return;
    try {
      const res = await api(
        "POST",
        `/super-mimic/experiment/${activeExperimentId}/deploy`,
        { target: "kelly_config_override" }
      );
      if (!res.ok) {
        const gate = res.gate || {};
        const warnings = (gate.warnings || []).join("; ");
        throw new Error(res.error + (warnings ? " — " + warnings : ""));
      }
      const kellyApplied = res.kelly && res.kelly.applied ? Object.keys(res.kelly.applied) : [];
      $("smDeployGate").innerHTML =
        `✅ Desplegado. Score ${(res.gate && res.gate.score) || "—"}. Campos: ${kellyApplied.join(", ") || "—"}`;
      $("smDeployGate").style.color = "#69f0ae";
      $("smDeployBtn").disabled = true;
    } catch (e) {
      $("smDeployGate").innerHTML = `<span style="color:#ffab40">Error: ${e.message}</span>`;
    }
  };

  // ── Polling ───────────────────────────────────────────────────────────────
  async function pollActiveExperiment() {
    if (!activeExperimentId) return;
    try {
      const res = await api(
        "GET",
        `/super-mimic/experiment/${activeExperimentId}`
      );
      if (!res.ok || !res.experiment) return;
      updateExperimentUI(res.experiment);
    } catch (e) {
      console.warn("poll error", e);
    }
  }

  function updateExperimentUI(exp) {
    const artifact = exp.artifact || {};
    const status = artifact.status || exp.status || "—";
    $("smActiveStatus").textContent = status;

    // Progress
    const stat = exp.status_data || {};
    const total = stat.total_jobs || 0;
    const completed = stat.completed_jobs || 0;
    const pct = total ? Math.round((completed / total) * 100) : 0;
    $("smProgressBar").style.width = `${pct}%`;
    $("smEta").textContent = fmtEta(stat.eta_seconds);

    // Log tail
    if (artifact.log_tail && artifact.log_tail.length) {
      $("superMimicLogPanel").textContent = artifact.log_tail.join("");
      $("superMimicLogPanel").scrollTop = $("superMimicLogPanel").scrollHeight;
    }

    // Completed -> show deploy card
    if (status === "completed") {
      stopPolling();
      setStatusDot("#0d8f3c");
      setCardVisibility("superMimicExecuteCard", false);
      setCardVisibility("superMimicDeployCard", true);
      renderDeployGate(artifact);
      superMimicRefreshList();
      return;
    }
    if (status === "failed") {
      stopPolling();
      setStatusDot("#e53935");
      $("superMimicRecommendPanel").textContent =
        "Error: " + (artifact.error || "unknown");
      superMimicRefreshList();
      return;
    }
  }

  function renderDeployGate(artifact) {
    const rec = artifact.recommended_config || {};
    const hasRec = Object.keys(rec).length > 0;
    $("smRecommendedConfig").textContent = JSON.stringify(rec, null, 2);

    // Prefer backend deploy_gate if present
    const gate = artifact.deploy_gate || {};
    let gateText = "Pendiente de finalización.";
    let gateOk = false;
    if (gate.passed !== undefined) {
      gateOk = gate.passed;
      const reasons = (gate.reasons || []).join("; ");
      const warnings = (gate.warnings || []).join("; ");
      gateText = gateOk
        ? `✅ Gate aprobado (score ${gate.score}): ${reasons}`
        : `❌ Gate bloqueado: ${warnings}`;
    } else {
      // Fallback to simple local gate
      const engine = artifact.engine_results || {};
      const recs = engine.recommendations || [];
      const best = recs.find(
        (r) => r.profile === artifact.design.profile
      ) || recs[0];
      if (best) {
        const pnlDay = best.best_pnl_per_day_usdt || 0;
        const dd = best.best_drawdown_pct || 100;
        const trades = best.best_trades || 0;
        gateOk = pnlDay > 0 && dd < 15 && trades >= 3;
        gateText = gateOk
          ? `✅ Gate aprobado: PnL/día $${pnlDay.toFixed(2)}, DD ${dd.toFixed(2)}%, ${trades} trades. Revisar antes de desplegar.`
          : `❌ Gate bloqueado: PnL/día $${pnlDay.toFixed(2)}, DD ${dd.toFixed(2)}%, ${trades} trades. No desplegar.`;
      }
    }
    $("smDeployGate").textContent = gateText;
    $("smDeployGate").style.color = gateOk ? "#69f0ae" : "#ffab40";
    $("smDeployBtn").disabled = !(hasRec && gateOk);

    if (gateOk || hasRec) {
      const engine = artifact.engine_results || {};
      const recs = engine.recommendations || [];
      const best = recs.find(
        (r) => r.profile === artifact.design.profile
      ) || recs[0];
      if (best) {
      $("superMimicRecommendPanel").innerHTML = `
        <strong>Perfil ${best.profile}</strong><br>
        PnL/día: $${(best.best_pnl_per_day_usdt || 0).toFixed(2)}<br>
        PnL %: ${(best.best_pnl_pct || 0).toFixed(2)}%<br>
        Drawdown: ${(best.best_drawdown_pct || 0).toFixed(2)}%<br>
        Trades: ${best.best_trades || 0} | Win rate: ${(
        (best.best_win_rate || 0) * 100
      ).toFixed(1)}%<br>
        Profit factor: ${best.best_profit_factor || "—"} | Sharpe: ${
        best.best_sharpe || "—"
      }
      `;
      }
    }
  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(pollActiveExperiment, 2000);
  }
  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  // ── List ─────────────────────────────────────────────────────────────────
  window.superMimicRefreshList = async function () {
    try {
      const res = await api("GET", "/super-mimic/experiments?limit=20");
      if (!res.ok) throw new Error(res.error || "List failed");
      renderList(res.experiments || []);
    } catch (e) {
      console.warn("list error", e);
      $("superMimicListPanel").innerHTML = `<p style="color:#ffab40">${e.message}</p>`;
    }
  };

  function renderList(experiments) {
    if (!experiments.length) {
      $("superMimicListPanel").innerHTML =
        '<p style="font-size:12px;color:#888">No hay experimentos aún.</p>';
      return;
    }
    const rows = experiments
      .map(
        (e) => `
      <div class="sm-row" style="padding:8px;border-bottom:1px solid #333;font-size:12px;cursor:pointer" onclick="superMimicSelect('${e.id}')">
        <div style="display:flex;justify-content:space-between">
          <span style="font-family:monospace;color:#90caf9">${e.id}</span>
          <span class="sm-status" style="color:${statusColor(e.status)}">${e.status}</span>
        </div>
        <div style="color:#aaa;margin-top:4px">${e.mode} / ${e.profile} / ${e.engine}</div>
        <div style="color:#888;font-size:11px">${new Date(e.created_at).toLocaleString()}</div>
      </div>
    `
      )
      .join("");
    $("superMimicListPanel").innerHTML = rows;
  }

  function statusColor(status) {
    const map = {
      designing: "#90caf9",
      running: "#1976d2",
      completed: "#0d8f3c",
      failed: "#e53935",
      cancelled: "#888",
    };
    return map[status] || "#aaa";
  }

  window.superMimicSelect = async function (id) {
    activeExperimentId = id;
    try {
      const res = await api("GET", `/super-mimic/experiment/${id}`);
      if (!res.ok || !res.experiment) return;
      const exp = res.experiment;
      $("smActiveId").textContent = id;
      setCardVisibility("superMimicDesignCard", false);
      setCardVisibility("superMimicExecuteCard", true);
      setCardVisibility("superMimicDeployCard", false);
      updateExperimentUI(exp);
      // If still running, poll
      const status = exp.artifact?.status || exp.status;
      if (status === "running") startPolling();
    } catch (e) {
      console.warn("select error", e);
    }
  };

  // ── Meta-Learner ────────────────────────────────────────────────────────────
  window.superMimicCaptureFingerprint = async function () {
    try {
      const res = await api("GET", "/super-mimic/meta/fingerprint");
      if (!res.ok) throw new Error(res.error || "Fingerprint failed");
      const values = res.fingerprint.values || {};
      let html = "<strong>Regimen fingerprint:</strong><br>";
      Object.entries(values).forEach(([k, v]) => {
        html += `${k}: ${typeof v === "number" ? v.toFixed(3) : v}<br>`;
      });
      $("superMimicFingerprintPanel").innerHTML = html;
    } catch (e) {
      $("superMimicFingerprintPanel").innerHTML = `<span style="color:#ffab40">Error: ${e.message}</span>`;
    }
  };

  window.superMimicWarmStart = async function () {
    const mode = activeExperimentId ? $("smActiveId").textContent : $("smDesignMode").value;
    const profile = $("smDesignProfile").value;
    try {
      const res = await api(
        "GET",
        `/super-mimic/meta/warm-start?mode=${encodeURIComponent(mode)}&profile=${encodeURIComponent(profile)}&top_k=5`
      );
      if (!res.ok) throw new Error(res.error || "Warm-start failed");
      const recs = res.recommendations || [];
      if (!recs.length) {
        $("superMimicWarmStartPanel").textContent = "No hay configs históricas similares.";
        return;
      }
      let html = `<strong>Top ${recs.length} warm-start configs:</strong><br>`;
      recs.forEach((r, i) => {
        html += `<div style="margin-top:6px;padding:6px;background:#1a1a1a;border:1px solid #333;border-radius:4px">`;
        html += `#${i + 1} exp=${r.experiment_id.substring(0, 8)} score=${r.score.toFixed(2)} dist=${r.distance.toFixed(3)}<br>`;
        html += `PnL/día: $${(r.metrics.pnl_per_day_usdt || 0).toFixed(2)} | WR: ${((r.metrics.win_rate || 0) * 100).toFixed(1)}% | trades: ${r.metrics.trades || 0}<br>`;
        html += `<pre style="margin:4px 0 0 0;font-size:10px;overflow:auto">${JSON.stringify(r.config, null, 2)}</pre>`;
        html += `</div>`;
      });
      $("superMimicWarmStartPanel").innerHTML = html;
    } catch (e) {
      $("superMimicWarmStartPanel").innerHTML = `<span style="color:#ffab40">Error: ${e.message}</span>`;
    }
  };

  window.superMimicRetrain = async function () {
    try {
      const res = await api("POST", "/super-mimic/meta/retrain");
      const status = res.ok ? "✅" : "⚠️";
      const result = res.result || {};
      $("superMimicWarmStartPanel").innerHTML =
        `${status} Retrain: ${result.status || "unknown"}<br>` +
        `trades: ${result.trades_analyzed || 0} | symbols: ${(result.symbols_updated || []).length}<br>` +
        `<span style="color:#888">${result.reason || result.error || ""}</span>`;
    } catch (e) {
      $("superMimicWarmStartPanel").innerHTML = `<span style="color:#ffab40">Error: ${e.message}</span>`;
    }
  };

  // ── Quantum Landscape ───────────────────────────────────────────────────────
  function renderHeatmap(landscape) {
    const grid = landscape.grid || [];
    if (!grid.length) return "<em>Sin datos de landscape.</em>";
    const flat = grid.flat().filter(v => v !== null);
    const min = Math.min(...flat);
    const max = Math.max(...flat);
    const range = max - min || 1;
    let html = `<div style="display:grid;gap:1px;grid-template-columns:repeat(${grid[0].length}, 1fr);border:1px solid #333;background:#111;padding:4px;border-radius:4px">`;
    for (const row of grid) {
      for (const v of row) {
        let bg = "#222";
        if (v !== null) {
          const t = (v - min) / range;
          // red (low) -> black (mid) -> green (high)
          const r = Math.round(255 * (1 - t));
          const g = Math.round(255 * t);
          bg = `rgb(${r},${g},40)`;
        }
        html += `<div style="aspect-ratio:1;background:${bg};" title="${v !== null ? v.toFixed(2) : "no data"}"></div>`;
      }
    }
    html += "</div>";
    html += `<div style="margin-top:6px;color:#aaa">${landscape.param_x} vs ${landscape.param_y} | metric: ${landscape.metric}</div>`;
    const pm = landscape.phase_map || {};
    html += `<div style="color:#888">zones: ${JSON.stringify(pm.zones || {})}</div>`;
    return html;
  }

  window.superMimicLoadLandscape = async function () {
    if (!activeExperimentId) {
      $("superMimicLandscapeMap").innerHTML = "<span style='color:#ffab40'>Selecciona un experimento primero.</span>";
      return;
    }
    try {
      const res = await api("GET", `/super-mimic/experiment/${activeExperimentId}/landscape`);
      if (!res.ok) throw new Error(res.error || "Landscape failed");
      const landscape = res.landscape && res.landscape.landscape ? res.landscape.landscape : res.landscape;
      $("superMimicLandscapeMap").innerHTML = renderHeatmap(landscape);
    } catch (e) {
      $("superMimicLandscapeMap").innerHTML = `<span style="color:#ffab40">Error: ${e.message}</span>`;
    }
  };

  window.superMimicLoadAttractors = async function () {
    if (!activeExperimentId) {
      $("superMimicAttractorsPanel").innerHTML = "<span style='color:#ffab40'>Selecciona un experimento primero.</span>";
      return;
    }
    try {
      const res = await api("GET", `/super-mimic/experiment/${activeExperimentId}/attractors`);
      if (!res.ok) throw new Error(res.error || "Attractors failed");
      const attractors = res.attractors || [];
      if (!attractors.length) {
        $("superMimicAttractorsPanel").innerHTML = "No se detectaron atractores.";
        return;
      }
      let html = `<strong>🧲 Atractores detectados (${attractors.length}):</strong><br>`;
      attractors.forEach((a, i) => {
        html += `<div style="margin-top:6px;padding:6px;background:#1a1a1a;border:1px solid #333;border-radius:4px">`;
        html += `#${i + 1} mean=${a.mean_metric.toFixed(2)} std=${a.std_metric.toFixed(3)} DD=${a.max_drawdown.toFixed(2)}%<br>`;
        html += `<pre style="margin:4px 0 0 0;font-size:10px;overflow:auto">${JSON.stringify(a.params, null, 2)}</pre>`;
        html += `</div>`;
      });
      $("superMimicAttractorsPanel").innerHTML = html;
    } catch (e) {
      $("superMimicAttractorsPanel").innerHTML = `<span style="color:#ffab40">Error: ${e.message}</span>`;
    }
  };

  window.superMimicRollback = async function () {
    if (!activeExperimentId) return;
    if (!confirm("¿Revertir el último despliegue de este experimento?")) return;
    try {
      const res = await api(
        "POST",
        `/super-mimic/experiment/${activeExperimentId}/rollback`,
        {}
      );
      if (!res.ok) throw new Error(res.error || "Rollback failed");
      const applied = res.kelly && res.kelly.applied ? Object.keys(res.kelly.applied) : [];
      $("smDeployGate").innerHTML = `↩ Rollback aplicado. Campos: ${applied.join(", ") || "—"}`;
      $("smDeployGate").style.color = "#69f0ae";
    } catch (e) {
      $("smDeployGate").innerHTML = `<span style="color:#ffab40">Error rollback: ${e.message}</span>`;
    }
  };

  // ── Init ──────────────────────────────────────────────────────────────────
  // Fix button labels that may have encoding issues in the static HTML
  try {
    $("smDeployBtn").textContent = "🚀 Desplegar config (manual)";
    // Inject rollback button if missing
    if (!$("smRollbackBtn")) {
      const rb = document.createElement("button");
      rb.className = "btn";
      rb.style.width = "100%";
      rb.id = "smRollbackBtn";
      rb.textContent = "↩ Rollback";
      rb.onclick = window.superMimicRollback;
      $("smDeployBtn").parentNode.insertBefore(rb, $("smDeployBtn").nextSibling);
    }
  } catch (e) {
    console.warn("init buttons", e);
  }
  window.superMimicRefreshList();
  window.superMimicCaptureFingerprint();
})();
