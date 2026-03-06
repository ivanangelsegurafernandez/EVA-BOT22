#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import hashlib
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path('.')
LOG_SIGNALS = ROOT / 'ia_signals_log.csv'
DIAG = ROOT / 'diagnostico_pipeline_ia.json'
MODEL_META = ROOT / 'model_meta.json'
REAL_STATE = ROOT / 'real_sim_state.json'
PROMOS = ROOT / 'registro_promociones.txt'
RUNTIME_DEFAULT = ROOT / 'runtime_log_ia.txt'
BOT_FILES = [ROOT / f'registro_enriquecido_fulll{n}.csv' for n in (45, 46, 47, 48, 49, 50)]

OUT_JSON = ROOT / 'reporte_integral_sistema_ia.json'
OUT_MD = ROOT / 'reporte_integral_sistema_ia.md'
MIN_SIGNALS_FOR_BOT_GAP = 5
EWMA_ALPHA_DEFAULT = 0.25
CALIB_BINS = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 1.01)]
EWMA_MIN_SIGNALS_MATURE = 8
THRESHOLD_HINT_MIN_CLOSED = 20
THRESHOLD_HINT_MIN_BIN_90 = 8
EWMA_HEALTH_MIN_MATURE_BOTS = 2


def _safe_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        s = str(v).strip().replace(',', '.')
        if not s:
            return None
        return float(s)
    except Exception:
        return None


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    for enc in ('utf-8', 'utf-8-sig', 'latin-1', 'windows-1252'):
        try:
            with path.open('r', encoding=enc, newline='') as f:
                return list(csv.DictReader(f))
        except Exception:
            continue
    return []


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot_id(paths: list[Path]) -> str:
    payload = []
    for p in paths:
        try:
            if p.exists():
                st = p.stat()
                payload.append(f"{p.name}:{int(st.st_mtime)}:{st.st_size}")
            else:
                payload.append(f"{p.name}:missing")
        except Exception:
            payload.append(f"{p.name}:err")
    raw = "|".join(payload).encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()[:12]


