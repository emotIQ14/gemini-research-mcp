"""
learning.py — Módulo de auto-aprendizaje para WeatherBet
=========================================================
Analiza los resultados históricos de mercados (resueltos Y cerrados
por stop_loss/forecast_changed) y ajusta:
  1. Kelly adaptativo por ciudad (basado en win-rate real)
  2. MIN_EV dinámico global (basado en rendimiento reciente)
  3. Ranking de fuentes de previsión por ciudad (menor sigma = más peso)
  4. Penalty por volatilidad: ciudades con muchos stop_loss / forecast_changed
     reciben un ev_multiplier (sube el umbral EV requerido) y un
     size_multiplier (reduce el tamaño de la apuesta).
  5. Blacklist: ciudades con PnL < umbral crítico quedan pausadas
     temporalmente hasta que la calibración demuestre recuperación.

Los parámetros se guardan en data/adaptive_config.json y el bot los
carga en cada ciclo para tomar mejores decisiones de entrada/salida.
"""

import json
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

DATA_DIR       = Path("data")
ADAPTIVE_FILE  = DATA_DIR / "adaptive_config.json"
CALIBRATION_FILE = DATA_DIR / "calibration.json"
REAL_PNL_FILE  = DATA_DIR / "real_pnl.json"   # GROUND TRUTH: per-city PnL from Polymarket

# Límites para evitar sobre-ajuste
KELLY_SCALE_MIN  = 0.60   # nunca bajar el Kelly más del 40%
KELLY_SCALE_MAX  = 1.40   # nunca subirlo más del 40%
MIN_EV_BASE      = 0.10   # valor base original
MIN_EV_MIN       = 0.05   # floor — nunca menos de 5%
MIN_EV_MAX       = 0.20   # ceiling — nunca más de 20%
MIN_SAMPLES      = 5      # mínimo de mercados resueltos para ajustar una ciudad
RECENT_WINDOW    = 20     # últimos N mercados para MIN_EV global

# ── Penalty por volatilidad de ciudad ─────────────────────────────────────────
# Cerrar por stop_loss o forecast_changed = nuestra señal fue mala.
# Si una ciudad lo hace repetidamente → nos castigamos: subimos MIN_EV y
# bajamos el tamaño. Si la pérdida acumulada es grande → blacklist.
CLOSE_PENALTY_MIN_TRADES = 5      # analizar solo si hay >=5 trades cerrados
EV_MULT_MIN              = 1.0    # neutral
EV_MULT_MAX              = 2.5    # 2.5× más exigente en EV (bloquea señales débiles)
SIZE_MULT_MIN            = 0.4    # reduce tamaño a 40% mínimo
SIZE_MULT_MAX            = 1.2    # bonus 20% en ciudades ganadoras
BLACKLIST_PNL_THRESHOLD  = -40.0  # pnl acumulado < -$40 → pausar city
BLACKLIST_MIN_TRADES     = 6      # con al menos 6 trades (no por ruido aleatorio)


def load_adaptive() -> dict:
    if ADAPTIVE_FILE.exists():
        cfg = json.loads(ADAPTIVE_FILE.read_text(encoding="utf-8"))
        cfg.setdefault("ev_mult", {})
        cfg.setdefault("size_mult", {})
        cfg.setdefault("blacklist", [])
        cfg.setdefault("close_stats", {})
        return cfg
    return {
        "kelly_scale": {},     # city_slug → float (multiplicador)
        "min_ev":      MIN_EV_BASE,
        "best_source": {},     # city_slug → "ecmwf" | "hrrr" | "metar"
        "city_stats":  {},     # city_slug → {wins, losses, pnl, win_rate}
        "ev_mult":     {},     # city_slug → float (>=1.0, sube MIN_EV en ciudades volátiles)
        "size_mult":   {},     # city_slug → float (<=1.0, reduce tamaño en ciudades volátiles)
        "blacklist":   [],     # list[str] — ciudades pausadas por PnL crítico
        "close_stats": {},     # city_slug → {total, sl, tr, fc, resolved, pnl}
        "updated_at":  None,
    }


