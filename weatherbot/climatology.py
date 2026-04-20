#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
climatology.py — 30-year normales y anomaly score por ciudad/fecha
====================================================================
Consulta la API Archive gratuita de Open-Meteo para construir una
media y desviación típica histórica del Tmax (1994-2024) para cada
pareja (ciudad, mes-día). Devuelve un z-score que mide cuán "anómalo"
es el forecast actual frente al clima típico.

Uso en bot_v2:
  from climatology import get_climatology, anomaly_z
  norm = get_climatology(city_slug, date)   # {mean, std, min, max, n}
  z    = anomaly_z(forecast_temp, norm)      # float — e.g. +2.3σ

Un z-score |z| ≥ 1.5 indica un día estadísticamente anómalo (≈top/bottom 15%).
|z| ≥ 2.0 es un evento raro (≈top/bottom 2.5%) que el mercado suele tardar
en descontar — es decir, el mejor terreno para Alfa.
"""
import json
import requests
import statistics
from pathlib import Path
from datetime import datetime, timezone

DATA_DIR   = Path(__file__).parent / "data"
CLIM_FILE  = DATA_DIR / "climatology.json"
START_YEAR = 1994
END_YEAR   = 2024  # 31 años de histórico

def _load() -> dict:
    if CLIM_FILE.exists():
        try:
            return json.loads(CLIM_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save(cache: dict):
    DATA_DIR.mkdir(exist_ok=True)
    CLIM_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def _fetch_from_openmeteo(lat: float, lon: float, unit: str, tz: str,
                          start_date: str, end_date: str) -> list:
    """Devuelve lista de Tmax diarios del archivo ERA5."""
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    url = "https://archive-api.open-meteo.com/v1/archive"
    try:
        r = requests.get(url, params={
            "latitude": lat, "longitude": lon,
            "start_date": start_date, "end_date": end_date,
            "daily": "temperature_2m_max",
            "temperature_unit": temp_unit,
            "timezone": tz,
        }, timeout=(5, 25))
        if r.status_code != 200:
            return []
        d = r.json()
        daily = d.get("daily", {})
        temps = daily.get("temperature_2m_max", [])
        times = daily.get("time", [])
        return [(t, v) for t, v in zip(times, temps) if v is not None]
    except Exception:
        return []


def get_climatology(city_slug: str, date: str, city_meta: dict = None) -> dict | None:
    """Devuelve normales climatológicas para (city_slug, date).

    city_meta = {lat, lon, unit, tz} — si se omite, busca en data/climatology.json
    para evitar refetches. Caché persistente: si ya se computó, no re-consulta.

    Returns {mean, std, min, max, n, updated_at} o None si falla.
    """
    cache = _load()
    key   = f"{city_slug}_{date[5:]}"   # city_MM-DD (agnóstico al año)
    if key in cache and cache[key].get("n", 0) >= 15:
        return cache[key]

    if not city_meta:
        return cache.get(key)  # sin metadata, no podemos fetchear

    # Fetch: +/- 3 días alrededor de MM-DD para cada año (más datos)
    month_day = date[5:]  # "04-20"
    all_temps = []
    # Ventana: el día exacto en cada año
    for year in range(START_YEAR, END_YEAR + 1):
        # Un rango corto de 7 días centrado reduce varianza climática
        sd = f"{year}-{month_day}"
        records = _fetch_from_openmeteo(
            city_meta["lat"], city_meta["lon"], city_meta["unit"],
            city_meta.get("tz", "UTC"), sd, sd,
        )
        for t, v in records:
            all_temps.append(v)

    if len(all_temps) < 15:
        return None

    norm = {
        "mean":       round(statistics.mean(all_temps), 1),
        "std":        round(statistics.stdev(all_temps), 2),
        "min":        round(min(all_temps), 1),
        "max":        round(max(all_temps), 1),
        "n":          len(all_temps),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    cache[key] = norm
    _save(cache)
    return norm


def anomaly_z(forecast_temp: float, norm: dict | None) -> float | None:
    """Z-score del forecast respecto a la normal climatológica.

    z > 0  → más caliente de lo normal
    z < 0  → más frío
    |z| ≥ 1.5 → día anómalo estadísticamente (~top/bottom 15%)
    |z| ≥ 2.0 → evento raro (top/bottom 2.5%) — máxima oportunidad de Alfa.
    """
    if not norm or forecast_temp is None:
        return None
    std = norm.get("std") or 0
    if std <= 0:
        return 0.0
    return round((forecast_temp - norm["mean"]) / std, 2)


def anomaly_class(z: float | None) -> str:
    """Clasificación textual del z-score para logs/telegram."""
    if z is None:
        return "normal"
    a = abs(z)
    if a >= 2.5: return "extreme"
    if a >= 1.5: return "anomalous"
    if a >= 0.8: return "unusual"
    return "normal"


if __name__ == "__main__":
    # CLI test
    import sys
    sys.stdout.reconfigure(encoding='utf-8')
    city_meta = {"lat": 48.9962, "lon": 2.5979, "unit": "C", "tz": "Europe/Paris"}
    n = get_climatology("paris", "2026-04-20", city_meta)
    print(f"Paris 04-20 normal: {n}")
    print(f"Si forecast = 24°C → z = {anomaly_z(24, n)} ({anomaly_class(anomaly_z(24, n))})")
    print(f"Si forecast = 10°C → z = {anomaly_z(10, n)} ({anomaly_class(anomaly_z(10, n))})")
