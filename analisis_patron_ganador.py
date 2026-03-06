#!/usr/bin/env python3
"""Analiza patrones prometedores con drift y propone lógica operativa v1.

Incluye:
- Reglas duales por cuantiles (exploratorio)
- Pattern Score compuesto
- Penalización por entrada tardía (persecución de racha)
- Persistencia por ventanas (drift check)
- Ranking híbrido (score + bonus - penalización)
"""
from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

DEFAULT_DATASET = "dataset_incremental.csv"

FEATURES = [
    "rsi_9",
    "rsi_14",
    "payout",
    "puntaje_estrategia",
    "volatilidad",
    "breakout",
    "racha_actual",
    "es_rebote",
    "rsi_reversion",
    "cruce_sma",
    "sma_spread",
]


class DataError(Exception):
    pass


@dataclass
class RuleResult:
    rule: str
    n: int
    wr: float
    lift: float


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Patrón prometedor + drift + score operativo")
    p.add_argument("--dataset", default=DEFAULT_DATASET)
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--min-muestras", type=int, default=30)
    p.add_argument("--score-th", type=float, default=6.0, help="Umbral de Pattern Score")
    p.add_argument("--guardar", default="")
    return p.parse_args()


def _to_float(v: str | None) -> float | None:
    try:
        if v is None:
            return None
        txt = str(v).strip()
        if txt == "":
            return None
        x = float(txt)
        if not math.isfinite(x):
            return None
        return x
    except Exception:
        return None


def load_rows(path: Path) -> list[dict[str, float]]:
    if not path.exists():
        raise DataError(f"Dataset no encontrado: {path}")
    with path.open(encoding="utf-8", newline="") as f:
        rd = csv.DictReader(f)
        if rd.fieldnames is None:
            raise DataError("CSV vacío")
        required = FEATURES + ["result_bin"]
        missing = [c for c in required if c not in rd.fieldnames]
        if missing:
            raise DataError(f"Faltan columnas: {', '.join(missing)}")

        rows: list[dict[str, float]] = []
        skipped = 0
        for r in rd:
            item: dict[str, float] = {}
            valid = True
            for k in required:
                x = _to_float(r.get(k))
                if x is None:
                    valid = False
                    break
                item[k] = x
            if not valid:
                skipped += 1
                continue
            rows.append(item)

    if not rows:
        raise DataError("Sin filas válidas para analizar")
    if skipped:
        print(f"[WARN] Filas descartadas por datos faltantes/no numéricos: {skipped}")
    return rows


def q(values: list[float], pct: float) -> float:
    s = sorted(values)
    return s[int((len(s) - 1) * pct)]


def wr(rows: list[dict[str, float]]) -> float:
    return sum(r["result_bin"] for r in rows) / len(rows) if rows else 0.0


def quantiles(rows: list[dict[str, float]]) -> dict[str, dict[str, float]]:
    out = {}
    for f in FEATURES:
        vals = [r[f] for r in rows]
        out[f] = {"q1": q(vals, 0.25), "q2": q(vals, 0.50), "q3": q(vals, 0.75)}
    return out


def best_pair_rules(rows: list[dict[str, float]], min_muestras: int) -> list[RuleResult]:
    base = wr(rows)
    qs = quantiles(rows)
    cand: list[RuleResult] = []
    for f1, f2 in combinations(FEATURES, 2):
        for op1 in ("<=Q1", ">=Q3"):
            for op2 in ("<=Q1", ">=Q3"):
                def c1(x: float, f=f1, op=op1):
                    return x <= qs[f]["q1"] if op == "<=Q1" else x >= qs[f]["q3"]

                def c2(x: float, f=f2, op=op2):
                    return x <= qs[f]["q1"] if op == "<=Q1" else x >= qs[f]["q3"]

                ss = [r for r in rows if c1(r[f1]) and c2(r[f2])]
                if len(ss) < min_muestras:
                    continue
                w = wr(ss)
                cand.append(RuleResult(f"{f1} {op1} AND {f2} {op2}", len(ss), w, w - base))
    return sorted(cand, key=lambda x: x.lift, reverse=True)


