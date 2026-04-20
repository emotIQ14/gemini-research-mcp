#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
climate_indices.py — índices climáticos de largo plazo (ENSO, AO)
==================================================================
Pulls free data feeds from NOAA CPC:
  * ONI (Oceanic Niño Index) — estado El Niño / La Niña trimestral
  * AO  (Arctic Oscillation) — patrón de vientos árticos mensual

Cada índice modula el bias estacional para mercados afectados:

  ENSO +1 (El Niño fuerte)  → inviernos más cálidos en norte de US
                             y Europa; Australia más seca y cálida
  ENSO -1 (La Niña fuerte)  → frío en US, calor en SE Asia
  AO    +1 (positiva)       → vientos árticos retenidos → Europa
                             y US NE más cálidos
  AO    -1 (negativa)       → aire ártico desciende → cold snaps

Uso:
  from climate_indices import get_climate_bias
  bias_c = get_climate_bias(city_slug, date)   # °C de sesgo a añadir

El bias es SUTIL (|bias| < 0.5°C en la mayoría de casos). Solo se activa
cuando el índice está claramente en territorio extremo (|ONI| > 1, |AO| > 1.5).
"""
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta
import json

DATA_DIR   = Path(__file__).parent / "data"
CACHE_FILE = DATA_DIR / "climate_indices_cache.json"
CACHE_TTL  = 6 * 3600   # 6h — índices se actualizan mensualmente, 6h es seguro

_ONI_URL = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"
_AO_URL  = "https://www.cpc.ncep.noaa.gov/products/precip/CWlink/daily_ao_index/monthly.ao.index.b50.current.ascii.table"


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cache(c: dict):
    DATA_DIR.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps(c, indent=2), encoding="utf-8")


def _fresh(cache: dict) -> bool:
    ts = cache.get("fetched_at")
    if not ts:
        return False
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
        return age < CACHE_TTL
    except Exception:
        return False


def _fetch_oni() -> float | None:
    """Parsea la última línea del feed ONI. Devuelve el valor ANOM."""
    try:
        r = requests.get(_ONI_URL, timeout=8)
        if r.status_code != 200:
            return None
        # Cada línea: "  SEAS YEAR   SST   ANOM"
        for line in reversed(r.text.strip().splitlines()):
            parts = line.split()
            if len(parts) == 4:
                try:
                    return float(parts[-1])
                except ValueError:
                    continue
    except Exception:
        pass
    return None


def _fetch_ao() -> float | None:
    """Última AO mensual de la tabla texto NOAA."""
    try:
        r = requests.get(_AO_URL, timeout=8)
        if r.status_code != 200:
            return None
        lines = r.text.strip().splitlines()
        # Última fila no vacía: YEAR val1 val2 ...
        for line in reversed(lines):
            parts = line.split()
            # Primer token = año (4 dígitos); el resto son valores
            if len(parts) >= 2 and parts[0].isdigit() and len(parts[0]) == 4:
                # Tomar el último valor numérico de la línea
                for v in reversed(parts[1:]):
                    try:
                        return float(v)
                    except ValueError:
                        continue
    except Exception:
        pass
    return None


def _refresh_if_stale() -> dict:
    cache = _load_cache()
    if _fresh(cache):
        return cache
    oni = _fetch_oni()
    ao  = _fetch_ao()
    cache = {
        "oni":         oni,
        "ao":          ao,
        "fetched_at":  datetime.now(timezone.utc).isoformat(),
    }
    _save_cache(cache)
    return cache


def get_climate_bias(city_slug: str, date: str = None) -> float:
    """Devuelve bias de temperatura esperada (°C) basado en índices climáticos.

    El bias se aplica como offset al forecast del ensemble antes de calcular
    la probabilidad. Diseño: bias máximo ±0.5°C, neutral si índices < umbral.
    """
    cache = _refresh_if_stale()
    oni = cache.get("oni") or 0.0
    ao  = cache.get("ao")  or 0.0
    bias = 0.0

    # Región de la ciudad (si está configurada en bot_v2.LOCATIONS)
    try:
        from bot_v2 import LOCATIONS
        region = LOCATIONS.get(city_slug, {}).get("region", "")
    except Exception:
        region = ""

    # --- ENSO effect ---
    # |ONI| > 0.5 ≡ el Niño o la Niña declarados
    if abs(oni) > 0.5:
        # El Niño (oni > 0)
        if oni > 0:
            if region == "us":         bias += min(0.4, oni * 0.3)
            elif region == "eu":       bias += min(0.3, oni * 0.2)
            elif region == "asia":     bias -= min(0.3, oni * 0.15)  # Indonesia/SE Asia más seco pero variable
            elif region == "sa":       bias += min(0.4, oni * 0.3)
            elif region == "oc":       bias += min(0.4, oni * 0.3)  # AU/NZ sequedad/calor
        # La Niña (oni < 0)
        else:
            if region == "us":         bias -= min(0.4, abs(oni) * 0.3)
            elif region == "eu":       bias -= min(0.3, abs(oni) * 0.2)
            elif region == "asia":     bias += min(0.3, abs(oni) * 0.15)
            elif region == "sa":       bias -= min(0.4, abs(oni) * 0.3)
            elif region == "oc":       bias -= min(0.4, abs(oni) * 0.3)

    # --- Arctic Oscillation effect (solo afecta mid-latitudes Norte) ---
    if abs(ao) > 1.0:
        if region in ("us", "eu"):
            # AO positiva = jet stream fuerte, vientos zonales → Norte templado
            # AO negativa = jet débil, aire ártico baja → frío
            bias += max(-0.5, min(0.5, ao * 0.2))

    return round(bias, 2)


def get_indices() -> dict:
    """Debug / logging: devuelve los índices actuales en caché."""
    return _refresh_if_stale()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    print("Índices actuales:", get_indices())
    for city in ["nyc", "london", "tokyo", "sao-paulo", "wellington"]:
        bias = get_climate_bias(city)
        print(f"  {city}: bias = {bias:+.2f}°C")
