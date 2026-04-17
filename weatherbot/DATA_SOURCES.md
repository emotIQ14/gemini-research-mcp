# WeatherBet — Fuentes de datos

El bot consulta múltiples proveedores meteorológicos antes de cada decisión.
Todas las fuentes sin coste están activas por defecto; las que requieren API
key se activan automáticamente cuando la key está configurada.

## Fuentes activas (gratuitas, sin API key)

| Fuente | Tipo | Ventana | Cobertura |
|--------|------|---------|-----------|
| **ECMWF IFS 0.25°** (Open-Meteo) | Modelo global, bias-corregido | 7 días | Global |
| **GFS/HRRR Seamless** (Open-Meteo) | NOAA | 3 días | Global (mejor en EE.UU.) |
| **ICON Seamless** (Open-Meteo) | DWD (Alemania) | 7 días | Global |
| **GEM Seamless** (Open-Meteo) | CMC (Canadá) | 7 días | Global |
| **UKMO Seamless** (Open-Meteo) | UK Met Office | 7 días | Solo ciudades `eu` |
| **Arpège World** (Open-Meteo) | Météo-France | 7 días | Solo ciudades `eu` |
| **JMA Seamless** (Open-Meteo) | Japan Met Agency | 7 días | Solo ciudades `asia` |
| **METAR** (aviationweather.gov) | Observación aeropuerto | Tiempo real | 20/20 ciudades |
| **Yr.no / Met.no** | Instituto Noruego | 10 días | Global |

Por ciudad se usan **4-7 fuentes simultáneas**. Europa tiene el mejor cobertura
(7 fuentes: ECMWF + GFS + ICON + GEM + UKMO + Arpège + Yr.no). Asia tiene 6.
EE.UU. tiene 5.

## Fuentes opcionales (requieren API key)

### OpenWeatherMap

Ya tienes el MCP instalado en `Weather-MCP-ClaudeDesktop/`. Para activarlo:

1. Obtén una API key gratis en https://openweathermap.org/api
2. Edita `polymarket-trading-bot/.env` o `weatherbot/.env` y añade:
   ```
   OPENWEATHER_API_KEY=tu_key_aquí
   ```
3. Reinicia el bot: `python start_weatherbet.py --stop && python start_weatherbet.py`

OWM aparecerá como fuente de cross-check en el log y en los snapshots.

### QWeather / HeFeng (fuerte para Asia)

Ya tienes `hefeng_weather/` instalado. Para activarlo:

1. Obtén una API key gratis en https://dev.qweather.com/en/
2. Edita `weatherbot/.env` o `polymarket-trading-bot/.env`:
   ```
   QWEATHER_API_KEY=tu_key_aquí
   QWEATHER_API_BASE=https://devapi.qweather.com   # o https://api.qweather.com (paid)
   ```
3. Reinicia el bot

Especialmente útil para Tokyo, Shanghai, Seoul, Singapore, Lucknow.

## Cómo se combinan

1. **Fase 1 — Forecast principal:** el bot usa ECMWF/HRRR como "best" forecast.
2. **Fase 2 — Ensemble score:** calcula media y desviación estándar de los 4-6
   modelos de Open-Meteo. `agreement ≥ 0.70` y `spread ≤ 1.5°C` = consensus.
3. **Fase 3 — Cross-check:** Yr.no, OWM, QWeather confirman independientemente
   que el bucket predicho está correcto. Si alguna fuente contradice, NO se
   activa HIGH_CONF.
4. **Fase 4 — Pre-execución (bridge):** justo antes de mandar la orden, el
   bridge re-consulta el order book de Polymarket y aborta si:
   - el ask saltó >20% respecto a cuando el bot valoró
   - el spread bid-ask supera 8c
   - el ask cruzó el umbral `MAX_PRICE=0.45`

## Histórico por ciudad

`data/real_pnl.json` se actualiza cada scan con el PnL REAL desde Polymarket
(no paper-trading). `learning.py` usa estos datos como ground truth para:

- **ev_multiplier** por ciudad (0.9-2.5×): ajusta el umbral EV requerido
- **size_multiplier** por ciudad (0.4-1.2×): ajusta el tamaño de la apuesta
- **blacklist**: pausa ciudades con pérdida acumulada > $12 y pérdida promedio
  > $0.8 por trade

## Triggers de salida

| Trigger | Condición | Acción |
|---------|-----------|--------|
| 🎯 `take_profit_2x` | bid ≥ entry × 2.0 | Vender ya (ganancia ≥ 100%) |
| 🎯 `take_profit_near_win` | bid ≥ $0.80 | Vender (casi ganador) |
| 🎯 `take_profit_end` | bid ≥ entry × 1.5 Y ≤6h restantes | Vender antes del cierre |
| `trailing_stop` | precio cae tras activación | Vender a breakeven o +20% |
| `stop_loss` | precio cae 20% del entry | Vender (corte de pérdida) |
| `forecast_changed` | forecast se aleja 2+ grados del bucket | Cancelar preventivo |
| `resolved` | Polymarket liquida el mercado | Auto (USDC aparece en safe) |
