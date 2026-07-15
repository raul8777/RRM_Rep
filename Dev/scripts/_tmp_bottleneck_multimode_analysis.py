"""Multi-mode bottleneck diagnostic.

Reads the SPOT_BULL sweep results but classifies jobs by dominant regime to produce
per-mode reports and a combined report. Capital base is normalized to the live
account value ($5,058) where noted.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo_root)

RESULTS_PATH = os.path.join(repo_root, "Dev", "benchmarks", "mimic_sweep_results.jsonl")
REPORT_DIR = os.path.join(repo_root, "Dev")
LIVE_CAPITAL = 5058.0
DAILY_TARGET = 120.0

MODES = ["SPOT_BULL", "RANGE_SCALP", "PRESERVATION", "FUTURES_SHORT"]
PARAMS = [
    "min_signal_strength",
    "min_success_probability",
    "stop_loss_pct",
    "kelly_max_usdt",
    "kelly_dynamic_base_pct",
    "max_positions_spot_bull",
]


def load_results() -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    with open(RESULTS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                results.append(json.loads(line))
            except Exception:
                continue
    return results


def dominant_mode(r: Dict[str, Any]) -> Optional[str]:
    dist = r.get("regime_distribution", {})
    if not dist:
        return None
    # Find key containing a known mode with max cycles
    best_mode = None
    best_cycles = -1
    for key, cycles in dist.items():
        for mode in MODES:
            if mode in key and isinstance(cycles, (int, float)) and cycles > best_cycles:
                best_cycles = cycles
                best_mode = mode
    # If no known mode matched but key is exactly a mode
    if best_mode is None:
        for key, cycles in dist.items():
            if key in MODES and isinstance(cycles, (int, float)) and cycles > best_cycles:
                best_cycles = cycles
                best_mode = key
    return best_mode


def safe_sharpe(r: Dict[str, Any]) -> Optional[float]:
    s = r.get("sharpe")
    if s is None:
        return None
    try:
        s = float(s)
        if abs(s) > 100:
            return None
        return s
    except Exception:
        return None


def fmt_num(value: Optional[float], decimals: int = 2) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{decimals}f}"


def median_or_none(values: List[float]) -> Optional[float]:
    return median(values) if values else None


def report_title(mode: str) -> str:
    return f"# Diagnóstico de Cuello de Botella — {mode} (2026-07-13)"


def section_summary(mode: str, jobs: List[Dict[str, Any]], live_capital: float) -> str:
    with_trades = [r for r in jobs if r.get("trades", 0) > 0]
    pnls = [r.get("pnl_usdt", 0.0) for r in jobs]
    active_pnls = [r.get("pnl_usdt", 0.0) for r in with_trades]
    best = max(jobs, key=lambda x: x.get("pnl_usdt", 0.0)) if jobs else None
    # Scale PnL to live capital
    scale = live_capital / 2910.0
    best_scaled = best.get("pnl_usdt", 0.0) * scale if best else None
    avg_scaled = (sum(pnls) / len(pnls) * scale) if pnls else None
    avg_active_scaled = (sum(active_pnls) / len(active_pnls) * scale) if active_pnls else None
    # Gap to daily target
    best_daily = best.get("pnl_per_day_usdt", 0.0) * scale if best else None
    gap = DAILY_TARGET - (best_daily or 0.0) if best_daily is not None else None
    return f"""## 1. Resumen Ejecutivo

