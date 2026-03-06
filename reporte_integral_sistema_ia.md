# Reporte Integral de Salud IA

Generado UTC: `2026-03-04T01:08:01.424466+00:00`
Reporte ID: `b13b0d1e83e1` (JSON/MD del mismo corte temporal)

## 1) Calibración real de probabilidades
- Señales cerradas: **0**
- Precisión @>=70%: **N/A** (n=0)
- Precisión @>=85%: **N/A** (n=0)
- ⚠️ Muestra cerrada muy baja: estas precisiones son orientativas, no concluyentes.

## 2) Desalineación Prob IA vs hitrate por bot (last_n=40)
| Bot | WR last40 (csv) | n señales IA | Hit last40 (señales) | Prob media last40 (señales) | Gap Prob-Hit señales | Gap Prob-WR csv | Muestra señales |
|---|---:|---:|---:|---:|---:|---:|---|
| fulll45 | 57.5% | 0 | N/A | N/A | N/A | N/A | BAJA(<5) |
| fulll46 | 35.0% | 0 | N/A | N/A | N/A | N/A | BAJA(<5) |
| fulll47 | 50.0% | 0 | N/A | N/A | N/A | N/A | BAJA(<5) |
| fulll48 | 42.5% | 0 | N/A | N/A | N/A | N/A | BAJA(<5) |
| fulll49 | 45.0% | 0 | N/A | N/A | N/A | N/A | BAJA(<5) |
| fulll50 | 40.0% | 0 | N/A | N/A | N/A | N/A | BAJA(<5) |

## 3) Calibración por rangos de probabilidad
| Rango Prob IA | n | Prob media | Winrate real | IC95% winrate | Gap (Prob-Winrate) |
|---|---:|---:|---:|---:|---:|
| 50-60% | 0 | N/A | N/A | N/A | N/A |
| 60-70% | 0 | N/A | N/A | N/A | N/A |
| 70-80% | 0 | N/A | N/A | N/A | N/A |
| 80-90% | 0 | N/A | N/A | N/A | N/A |
| 90-100% | 0 | N/A | N/A | N/A | N/A |

## 4) Capa adaptativa sugerida (EWMA + umbral dinámico)
- Umbral base: **85.0%**
- Umbral dinámico sugerido: **85.0%**
- Salud global EWMA bots: **N/A**
- EWMA usada para umbral: **NO** (bots maduros: 0/2)
- Modo: **solo sugerencia (no automatizar)** | confianza: **low**
- Cobertura mínima para automatizar: closed>=20 y n(90-100)>=8; actual: closed=0, n90=0
- Razones: muestra_insuficiente_para_automatizar

| Bot | n señales | Muestra madura | WR crudo | IC95% WR | EWMA acierto | EWMA penalización falsas altas | Salud bot |
|---|---:|---|---:|---:|---:|---:|---:|

## 5) Guía operativa inmediata (shadow mode)
- Compuerta operativa actual: **85.0%**
- Umbral sugerido en sombra: **85.0%**
- Aplicar solo en sombra: **SI**
- Bots sin señales IA: fulll45, fulll46, fulll47, fulll48, fulll49, fulll50
- Bots con muestra baja (<8): ninguno
- Focos amarillos: decisiones_en_shadow_mode
- Próximo checkpoint: closed>=20, n(90-100)>=8

## 6) Salud de modelo (anti-colapso de features)
- Features activas del campeón: **2**
- Colapso (<5 features): **SI**
- reliable: **NO** | AUC: **0.5054347826086957**
- Bloquear promoción por colapso: **SI**
## 7) Salud de ejecución (auth/ws/timeout)
- Archivo auditado: `runtime_log_ia.txt` | líneas: 79788
- auth_error=0, ws_error=0, timeout=0, api_error=0
- WHY-NO más frecuentes: trigger_no:9894, warmup:6404, n<80:6404, confirm_pending(0/2):6284, p_best<58.0%:5220, confirm_pending(0/1):3681, n15_pending:2705, none:815

## 8) Recomendación de cuándo correr este programa
- **Recomendado siempre**: al iniciar sesión y luego cada 30-60 min.
- **Corte de calidad fuerte**: después de cada bloque de +20 cierres nuevos.
- **Punto mínimo para decisiones estructurales**:
  - ✅ n_samples>=250
  - ❌ closed_signals>=80
  - ❌ reliable=true
  - ❌ auc>=0.53
- Ready for full diagnosis: **False**

## 9) Qué falta corregir si no está “bien”
- Nota: `Gap Prob-Hit señales` usa SOLO señales cerradas en `ia_signals_log.csv` y puede diferir de `WR last40 (csv)` del bot.
- Gaps por bot se publican solo si `n señales IA >= 5` para evitar conclusiones con muestra mínima.
- Si `precision@85` baja o n es pequeño: recalibrar/proteger compuerta.
- Si gap Prob-Hit por bot es alto: bajar exposición o bloquear bot temporalmente.
- EWMA por bot con n bajo debe leerse como semáforo blando; evitar castigos duros hasta tener muestra madura.
- Si auth/ws/timeouts suben: estabilizar conectividad antes de evaluar modelo.
- Si WHY-NO se concentra en `trigger_no`/`confirm_pending`: revisar timing de señales y trigger.