def save_adaptive(cfg: dict):
    cfg["updated_at"] = datetime.now(timezone.utc).isoformat()
    DATA_DIR.mkdir(exist_ok=True)
    ADAPTIVE_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _city_stats(markets: list) -> dict:
    """Calcula win/loss/pnl por ciudad desde mercados resueltos."""
    stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "entries": []})
    for m in markets:
        if m.get("status") != "resolved":
            continue
        pos = m.get("position")
        if not pos or pos.get("status") not in ("closed",):
            continue
        city = m.get("city")
        if not city:
            continue
        outcome = m.get("resolved_outcome")
        pnl     = pos.get("pnl", 0.0) or 0.0
        if outcome == "win":
            stats[city]["wins"]  += 1
        elif outcome == "loss":
            stats[city]["losses"] += 1
        stats[city]["pnl"] += pnl
        stats[city]["entries"].append({
            "outcome": outcome,
            "pnl":     pnl,
            "price":   pos.get("entry_price"),
            "ev":      pos.get("ev"),
        })
    return dict(stats)


def _kelly_scale_for_city(wins: int, losses: int) -> float:
    """
    Calcula un multiplicador para el Kelly según win-rate de la ciudad.
    Win-rate > 60% → escala arriba (más confianza).
    Win-rate < 40% → escala abajo (menos confianza).
    Entre 40-60%   → sin cambio.
    """
    total = wins + losses
    if total < MIN_SAMPLES:
        return 1.0
    wr = wins / total
    if wr >= 0.70:
        scale = 1.40
    elif wr >= 0.60:
        scale = 1.20
    elif wr <= 0.30:
        scale = 0.60
    elif wr <= 0.40:
        scale = 0.80
    else:
        scale = 1.0
    return round(scale, 2)


def _dynamic_min_ev(markets: list) -> float:
    """
    Ajusta MIN_EV global según el rendimiento de los últimos RECENT_WINDOW mercados.
    Buen rendimiento  → umbral más bajo (entramos en más mercados)
    Mal rendimiento   → umbral más alto (somos más selectivos)
    """
    resolved = [
        m for m in markets
        if m.get("status") == "resolved" and m.get("resolved_outcome") in ("win", "loss")
    ]
    recent = resolved[-RECENT_WINDOW:]
    if len(recent) < 5:
        return MIN_EV_BASE

    wins   = sum(1 for m in recent if m.get("resolved_outcome") == "win")
    wr     = wins / len(recent)

    if wr >= 0.65:
        new_ev = MIN_EV_BASE * 0.85     # bajamos filtro → más oportunidades
    elif wr >= 0.55:
        new_ev = MIN_EV_BASE * 0.93
    elif wr <= 0.35:
        new_ev = MIN_EV_BASE * 1.20     # subimos filtro → más selectivos
    elif wr <= 0.45:
        new_ev = MIN_EV_BASE * 1.10
    else:
        new_ev = MIN_EV_BASE

    return round(min(max(new_ev, MIN_EV_MIN), MIN_EV_MAX), 4)


def _close_stats(markets: list) -> dict:
    """Contabiliza TODOS los cierres por ciudad (no solo resolved).

    Cuenta: resolved (win/loss) + stop_loss + trailing_stop + forecast_changed.
    Computa PnL acumulado incluyendo TODOS los cierres.
    Esto revela ciudades volátiles que generan pérdidas sin llegar a resolver.
    """
    stats = defaultdict(lambda: {
        "total":     0,
        "sl":        0,   # stop_loss
        "tr":        0,   # trailing_stop
        "fc":        0,   # forecast_changed
        "resolved":  0,
        "wins":      0,
        "losses":    0,
        "pnl":       0.0,
    })
    for m in markets:
        pos = m.get("position") or {}
        if not pos or pos.get("status") != "closed":
            continue
        city = m.get("city") or m.get("city_slug")
        if not city:
            continue
        reason = pos.get("close_reason", "")
        pnl    = pos.get("pnl", 0.0) or 0.0
        stats[city]["total"] += 1
        stats[city]["pnl"]   += pnl
        if reason == "stop_loss":
            stats[city]["sl"] += 1
        elif reason == "trailing_stop":
            stats[city]["tr"] += 1
        elif reason == "forecast_changed":
            stats[city]["fc"] += 1
        elif reason == "resolved":
            stats[city]["resolved"] += 1
            if m.get("resolved_outcome") == "win":
                stats[city]["wins"] += 1
            elif m.get("resolved_outcome") == "loss":
                stats[city]["losses"] += 1
    return dict(stats)