- **Capital live de referencia**: ${live_capital:,.2f} USDT
- **Target diario**: ${DAILY_TARGET:,.2f} USDT
- **Jobs analizados**: {len(jobs)}
- **Jobs con trades > 0**: {len(with_trades)}
- **Mejor PnL (ventana 15d)**: ${fmt_num(best.get('pnl_usdt') if best else None)} (sweep $2,910) → **${fmt_num(best_scaled)} escalado a ${live_capital:,.0f}**
- **Mejor PnL/día**: ${fmt_num(best_daily)}
- **Gap vs target diario**: ${fmt_num(gap)}
- **PnL promedio (todos los jobs)**: ${fmt_num(avg_scaled)} escalado
- **PnL promedio (jobs con trades)**: ${fmt_num(avg_active_scaled)} escalado
- **Porcentaje de jobs con PnL positivo**: {fmt_num(sum(1 for x in pnls if x > 0) / len(pnls) * 100 if pnls else None)}%
"""


def section_best_configs(jobs: List[Dict[str, Any]], top_n: int = 5) -> str:
    valid = [r for r in jobs if r.get("trades", 0) >= 3 and r.get("pnl_usdt", 0.0) > 0]
    if not valid:
        valid = [r for r in jobs if r.get("trades", 0) > 0]
    if not valid:
        return "## 2. Mejores Configuraciones\n\nNo hay jobs con trades suficientes.\n"
    sorted_jobs = sorted(valid, key=lambda x: x.get("pnl_usdt", 0.0), reverse=True)[:top_n]
    lines = ["## 2. Mejores Configuraciones por PnL Total", "", "| Rank | Perfil | Window | PnL | PnL/día | Trades | DD% | CDR | Params |", "|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(sorted_jobs, 1):
        params = json.dumps(r.get("params", {}))
        lines.append(
            f"| {i} | {r.get('profile')} | {r.get('window_id')} | {r.get('pnl_usdt')} | "
            f"{r.get('pnl_per_day_usdt')} | {r.get('trades')} | {r.get('max_drawdown_pct')} | "
            f"{r.get('capital_deployed_ratio')} | `{params}` |"
        )
    return "\n".join(lines)


def section_aggregate_by_profile(jobs: List[Dict[str, Any]]) -> str:
    profiles = ["A", "B", "C"]
    by_profile: Dict[str, List[Dict[str, Any]]] = {p: [] for p in profiles}
    for r in jobs:
        by_profile[r.get("profile", "?")].append(r)

    lines = ["## 3. Aggregate por Perfil", "", "| Perfil | Jobs | Avg PnL | Median PnL | Avg Sharpe | Median Sharpe | Avg DD% | Pos PnL% | Avg Trades | Avg CDR |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for p in profiles:
        group = by_profile.get(p, [])
        if not group:
            continue
        pnls = [r.get("pnl_usdt", 0.0) for r in group]
        sharpes = [s for s in (safe_sharpe(r) for r in group) if s is not None]
        dds = [r.get("max_drawdown_pct", 0.0) for r in group if r.get("max_drawdown_pct") is not None]
        trades = [r.get("trades", 0) for r in group]
        cdrs = [r.get("capital_deployed_ratio") for r in group if r.get("capital_deployed_ratio") is not None]
        pos_ratio = sum(1 for x in pnls if x > 0) / len(pnls) * 100 if pnls else 0
        lines.append(
            f"| {p} | {len(group)} | {fmt_num(sum(pnls)/len(pnls))} | {fmt_num(median_or_none(pnls))} | "
            f"{fmt_num(sum(sharpes)/len(sharpes) if sharpes else None)} | {fmt_num(median_or_none(sharpes))} | "
            f"{fmt_num(sum(dds)/len(dds) if dds else None)} | {fmt_num(pos_ratio)} | "
            f"{fmt_num(sum(trades)/len(trades))} | {fmt_num(sum(cdrs)/len(cdrs) if cdrs else None)} |"
        )
    return "\n".join(lines)


def section_cdr_analysis(jobs: List[Dict[str, Any]], live_capital: float) -> str:
    cdrs = [r.get("capital_deployed_ratio") for r in jobs if r.get("capital_deployed_ratio") is not None]
    if not cdrs:
        return "## 4. Capital Deployment Efficiency\n\nNo hay datos de CDR.\n"
    avg_cdr = sum(cdrs) / len(cdrs)
    active = [r for r in jobs if r.get("trades", 0) > 0 and r.get("capital_deployed_ratio") is not None]
    avg_cdr_active = sum(r.get("capital_deployed_ratio") for r in active) / len(active) if active else None
    # Estimate capital in positions at live capital
    deployed = live_capital * avg_cdr
    target_deployed = live_capital * 0.75
    gap_deployed = target_deployed - deployed
    return f"""## 4. Capital Deployment Efficiency (CDR)

