#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weatherbet.py — Weather Trading Bot for Polymarket
=====================================================
Tracks weather forecasts from 3 sources (ECMWF, HRRR, METAR),
compares with Polymarket markets, paper trades using Kelly criterion.

Usage:
    python weatherbet.py          # main loop
    python weatherbet.py report   # full report
    python weatherbet.py status   # balance and open positions
"""

import re
import sys
import json
import math
import time
import statistics
import os
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

try:
    import telegram_notify as tg
except ImportError:
    class _NoTG:
        def __getattr__(self, _): return lambda *a, **k: None
    tg = _NoTG()

try:
    from learning import run_learning, load_adaptive
    _LEARNING = True
except ImportError:
    _LEARNING = False
    def load_adaptive(): return {"kelly_scale": {}, "min_ev": None, "best_source": {}}
    def run_learning(markets): return {}

try:
    from climatology import get_climatology, anomaly_z, anomaly_class
    _CLIMATOLOGY = True
except ImportError:
    _CLIMATOLOGY = False
    def get_climatology(*a, **k): return None
    def anomaly_z(t, n): return None
    def anomaly_class(z): return "normal"

try:
    from climate_indices import get_climate_bias
    _CLIMATE_INDICES = True
except ImportError:
    _CLIMATE_INDICES = False
    def get_climate_bias(*a, **k): return 0.0

# =============================================================================
# CONFIG
# =============================================================================

with open("config.json", encoding="utf-8") as f:
    _cfg = json.load(f)

BALANCE          = _cfg.get("balance", 10000.0)
MAX_BET          = _cfg.get("max_bet", 20.0)        # max bet per trade
MIN_EV           = _cfg.get("min_ev", 0.10)
MAX_PRICE        = _cfg.get("max_price", 0.45)
MIN_VOLUME       = _cfg.get("min_volume", 500)
MIN_HOURS        = _cfg.get("min_hours", 2.0)
MAX_HOURS        = _cfg.get("max_hours", 72.0)
KELLY_FRACTION   = _cfg.get("kelly_fraction", 0.25)
MAX_SLIPPAGE     = _cfg.get("max_slippage", 0.03)  # max allowed ask-bid spread
SCAN_INTERVAL    = _cfg.get("scan_interval", 3600)   # every hour
CALIBRATION_MIN  = _cfg.get("calibration_min", 30)
VC_KEY           = _cfg.get("vc_key", "")

SIGMA_F = 2.0
SIGMA_C = 1.2

# ── High-Confidence Ensemble Betting ──────────────────────────────────────────
# When 4-5 global models agree tightly AND Polymarket hasn't repriced yet
# → bet more budget to capture the edge before the market catches up
HIGH_CONF_MAX_BET        = _cfg.get("high_conf_max_bet", 15.0)          # max bet for HC trades
HIGH_CONF_AGREEMENT      = _cfg.get("high_conf_agreement", 0.70)         # min inter-model agreement
HIGH_CONF_MIN_MARKET_LAG = _cfg.get("high_conf_min_market_lag", 0.10)   # min p−price gap
HIGH_CONF_MIN_PROB       = _cfg.get("high_conf_min_prob", 0.55)          # min ensemble probability

# Open-Meteo multi-model ensemble (all free, no API key needed).
# Region-aware: add the LOCAL model(s) for each zone (UKMO for EU, JMA for Asia,
# MeteoFrance AROME for France) — they tend to be the most accurate inside
# their own territory.
ENSEMBLE_MODELS_BASE = ["ecmwf_ifs025", "gfs_seamless", "icon_seamless", "gem_seamless"]
ENSEMBLE_MODELS_BY_REGION = {
    "us":   ENSEMBLE_MODELS_BASE,                                    # 4 models
    "eu":   ENSEMBLE_MODELS_BASE + ["ukmo_seamless", "arpege_world"], # 6 models
    "asia": ENSEMBLE_MODELS_BASE + ["jma_seamless"],                  # 5 models
    "ca":   ENSEMBLE_MODELS_BASE,                                    # 4 models (GEM already Canadian)
    "sa":   ENSEMBLE_MODELS_BASE,                                    # 4 models
    "oc":   ENSEMBLE_MODELS_BASE,                                    # 4 models
}
# Back-compat constants (deprecated but kept for external callers)
ENSEMBLE_MODELS_C = ENSEMBLE_MODELS_BASE + ["ukmo_seamless", "arpege_world", "jma_seamless"]
ENSEMBLE_MODELS_F = ENSEMBLE_MODELS_BASE

# OpenWeatherMap keys (primary + optional backup for rate-limit fallback)
_OWM_KEY = (os.getenv("OPENWEATHER_API_KEY", "") or "").strip()
_OWM_KEY = None if _OWM_KEY in ("", "your_api_key_here") else _OWM_KEY
_OWM_KEY_BACKUP = (os.getenv("OPENWEATHER_API_KEY_BACKUP", "") or "").strip()
_OWM_KEY_BACKUP = None if _OWM_KEY_BACKUP in ("", "your_api_key_here") else _OWM_KEY_BACKUP

DATA_DIR         = Path("data")
DATA_DIR.mkdir(exist_ok=True)
STATE_FILE       = DATA_DIR / "state.json"
MARKETS_DIR      = DATA_DIR / "markets"
MARKETS_DIR.mkdir(exist_ok=True)
CALIBRATION_FILE = DATA_DIR / "calibration.json"

LOCATIONS = {
    "nyc":          {"lat": 40.7772,  "lon":  -73.8726, "name": "New York City", "station": "KLGA", "unit": "F", "region": "us"},
    "chicago":      {"lat": 41.9742,  "lon":  -87.9073, "name": "Chicago",       "station": "KORD", "unit": "F", "region": "us"},
    "miami":        {"lat": 25.7959,  "lon":  -80.2870, "name": "Miami",         "station": "KMIA", "unit": "F", "region": "us"},
    "dallas":       {"lat": 32.8471,  "lon":  -96.8518, "name": "Dallas",        "station": "KDAL", "unit": "F", "region": "us"},
    "seattle":      {"lat": 47.4502,  "lon": -122.3088, "name": "Seattle",       "station": "KSEA", "unit": "F", "region": "us"},
    "atlanta":      {"lat": 33.6407,  "lon":  -84.4277, "name": "Atlanta",       "station": "KATL", "unit": "F", "region": "us"},
    "london":       {"lat": 51.5048,  "lon":    0.0495, "name": "London",        "station": "EGLC", "unit": "C", "region": "eu"},
    "paris":        {"lat": 48.9962,  "lon":    2.5979, "name": "Paris",         "station": "LFPG", "unit": "C", "region": "eu"},
    "munich":       {"lat": 48.3537,  "lon":   11.7750, "name": "Munich",        "station": "EDDM", "unit": "C", "region": "eu"},
    "ankara":       {"lat": 40.1281,  "lon":   32.9951, "name": "Ankara",        "station": "LTAC", "unit": "C", "region": "eu"},
    "seoul":        {"lat": 37.4691,  "lon":  126.4505, "name": "Seoul",         "station": "RKSI", "unit": "C", "region": "asia"},
    "tokyo":        {"lat": 35.7647,  "lon":  140.3864, "name": "Tokyo",         "station": "RJTT", "unit": "C", "region": "asia"},
    "shanghai":     {"lat": 31.1443,  "lon":  121.8083, "name": "Shanghai",      "station": "ZSPD", "unit": "C", "region": "asia"},
    "singapore":    {"lat":  1.3502,  "lon":  103.9940, "name": "Singapore",     "station": "WSSS", "unit": "C", "region": "asia"},
    "lucknow":      {"lat": 26.7606,  "lon":   80.8893, "name": "Lucknow",       "station": "VILK", "unit": "C", "region": "asia"},
    "tel-aviv":     {"lat": 32.0114,  "lon":   34.8867, "name": "Tel Aviv",      "station": "LLBG", "unit": "C", "region": "asia"},
    "toronto":      {"lat": 43.6772,  "lon":  -79.6306, "name": "Toronto",       "station": "CYYZ", "unit": "C", "region": "ca"},
    "sao-paulo":    {"lat": -23.4356, "lon":  -46.4731, "name": "Sao Paulo",     "station": "SBGR", "unit": "C", "region": "sa"},
    "buenos-aires": {"lat": -34.8222, "lon":  -58.5358, "name": "Buenos Aires",  "station": "SAEZ", "unit": "C", "region": "sa"},
    "wellington":   {"lat": -41.3272, "lon":  174.8052, "name": "Wellington",    "station": "NZWN", "unit": "C", "region": "oc"},
}

TIMEZONES = {
    "nyc": "America/New_York", "chicago": "America/Chicago",
    "miami": "America/New_York", "dallas": "America/Chicago",
    "seattle": "America/Los_Angeles", "atlanta": "America/New_York",
    "london": "Europe/London", "paris": "Europe/Paris",
    "munich": "Europe/Berlin", "ankara": "Europe/Istanbul",
    "seoul": "Asia/Seoul", "tokyo": "Asia/Tokyo",
    "shanghai": "Asia/Shanghai", "singapore": "Asia/Singapore",
    "lucknow": "Asia/Kolkata", "tel-aviv": "Asia/Jerusalem",
    "toronto": "America/Toronto", "sao-paulo": "America/Sao_Paulo",
    "buenos-aires": "America/Argentina/Buenos_Aires", "wellington": "Pacific/Auckland",
}

MONTHS = ["january","february","march","april","may","june",
          "july","august","september","october","november","december"]

# =============================================================================
# MATH
# =============================================================================

def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def bucket_prob(forecast, t_low, t_high, sigma=None):
    """For regular buckets — exact match. For edge buckets — normal distribution."""
    s = sigma or 2.0
    if t_low == -999:
        return norm_cdf((t_high - float(forecast)) / s)
    if t_high == 999:
        return 1.0 - norm_cdf((t_low - float(forecast)) / s)
    return 1.0 if in_bucket(forecast, t_low, t_high) else 0.0

def calc_ev(p, price):
    if price <= 0 or price >= 1: return 0.0
    return round(p * (1.0 / price - 1.0) - (1.0 - p), 4)

def calc_kelly(p, price):
    if price <= 0 or price >= 1: return 0.0
    b = 1.0 / price - 1.0
    f = (p * b - (1.0 - p)) / b
    return round(min(max(0.0, f) * KELLY_FRACTION, 1.0), 4)

def bet_size(kelly, balance):
    raw = kelly * balance
    return round(min(raw, MAX_BET), 2)

# =============================================================================
# CALIBRATION
# =============================================================================

_cal: dict = {}

def load_cal():
    if CALIBRATION_FILE.exists():
        return json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
    return {}

def get_sigma(city_slug, source="ecmwf"):
    key = f"{city_slug}_{source}"
    if key in _cal:
        return _cal[key]["sigma"]
    return SIGMA_F if LOCATIONS[city_slug]["unit"] == "F" else SIGMA_C

def run_calibration(markets):
    """Recalculates sigma from resolved markets."""
    resolved = [m for m in markets if m.get("resolved") and m.get("actual_temp") is not None]
    cal = load_cal()
    updated = []

    for source in ["ecmwf", "hrrr", "metar"]:
        for city in set(m["city"] for m in resolved):
            group = [m for m in resolved if m["city"] == city]
            errors = []
            for m in group:
                snap = next((s for s in reversed(m.get("forecast_snapshots", []))
                             if s["source"] == source), None)
                if snap and snap.get("temp") is not None:
                    errors.append(abs(snap["temp"] - m["actual_temp"]))
            if len(errors) < CALIBRATION_MIN:
                continue
            mae  = sum(errors) / len(errors)
            key  = f"{city}_{source}"
            old  = cal.get(key, {}).get("sigma", SIGMA_F if LOCATIONS[city]["unit"] == "F" else SIGMA_C)
            new  = round(mae, 3)
            cal[key] = {"sigma": new, "n": len(errors), "updated_at": datetime.now(timezone.utc).isoformat()}
            if abs(new - old) > 0.05:
                updated.append(f"{LOCATIONS[city]['name']} {source}: {old:.2f}->{new:.2f}")

    CALIBRATION_FILE.write_text(json.dumps(cal, indent=2), encoding="utf-8")
    if updated:
        print(f"  [CAL] {', '.join(updated)}")
    return cal

# =============================================================================
# FORECASTS
# =============================================================================

def get_ecmwf(city_slug, dates):
    """ECMWF via Open-Meteo with bias correction. For all cities."""
    loc = LOCATIONS[city_slug]
    unit = loc["unit"]
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    result = {}
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max&temperature_unit={temp_unit}"
        f"&forecast_days=7&timezone={TIMEZONES.get(city_slug, 'UTC')}"
        f"&models=ecmwf_ifs025&bias_correction=true"
    )
    for attempt in range(3):
        try:
            data = requests.get(url, timeout=(5, 10)).json()
            if "error" not in data:
                for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                    if date in dates and temp is not None:
                        result[date] = round(temp, 1) if unit == "C" else round(temp)
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                print(f"  [ECMWF] {city_slug}: {e}")
    return result

def get_hrrr(city_slug, dates):
    """HRRR via Open-Meteo. US cities only, up to 48h horizon."""
    loc = LOCATIONS[city_slug]
    if loc["region"] != "us":
        return {}
    result = {}
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={loc['lat']}&longitude={loc['lon']}"
        f"&daily=temperature_2m_max&temperature_unit=fahrenheit"
        f"&forecast_days=3&timezone={TIMEZONES.get(city_slug, 'UTC')}"
        f"&models=gfs_seamless"  # HRRR+GFS seamless — best option for US
    )
    for attempt in range(3):
        try:
            data = requests.get(url, timeout=(5, 10)).json()
            if "error" not in data:
                for date, temp in zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]):
                    if date in dates and temp is not None:
                        result[date] = round(temp)
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(3)
            else:
                print(f"  [HRRR] {city_slug}: {e}")
    return result

def get_ensemble_forecast(city_slug, dates):
    """Fetch multi-model ensemble via Open-Meteo (4-6 models, no API key).

    Region-aware: pulls the local model(s) for each zone (UKMO for EU,
    JMA for Asia, MeteoFrance AROME for France) in addition to the global
    4 (ECMWF, GFS, ICON, GEM).

    Returns per-date dict:
        mean        — weighted ensemble mean (°C or °F)
        spread      — inter-model std dev (lower = more agreement)
        agreement   — 0‥1 score (1 = perfect consensus)
        high_conf   — True when agreement ≥ HIGH_CONF_AGREEMENT with ≥3 models
        n_models    — how many models returned data for this date
        models      — {model_name: temp}
    """
    loc       = LOCATIONS[city_slug]
    unit      = loc["unit"]
    region    = loc.get("region", "us")
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    models    = ENSEMBLE_MODELS_BY_REGION.get(region, ENSEMBLE_MODELS_BASE)
    result    = {}
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={loc['lat']}&longitude={loc['lon']}"
            f"&daily=temperature_2m_max&temperature_unit={temp_unit}"
            f"&forecast_days=7&timezone={TIMEZONES.get(city_slug, 'UTC')}"
            f"&models={','.join(models)}"
        )
        data = requests.get(url, timeout=(5, 12)).json()
        if "error" in data:
            return {}
        daily     = data.get("daily", {})
        date_list = daily.get("time", [])

        for i, date in enumerate(date_list):
            if date not in dates:
                continue
            model_temps = {}
            for model in models:
                key = f"temperature_2m_max_{model}"
                vals = daily.get(key, [])
                if i < len(vals) and vals[i] is not None:
                    t = round(vals[i]) if unit == "F" else round(vals[i], 1)
                    model_temps[model] = t

            if len(model_temps) < 2:
                continue

            temps  = list(model_temps.values())
            mean   = round(statistics.mean(temps), 0 if unit == "F" else 1)
            spread = round(statistics.stdev(temps), 2) if len(temps) > 1 else 0.0
            # Agreement thresholds calibrated to forecast skill per unit
            # Celsius bucket ~1°C wide → HC if spread < 1.5°C
            # Fahrenheit bucket ~2°F wide → HC if spread < 3.0°F
            threshold  = 1.5 if unit == "C" else 3.0
            agreement  = round(max(0.0, 1.0 - spread / threshold), 3)
            high_conf  = agreement >= HIGH_CONF_AGREEMENT and len(model_temps) >= 3

            result[date] = {
                "mean":      mean,
                "spread":    spread,
                "agreement": agreement,
                "high_conf": high_conf,
                "n_models":  len(model_temps),
                "models":    model_temps,
            }
    except Exception as e:
        print(f"  [ENSEMBLE] {city_slug}: {e}")
    return result


def get_yr_forecast(city_slug, dates):
    """Yr.no / Met.no Norwegian Met Institute — free, no API key.

    Gives hourly forecasts up to ~10 days. We aggregate per-date daily max
    in the CITY's local timezone. Un día sólo se devuelve si tenemos la
    franja diurna completa (la que contiene el máximo diario, normalmente
    10:00-18:00 local). Si se pregunta por "hoy" y ya pasó la tarde, el
    dato se OMITE (para no reportar min como max).
    """
    loc    = LOCATIONS[city_slug]
    unit   = loc["unit"]
    tz_str = TIMEZONES.get(city_slug, "UTC")
    result = {}
    try:
        r = requests.get(
            "https://api.met.no/weatherapi/locationforecast/2.0/compact",
            params={"lat": loc["lat"], "lon": loc["lon"]},
            headers={"User-Agent": "WeatherBet/1.0 (trading-research)"},
            timeout=(5, 10),
        )
        if r.status_code != 200:
            return {}
        d = r.json()
        from datetime import datetime as _dt
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo(tz_str)
        except Exception:
            tz = timezone.utc

        # Agrupar por fecha local + recordar qué horas se vieron por día
        daily_max = {}     # date -> max temp
        daily_hours = {}   # date -> set of local hours we have data for
        for entry in d.get("properties", {}).get("timeseries", []):
            iso = entry.get("time")
            t_c = entry.get("data", {}).get("instant", {}).get("details", {}).get("air_temperature")
            if not iso or t_c is None:
                continue
            dt_utc  = _dt.fromisoformat(iso.replace("Z", "+00:00"))
            dt_loc  = dt_utc.astimezone(tz)
            date_s  = dt_loc.strftime("%Y-%m-%d")
            if date_s not in dates:
                continue
            t_val = t_c * 9/5 + 32 if unit == "F" else t_c
            t_val = round(t_val) if unit == "F" else round(t_val, 1)
            if date_s not in daily_max or t_val > daily_max[date_s]:
                daily_max[date_s] = t_val
            daily_hours.setdefault(date_s, set()).add(dt_loc.hour)

        # Sólo devolver días donde tengamos la ventana diurna (13:00-15:00)
        for date_s, tmax in daily_max.items():
            hours_seen = daily_hours.get(date_s, set())
            diurnal = {13, 14, 15}
            if diurnal & hours_seen:
                result[date_s] = tmax
            # else: pierde esa fecha (datos incompletos, no reportar)
    except Exception as e:
        print(f"  [YR] {city_slug}: {e}")
    return result


def get_qweather_forecast(city_slug, dates):
    """QWeather / HeFeng (Chinese weather provider) — optional, requires
    QWEATHER_API_KEY and QWEATHER_API_BASE in env. Particularly strong for
    Asian cities. Falls back silently if not configured.
    """
    key  = os.getenv("QWEATHER_API_KEY", "").strip()
    base = os.getenv("QWEATHER_API_BASE", "https://devapi.qweather.com").strip()
    if not key or key == "your_api_key_here":
        return {}
    loc  = LOCATIONS[city_slug]
    unit = loc["unit"]
    result = {}
    try:
        # QWeather uses LocationID; lookup by coords
        g = requests.get(
            f"{base}/geo/v2/city/lookup",
            params={"location": f"{loc['lon']:.2f},{loc['lat']:.2f}", "key": key},
            timeout=8,
        ).json()
        locs = g.get("location") or []
        if not locs:
            return {}
        loc_id = locs[0]["id"]
        fc = requests.get(
            f"{base}/v7/weather/7d",
            params={"location": loc_id, "key": key},
            timeout=8,
        ).json()
        for day in fc.get("daily", []):
            date = day.get("fxDate")
            tmax = day.get("tempMax")
            if date in dates and tmax is not None:
                t = float(tmax)
                t_val = t * 9/5 + 32 if unit == "F" else t
                result[date] = round(t_val) if unit == "F" else round(t_val, 1)
    except Exception as e:
        print(f"  [QWEATHER] {city_slug}: {e}")
    return result


def _owm_fetch(loc, unit, key, dates):
    """Un intento de fetch a OWM con una key concreta. Devuelve {date: temp_max}."""
    units_param = "imperial" if unit == "F" else "metric"
    # Usamos lat/lon directo de LOCATIONS (evita geocoding extra)
    fc_url = (
        f"https://api.openweathermap.org/data/2.5/forecast"
        f"?lat={loc['lat']}&lon={loc['lon']}&appid={key}&units={units_param}&cnt=40"
    )
    r = requests.get(fc_url, timeout=(5, 10))
    if r.status_code != 200:
        return None  # signal failure, allow fallback
    fc = r.json()
    daily_max = {}
    for item in fc.get("list", []):
        day = item["dt_txt"][:10]
        if day not in dates:
            continue
        t = item["main"].get("temp_max", item["main"].get("temp"))
        if t is not None:
            if day not in daily_max or t > daily_max[day]:
                daily_max[day] = round(t) if unit == "F" else round(t, 1)
    return daily_max


def get_openweathermap_forecast(city_slug, dates):
    """OpenWeatherMap 5-day forecast con failover a key backup si hay una.

    Devuelve {date: temp_max} en la misma unidad que la ciudad.
    """
    if not _OWM_KEY:
        return {}
    loc  = LOCATIONS[city_slug]
    unit = loc["unit"]
    try:
        res = _owm_fetch(loc, unit, _OWM_KEY, dates)
        if res is None and _OWM_KEY_BACKUP:
            # Primary falló (401/429/etc) → probar backup
            print(f"  [OWM] {city_slug}: primary falló, probando backup key")
            res = _owm_fetch(loc, unit, _OWM_KEY_BACKUP, dates)
        return res or {}
    except Exception as e:
        print(f"  [OWM] {city_slug}: {e}")
        return {}


def get_metar(city_slug):
    """Current observed temperature from METAR station. D+0 only."""
    loc = LOCATIONS[city_slug]
    station = loc["station"]
    unit = loc["unit"]
    try:
        url = f"https://aviationweather.gov/api/data/metar?ids={station}&format=json"
        data = requests.get(url, timeout=(5, 8)).json()
        if data and isinstance(data, list):
            temp_c = data[0].get("temp")
            if temp_c is not None:
                if unit == "F":
                    return round(float(temp_c) * 9/5 + 32)
                return round(float(temp_c), 1)
    except Exception as e:
        print(f"  [METAR] {city_slug}: {e}")
    return None

def get_actual_temp(city_slug, date_str):
    """Actual temperature via Visual Crossing for closed markets."""
    loc = LOCATIONS[city_slug]
    station = loc["station"]
    unit = loc["unit"]
    vc_unit = "us" if unit == "F" else "metric"
    url = (
        f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline"
        f"/{station}/{date_str}/{date_str}"
        f"?unitGroup={vc_unit}&key={VC_KEY}&include=days&elements=tempmax"
    )
    try:
        data = requests.get(url, timeout=(5, 8)).json()
        days = data.get("days", [])
        if days and days[0].get("tempmax") is not None:
            return round(float(days[0]["tempmax"]), 1)
    except Exception as e:
        print(f"  [VC] {city_slug} {date_str}: {e}")
    return None

def check_market_resolved(market_id):
    """
    Checks if the market closed on Polymarket and who won.
    Returns: None (still open), True (YES won), False (NO won)
    """
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(5, 8))
        data = r.json()
        closed = data.get("closed", False)
        if not closed:
            return None
        # Check YES price — if ~1.0 then WIN, if ~0.0 then LOSS
        prices = json.loads(data.get("outcomePrices", "[0.5,0.5]"))
        yes_price = float(prices[0])
        if yes_price >= 0.95:
            return True   # WIN
        elif yes_price <= 0.05:
            return False  # LOSS
        return None  # not yet determined
    except Exception as e:
        print(f"  [RESOLVE] {market_id}: {e}")
    return None

# =============================================================================
# POLYMARKET
# =============================================================================

def get_polymarket_event(city_slug, month, day, year):
    slug = f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=(5, 8))
        data = r.json()
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
    except Exception:
        pass
    return None

def get_market_price(market_id):
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=(3, 5))
        prices = json.loads(r.json().get("outcomePrices", "[0.5,0.5]"))
        return float(prices[0])
    except Exception:
        return None

def parse_temp_range(question):
    if not question: return None
    num = r'(-?\d+(?:\.\d+)?)'
    if re.search(r'or below', question, re.IGNORECASE):
        m = re.search(num + r'[°]?[FC] or below', question, re.IGNORECASE)
        if m: return (-999.0, float(m.group(1)))
    if re.search(r'or higher', question, re.IGNORECASE):
        m = re.search(num + r'[°]?[FC] or higher', question, re.IGNORECASE)
        if m: return (float(m.group(1)), 999.0)
    m = re.search(r'between ' + num + r'-' + num + r'[°]?[FC]', question, re.IGNORECASE)
    if m: return (float(m.group(1)), float(m.group(2)))
    m = re.search(r'be ' + num + r'[°]?[FC] on', question, re.IGNORECASE)
    if m:
        v = float(m.group(1))
        return (v, v)
    return None

def hours_to_resolution(end_date_str):
    try:
        end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        return max(0.0, (end - datetime.now(timezone.utc)).total_seconds() / 3600)
    except Exception:
        return 999.0

def in_bucket(forecast, t_low, t_high):
    if t_low == t_high:
        return round(float(forecast)) == round(t_low)
    return t_low <= float(forecast) <= t_high

# =============================================================================
# MARKET DATA STORAGE
# Each market is stored in a separate file: data/markets/{city}_{date}.json
# =============================================================================

def market_path(city_slug, date_str):
    return MARKETS_DIR / f"{city_slug}_{date_str}.json"

def load_market(city_slug, date_str):
    p = market_path(city_slug, date_str)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return None

def save_market(market):
    p = market_path(market["city"], market["date"])
    p.write_text(json.dumps(market, indent=2, ensure_ascii=False), encoding="utf-8")

def load_all_markets():
    markets = []
    for f in MARKETS_DIR.glob("*.json"):
        try:
            markets.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return markets

def new_market(city_slug, date_str, event, hours):
    loc = LOCATIONS[city_slug]
    return {
        "city":               city_slug,
        "city_name":          loc["name"],
        "date":               date_str,
        "unit":               loc["unit"],
        "station":            loc["station"],
        "event_end_date":     event.get("endDate", ""),
        "hours_at_discovery": round(hours, 1),
        "status":             "open",           # open | closed | resolved
        "position":           None,             # filled when position opens
        "actual_temp":        None,             # filled after resolution
        "resolved_outcome":   None,             # win / loss / no_position
        "pnl":                None,
        "forecast_snapshots": [],               # list of forecast snapshots
        "market_snapshots":   [],               # list of market price snapshots
        "all_outcomes":       [],               # all market buckets
        "created_at":         datetime.now(timezone.utc).isoformat(),
    }

# =============================================================================
# STATE (balance and open positions)
# =============================================================================

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {
        "balance":          BALANCE,
        "starting_balance": BALANCE,
        "total_trades":     0,
        "wins":             0,
        "losses":           0,
        "peak_balance":     BALANCE,
    }

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

# =============================================================================
# CORE LOGIC
# =============================================================================

def take_forecast_snapshot(city_slug, dates):
    """Fetches forecasts from all sources and returns a snapshot.

    Sources queried (all per city+date):
      ecmwf    — ECMWF IFS 0.25° via Open-Meteo (bias-corrected, global)
      hrrr     — GFS/HRRR seamless via Open-Meteo (US only, D+0..D+2)
      metar    — live METAR observation (D+0 only)
      ensemble — 4-6 model ensemble via Open-Meteo, region-aware
                 (adds UKMO/Arpège in EU, JMA in Asia)
      yr       — Yr.no / Met.no (Norwegian Met Institute) — free, very accurate
      owm      — OpenWeatherMap 5-day (optional, if OPENWEATHER_API_KEY set)
      qweather — QWeather/HeFeng (optional, strong for Asia; QWEATHER_API_KEY)
    """
    now_str  = datetime.now(timezone.utc).isoformat()
    ecmwf    = get_ecmwf(city_slug, dates)
    hrrr     = get_hrrr(city_slug, dates)
    ensemble = get_ensemble_forecast(city_slug, dates)
    yr       = get_yr_forecast(city_slug, dates)
    owm      = get_openweathermap_forecast(city_slug, dates)
    qweather = get_qweather_forecast(city_slug, dates)
    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    snapshots = {}
    for date in dates:
        snap = {
            "ts":    now_str,
            "ecmwf": ecmwf.get(date),
            "hrrr":  hrrr.get(date) if date <= (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d") else None,
            "metar": get_metar(city_slug) if date == today else None,
            "ensemble": ensemble.get(date),   # multi-model consensus
            "yr":       yr.get(date),         # Yr.no / Met.no (free)
            "owm":      owm.get(date),        # OpenWeatherMap (optional)
            "qweather": qweather.get(date),   # QWeather/HeFeng (optional)
        }
        # Best forecast: HRRR for US D+0/D+1, otherwise ECMWF
        loc = LOCATIONS[city_slug]
        if loc["region"] == "us" and snap["hrrr"] is not None:
            snap["best"] = snap["hrrr"]
            snap["best_source"] = "hrrr"
        elif snap["ecmwf"] is not None:
            snap["best"] = snap["ecmwf"]
            snap["best_source"] = "ecmwf"
        else:
            snap["best"] = None
            snap["best_source"] = None
        snapshots[date] = snap
    return snapshots

def scan_and_update():
    """Main function of one cycle: updates forecasts, opens/closes positions."""
    global _cal
    now      = datetime.now(timezone.utc)
    state    = load_state()
    balance  = state["balance"]
    new_pos  = 0
    closed   = 0
    resolved = 0

    for city_slug, loc in LOCATIONS.items():
        unit = loc["unit"]
        unit_sym = "F" if unit == "F" else "C"
        print(f"  -> {loc['name']}...", end=" ", flush=True)

        try:
            dates = [(now + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(4)]
            snapshots = take_forecast_snapshot(city_slug, dates)
            time.sleep(0.3)
        except Exception as e:
            print(f"skipped ({e})")
            continue

        for i, date in enumerate(dates):
            dt    = datetime.strptime(date, "%Y-%m-%d")
            event = get_polymarket_event(city_slug, MONTHS[dt.month - 1], dt.day, dt.year)
            if not event:
                continue

            end_date = event.get("endDate", "")
            hours    = hours_to_resolution(end_date) if end_date else 0
            horizon  = f"D+{i}"

            # Load or create market record
            mkt = load_market(city_slug, date)
            if mkt is None:
                if hours < MIN_HOURS or hours > MAX_HOURS:
                    continue
                mkt = new_market(city_slug, date, event, hours)

            # Skip if market already resolved
            if mkt["status"] == "resolved":
                continue

            # Update outcomes list — prices taken directly from event
            outcomes = []
            for market in event.get("markets", []):
                question = market.get("question", "")
                mid      = str(market.get("id", ""))
                volume   = float(market.get("volume", 0))
                rng      = parse_temp_range(question)
                if not rng:
                    continue
                try:
                    prices = json.loads(market.get("outcomePrices", "[0.5,0.5]"))
                    bid = float(prices[0])
                    ask = float(prices[1]) if len(prices) > 1 else bid
                except Exception:
                    continue
                outcomes.append({
                    "question":  question,
                    "market_id": mid,
                    "range":     rng,
                    "bid":       round(bid, 4),
                    "ask":       round(ask, 4),
                    "price":     round(bid, 4),   # for compatibility
                    "spread":    round(ask - bid, 4),
                    "volume":    round(volume, 0),
                })

            outcomes.sort(key=lambda x: x["range"][0])
            mkt["all_outcomes"] = outcomes

            # Forecast snapshot (includes multi-model ensemble + climatología)
            snap = snapshots.get(date, {})
            ens  = snap.get("ensemble") or {}

            # ── Climatología + climate bias ──────────────────────────────────
            # Normal histórica (30 años) + anomaly z-score del forecast vs normal
            clim_norm = None
            if _CLIMATOLOGY:
                try:
                    clim_norm = get_climatology(
                        city_slug, date,
                        {"lat": loc["lat"], "lon": loc["lon"],
                         "unit": loc["unit"], "tz": TIMEZONES.get(city_slug, "UTC")}
                    )
                except Exception as e:
                    print(f"  [CLIM] {city_slug}: {e}")
                    clim_norm = None

            # Forecast "ajustado" con climate bias (ENSO / AO)
            fc_raw     = snap.get("best")
            clim_bias  = get_climate_bias(city_slug, date) if _CLIMATE_INDICES else 0.0
            fc_adjusted = (fc_raw + clim_bias) if fc_raw is not None else None
            anom_z     = anomaly_z(fc_adjusted, clim_norm) if (fc_adjusted and clim_norm) else None
            anom_cls   = anomaly_class(anom_z)
            forecast_snap = {
                "ts":              snap.get("ts"),
                "horizon":         horizon,
                "hours_left":      round(hours, 1),
                "ecmwf":           snap.get("ecmwf"),
                "hrrr":            snap.get("hrrr"),
                "metar":           snap.get("metar"),
                "yr":              snap.get("yr"),        # Yr.no / Met.no
                "owm":             snap.get("owm"),        # OpenWeatherMap (optional)
                "qweather":        snap.get("qweather"),  # QWeather (optional)
                "best":            fc_raw,
                "best_adjusted":   fc_adjusted,           # con climate bias
                "climate_bias":    clim_bias,
                "best_source":     snap.get("best_source"),
                # Ensemble consensus fields
                "ensemble_mean":   ens.get("mean"),
                "ensemble_spread": ens.get("spread"),
                "ensemble_agree":  ens.get("agreement"),
                "ensemble_hc":     ens.get("high_conf"),
                "ensemble_models": ens.get("n_models", 0),
                # Climatología (30-year normal)
                "clim_mean":       clim_norm.get("mean") if clim_norm else None,
                "clim_std":        clim_norm.get("std")  if clim_norm else None,
                "anomaly_z":       anom_z,
                "anomaly_class":   anom_cls,
            }
            mkt["forecast_snapshots"].append(forecast_snap)

            # Market price snapshot
            top = max(outcomes, key=lambda x: x["price"]) if outcomes else None
            market_snap = {
                "ts":       snap.get("ts"),
                "top_bucket": f"{top['range'][0]}-{top['range'][1]}{unit_sym}" if top else None,
                "top_price":  top["price"] if top else None,
            }
            mkt["market_snapshots"].append(market_snap)

            # Usar forecast AJUSTADO (con climate bias) para decisiones
            forecast_temp = fc_adjusted if fc_adjusted is not None else snap.get("best")
            best_source   = snap.get("best_source")

            # --- PRE-RESOLUTION SELL (vender antes de que el mercado liquide a 0) ---
            # Si quedan <2h y nuestra posición pierde → salir al bid para recuperar algo
            # en lugar de esperar la liquidación que la dejaría en cero.
            if mkt.get("position") and mkt["position"].get("status") == "open" and hours < 2.0:
                pos = mkt["position"]
                cur_bid = None
                for o in outcomes:
                    if o["market_id"] == pos["market_id"]:
                        cur_bid = o.get("bid", o["price"])
                        break
                entry = pos["entry_price"]
                # Si el precio ha caído por debajo del entry Y aún hay bid>0.05 → vender ya
                if cur_bid is not None and 0.05 <= cur_bid < entry * 0.95:
                    pnl = round((cur_bid - entry) * pos["shares"], 2)
                    balance += pos["cost"] + pnl
                    pos["closed_at"]    = snap.get("ts")
                    pos["close_reason"] = "pre_resolution_close"
                    pos["exit_price"]   = cur_bid
                    pos["pnl"]          = pnl
                    pos["status"]       = "closed"
                    closed += 1
                    print(f"  [PRE-RES] {loc['name']} {date} | entry ${entry:.3f} → ${cur_bid:.3f} ({hours:.1f}h left) | PnL: {pnl:+.2f}")
                    tg.notify_close(loc["name"], date, "pre_resolution_close", entry, cur_bid, pnl)

            # --- TAKE-PROFIT (primero que stop-loss: si el precio subió mucho, salir ya) ---
            # Triggers:
            #   (a) Bid ≥ entry × 2.0  → ganancia ≥ 100%, cobrar ya
            #   (b) Bid ≥ 0.80         → muy cerca de resolución WIN, asegurar ganancia
            #   (c) Bid ≥ entry × 1.5 Y horas_restantes ≤ 6 → cerca del fin con buena ganancia
            if mkt.get("position") and mkt["position"].get("status") == "open":
                pos = mkt["position"]
                cur_bid = None
                for o in outcomes:
                    if o["market_id"] == pos["market_id"]:
                        cur_bid = o.get("bid", o["price"])
                        break
                entry = pos["entry_price"]

                if cur_bid is not None and cur_bid > 0:
                    tp_trigger = None
                    if cur_bid >= entry * 2.0:
                        tp_trigger = "take_profit_2x"
                    elif cur_bid >= 0.80:
                        tp_trigger = "take_profit_near_win"
                    elif cur_bid >= entry * 1.5 and hours <= 6:
                        tp_trigger = "take_profit_end"

                    if tp_trigger:
                        pnl = round((cur_bid - entry) * pos["shares"], 2)
                        balance += pos["cost"] + pnl
                        pos["closed_at"]    = snap.get("ts")
                        pos["close_reason"] = tp_trigger
                        pos["exit_price"]   = cur_bid
                        pos["pnl"]          = pnl
                        pos["status"]       = "closed"
                        closed += 1
                        mult = cur_bid / entry if entry > 0 else 0
                        print(f"  [TAKE-PROFIT] {loc['name']} {date} | entry ${entry:.3f} → ${cur_bid:.3f} ({mult:.1f}x) | PnL: +{pnl:.2f} [{tp_trigger}]")
                        tg.notify_close(loc["name"], date, tp_trigger, entry, cur_bid, pnl)

            # --- STOP-LOSS CONFIRMADO POR ENSEMBLE ──────────────────────
            # Análisis forense: stop_loss perdió -$480 en 88 cierres. Muchos
            # eran "whipsaws" — el precio bajó 20% pero nuestra previsión
            # seguía correcta (ruido de mercado, no cambio real).
            # Ahora el stop_loss requiere que:
            #   (a) El precio haya caído por debajo del umbral Y
            #   (b) El ensemble mean ESTÉ FUERA de nuestro bucket
            # Si ensemble sigue confirmando el bucket, aguantamos.
            # TRAILING-STOP se mantiene igual (si ya subimos, el trailing protege).
            if mkt.get("position") and mkt["position"].get("status") == "open":
                pos = mkt["position"]
                current_price = None
                for o in outcomes:
                    if o["market_id"] == pos["market_id"]:
                        current_price = o["price"]
                        break

                if current_price is not None:
                    current_price = o.get("bid", current_price)  # sell at bid
                    entry = pos["entry_price"]
                    stop  = pos.get("stop_price", entry * 0.80)  # 20% stop por defecto
                    trailing_active = pos.get("trailing_activated", False)

                    # Trailing: si sube 20%+ → stop a breakeven; si sube 50%+ → stop a entry*1.2
                    if current_price >= entry * 1.50 and stop < entry * 1.20:
                        pos["stop_price"] = entry * 1.20
                        pos["trailing_activated"] = True
                        trailing_active = True
                    elif current_price >= entry * 1.20 and stop < entry:
                        pos["stop_price"] = entry
                        pos["trailing_activated"] = True
                        trailing_active = True

                    # Check stop
                    if current_price <= stop:
                        # Si es trailing (ya ganamos antes), SIEMPRE vender (protege beneficios)
                        # Si es stop_loss puro (nunca ganamos), requerir confirmación ensemble
                        should_close = True
                        if not trailing_active:
                            # stop_loss puro → exigir que ensemble coincida con el precio
                            ens_data = snap.get("ensemble") or {}
                            ens_mean = ens_data.get("mean")
                            old_low  = pos["bucket_low"]
                            old_high = pos["bucket_high"]
                            if ens_mean is not None and in_bucket(ens_mean, old_low, old_high):
                                # El ensemble sigue dándonos la razón → aguantar pese al drop
                                should_close = False
                                print(f"  [STOP-HOLD] {loc['name']} {date} | precio ${current_price:.3f} bajó a stop, pero ensemble μ={ens_mean} sigue en bucket. Aguantando.")

                        if should_close:
                            pnl = round((current_price - entry) * pos["shares"], 2)
                            balance += pos["cost"] + pnl
                            pos["closed_at"]    = snap.get("ts")
                            pos["close_reason"] = "stop_loss" if current_price < entry else "trailing_stop"
                            pos["exit_price"]   = current_price
                            pos["pnl"]          = pnl
                            pos["status"]       = "closed"
                            closed += 1
                            reason = "STOP" if current_price < entry else "TRAILING BE"
                            print(f"  [{reason}] {loc['name']} {date} | entry ${entry:.3f} exit ${current_price:.3f} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

            # --- CLOSE POSITION si forecast cambió de forma CONFIRMADA ─────────
            # Análisis forense del histórico: forecast_changed nos costó -$758 en
            # 111 cierres (-$6.83/trade). Estaba sobre-reaccionando a ruido de una
            # sola fuente. Ahora requerimos DOBLE confirmación:
            #   1. Forecast principal (ECMWF/HRRR) fuera del bucket
            #   2. Ensemble mean TAMBIÉN fuera del bucket
            #   3. Buffer aumentado a 3°F / 1.5°C (antes 2°F / 1°C)
            #   4. Además, si tenemos tiempo de sobra (hours > 12), aguantamos —
            #      los forecasts se auto-corrigen en 1-2 ciclos con frecuencia.
            if mkt.get("position") and forecast_temp is not None:
                pos = mkt["position"]
                old_bucket_low  = pos["bucket_low"]
                old_bucket_high = pos["bucket_high"]
                unit = loc["unit"]
                buffer = 3.0 if unit == "F" else 1.5      # antes 2.0 / 1.0
                mid_bucket = (old_bucket_low + old_bucket_high) / 2 if old_bucket_low != -999 and old_bucket_high != 999 else forecast_temp
                forecast_far = abs(forecast_temp - mid_bucket) > (abs(mid_bucket - old_bucket_low) + buffer)
                primary_out = not in_bucket(forecast_temp, old_bucket_low, old_bucket_high)

                # Confirmación con ensemble (si disponible)
                ens_data = snap.get("ensemble") or {}
                ens_mean = ens_data.get("mean")
                ens_out = (ens_mean is not None
                           and not in_bucket(ens_mean, old_bucket_low, old_bucket_high))
                # Si el ensemble NO confirma la salida → es ruido de ECMWF, aguantar
                confirmed = primary_out and forecast_far and (ens_out or ens_mean is None)

                # Salvaguarda temporal: si quedan >12h, dar tiempo a auto-corrección
                # (a menos que el cambio sea MUY grande > buffer*2)
                very_far = abs(forecast_temp - mid_bucket) > (abs(mid_bucket - old_bucket_low) + buffer * 2)
                if hours > 12 and not very_far:
                    confirmed = False

                if confirmed:
                    current_price = None
                    for o in outcomes:
                        if o["market_id"] == pos["market_id"]:
                            current_price = o["price"]
                            break
                    if current_price is not None:
                        pnl = round((current_price - pos["entry_price"]) * pos["shares"], 2)
                        balance += pos["cost"] + pnl
                        mkt["position"]["closed_at"]    = snap.get("ts")
                        mkt["position"]["close_reason"] = "forecast_changed"
                        mkt["position"]["exit_price"]   = current_price
                        mkt["position"]["pnl"]          = pnl
                        mkt["position"]["status"]       = "closed"
                        closed += 1
                        print(f"  [CLOSE] {loc['name']} {date} — forecast changed (confirmed) | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

            # --- OPEN POSITION ---
            if not mkt.get("position") and forecast_temp is not None and hours >= MIN_HOURS:
                # Use adaptive best source if available, else fall back to best_source from forecasts
                _adp      = load_adaptive()
                _adp_src  = _adp.get("best_source", {})
                _ev_mult  = _adp.get("ev_mult", {}).get(city_slug, 1.0)
                _size_mult = _adp.get("size_mult", {}).get(city_slug, 1.0)
                _blacklist = set(_adp.get("blacklist", []))
                effective_source = _adp_src.get(city_slug) or best_source or "ecmwf"
                sigma = get_sigma(city_slug, effective_source)
                best_signal = None

                # ── BLACKLIST: ciudades con PnL crítico acumulado quedan pausadas.
                # Re-activadas automáticamente cuando learning detecte recuperación.
                if city_slug in _blacklist:
                    # No abrir nuevas posiciones en ciudades blacklisted
                    save_market(mkt)
                    time.sleep(0.1)
                    continue

                # ── CORRELATION LIMIT: máximo 2 posiciones abiertas para la misma ciudad.
                # Evita cargar 5 apuestas correlacionadas en NYC (lunes, martes, miércoles…)
                # que son esencialmente la misma "bet": si acertamos un buen día, acertamos todos.
                open_city_count = sum(
                    1 for m in load_all_markets()
                    if m.get("city") == city_slug
                    and m.get("position")
                    and m["position"].get("status") == "open"
                )
                MAX_OPEN_PER_CITY = 2
                if open_city_count >= MAX_OPEN_PER_CITY:
                    save_market(mkt)
                    time.sleep(0.1)
                    continue

                # Find exactly ONE bucket that matches the forecast
                # If forecast doesn't fit any bucket cleanly — skip this market
                matched_bucket = None
                for o in outcomes:
                    t_low, t_high = o["range"]
                    if in_bucket(forecast_temp, t_low, t_high):
                        matched_bucket = o
                        break

                if matched_bucket:
                    o = matched_bucket
                    t_low, t_high = o["range"]
                    volume = o["volume"]
                    bid    = o.get("bid", o["price"])
                    ask    = o.get("ask", o["price"])
                    spread = o.get("spread", 0)

                    # Load adaptive params (updated each cycle by run_learning)
                    # Per-city ev_multiplier → sube el umbral en ciudades volátiles
                    eff_min_ev = (_adp.get("min_ev") or MIN_EV) * _ev_mult
                    kelly_scale = _adp.get("kelly_scale", {}).get(city_slug, 1.0)

                    # ── LIQUIDITY FILTER ──────────────────────────────────
                    # Rechazar mercados con spread >5c o bid ≤ 0.02 (ilíquidos)
                    if spread > 0.05 or bid <= 0.02:
                        continue

                    # ── MID-PRICE TRAP FILTER (suavizado) ─────────────────
                    # Aprendizaje: el filtro original era demasiado severo
                    # combinado con el resto, y nos dejó 63 scans sin BUYs
                    # tras la calibración. Lo suavizamos: rango muerto reducido
                    # a $0.22-$0.28, EV requerido baja a 0.18 (vs 0.25).
                    if 0.22 <= ask <= 0.28:
                        p_provisional = bucket_prob(forecast_temp, t_low, t_high, sigma)
                        ev_provisional = calc_ev(p_provisional, ask)
                        if ev_provisional < 0.18:
                            continue   # zona muerta sin ventaja suficiente

                    # All filters — if any fails, skip this market entirely
                    if volume >= MIN_VOLUME:
                        p  = bucket_prob(forecast_temp, t_low, t_high, sigma)
                        ev = calc_ev(p, ask)
                        if ev >= eff_min_ev:
                            kelly = round(calc_kelly(p, ask) * kelly_scale, 4)
                            size  = bet_size(kelly, balance)
                            # Aplicar size_multiplier por ciudad (volátiles → más pequeño)
                            size  = round(size * _size_mult, 2)
                            if size >= 0.50:
                                # ── HIGH CONFIDENCE check ─────────────────────
                                # Ensemble agrees AND market price is lagging
                                ens_data      = snap.get("ensemble") or {}
                                ens_mean      = ens_data.get("mean")
                                ens_agree     = ens_data.get("agreement", 0.0)
                                ens_spread    = ens_data.get("spread")
                                ens_hc_base   = ens_data.get("high_conf", False)
                                market_lag    = round(p - ask, 3)

                                # HC requires: ensemble agrees + ensemble mean in same bucket
                                # + our probability high enough + market price not caught up
                                ens_in_bucket = (
                                    ens_mean is not None
                                    and in_bucket(ens_mean, t_low, t_high)
                                )
                                # Cross-check con todas las fuentes disponibles.
                                # Cada fuente que devuelva datos DEBE estar dentro del bucket
                                # para que HC se active — si ninguna devuelve, no bloqueamos.
                                extra_sources = {
                                    "owm":      snap.get("owm"),
                                    "yr":       snap.get("yr"),
                                    "qweather": snap.get("qweather"),
                                }
                                # cross_ok = TODAS las fuentes con datos coinciden con el bucket
                                cross_ok = True
                                n_confirmations = 0
                                for name, val in extra_sources.items():
                                    if val is not None:
                                        if in_bucket(val, t_low, t_high):
                                            n_confirmations += 1
                                        else:
                                            cross_ok = False
                                            break
                                # Anomaly boost: si el forecast es 1.5σ+ de la normal,
                                # el mercado tarda más en pricear → ventaja extra
                                anomaly_boost = abs(anom_z) >= 1.5 if anom_z is not None else False
                                high_conf = (
                                    ens_hc_base
                                    and ens_in_bucket
                                    and cross_ok
                                    and p >= HIGH_CONF_MIN_PROB
                                    and market_lag >= HIGH_CONF_MIN_MARKET_LAG
                                )
                                # Override: día climáticamente extremo + 4 modelos + bucket match
                                # → HC aunque agreement score no llegue al umbral normal
                                if anomaly_boost and ens_in_bucket and cross_ok and p >= 0.60 \
                                        and market_lag >= 0.12 and len(extra_sources) > 0:
                                    high_conf = True

                                # HIGH CONF → use larger budget, else normal
                                hc_size = (
                                    round(min(calc_kelly(p, ask) * kelly_scale * BALANCE,
                                              HIGH_CONF_MAX_BET), 2)
                                    if high_conf else size
                                )
                                hc_size = max(hc_size, size)  # never less than normal

                                best_signal = {
                                    "market_id":       o["market_id"],
                                    "question":        o["question"],
                                    "bucket_low":      t_low,
                                    "bucket_high":     t_high,
                                    "entry_price":     ask,
                                    "bid_at_entry":    bid,
                                    "spread":          spread,
                                    "shares":          round(hc_size / ask, 2),
                                    "cost":            hc_size,
                                    "p":               round(p, 4),
                                    "ev":              round(ev, 4),
                                    "kelly":           round(kelly, 4),
                                    "forecast_temp":   forecast_temp,
                                    "forecast_src":    best_source,
                                    "sigma":           sigma,
                                    "opened_at":       snap.get("ts"),
                                    "status":          "open",
                                    "pnl":             None,
                                    "exit_price":      None,
                                    "close_reason":    None,
                                    "closed_at":       None,
                                    # Ensemble / high-confidence metadata
                                    "high_conf":       high_conf,
                                    "market_lag":      market_lag,
                                    "ensemble_mean":   ens_mean,
                                    "ensemble_spread": ens_spread,
                                    "ensemble_agree":  ens_agree,
                                    "ensemble_models": ens_data.get("n_models", 0),
                                    # Climatología + climate indices
                                    "anomaly_z":       anom_z,
                                    "anomaly_class":   anom_cls,
                                    "climate_bias":    clim_bias,
                                    "clim_mean":       clim_norm.get("mean") if clim_norm else None,
                                }

                if best_signal:
                    # Fetch real bestAsk from Polymarket API for accurate entry price
                    skip_position = False

                    try:
                        r = requests.get(f"https://gamma-api.polymarket.com/markets/{best_signal['market_id']}", timeout=(3, 5))
                        mdata = r.json()
                        real_ask = float(mdata.get("bestAsk", best_signal["entry_price"]))
                        real_bid = float(mdata.get("bestBid", best_signal["bid_at_entry"]))
                        real_spread = round(real_ask - real_bid, 4)
                        # Re-check slippage and price with real values
                        if real_spread > MAX_SLIPPAGE or real_ask >= MAX_PRICE:
                            print(f"  [SKIP] {loc['name']} {date} — real ask ${real_ask:.3f} spread ${real_spread:.3f}")
                            skip_position = True
                        else:
                            best_signal["entry_price"]  = real_ask
                            best_signal["bid_at_entry"] = real_bid
                            best_signal["spread"]       = real_spread
                            best_signal["shares"]       = round(best_signal["cost"] / real_ask, 2)
                            best_signal["ev"]           = round(calc_ev(best_signal["p"], real_ask), 4)
                    except Exception as e:
                        print(f"  [WARN] Could not fetch real ask for {best_signal['market_id']}: {e}")

                    if not skip_position and best_signal["entry_price"] < MAX_PRICE:
                        balance -= best_signal["cost"]
                        mkt["position"] = best_signal
                        state["total_trades"] += 1
                        new_pos += 1
                        bucket_label = f"{best_signal['bucket_low']}-{best_signal['bucket_high']}{unit_sym}"
                        hc_tag = " ⚡HC" if best_signal.get("high_conf") else ""
                        print(f"  [BUY{hc_tag}]  {loc['name']} {horizon} {date} | {bucket_label} | "
                              f"${best_signal['entry_price']:.3f} | EV {best_signal['ev']:+.2f} | "
                              f"${best_signal['cost']:.2f} ({best_signal['forecast_src'].upper()})"
                              + (f" | agree={best_signal.get('ensemble_agree', 0):.2f} lag={best_signal.get('market_lag', 0):+.2f}" if best_signal.get("high_conf") else ""))
                        tg.notify_buy(
                            loc["name"], date, bucket_label,
                            best_signal["entry_price"], best_signal["ev"],
                            best_signal["cost"], best_signal["forecast_src"], horizon,
                            high_conf=best_signal.get("high_conf", False),
                            ensemble_agree=best_signal.get("ensemble_agree"),
                            market_lag=best_signal.get("market_lag"),
                        )

            # Market closed by time
            if hours < 0.5 and mkt["status"] == "open":
                mkt["status"] = "closed"

            save_market(mkt)
            time.sleep(0.1)

        print("ok")

    # --- AUTO-RESOLUTION ---
    for mkt in load_all_markets():
        if mkt["status"] == "resolved":
            continue

        pos = mkt.get("position")
        if not pos or pos.get("status") != "open":
            continue

        market_id = pos.get("market_id")
        if not market_id:
            continue

        # Check if market closed on Polymarket
        won = check_market_resolved(market_id)
        if won is None:
            continue  # market still open

        # Market closed — record result
        price  = pos["entry_price"]
        size   = pos["cost"]
        shares = pos["shares"]
        pnl    = round(shares * (1 - price), 2) if won else round(-size, 2)

        balance += size + pnl
        pos["exit_price"]   = 1.0 if won else 0.0
        pos["pnl"]          = pnl
        pos["close_reason"] = "resolved"
        pos["closed_at"]    = now.isoformat()
        pos["status"]       = "closed"
        mkt["pnl"]          = pnl
        mkt["status"]       = "resolved"
        mkt["resolved_outcome"] = "win" if won else "loss"

        if won:
            state["wins"] += 1
        else:
            state["losses"] += 1

        result = "WIN" if won else "LOSS"
        print(f"  [{result}] {mkt['city_name']} {mkt['date']} | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
        tg.notify_resolve(mkt["city_name"], mkt["date"], "win" if won else "loss", pnl, balance)
        resolved += 1

        save_market(mkt)
        time.sleep(0.3)

    state["balance"]      = round(balance, 2)
    state["peak_balance"] = max(state.get("peak_balance", balance), balance)
    save_state(state)

    # Refresh Polymarket ground-truth PnL antes de learning (cada scan)
    try:
        from refresh_real_pnl import refresh as _refresh_real
        _refresh_real(verbose=False)
    except Exception as e:
        print(f"  [REAL_PNL] refresh failed: {e}")

    # Run learning every cycle (usa real_pnl.json como ground truth para
    # ev_mult, size_mult, blacklist). La calibración de sigma necesita más datos.
    all_mkts = load_all_markets()
    if _LEARNING:
        run_learning(all_mkts)

    resolved_count = len([m for m in all_mkts if m["status"] == "resolved"])
    if resolved_count >= CALIBRATION_MIN:
        global _cal
        _cal = run_calibration(all_mkts)

    return new_pos, closed, resolved

# =============================================================================
# REPORT
# =============================================================================

def print_status():
    state    = load_state()
    markets  = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]
    resolved = [m for m in markets if m["status"] == "resolved" and m.get("pnl") is not None]

    bal     = state["balance"]
    start   = state["starting_balance"]
    ret_pct = (bal - start) / start * 100
    wins    = state["wins"]
    losses  = state["losses"]
    total   = wins + losses

    print(f"\n{'='*55}")
    print(f"  WEATHERBET — STATUS")
    print(f"{'='*55}")
    print(f"  Balance:     ${bal:,.2f}  (start ${start:,.2f}, {'+'if ret_pct>=0 else ''}{ret_pct:.1f}%)")
    print(f"  Trades:      {total} | W: {wins} | L: {losses} | WR: {wins/total:.0%}" if total else "  No trades yet")
    print(f"  Open:        {len(open_pos)}")
    print(f"  Resolved:    {len(resolved)}")

    if open_pos:
        print(f"\n  Open positions:")
        total_unrealized = 0.0
        for m in open_pos:
            pos      = m["position"]
            unit_sym = "F" if m["unit"] == "F" else "C"
            label    = f"{pos['bucket_low']}-{pos['bucket_high']}{unit_sym}"

            # Current price from latest market snapshot
            current_price = pos["entry_price"]
            snaps = m.get("market_snapshots", [])
            if snaps:
                # Find our bucket price in all_outcomes
                for o in m.get("all_outcomes", []):
                    if o["market_id"] == pos["market_id"]:
                        current_price = o["price"]
                        break

            unrealized = round((current_price - pos["entry_price"]) * pos["shares"], 2)
            total_unrealized += unrealized
            pnl_str = f"{'+'if unrealized>=0 else ''}{unrealized:.2f}"

            print(f"    {m['city_name']:<16} {m['date']} | {label:<14} | "
                  f"entry ${pos['entry_price']:.3f} -> ${current_price:.3f} | "
                  f"PnL: {pnl_str} | {pos['forecast_src'].upper()}")

        sign = "+" if total_unrealized >= 0 else ""
        print(f"\n  Unrealized PnL: {sign}{total_unrealized:.2f}")

    print(f"{'='*55}\n")

def print_report():
    markets  = load_all_markets()
    resolved = [m for m in markets if m["status"] == "resolved" and m.get("pnl") is not None]

    print(f"\n{'='*55}")
    print(f"  WEATHERBET — FULL REPORT")
    print(f"{'='*55}")

    if not resolved:
        print("  No resolved markets yet.")
        return

    total_pnl = sum(m["pnl"] for m in resolved)
    wins      = [m for m in resolved if m["resolved_outcome"] == "win"]
    losses    = [m for m in resolved if m["resolved_outcome"] == "loss"]

    print(f"\n  Total resolved: {len(resolved)}")
    print(f"  Wins:           {len(wins)} | Losses: {len(losses)}")
    print(f"  Win rate:       {len(wins)/len(resolved):.0%}")
    print(f"  Total PnL:      {'+'if total_pnl>=0 else ''}{total_pnl:.2f}")

    print(f"\n  By city:")
    for city in sorted(set(m["city"] for m in resolved)):
        group = [m for m in resolved if m["city"] == city]
        w     = len([m for m in group if m["resolved_outcome"] == "win"])
        pnl   = sum(m["pnl"] for m in group)
        name  = LOCATIONS[city]["name"]
        print(f"    {name:<16} {w}/{len(group)} ({w/len(group):.0%})  PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")

    print(f"\n  Market details:")
    for m in sorted(resolved, key=lambda x: x["date"]):
        pos      = m.get("position", {})
        unit_sym = "F" if m["unit"] == "F" else "C"
        snaps    = m.get("forecast_snapshots", [])
        first_fc = snaps[0]["best"] if snaps else None
        last_fc  = snaps[-1]["best"] if snaps else None
        label    = f"{pos.get('bucket_low')}-{pos.get('bucket_high')}{unit_sym}" if pos else "no position"
        result   = m["resolved_outcome"].upper()
        pnl_str  = f"{'+'if m['pnl']>=0 else ''}{m['pnl']:.2f}" if m["pnl"] is not None else "-"
        fc_str   = f"forecast {first_fc}->{last_fc}{unit_sym}" if first_fc else "no forecast"
        actual   = f"actual {m['actual_temp']}{unit_sym}" if m["actual_temp"] else ""
        print(f"    {m['city_name']:<16} {m['date']} | {label:<14} | {fc_str} | {actual} | {result} {pnl_str}")

    print(f"{'='*55}\n")

# =============================================================================
# MAIN LOOP
# =============================================================================

MONITOR_INTERVAL = 600  # monitor positions every 10 minutes

def monitor_positions():
    """Quick stop check on open positions without full scan."""
    markets  = load_all_markets()
    open_pos = [m for m in markets if m.get("position") and m["position"].get("status") == "open"]
    if not open_pos:
        return 0

    state   = load_state()
    balance = state["balance"]
    closed  = 0

    for mkt in open_pos:
        pos = mkt["position"]
        mid = pos["market_id"]

        # Fetch real bestBid from Polymarket API — actual sell price
        current_price = None
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/markets/{mid}", timeout=(3, 5))
            mdata = r.json()
            best_bid = mdata.get("bestBid")
            if best_bid is not None:
                current_price = float(best_bid)
        except Exception:
            pass

        # Fallback to cached price if API failed
        if current_price is None:
            for o in mkt.get("all_outcomes", []):
                if o["market_id"] == mid:
                    current_price = o.get("bid", o["price"])
                    break

        if current_price is None:
            continue

        entry = pos["entry_price"]
        stop  = pos.get("stop_price", entry * 0.80)
        city_name = LOCATIONS.get(mkt["city"], {}).get("name", mkt["city"])

        # Hours left to resolution
        end_date = mkt.get("event_end_date", "")
        hours_left = hours_to_resolution(end_date) if end_date else 999.0

        # Take-profit threshold based on hours to resolution
        if hours_left < 24:
            take_profit = None        # hold to resolution
        elif hours_left < 48:
            take_profit = 0.85        # 24-48h: take profit at $0.85
        else:
            take_profit = 0.75        # 48h+: take profit at $0.75

        # Trailing: if up 20%+ — move stop to breakeven
        if current_price >= entry * 1.20 and stop < entry:
            pos["stop_price"] = entry
            pos["trailing_activated"] = True
            print(f"  [TRAILING] {city_name} {mkt['date']} — stop moved to breakeven ${entry:.3f}")

        # Check take-profit
        take_triggered = take_profit is not None and current_price >= take_profit
        # Check stop
        stop_triggered = current_price <= stop

        if take_triggered or stop_triggered:
            pnl = round((current_price - entry) * pos["shares"], 2)
            balance += pos["cost"] + pnl
            pos["closed_at"]    = datetime.now(timezone.utc).isoformat()
            if take_triggered:
                pos["close_reason"] = "take_profit"
                reason = "TAKE"
            elif current_price < entry:
                pos["close_reason"] = "stop_loss"
                reason = "STOP"
            else:
                pos["close_reason"] = "trailing_stop"
                reason = "TRAILING BE"
            pos["exit_price"]   = current_price
            pos["pnl"]          = pnl
            pos["status"]       = "closed"
            closed += 1
            print(f"  [{reason}] {city_name} {mkt['date']} | entry ${entry:.3f} exit ${current_price:.3f} | {hours_left:.0f}h left | PnL: {'+'if pnl>=0 else ''}{pnl:.2f}")
            save_market(mkt)

    if closed:
        state["balance"] = round(balance, 2)
        save_state(state)

    return closed


def run_loop():
    global _cal
    _cal = load_cal()

    print(f"\n{'='*55}")
    print(f"  WEATHERBET — STARTING")
    print(f"{'='*55}")
    print(f"  Cities:     {len(LOCATIONS)}")
    print(f"  Balance:    ${BALANCE:,.0f} | Max bet: ${MAX_BET}")
    print(f"  Scan:       {SCAN_INTERVAL//60} min | Monitor: {MONITOR_INTERVAL//60} min")
    print(f"  Sources:    ECMWF + HRRR(US) + METAR(D+0)")
    print(f"  Data:       {DATA_DIR.resolve()}")
    print(f"  Ctrl+C to stop\n")

    tg.notify_start(len(LOCATIONS), BALANCE, SCAN_INTERVAL // 60)

    last_full_scan = 0
    _running = True

    import signal
    def _shutdown(sig, frame):
        nonlocal _running
        _running = False
        st = load_state()
        tg.notify_shutdown("señal SIGINT/SIGTERM", st["balance"], st.get("wins",0), st.get("losses",0))
        print(f"\n  Stopping — saving state...")
        save_state(st)
        print(f"  Done. Bye!")
        sys.exit(0)
    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    consecutive_scan_errors = 0
    consecutive_monitor_errors = 0
    last_error_alert = 0

    while _running:
        now_ts  = time.time()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Full scan once per hour
        if now_ts - last_full_scan >= SCAN_INTERVAL:
            print(f"[{now_str}] full scan...")
            try:
                new_pos, closed, resolved = scan_and_update()
                state = load_state()
                print(f"  balance: ${state['balance']:,.2f} | "
                      f"new: {new_pos} | closed: {closed} | resolved: {resolved}")
                last_full_scan = time.time()
                consecutive_scan_errors = 0   # reset al éxito
            except requests.exceptions.ConnectionError as e:
                consecutive_scan_errors += 1
                print(f"  Connection lost — waiting 60 sec (consecutive errors: {consecutive_scan_errors})")
                # Alerta TG si llevamos 3+ errores de conexión seguidos (rate-limited cada 1h)
                if consecutive_scan_errors >= 3 and time.time() - last_error_alert > 3600:
                    last_error_alert = time.time()
                    try:
                        tg.notify_error(
                            "weatherbot", "Conexión perdida",
                            str(e), critical=False,
                            context={"errores consecutivos": consecutive_scan_errors}
                        )
                    except Exception: pass
                time.sleep(60)
                continue
            except Exception as e:
                consecutive_scan_errors += 1
                import traceback as _tb
                tb_tail = _tb.format_exc()[-300:]
                print(f"  Error: {e} — waiting 60 sec (consecutive errors: {consecutive_scan_errors})")
                # Alerta TG: cualquier excepción en el scan (rate-limited)
                if time.time() - last_error_alert > 1800:
                    last_error_alert = time.time()
                    try:
                        tg.notify_error(
                            "weatherbot", "Excepción en full scan",
                            tb_tail,
                            critical=(consecutive_scan_errors >= 5),
                            context={"errores consecutivos": consecutive_scan_errors}
                        )
                    except Exception: pass
                time.sleep(60)
                continue
        else:
            # Quick stop monitoring
            print(f"[{now_str}] monitoring positions...")
            try:
                stopped = monitor_positions()
                consecutive_monitor_errors = 0
                if stopped:
                    state = load_state()
                    print(f"  balance: ${state['balance']:,.2f}")
            except Exception as e:
                consecutive_monitor_errors += 1
                print(f"  Monitor error: {e}")
                if consecutive_monitor_errors >= 3 and time.time() - last_error_alert > 3600:
                    last_error_alert = time.time()
                    try:
                        tg.notify_error(
                            "weatherbot", "Excepción en monitor",
                            str(e), critical=False,
                            context={"errores consecutivos": consecutive_monitor_errors}
                        )
                    except Exception: pass

        time.sleep(MONITOR_INTERVAL)

# =============================================================================
# CLI
# =============================================================================

def _crash_alert(exc: Exception):
    """Alerta crítica antes de morir cuando una excepción no manejada
    escapa del bucle principal. El supervisor reiniciará después."""
    import traceback
    tb = traceback.format_exc()
    try:
        tg.notify_error(
            "weatherbot", "CAÍDO — Excepción no manejada",
            tb[-400:], critical=True,
            context={"error": str(exc)[:120]}
        )
    except Exception:
        pass


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    try:
        if cmd == "run":
            run_loop()
        elif cmd == "status":
            _cal = load_cal()
            print_status()
        elif cmd == "report":
            _cal = load_cal()
            print_report()
        else:
            print("Usage: python weatherbet.py [run|status|report]")
    except KeyboardInterrupt:
        pass
    except Exception as e:
        if cmd == "run":
            _crash_alert(e)
        raise