def _closed_signals(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        p = _safe_float(r.get('prob'))
        y = _safe_float(r.get('y'))
        if p is None or y is None:
            continue
        out.append({'bot': str(r.get('bot', '')).strip(), 'prob': float(p), 'y': int(y)})
    return out


def _precision_at(closed: list[dict[str, Any]], thr: float) -> dict[str, Any]:
    filt = [r for r in closed if r['prob'] >= thr]
    n = len(filt)
    hits = sum(1 for r in filt if r['y'] == 1)
    prec = (hits / n) if n > 0 else None
    return {'threshold': thr, 'n': n, 'hits': hits, 'precision': prec}


def _bot_winrate_from_reg(path: Path, last_n: int = 40) -> dict[str, Any]:
    rows = _read_csv(path)
    closes: list[int] = []
    for r in rows:
        rb = _safe_float(r.get('result_bin'))
        if rb in (0.0, 1.0):
            closes.append(int(rb))
    n = len(closes)
    wr_all = (sum(closes) / n) if n > 0 else None
    tail = closes[-last_n:] if n > 0 else []
    wr_tail = (sum(tail) / len(tail)) if tail else None
    return {'rows_closed': n, 'wr_all': wr_all, 'wr_last_n': wr_tail, 'last_n': last_n}


def _bot_prob_from_signals(closed: list[dict[str, Any]], last_n: int = 40) -> dict[str, dict[str, Any]]:
    by_bot: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in closed:
        by_bot[r['bot']].append(r)
    out: dict[str, dict[str, Any]] = {}
    for bot, vals in by_bot.items():
        probs = [v['prob'] for v in vals]
        ys = [v['y'] for v in vals]
        tail = vals[-last_n:]
        out[bot] = {
            'n': len(vals),
            'prob_mean_all': (sum(probs) / len(probs)) if probs else None,
            'hit_all': (sum(ys) / len(ys)) if ys else None,
            'prob_mean_last_n': (sum(v['prob'] for v in tail) / len(tail)) if tail else None,
            'hit_last_n': (sum(v['y'] for v in tail) / len(tail)) if tail else None,
            'last_n': last_n,
        }
    return out


def _wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    phat = successes / n
    den = 1.0 + (z * z) / n
    center = (phat + (z * z) / (2.0 * n)) / den
    margin = (z / den) * ((phat * (1.0 - phat) / n + (z * z) / (4.0 * n * n)) ** 0.5)
    lo = max(0.0, center - margin)
    hi = min(1.0, center + margin)
    return lo, hi


def _calibration_by_bins(closed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for lo, hi in CALIB_BINS:
        bucket = [r for r in closed if lo <= float(r['prob']) < hi]
        n = len(bucket)
        if n == 0:
            rows.append({'range': f'{int(lo*100)}-{int((hi if hi < 1.0 else 1.0)*100)}', 'n': 0, 'hits': 0, 'pred_mean': None, 'win_rate': None, 'gap_pp': None, 'wr_ci_low': None, 'wr_ci_high': None})
            continue
        pred_mean = sum(float(r['prob']) for r in bucket) / n
        hits = sum(int(r['y']) for r in bucket)
        win_rate = hits / n
        ci_low, ci_high = _wilson_interval(hits, n)
        rows.append({
            'range': f'{int(lo*100)}-{int((hi if hi < 1.0 else 1.0)*100)}',
            'n': n,
            'hits': hits,
            'pred_mean': pred_mean,
            'win_rate': win_rate,
            'gap_pp': pred_mean - win_rate,
            'wr_ci_low': ci_low,
            'wr_ci_high': ci_high,
        })
    return rows


def _ewma_bot_health(closed: list[dict[str, Any]], alpha: float = EWMA_ALPHA_DEFAULT) -> dict[str, dict[str, Any]]:
    by_bot: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in closed:
        by_bot[str(r.get('bot', '')).strip()].append(r)

    out: dict[str, dict[str, Any]] = {}
    for bot, rows in by_bot.items():
        acc_ewma: float | None = None
        penalty_ewma: float | None = None
        false_high = 0
        for r in rows:
            y = int(r['y'])
            p = float(r['prob'])
            hit = 1.0 if y == 1 else 0.0
            fp_high = 1.0 if (p >= 0.85 and y == 0) else 0.0
            if acc_ewma is None:
                acc_ewma = hit
                penalty_ewma = fp_high
            else:
                acc_ewma = (alpha * hit) + ((1.0 - alpha) * acc_ewma)
                penalty_ewma = (alpha * fp_high) + ((1.0 - alpha) * float(penalty_ewma))
            if fp_high > 0:
                false_high += 1

        if acc_ewma is None or penalty_ewma is None:
            continue
        hits = sum(int(r['y']) for r in rows)
        n = len(rows)
        wr_raw = (hits / n) if n > 0 else None
        wr_ci_low, wr_ci_high = _wilson_interval(hits, n)
        health = max(0.0, min(1.0, acc_ewma - (0.35 * penalty_ewma)))
        out[bot] = {
            'n': n,
            'hits': hits,
            'wr_raw': wr_raw,
            'wr_ci_low': wr_ci_low,
            'wr_ci_high': wr_ci_high,
            'mature_sample': bool(n >= int(EWMA_MIN_SIGNALS_MATURE)),
            'ewma_hit': acc_ewma,
            'ewma_false_high_penalty': penalty_ewma,
            'false_high_count': false_high,
            'health_score': health,
            'alpha': alpha,
        }
    return out


def _adaptive_threshold_hint(calibration_bins: list[dict[str, Any]], ewma_health: dict[str, dict[str, Any]], closed_n: int) -> dict[str, Any]:
    base = 0.85
    over = 0.0
    n90 = 0
    for b in calibration_bins:
        if b.get('range') == '90-100' and isinstance(b.get('gap_pp'), (int, float)):
            over = float(b['gap_pp'])
            n90 = int(b.get('n', 0) or 0)
            break

    advisory_only = bool(int(closed_n) < int(THRESHOLD_HINT_MIN_CLOSED) or int(n90) < int(THRESHOLD_HINT_MIN_BIN_90))

    mature = [v for v in (ewma_health or {}).values() if bool(v.get('mature_sample', False))]
    global_health = None
    health_used = False
    if len(mature) >= int(EWMA_HEALTH_MIN_MATURE_BOTS):
        global_health = sum(float(v.get('health_score', 0.0) or 0.0) for v in mature) / len(mature)
        health_used = True

    dynamic = base
    reason = []
    confidence = 'low'

    if advisory_only:
        reason.append('muestra_insuficiente_para_automatizar')

    # Sobreconfianza en 90-100: ajuste fuerte solo con evidencia suficiente;
    # con muestra baja mantener ajuste más conservador.
    if over > 0.15:
        dynamic += 0.04 if not advisory_only else 0.02
        reason.append('sobreconfianza_alta_90_100')
    elif over > 0.08:
        dynamic += 0.02 if not advisory_only else 0.01
        reason.append('sobreconfianza_media_90_100')

    # Salud EWMA solo si hay madurez mínima por bot.
    if health_used and isinstance(global_health, (int, float)):
        if global_health < 0.45:
            dynamic += 0.03
            reason.append('salud_bots_baja')
        elif global_health > 0.62:
            dynamic -= 0.02
            reason.append('salud_bots_fuerte')
    else:
        if ewma_health:
            reason.append('salud_ewma_solo_informativa_por_baja_muestra')

    if not advisory_only:
        confidence = 'medium' if int(closed_n) < 80 else 'high'

    return {
        'base_threshold': base,
        'dynamic_threshold': max(0.75, min(0.93, dynamic)),
        'global_health_score': global_health,
        'health_used_for_threshold': health_used,
        'mature_bots_for_health': int(len(mature)),
        'advisory_only': advisory_only,
        'confidence': confidence,
        'min_closed_required': int(THRESHOLD_HINT_MIN_CLOSED),
        'min_n90_required': int(THRESHOLD_HINT_MIN_BIN_90),
        'min_mature_bots_for_health': int(EWMA_HEALTH_MIN_MATURE_BOTS),
        'closed_signals': int(closed_n),
        'bin_90_100_n': int(n90),
        'reasons': reason,
    }



def _model_collapse_guard(meta: dict[str, Any]) -> dict[str, Any]:
    feats = meta.get('feature_names', []) if isinstance(meta, dict) else []
    try:
        n_feats = len(feats) if isinstance(feats, list) else 0
    except Exception:
        n_feats = 0
    auc = _safe_float((meta or {}).get('auc'))
    reliable = bool((meta or {}).get('reliable', False))
    collapsed = bool(n_feats > 0 and n_feats < 5)
    should_block = bool(collapsed and ((auc is None) or (auc < 0.53) or (not reliable)))
    return {
        "feature_count": int(n_feats),
        "collapsed_lt5": bool(collapsed),
        "auc": auc,
        "reliable": reliable,
        "block_promotion": should_block,
    }


def _parse_runtime_log(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {'exists': False, 'path': str(path), 'errors': {}, 'why_no_counts': {}}
    text = path.read_text(encoding='utf-8', errors='ignore')
    lines = text.splitlines()
    keys = {
        'auth_error': ['Error en auth', 'auth failed', 'authorization failed'],
        'ws_error': ['websocket', 'WebSocket', 'connection closed', 'socket'],
        'timeout': ['TimeoutError', 'timeout'],
        'api_error': ['api_call', 'API error', 'error code'],
    }
    err_counts = {}
    low = text.lower()
    for k, pats in keys.items():
        c = 0
        for p in pats:
            c += low.count(p.lower())
        err_counts[k] = c

    why_counter: Counter[str] = Counter()
    for ln in lines:
        if 'WHY-NO:' not in ln:
            continue
        if 'why=' in ln:
            part = ln.split('why=', 1)[1]
            part = part.split('|', 1)[0]
            for token in part.split(','):
                t = token.strip()
                if t:
                    why_counter[t] += 1

    return {
        'exists': True,
        'path': str(path),
        'lines': len(lines),
        'errors': err_counts,
        'why_no_counts': dict(why_counter),
    }


def _readiness(meta: dict[str, Any], closed_n: int) -> dict[str, Any]:
    n = int(_safe_float(meta.get('n_samples', meta.get('n', 0))) or 0)
    reliable = bool(meta.get('reliable', False))
    auc = _safe_float(meta.get('auc'))
    ready = (
        n >= 250 and
        closed_n >= 80 and
        reliable and
        (auc is not None and auc >= 0.53)
    )
    return {
        'ready_for_full_diagnosis': bool(ready),
        'criteria': {
            'n_samples>=250': n >= 250,
            'closed_signals>=80': closed_n >= 80,
            'reliable=true': reliable,
            'auc>=0.53': (auc is not None and auc >= 0.53),
        },
        'n_samples': n,
        'closed_signals': closed_n,
        'auc': auc,
        'reliable': reliable,
    }

def _operational_guidance(bots: dict[str, dict[str, Any]], adaptive_hint: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    silent_bots = sorted([b for b, d in bots.items() if int(d.get('signals_n', 0) or 0) == 0])
    low_sample_bots = sorted([b for b, d in bots.items() if int(d.get('signals_n', 0) or 0) > 0 and int(d.get('signals_n', 0) or 0) < int(EWMA_MIN_SIGNALS_MATURE)])

    yellow_flags: list[str] = []
    health = adaptive_hint.get('global_health_score')
    if isinstance(health, (int, float)) and float(health) < 0.45:
        yellow_flags.append('salud_global_baja')
    if bool(adaptive_hint.get('advisory_only', False)):
        yellow_flags.append('decisiones_en_shadow_mode')
    if not bool(runtime.get('exists', False)):
        yellow_flags.append('falta_runtime_log')

    return {
        'official_gate_threshold': float(adaptive_hint.get('base_threshold', 0.85) or 0.85),
        'shadow_suggested_threshold': float(adaptive_hint.get('dynamic_threshold', 0.85) or 0.85),
        'apply_shadow_only': bool(adaptive_hint.get('advisory_only', True)),
        'silent_bots': silent_bots,
        'low_sample_bots': low_sample_bots,
        'yellow_flags': yellow_flags,
        'next_checkpoint': {
            'closed_signals_target': int(adaptive_hint.get('min_closed_required', THRESHOLD_HINT_MIN_CLOSED) or THRESHOLD_HINT_MIN_CLOSED),
            'bin_90_100_target': int(adaptive_hint.get('min_n90_required', THRESHOLD_HINT_MIN_BIN_90) or THRESHOLD_HINT_MIN_BIN_90),
        },
    }



def build_report(runtime_log: Path | None) -> dict[str, Any]:
    diag = _read_json(DIAG)
    meta = _read_json(MODEL_META)
    real_state = _read_json(REAL_STATE)

    rows = _read_csv(LOG_SIGNALS)
    closed = _closed_signals(rows)

    p70 = _precision_at(closed, 0.70)
    p85 = _precision_at(closed, 0.85)
    calibration_bins = _calibration_by_bins(closed)
    ewma_health = _ewma_bot_health(closed)
    adaptive_hint = _adaptive_threshold_hint(calibration_bins, ewma_health, len(closed))

    by_bot_probs = _bot_prob_from_signals(closed, last_n=40)

    bots = {}
    for fp in BOT_FILES:
        bot = fp.stem.replace('registro_enriquecido_', '')
        wr = _bot_winrate_from_reg(fp, last_n=40)
        pr = by_bot_probs.get(bot, {})
        signals_n = int(pr.get('n', 0) or 0)
        has_enough_signals = signals_n >= int(MIN_SIGNALS_FOR_BOT_GAP)
        bots[bot] = {
            **wr,
            **pr,
            'signals_n': signals_n,
            'signals_min_for_gap': int(MIN_SIGNALS_FOR_BOT_GAP),
            'signals_sample_ok': bool(has_enough_signals),
            'prob_vs_hit_gap_last_n': (
                (pr.get('prob_mean_last_n') - pr.get('hit_last_n'))
                if has_enough_signals and isinstance(pr.get('prob_mean_last_n'), (int, float)) and isinstance(pr.get('hit_last_n'), (int, float))
                else None
            ),
            'prob_vs_wr_gap_last_n': (
                (pr.get('prob_mean_last_n') - wr.get('wr_last_n'))
                if has_enough_signals and isinstance(pr.get('prob_mean_last_n'), (int, float)) and isinstance(wr.get('wr_last_n'), (int, float))
                else None
            )
        }

    runtime = {
        'exists': False,
        'note': 'Pasa --runtime-log <archivo> para auditar auth/websocket/WHY-NO tick a tick.'
    }
    runtime_used = runtime_log
    if runtime_used is None and RUNTIME_DEFAULT.exists():
        runtime_used = RUNTIME_DEFAULT
    if runtime_used is not None:
        runtime = _parse_runtime_log(runtime_used)

    promos_tail = []
    if PROMOS.exists():
        try:
            promos_tail = PROMOS.read_text(encoding='utf-8', errors='ignore').splitlines()[-10:]
        except Exception:
            promos_tail = []

    readiness = _readiness(meta, len(closed))
    guidance = _operational_guidance(bots, adaptive_hint, runtime)

    report_id = _snapshot_id([LOG_SIGNALS, DIAG, MODEL_META, REAL_STATE, runtime_used or RUNTIME_DEFAULT])
    collapse_guard = _model_collapse_guard(meta)

    report = {
        'report_id': report_id,
        'generated_at_utc': _now_iso(),
        'sources': {
            'signals_log': str(LOG_SIGNALS),
            'diag_json': str(DIAG),
            'model_meta': str(MODEL_META),
            'real_state': str(REAL_STATE),
            'runtime_log': str(runtime_used) if runtime_used else None,
        },
        'calibration': {
            'closed_signals': len(closed),
            'precision_at_70': p70,
            'precision_at_85': p85,
            'by_probability_bin': calibration_bins,
        },
        'adaptive_layer': {
            'ewma_bot_health': ewma_health,
            'threshold_hint': adaptive_hint,
        },
        'diagnostic_snapshot': {
            'diag_checklist': diag.get('checklist', []),
            'diag_actions': diag.get('actions', []),
            'diag_signals': diag.get('signals', {}),
            'diag_incremental': diag.get('incremental', {}),
            'diag_model_meta': diag.get('model_meta', {}),
        },
        'bot_alignment': bots,
        'runtime_health': runtime,
        'token_real_state': {
            'real_sim_state': real_state,
            'last_promotions_tail': promos_tail,
        },
        'readiness_recommendation': readiness,
        'model_health': collapse_guard,
        'operational_guidance': guidance,
    }
    return report


def render_md(rep: dict[str, Any]) -> str:
    cal = rep['calibration']
    p70 = cal['precision_at_70']
    p85 = cal['precision_at_85']
    rd = rep['readiness_recommendation']
    adaptive = rep.get('adaptive_layer', {})
    guidance = rep.get('operational_guidance', {})

    def pct(v: Any) -> str:
        return f"{float(v)*100:.1f}%" if isinstance(v, (int, float)) else "N/A"

    lines = []
    lines.append('# Reporte Integral de Salud IA')
    lines.append('')
    lines.append(f"Generado UTC: `{rep['generated_at_utc']}`")
    if rep.get('report_id'):
        lines.append(f"Reporte ID: `{rep['report_id']}` (JSON/MD del mismo corte temporal)")
    lines.append('')
    lines.append('## 1) Calibración real de probabilidades')
    lines.append(f"- Señales cerradas: **{cal['closed_signals']}**")
    lines.append(f"- Precisión @>=70%: **{pct(p70.get('precision'))}** (n={p70.get('n', 0)})")
    lines.append(f"- Precisión @>=85%: **{pct(p85.get('precision'))}** (n={p85.get('n', 0)})")
    if int(cal['closed_signals']) < 20:
        lines.append('- ⚠️ Muestra cerrada muy baja: estas precisiones son orientativas, no concluyentes.')
    lines.append('')
    lines.append('## 2) Desalineación Prob IA vs hitrate por bot (last_n=40)')
    lines.append('| Bot | WR last40 (csv) | n señales IA | Hit last40 (señales) | Prob media last40 (señales) | Gap Prob-Hit señales | Gap Prob-WR csv | Muestra señales |')
    lines.append('|---|---:|---:|---:|---:|---:|---:|---|')
    for bot, d in sorted(rep['bot_alignment'].items()):
        sample_txt = 'OK' if bool(d.get('signals_sample_ok', False)) else f"BAJA(<{int(d.get('signals_min_for_gap', MIN_SIGNALS_FOR_BOT_GAP))})"
        lines.append(
            f"| {bot} | {pct(d.get('wr_last_n'))} | {int(d.get('signals_n', 0) or 0)} | {pct(d.get('hit_last_n'))} | {pct(d.get('prob_mean_last_n'))} | {pct(d.get('prob_vs_hit_gap_last_n'))} | {pct(d.get('prob_vs_wr_gap_last_n'))} | {sample_txt} |"
        )
    lines.append('')
    lines.append('## 3) Calibración por rangos de probabilidad')
    lines.append('| Rango Prob IA | n | Prob media | Winrate real | IC95% winrate | Gap (Prob-Winrate) |')
    lines.append('|---|---:|---:|---:|---:|---:|')
    for b in cal.get('by_probability_bin', []):
        ci = f"[{pct(b.get('wr_ci_low'))}, {pct(b.get('wr_ci_high'))}]" if isinstance(b.get('wr_ci_low'), (int, float)) else 'N/A'
        lines.append(f"| {b.get('range')}% | {int(b.get('n', 0) or 0)} | {pct(b.get('pred_mean'))} | {pct(b.get('win_rate'))} | {ci} | {pct(b.get('gap_pp'))} |")
    lines.append('')
    lines.append('## 4) Capa adaptativa sugerida (EWMA + umbral dinámico)')
    hint = adaptive.get('threshold_hint', {})
    lines.append(f"- Umbral base: **{pct(hint.get('base_threshold'))}**")
    lines.append(f"- Umbral dinámico sugerido: **{pct(hint.get('dynamic_threshold'))}**")
    lines.append(f"- Salud global EWMA bots: **{pct(hint.get('global_health_score'))}**")
    lines.append(f"- EWMA usada para umbral: **{'SI' if hint.get('health_used_for_threshold') else 'NO'}** (bots maduros: {int(hint.get('mature_bots_for_health', 0) or 0)}/{int(hint.get('min_mature_bots_for_health', EWMA_HEALTH_MIN_MATURE_BOTS))})")
    lines.append(f"- Modo: **{'solo sugerencia (no automatizar)' if hint.get('advisory_only') else 'apto para piloto controlado'}** | confianza: **{hint.get('confidence', 'low')}**")
    lines.append(f"- Cobertura mínima para automatizar: closed>={int(hint.get('min_closed_required', THRESHOLD_HINT_MIN_CLOSED))} y n(90-100)>={int(hint.get('min_n90_required', THRESHOLD_HINT_MIN_BIN_90))}; actual: closed={int(hint.get('closed_signals', 0) or 0)}, n90={int(hint.get('bin_90_100_n', 0) or 0)}")
    rs = hint.get('reasons') or []
    lines.append(f"- Razones: {', '.join(rs) if rs else 'sin ajustes relevantes'}")
    lines.append('')
    lines.append('| Bot | n señales | Muestra madura | WR crudo | IC95% WR | EWMA acierto | EWMA penalización falsas altas | Salud bot |')
    lines.append('|---|---:|---|---:|---:|---:|---:|---:|')
    for bot, d in sorted((adaptive.get('ewma_bot_health') or {}).items()):
        ci = f"[{pct(d.get('wr_ci_low'))}, {pct(d.get('wr_ci_high'))}]" if isinstance(d.get('wr_ci_low'), (int, float)) else 'N/A'
        lines.append(
            f"| {bot} | {int(d.get('n', 0) or 0)} | {'SI' if d.get('mature_sample') else 'NO'} | {pct(d.get('wr_raw'))} | {ci} | {pct(d.get('ewma_hit'))} | {pct(d.get('ewma_false_high_penalty'))} | {pct(d.get('health_score'))} |"
        )
    lines.append('')
    lines.append('## 5) Guía operativa inmediata (shadow mode)')
    lines.append(f"- Compuerta operativa actual: **{pct(guidance.get('official_gate_threshold'))}**")
    lines.append(f"- Umbral sugerido en sombra: **{pct(guidance.get('shadow_suggested_threshold'))}**")
    lines.append(f"- Aplicar solo en sombra: **{'SI' if guidance.get('apply_shadow_only') else 'NO'}**")
    sb = guidance.get('silent_bots') or []
    lb = guidance.get('low_sample_bots') or []
    yf = guidance.get('yellow_flags') or []
    lines.append(f"- Bots sin señales IA: {', '.join(sb) if sb else 'ninguno'}")
    lines.append(f"- Bots con muestra baja (<{int(EWMA_MIN_SIGNALS_MATURE)}): {', '.join(lb) if lb else 'ninguno'}")
    lines.append(f"- Focos amarillos: {', '.join(yf) if yf else 'ninguno'}")
    cp = guidance.get('next_checkpoint') or {}
    lines.append(f"- Próximo checkpoint: closed>={int(cp.get('closed_signals_target', THRESHOLD_HINT_MIN_CLOSED))}, n(90-100)>={int(cp.get('bin_90_100_target', THRESHOLD_HINT_MIN_BIN_90))}")
    lines.append('')
    lines.append('## 6) Salud de modelo (anti-colapso de features)')
    mh = rep.get('model_health', {}) or {}
    lines.append(f"- Features activas del campeón: **{int(mh.get('feature_count', 0) or 0)}**")
    lines.append(f"- Colapso (<5 features): **{'SI' if mh.get('collapsed_lt5') else 'NO'}**")
    lines.append(f"- reliable: **{'SI' if mh.get('reliable') else 'NO'}** | AUC: **{mh.get('auc') if isinstance(mh.get('auc'), (int, float)) else 'N/A'}**")
    lines.append(f"- Bloquear promoción por colapso: **{'SI' if mh.get('block_promotion') else 'NO'}**")

    lines.append('## 7) Salud de ejecución (auth/ws/timeout)')
    rh = rep['runtime_health']
    if not rh.get('exists'):
        lines.append('- No auditado en este run (falta `--runtime-log`).')
    else:
        lines.append(f"- Archivo auditado: `{rh.get('path')}` | líneas: {rh.get('lines', 0)}")
        errs = rh.get('errors', {})
        lines.append(f"- auth_error={errs.get('auth_error', 0)}, ws_error={errs.get('ws_error', 0)}, timeout={errs.get('timeout', 0)}, api_error={errs.get('api_error', 0)}")
        wh = rh.get('why_no_counts', {})
        if wh:
            top = sorted(wh.items(), key=lambda x: x[1], reverse=True)[:8]
            lines.append('- WHY-NO más frecuentes: ' + ', '.join([f"{k}:{v}" for k, v in top]))
    lines.append('')
    lines.append('## 8) Recomendación de cuándo correr este programa')
    lines.append('- **Recomendado siempre**: al iniciar sesión y luego cada 30-60 min.')
    lines.append('- **Corte de calidad fuerte**: después de cada bloque de +20 cierres nuevos.')
    lines.append('- **Punto mínimo para decisiones estructurales**:')
    for k, ok in rd['criteria'].items():
        lines.append(f"  - {'✅' if ok else '❌'} {k}")
    lines.append(f"- Ready for full diagnosis: **{rd['ready_for_full_diagnosis']}**")
    lines.append('')
    lines.append('## 9) Qué falta corregir si no está “bien”')
    lines.append('- Nota: `Gap Prob-Hit señales` usa SOLO señales cerradas en `ia_signals_log.csv` y puede diferir de `WR last40 (csv)` del bot.')
    lines.append(f'- Gaps por bot se publican solo si `n señales IA >= {MIN_SIGNALS_FOR_BOT_GAP}` para evitar conclusiones con muestra mínima.')
    lines.append('- Si `precision@85` baja o n es pequeño: recalibrar/proteger compuerta.')
    lines.append('- Si gap Prob-Hit por bot es alto: bajar exposición o bloquear bot temporalmente.')
    lines.append('- EWMA por bot con n bajo debe leerse como semáforo blando; evitar castigos duros hasta tener muestra madura.')
    lines.append('- Si auth/ws/timeouts suben: estabilizar conectividad antes de evaluar modelo.')
    lines.append('- Si WHY-NO se concentra en `trigger_no`/`confirm_pending`: revisar timing de señales y trigger.')
    return '\n'.join(lines) + '\n'


def main() -> int:
    ap = argparse.ArgumentParser(description='Reporte integral único de salud IA + operación')
    ap.add_argument('--runtime-log', type=str, default='', help='Ruta a log de consola/runtime para auditar auth/ws y WHY-NO tick a tick. Si se omite, usa runtime_log_ia.txt si existe.')
    ap.add_argument('--json-out', type=str, default=str(OUT_JSON))
    ap.add_argument('--md-out', type=str, default=str(OUT_MD))
    args = ap.parse_args()

    runtime_path = Path(args.runtime_log) if args.runtime_log else None
    rep = build_report(runtime_path)

    Path(args.json_out).write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding='utf-8')
    Path(args.md_out).write_text(render_md(rep), encoding='utf-8')

    print(f"✅ Reporte JSON: {args.json_out}")
    print(f"✅ Reporte MD:   {args.md_out}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())