def _load_real_pnl() -> dict:
    """Carga PnL REAL por ciudad desde Polymarket (ground truth).

    Formato esperado (escrito por scripts/refresh_real_pnl.py):
      { "ankara": {"bought": 15.09, "sold": 61.70, "open_cost": 12.57,
                   "open_value": 11.24, "pnl_total": 57.85,
                   "n_trades": 12, "n_open": 1}, ... }
    """
    if REAL_PNL_FILE.exists():
        try:
            return json.loads(REAL_PNL_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _ev_and_size_mult_for_city(cs: dict, real: dict | None = None) -> tuple[float, float]:
    """Calcula ev_multiplier y size_multiplier para una ciudad.

    PRIORIDAD 1: si hay datos REALES de Polymarket (real), usar pnl_total/n_trades
                 como señal primaria — es la ground truth (incluye SELLs reales).
    PRIORIDAD 2: si no, caer en análisis de cierres del paper-trading.

    pnl_per_trade (USDC reales):
      ≥ +$5   → bonus fuerte (ev×0.90, size×1.20)
      ≥ +$2   → bonus suave (ev×0.95, size×1.15)
      ≥ +$0.5 → ligero favor (ev×1.00, size×1.10)
      ≤ -$2   → penalty (ev×1.5, size×0.70)
      ≤ -$4   → penalty fuerte (ev×2.0, size×0.50)
      ≤ -$8   → penalty máximo (ev×2.5, size×0.40)
    """
    # ── GROUND TRUTH: usar datos reales si disponibles
    if real and real.get("n_trades", 0) >= CLOSE_PENALTY_MIN_TRADES:
        pnl_tot = real.get("pnl_total", 0.0)
        n       = real["n_trades"]
        pnl_pt  = pnl_tot / n  # USDC por trade

        # Ganadores (bonus en budget y menor umbral EV)
        if pnl_pt >= 3.0:
            return 0.90, 1.20   # muy ganadora (ankara, miami…)
        if pnl_pt >= 1.0:
            return 0.95, 1.15   # ganadora
        if pnl_pt >= 0.3:
            return 0.97, 1.10   # ligeramente positiva
        # Perdedoras (más exigente en EV, menos tamaño)
        if pnl_pt <= -2.0:
            return EV_MULT_MAX, SIZE_MULT_MIN   # 2.5, 0.4 — pérdida grave
        if pnl_pt <= -1.0:
            return 1.8, 0.55                    # pérdida clara
        if pnl_pt <= -0.5:
            return 1.4, 0.75                    # pérdida moderada
        if pnl_pt <= -0.1:
            return 1.15, 0.90                   # pérdida leve
        # Neutral real (-0.1..+0.3): sin cambio
        return 1.0, 1.0

    # ── FALLBACK: paper-trading data (menos preciso)
    total = cs.get("total", 0)
    if total < CLOSE_PENALTY_MIN_TRADES:
        return 1.0, 1.0

    pnl_per_trade = cs.get("pnl", 0) / total
    if pnl_per_trade >= 10.0:
        return 0.95, 1.15
    if pnl_per_trade <= -10.0:
        return EV_MULT_MAX, SIZE_MULT_MIN
    if pnl_per_trade <= -4.0:
        return 2.0, 0.50
    return 1.0, 1.0


def _compute_blacklist(close_stats: dict, real: dict | None = None) -> list:
    """Devuelve lista de ciudades pausadas temporalmente.

    Si hay datos reales de Polymarket, usarlos como ÚNICA fuente de verdad:
    blacklist = ciudades con pnl_total ≤ −$15 AND n_trades ≥ 6 AND
                pnl_per_trade ≤ −$2 (es pérdida consistente, no un outlier).

    Sin datos reales (fallback), usar análisis de close_stats con criterios
    más conservadores.
    """
    real = real or {}
    bl = []

    # PRIORIDAD 1: datos reales de Polymarket
    cities_with_real = {c for c, r in real.items() if r.get("n_trades", 0) >= BLACKLIST_MIN_TRADES}
    if cities_with_real:
        for city in cities_with_real:
            r = real[city]
            n = r["n_trades"]
            pnl_tot = r.get("pnl_total", 0.0)
            pnl_pt  = pnl_tot / n
            # Blacklist si pierde ≥ $12 acumulados Y pierde al menos $0.8/trade
            if pnl_tot <= -12.0 and pnl_pt <= -0.8:
                bl.append(city)
        return sorted(bl)

    # PRIORIDAD 2: fallback a close_stats del paper-trading
    for city, cs in close_stats.items():
        total = cs["total"]
        if total < BLACKLIST_MIN_TRADES:
            continue
        if cs["pnl"] > BLACKLIST_PNL_THRESHOLD:
            continue
        if cs["pnl"] / total > -3.0:
            continue
        if cs["sl"] / total < 0.30:
            continue
        bl.append(city)
    return sorted(bl)


def _best_source_per_city(cal: dict) -> dict:
    """
    Para cada ciudad, devuelve la fuente con menor sigma (más precisa).
    Si no hay calibración suficiente para una ciudad, devuelve "ecmwf" (default).
    """
    best = {}
    cities = set(k.rsplit("_", 1)[0] for k in cal)
    for city in cities:
        candidates = {}
        for source in ["ecmwf", "hrrr", "metar"]:
            key = f"{city}_{source}"
            if key in cal and cal[key].get("n", 0) >= 3:
                candidates[source] = cal[key]["sigma"]
        if candidates:
            best[city] = min(candidates, key=candidates.get)
    return best


def run_learning(markets: list) -> dict:
    """
    Punto de entrada principal. Llámalo tras run_calibration().
    Retorna el adaptive_config actualizado.
    """
    cfg = load_adaptive()

    # 1. Estadísticas por ciudad
    stats = _city_stats(markets)
    city_stats_out = {}
    kelly_scale    = {}

    for city, s in stats.items():
        wins   = s["wins"]
        losses = s["losses"]
        total  = wins + losses
        wr     = wins / total if total > 0 else 0.0
        scale  = _kelly_scale_for_city(wins, losses)
        kelly_scale[city]    = scale
        city_stats_out[city] = {
            "wins":     wins,
            "losses":   losses,
            "pnl":      round(s["pnl"], 2),
            "win_rate": round(wr, 3),
            "kelly_scale": scale,
        }

    # 2. MIN_EV global dinámico
    new_min_ev = _dynamic_min_ev(markets)

    # 3. Mejor fuente por ciudad (desde calibration.json)
    cal = {}
    if CALIBRATION_FILE.exists():
        cal = json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
    best_source = _best_source_per_city(cal)

    # 4. Close-stats + REAL Polymarket PnL → ev_mult + size_mult + blacklist
    close_stats = _close_stats(markets)
    real_pnl    = _load_real_pnl()

    # Unión de ciudades (las que aparecen en close_stats O en real_pnl)
    all_cities = set(close_stats.keys()) | set(real_pnl.keys())
    ev_mult_map   = {}
    size_mult_map = {}
    for city in all_cities:
        cs = close_stats.get(city, {"total": 0, "sl": 0, "tr": 0, "fc": 0,
                                     "resolved": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        rp = real_pnl.get(city)
        ev_m, size_m = _ev_and_size_mult_for_city(cs, rp)
        ev_mult_map[city]   = ev_m
        size_mult_map[city] = size_m

    blacklist = _compute_blacklist(close_stats, real_pnl)

    # 5. Detectar cambios relevantes para logging
    changes = []
    old_ev = cfg.get("min_ev", MIN_EV_BASE)
    if abs(new_min_ev - old_ev) >= 0.005:
        changes.append(f"MIN_EV {old_ev:.3f}->{new_min_ev:.3f}")

    for city, scale in kelly_scale.items():
        old_scale = cfg.get("kelly_scale", {}).get(city, 1.0)
        if abs(scale - old_scale) >= 0.1:
            changes.append(f"Kelly[{city}] {old_scale:.2f}->{scale:.2f}")

    for city, src in best_source.items():
        old_src = cfg.get("best_source", {}).get(city, "ecmwf")
        if src != old_src:
            changes.append(f"Source[{city}] {old_src}->{src}")

    old_bl = set(cfg.get("blacklist", []))
    new_bl = set(blacklist)
    if old_bl != new_bl:
        added   = sorted(new_bl - old_bl)
        removed = sorted(old_bl - new_bl)
        if added:
            changes.append(f"Blacklist+[{','.join(added)}]")
        if removed:
            changes.append(f"Blacklist-[{','.join(removed)}]")

    # 6. Guardar
    cfg["kelly_scale"] = kelly_scale
    cfg["min_ev"]      = new_min_ev
    cfg["best_source"] = best_source
    cfg["city_stats"]  = city_stats_out
    cfg["ev_mult"]     = ev_mult_map
    cfg["size_mult"]   = size_mult_map
    cfg["blacklist"]   = blacklist
    cfg["close_stats"] = {
        c: {k: round(v, 2) if isinstance(v, float) else v for k, v in cs.items()}
        for c, cs in close_stats.items()
    }
    save_adaptive(cfg)

    if changes:
        print(f"  [LEARN] {' | '.join(changes)}")
    else:
        resolved_total = sum(s["wins"] + s["losses"] for s in stats.values())
        print(f"  [LEARN] Sin cambios — {resolved_total} mercados analizados")

    return cfg