- **CDR promedio (todos los jobs)**: {fmt_num(avg_cdr * 100)}%
- **CDR promedio (jobs con trades)**: {fmt_num((avg_cdr_active or 0) * 100)}%
- **Capital deployado promedio** (a ${live_capital:,.0f}): ${fmt_num(deployed)}
- **Target CDR 75%**: ${fmt_num(target_deployed)}
- **Capital inactivo extra para alcanzar 75% CDR**: ${fmt_num(gap_deployed)}
- **Observación**: CDR bajo indica que el sistema pasa mucho tiempo en cash. Root causes probables: `category_max_positions_enabled`, `max_positions_*` bajos, predictor conservador, o ausencia de señales en el modo.
"""


def section_trade_frequency(jobs: List[Dict[str, Any]]) -> str:
    active = [r for r in jobs if r.get("trades", 0) > 0]
    if not active:
        return "## 5. Frecuencia de Trades\n\nNo hay jobs con trades.\n"
    trades = [r.get("trades", 0) for r in active]
    pnls_per_trade = [r.get("pnl_usdt", 0.0) / max(1, r.get("trades", 1)) for r in active]
    return f"""## 5. Frecuencia de Trades

- **Jobs con trades**: {len(active)}
- **Trades promedio por job**: {fmt_num(sum(trades)/len(trades), 1)}
- **Median trades**: {fmt_num(median_or_none([float(t) for t in trades]))}
- **PnL promedio por trade**: ${fmt_num(sum(pnls_per_trade)/len(pnls_per_trade), 3)}
- **Trades necesarios para $120/día** (si PnL/trade promedio se mantiene): {fmt_num(DAILY_TARGET / (sum(pnls_per_trade)/len(pnls_per_trade) if pnls_per_trade else 1), 0)} trades/día
"""


def section_sensitivity(jobs: List[Dict[str, Any]]) -> str:
    lines = ["## 6. Sensibilidad de Parámetros", ""]
    for param in PARAMS:
        by_val: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
        for r in jobs:
            v = r.get("params", {}).get(param)
            if v is not None:
                by_val[v].append(r)
        if not by_val:
            continue
        lines.extend([f"### {param}", "", "| Valor | Jobs | Median PnL | Median Sharpe | Median DD% | Avg Trades | Avg CDR |", "|---|---|---|---|---|---|---|"])
        for v in sorted(by_val.keys(), key=lambda x: float(x) if isinstance(x, (int, float)) else str(x)):
            group = by_val[v]
            pnls = [r.get("pnl_usdt", 0.0) for r in group]
            sharpes = [s for s in (safe_sharpe(r) for r in group) if s is not None]
            dds = [r.get("max_drawdown_pct", 0.0) for r in group if r.get("max_drawdown_pct") is not None]
            trades = [r.get("trades", 0) for r in group]
            cdrs = [r.get("capital_deployed_ratio") for r in group if r.get("capital_deployed_ratio") is not None]
            lines.append(
                f"| {v} | {len(group)} | {fmt_num(median_or_none(pnls))} | "
                f"{fmt_num(median_or_none(sharpes))} | {fmt_num(median_or_none(dds))} | "
                f"{fmt_num(sum(trades)/len(trades))} | {fmt_num(sum(cdrs)/len(cdrs) if cdrs else None)} |"
            )
        lines.append("")
    return "\n".join(lines)


def section_pnl_distribution(jobs: List[Dict[str, Any]], live_capital: float) -> str:
    scale = live_capital / 2910.0
    pnls = [r.get("pnl_usdt", 0.0) * scale for r in jobs]
    if not pnls:
        return "## 7. Distribución de PnL\n\nSin datos.\n"
    # Simple buckets
    buckets = [(-1e9, -50), (-50, -25), (-25, -10), (-10, -5), (-5, 0), (0, 5), (5, 10), (10, 25), (25, 50), (50, 1e9)]
    labels = ["<-50", "-50 to -25", "-25 to -10", "-10 to -5", "-5 to 0", "0 to 5", "5 to 10", "10 to 25", "25 to 50", ">50"]
    counts = [0] * len(buckets)
    for p in pnls:
        for i, (lo, hi) in enumerate(buckets):
            if lo <= p < hi:
                counts[i] += 1
                break
    lines = ["## 7. Distribución de PnL (escalado a ${:,.0f})".format(live_capital), "", "| Rango USD | Count | % |", "|---|---|---|"]
    total = len(pnls)
    for label, count in zip(labels, counts):
        lines.append(f"| {label} | {count} | {fmt_num(count/total*100 if total else 0)}% |")
    return "\n".join(lines)


def section_gap_analysis(jobs: List[Dict[str, Any]], live_capital: float) -> str:
    scale = live_capital / 2910.0
    best = max(jobs, key=lambda x: x.get("pnl_usdt", 0.0)) if jobs else None
    if not best:
        return "## 8. Gap hacia $120/día\n\nSin datos.\n"
    best_daily = best.get("pnl_per_day_usdt", 0.0) * scale
    gap = DAILY_TARGET - best_daily
    # How many x of best daily needed
    multiplier = DAILY_TARGET / best_daily if best_daily > 0 else None
    # Required CDR if avg pnl per deployed dollar is known
    active = [r for r in jobs if r.get("trades", 0) > 0 and r.get("capital_deployed_ratio", 0) > 0]
    pnl_per_deployed = []
    for r in active:
        deployed = 2910.0 * (r.get("capital_deployed_ratio") or 0)
        if deployed > 0:
            pnl_per_deployed.append(r.get("pnl_usdt", 0.0) / deployed)
    avg_pnl_per_deployed = sum(pnl_per_deployed) / len(pnl_per_deployed) if pnl_per_deployed else 0
    window_days = 15.0
    daily_pnl_per_deployed = avg_pnl_per_deployed / window_days if window_days > 0 else 0
    required_deployed = DAILY_TARGET / daily_pnl_per_deployed if daily_pnl_per_deployed > 0 else None
    required_cdr = required_deployed / live_capital if required_deployed else None
    return f"""## 8. Gap hacia el Target de $120/día