def pattern_score(row: dict[str, float], qs: dict[str, dict[str, float]]) -> tuple[float, float, float]:
    """score, bonus, penalizacion_tardia"""
    s = 0.0
    if row["rsi_9"] >= qs["rsi_9"]["q3"]:
        s += 2
    if row["rsi_reversion"] >= qs["rsi_reversion"]["q3"]:
        s += 2
    if row["es_rebote"] >= qs["es_rebote"]["q3"]:
        s += 2
    if row["puntaje_estrategia"] >= qs["puntaje_estrategia"]["q3"]:
        s += 1
    if row["cruce_sma"] >= qs["cruce_sma"]["q3"]:
        s += 1
    if row["breakout"] >= qs["breakout"]["q3"]:
        s += 1
    if row["payout"] >= qs["payout"]["q3"]:
        s += 1
    # micro-régimen favorable (proxy): volatilidad contenida
    if row["volatilidad"] <= qs["volatilidad"]["q2"]:
        s += 1

    dual = (
        row["rsi_reversion"] >= qs["rsi_reversion"]["q3"]
        or row["es_rebote"] >= qs["es_rebote"]["q3"]
    )
    penal_tardia = 0.0
    # veto tardío: racha muy alta sin confirmación dual = persecución
    if row["racha_actual"] >= qs["racha_actual"]["q3"] and not dual:
        penal_tardia = 2.0

    bonus = 1.0 if dual and row["rsi_9"] >= qs["rsi_9"]["q3"] else 0.0
    return s, bonus, penal_tardia


def evaluate_score(rows: list[dict[str, float]], score_th: float):
    qs = quantiles(rows)
    base = wr(rows)
    eval_rows = []
    for i, r in enumerate(rows):
        s, bonus, pen = pattern_score(r, qs)
        final = s + bonus - pen
        eval_rows.append({"idx": i, "score": s, "bonus": bonus, "pen": pen, "final": final, "y": r["result_bin"]})

    selected = [e for e in eval_rows if e["final"] >= score_th]
    selected_wr = wr([{"result_bin": e["y"]} for e in selected]) if selected else 0.0
    return {
        "base": base,
        "n_sel": len(selected),
        "wr_sel": selected_wr,
        "lift": selected_wr - base if selected else 0.0,
        "eval_rows": eval_rows,
    }


def window_persistence(rows: list[dict[str, float]], score_th: float):
    n = len(rows)
    cuts = [0, n // 3, 2 * n // 3, n]
    out = []
    for i in range(3):
        w = rows[cuts[i]:cuts[i + 1]]
        if not w:
            continue
        ev = evaluate_score(w, score_th)
        out.append((i + 1, len(w), ev["wr_sel"], ev["lift"], ev["n_sel"], ev["base"]))
    return out


def build_report(rows: list[dict[str, float]], top: int, min_muestras: int, score_th: float) -> str:
    base = wr(rows)
    rules = best_pair_rules(rows, min_muestras=min_muestras)[:top]
    score_eval = evaluate_score(rows, score_th=score_th)
    pers = window_persistence(rows, score_th=score_th)

    lines = []
    lines.append("=== PATRÓN PROMETEDOR (NO DEFINITIVO) ===")
    lines.append(f"Filas: {len(rows)} | WR base: {base:.2%}")
    lines.append("Lectura: el patrón tiene edge, pero puede tener drift entre ventanas.")
    lines.append("")

    lines.append("[1] Top reglas duales (exploratorio):")
    if not rules:
        lines.append("- Sin reglas con mínimo de muestras. Baja --min-muestras.")
    for i, r in enumerate(rules, 1):
        lines.append(f"{i:02d}. {r.rule} | WR={r.wr:.2%} | lift={r.lift:+.2%} | n={r.n}")
    lines.append("")

    lines.append("[2] Pattern Score + veto tardío (persecución):")
    lines.append(f"- Umbral score_final >= {score_th:.1f}")
    lines.append(
        f"- Seleccionadas: {score_eval['n_sel']} | WR_sel={score_eval['wr_sel']:.2%} | lift={score_eval['lift']:+.2%}"
    )
    lines.append("- score_final = pattern_score + bonus_dual - penalizacion_tardia")
    lines.append("")

    lines.append("[3] Persistencia (drift check por 3 ventanas cronológicas):")
    for w_id, n, wr_sel, lift, n_sel, b in pers:
        lines.append(
            f"- Ventana {w_id}: n={n}, base={b:.2%}, sel={n_sel}, WR_sel={wr_sel:.2%}, lift={lift:+.2%}"
        )
    lines.append("Si el lift cae fuerte entre ventanas, el patrón está drifteando.")
    lines.append("")

    lines.append("[4] Ranking híbrido (concepto operativo v1):")
    lines.append("- score_final = prob_proxy + bonus_patron - penal_tardia - penal_crowding(proxy=0)")
    lines.append("- En este CSV no existe prob_ia_oper/confirm/trigger/crowding, por eso se reporta versión proxy.")
    lines.append("- Recomendación: integrar luego prob_ia_oper real + candados en el runtime principal.")

    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    try:
        rows = load_rows(Path(args.dataset).resolve())
    except DataError as e:
        print(f"[ERROR] {e}")
        return 1

    report = build_report(
        rows,
        top=max(1, int(args.top)),
        min_muestras=max(1, int(args.min_muestras)),
        score_th=float(args.score_th),
    )
    print(report)
    if args.guardar:
        out = Path(args.guardar).resolve()
        out.write_text(report + "\n", encoding="utf-8")
        print(f"\nReporte guardado en: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())