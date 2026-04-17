#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
refresh_real_pnl.py — Descarga PnL REAL de Polymarket por ciudad
=================================================================
Escribe data/real_pnl.json, que `learning.py` usa como ground truth
para decidir el multiplier y la blacklist por ciudad. Los archivos
internos (weatherbot/data/markets/*.json) son paper-trading y no
reflejan los SELLs reales del bridge, así que esta fuente es la fiable.

Ejecutar cada X scans desde scan_and_update() — o manualmente.
"""
import json
import requests
from pathlib import Path
from collections import defaultdict

SAFE_ADDRESS = "0xd3842227909efc0893047c88822cde2db750130e"
DATA_DIR     = Path(__file__).parent / "data"
OUT_FILE     = DATA_DIR / "real_pnl.json"

_CITIES = [
    ("new york", "nyc"), ("chicago", "chicago"), ("miami", "miami"),
    ("dallas", "dallas"), ("seattle", "seattle"), ("atlanta", "atlanta"),
    ("london", "london"), ("paris", "paris"), ("munich", "munich"),
    ("ankara", "ankara"), ("seoul", "seoul"), ("tokyo", "tokyo"),
    ("shanghai", "shanghai"), ("singapore", "singapore"), ("lucknow", "lucknow"),
    ("tel aviv", "tel-aviv"), ("toronto", "toronto"),
    ("sao paulo", "sao-paulo"), ("buenos aires", "buenos-aires"),
    ("wellington", "wellington"),
]


def _city_of(title: str) -> str:
    t = (title or "").lower()
    for name, slug in _CITIES:
        if name in t:
            return slug
    return "?"


def refresh(verbose: bool = False) -> dict:
    """Descarga trades + positions de Polymarket, agrega por ciudad, escribe JSON."""
    trades = requests.get(
        f"https://data-api.polymarket.com/trades?user={SAFE_ADDRESS}&limit=1000",
        timeout=20,
    ).json()
    positions = requests.get(
        f"https://data-api.polymarket.com/positions?user={SAFE_ADDRESS}&sizeThreshold=0.01",
        timeout=15,
    ).json()

    stats = defaultdict(lambda: {
        "bought": 0.0, "sold": 0.0,
        "open_cost": 0.0, "open_value": 0.0,
        "n_trades": 0, "n_open": 0,
    })

    for t in trades or []:
        city = _city_of(t.get("title", ""))
        u    = float(t["size"]) * float(t["price"])
        if t["side"] == "BUY":
            stats[city]["bought"] += u
        else:
            stats[city]["sold"]   += u
        stats[city]["n_trades"] += 1

    for p in positions or []:
        city = _city_of(p.get("title", ""))
        size = float(p.get("size", 0))
        cur  = float(p.get("curPrice", 0))
        avg  = float(p.get("avgPrice", 0))
        stats[city]["open_cost"]  += size * avg
        stats[city]["open_value"] += size * cur
        stats[city]["n_open"]     += 1

    out = {}
    for c, s in stats.items():
        pnl_total = round(s["sold"] + s["open_value"] - s["bought"], 2)
        out[c] = {
            "bought":     round(s["bought"], 2),
            "sold":       round(s["sold"], 2),
            "open_cost":  round(s["open_cost"], 2),
            "open_value": round(s["open_value"], 2),
            "pnl_total":  pnl_total,
            "n_trades":   s["n_trades"],
            "n_open":     s["n_open"],
        }

    DATA_DIR.mkdir(exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, indent=2), encoding="utf-8")

    if verbose:
        print(f"\nPnL REAL por ciudad (Polymarket)")
        print(f"{'CITY':<14} {'#':>3} {'PnL':>8}")
        for city, d in sorted(out.items(), key=lambda x: -x[1]["pnl_total"]):
            print(f"{city:<14} {d['n_trades']:>3}  ${d['pnl_total']:>+7.2f}")
        total = sum(d["pnl_total"] for d in out.values())
        print(f"{'TOTAL':<14}     ${total:>+7.2f}")

    return out


if __name__ == "__main__":
    import sys
    refresh(verbose=True)