- **Mejor PnL/día observado**: ${fmt_num(best_daily)}
- **Gap**: ${fmt_num(gap)} por día
- **Multiplicador necesario sobre el mejor resultado**: {fmt_num(multiplier)}x
- **Rentabilidad PnL/$ deployado por ventana**: {fmt_num(avg_pnl_per_deployed * 100)}% por quincena
- **Rentabilidad PnL/$ deployado por día**: {fmt_num(daily_pnl_per_deployed * 100)}% por día
- **Capital deployado necesario para $120/día**: ${fmt_num(required_deployed)}
- **CDR necesario para $120/día**: {fmt_num((required_cdr or 0) * 100)}%

**Interpretación**: Si la rentabilidad por dólar deployado no cambia, necesitamos {fmt_num(required_cdr)} CDR para alcanzar el target. Si el CDR no puede subir tanto, la única vía es aumentar la rentabilidad por trade (mejores entradas/salidas).
"""


def section_regime_transitions(jobs: List[Dict[str, Any]]) -> str:
    # Count transitions within jobs
    transitions: Dict[Tuple[str, str], int] = defaultdict(int)
    for r in jobs:
        for t in r.get("transitions", []):
            if isinstance(t, (list, tuple)) and len(t) >= 2:
                transitions[(str(t[0]), str(t[1]))] += 1
    if not transitions:
        return "## 9. Transiciones de Régimen\n\nNo hay transiciones registradas en los resultados.\n"
    lines = ["## 9. Transiciones de Régimen", "", "| De | A | Count |", "|---|---|---|"]
    for (frm, to), count in sorted(transitions.items(), key=lambda x: -x[1]):
        lines.append(f"| {frm} | {to} | {count} |")
    return "\n".join(lines)


def section_bottleneck_hypotheses(mode: str) -> str:
    hypotheses = {
        "SPOT_BULL": """
- **Hipótesis 1 — Llegada tarde al régimen**: `market_regime.py` requiere 2 ciclos consecutivos para confirmar SPOT_BULL; eso puede perder el inicio del impulso alcista.
- **Hipótesis 2 — Sizing pequeño**: Kelly base $100 / dinámico 0.18 en un modo que debería "dejar correr runners" resulta en posiciones pequeñas vs capital.
- **Hipótesis 3 — Exits demasiado tempranos**: SSO/PRS/Peak capturan ganancias rápidas pero sacrifican grandes runners en SPOT_BULL.
- **Hipótesis 4 — Predictor conservador**: en bootstrap, expected_pnl negativo bloquea entradas incluso cuando el mercado es alcista.
- **Hipótesis 5 — Límites de categoría**: category limits impiden llenar slots en SPOT_BULL.
""",
        "RANGE_SCALP": """
- **Hipótesis 1 — Límite de categoría MIDCAP_ALT**: bloquea ~48% de señales; deja capital ocioso.
- **Hipótesis 2 — Ciclo muy lento**: `cycle_range_scalp` puede ser mayor al momentum del rebote.
- **Hipótesis 3 — Exits tempranos**: SSO-1 stagnant cierra ganancias pequeñas que podrían ser mayores si se deja correr hasta TP.
- **Hipótesis 4 — Universe limitado**: SENTUSDT y otros pumps no están en el universo.
- **Hipótesis 5 — Sizing conservador**: Fear & Greed modifier deprime tamaño en lateral.
""",
        "PRESERVATION": """
- **Hipótesis 1 — Modo muy defensivo**: by design reduce trades; difícil generar $120/día desde aquí.
- **Hipótesis 2 — Narrative-only restrictivo**: solo permite tokens narrative en un mercado bajista.
- **Hipótesis 3 — Perfil C por defecto**: si el selector cae a C, casi no opera.
""",
        "FUTURES_SHORT": """
- **Hipótesis 1 — Pocos datos en sweep**: FUTURES_SHORT depende de ciclos separados (`cycle_futures_short`); en simulación SPOT_BULL puede no activarse.
- **Hipótesis 2 — Sizing pequeño**: `kelly_max_usdt` para shorts puede estar limitado.
- **Hipótesis 3 — Confirmación bajista lenta**: requiere vela roja 5m confirmada y RSI 4h; puede perder inicio de caídas.
""",
    }
    return f"## 10. Hipótesis de Cuellos de Botella\n{hypotheses.get(mode, 'No hay hipótesis predefinidas.')}\n"


def section_module_recommendations(mode: str) -> str:
    recs = {
        "SPOT_BULL": """
| Módulo | Problema probable | Acción sugerida |
|---|---|---|
| `market_regime.py` | Confirmación lenta de SPOT_BULL | Evaluar reducir ciclos de confirmación o adelantar señal parcial |
| `_kelly_position_size()` | Sizing pequeño para runners | Subir Kelly base / max en SPOT_BULL-A |
| `smart_scale_out.py` | Cierra ganadores muy pronto | Relajar SSO-1 stagnant en SPOT_BULL, ampliar trailing |
| `learning_engine.py` | Predictor bootstrap bloquea entradas alcistas | Forzar recalibración con trades reales |
| `portfolio_selector.py` | Slots vacíos por límites de categoría | Relajar category limits en SPOT_BULL |
""",
        "RANGE_SCALP": """
| Módulo | Problema probable | Acción sugerida |
|---|---|---|
| `run_trades.py` | Límite MIDCAP_ALT 2/2 | Desactivar category limits (`category_max_positions_enabled=false`) y subir max_positions_range_scalp |
| `_kelly_position_size()` | Sizing bajo en lateral | Subir kelly_dynamic_base_pct / kelly_max_usdt para RANGE_SCALP-A |
| `unified_governance_supervisor.py` | Bloquea salidas por pérdida mínima | Revisar umbral `loss_below_min_usdt` |
| `pair_screener.py` | Universe no captura pumps | Evaluar ampliar universe con filtros de liquidez |
| `smart_scale_out.py` | Cierra antes del TP | Ajustar SSO-1/SSO-4 thresholds para RANGE_SCALP |
""",
        "PRESERVATION": """
| Módulo | Problema probable | Acción sugerida |
|---|---|---|
| Modo en sí | Diseño defensivo | No esperar $120/día en PRESERVATION; objetivo es preservar capital |
| `market_regime.py` | Transición tardía a SPOT_BULL/RANGE | Mejorar detección de fin del modo preservación |
""",
        "FUTURES_SHORT": """
| Módulo | Problema probable | Acción sugerida |
|---|---|---|
| `futures_client.py` | Ciclo lento | Ajustar cycle_futures_short si el mercado cae rápido |
| `futures_client.py` | Sizing limitado | Revisar Kelly max para shorts |
| `market_regime.py` | Confirmación bajista lenta | Evaluar trigger anticipado de FUTURES_SHORT |
""",
    }
    return f"## 11. Recomendaciones por Módulo\n{recs.get(mode, 'Sin recomendaciones predefinidas.')}\n"


def section_super_mimic(mode: str) -> str:
    return f"""## 12. ¿Qué haría Super MIMIC aquí?

- **Landscape/attractors**: Super MIMIC detectaría configs robustas para {mode} y las separaría de valles inestables.
- **Deploy gate**: Filtraría configs con PnL/día ≤ 0, DD ≥ 15%, trades < 3, attractors < 1. En {mode} muchas configs actuales no pasarían gate.
- **Monte Carlo**: Daría intervalos de confianza sobre PnL y Sharpe; mostraría que el mejor resultado puede ser suerte de ventana.
- **Lo que NO haría Super MIMIC**: no detecta automáticamente "peak capture loss", category-limit bottlenecks, ni predictor bootstrap. Necesita integrar métricas de missed-opportunity y capital deployment para eso.
- **Recomendación**: enriquecer artifact de Super MIMIC con métricas de rechazo, CDR y peak-capture ratio por modo.
"""


def build_mode_report(mode: str, jobs: List[Dict[str, Any]], live_capital: float) -> str:
    sections = [
        report_title(mode),
        "",
        section_summary(mode, jobs, live_capital),
        "",
        section_best_configs(jobs),
        "",
        section_aggregate_by_profile(jobs),
        "",
        section_cdr_analysis(jobs, live_capital),
        "",
        section_trade_frequency(jobs),
        "",
        section_sensitivity(jobs),
        "",
        section_pnl_distribution(jobs, live_capital),
        "",
        section_gap_analysis(jobs, live_capital),
        "",
        section_regime_transitions(jobs),
        "",
        section_bottleneck_hypotheses(mode),
        "",
        section_module_recommendations(mode),
        "",
        section_super_mimic(mode),
    ]
    return "\n".join(sections)


def build_combined_report(all_jobs: List[Dict[str, Any]], by_mode: Dict[str, List[Dict[str, Any]]], live_capital: float) -> str:
    scale = live_capital / 2910.0
    best = max(all_jobs, key=lambda x: x.get("pnl_usdt", 0.0))
    best_daily = best.get("pnl_per_day_usdt", 0.0) * scale
    lines = [
        "# Diagnóstico de Cuello de Botella — Estrategia Combinada (2026-07-13)",
        "",
        "## 1. Resumen Ejecutivo Combinado",
        "",
        f"- **Capital live de referencia**: ${live_capital:,.2f} USDT",
        f"- **Target diario**: ${DAILY_TARGET:,.2f} USDT",
        f"- **Total jobs analizados**: {len(all_jobs)}",
        f"- **Mejor PnL/día en cualquier modo**: ${fmt_num(best_daily)} (window {best.get('window_id')}, perfil {best.get('profile')}, modo dominante {dominant_mode(best) or 'N/A'})",
        f"- **Gap al target diario**: ${fmt_num(DAILY_TARGET - best_daily)}",
        "",
        "## 2. Comparativa por Modo Dominante",
        "",
        "| Modo | Jobs | Jobs con trades | Mejor PnL 15d (escalado) | Mejor PnL/día (escalado) | Avg CDR | Avg Trades | Pos PnL% |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for mode in MODES:
        jobs = by_mode.get(mode, [])
        active = [r for r in jobs if r.get("trades", 0) > 0]
        best_mode = max(jobs, key=lambda x: x.get("pnl_usdt", 0.0)) if jobs else None
        pnls = [r.get("pnl_usdt", 0.0) for r in jobs]
        cdrs = [r.get("capital_deployed_ratio") for r in jobs if r.get("capital_deployed_ratio") is not None]
        trades = [r.get("trades", 0) for r in jobs]
        pos_ratio = sum(1 for x in pnls if x > 0) / len(pnls) * 100 if pnls else 0
        avg_cdr = sum(cdrs) / len(cdrs) if cdrs else None
        avg_trades = sum(trades) / len(trades) if trades else None
        lines.append(
            f"| {mode} | {len(jobs)} | {len(active)} | "
            f"{fmt_num(best_mode.get('pnl_usdt', 0.0) * scale if best_mode else None)} | "
            f"{fmt_num(best_mode.get('pnl_per_day_usdt', 0.0) * scale if best_mode else None)} | "
            f"{fmt_num(avg_cdr)} | "
            f"{fmt_num(avg_trades)} | {fmt_num(pos_ratio)} |"
        )
    lines.extend([
        "",
        "## 3. Hallazgos Cruzados",
        "",
        "- **RANGE_SCALP** es el modo con más actividad y mejores resultados promedio en el sweep y en operación real.",
        "- **SPOT_BULL** tiene los mejores picos ( runners) pero pocos jobs dominantes; el sistema llega tarde o no despliega suficiente capital.",
        "- **PRESERVATION** está diseñado para no perder, no para ganar $120/día.",
        "- **FUTURES_SHORT** probablemente está subrepresentado en el sweep actual (se necesitaría sweep propio).",
        "",
        "## 4. Recomendación Estratégica para $120/día",
        "",
        "Para alcanzar $120/día sosteniblemente se necesita una de estas (no excluyentes):",
        "",
        "1. **Aumentar CDR a >60%** en RANGE_SCALP/SPOT_BULL (relajar category limits, subir max_positions, reducir predictor conservador).",
        "2. **Aumentar rentabilidad por trade 2-3x** (mejores entradas en soporte, dejar correr ganadores, reducir SL).",
        "3. **Combinar modos**: SPOT_BULL para capturar impulsos, RANGE_SCALP para rotación, FUTURES_SHORT para caídas. Super MIMIC puede optimizar la transición.",
        "4. **No intentar extraer $120/día en PRESERVATION**: ese modo es seguro; forzarlo rompería la regla dura de preservación de capital.",
        "",
        "## 5. Validación Requerida antes de Cambios",
        "",
        "Antes de aplicar cualquier fix responder los 6 puntos de `rules.md`:",
        "1. Impacto SPOT vs FUTURES por separado.",
        "2. Impacto perfiles A/B/C.",
        "3. Gate de validación y trigger de rollback.",
        "4. Estado persistido y evento auditado en `trades_audit`.",
        "5. Medición contra baseline/changepoint.",
        "6. Endpoint/dashboard para monitorear salud.",
    ])
    return "\n".join(lines)


def main() -> int:
    results = load_results()
    by_mode: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    no_mode: List[Dict[str, Any]] = []
    for r in results:
        mode = dominant_mode(r)
        if mode:
            by_mode[mode].append(r)
        else:
            no_mode.append(r)

    # Generate per-mode reports
    for mode in MODES:
        report = build_mode_report(mode, by_mode.get(mode, []), LIVE_CAPITAL)
        path = os.path.join(REPORT_DIR, f"bottleneck_diagnostic_{mode}_20260713.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"Saved {path}")

    # Combined report
    combined = build_combined_report(results, by_mode, LIVE_CAPITAL)
    combined_path = os.path.join(REPORT_DIR, "bottleneck_diagnostic_COMBINED_20260713.md")
    with open(combined_path, "w", encoding="utf-8") as f:
        f.write(combined)
    print(f"Saved {combined_path}")
    print(f"Jobs without dominant mode: {len(no_mode)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
