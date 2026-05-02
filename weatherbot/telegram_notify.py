"""
Telegram notifications for weatherbet.py
Drop-in module: import and call notify_* functions.
Falls back silently if TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set.
"""

import os
import requests
from datetime import datetime, timezone

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
NOTION_TOKEN     = os.getenv("NOTION_TOKEN", "")
NOTION_PAGE_ID   = os.getenv("NOTION_PAGE_ID", "")

_ENABLED = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
_NOTION  = bool(NOTION_TOKEN and NOTION_PAGE_ID)


def _ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _send(text: str):
    if not _ENABLED:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception as e:
        try:
            print(f"  [TG] send error: {e}")
        except Exception:
            pass


def _notion_append(blocks: list):
    if not _NOTION:
        return
    try:
        requests.patch(
            f"https://api.notion.com/v1/blocks/{NOTION_PAGE_ID}/children",
            headers={
                "Authorization":  f"Bearer {NOTION_TOKEN}",
                "Notion-Version": "2022-06-28",
                "Content-Type":   "application/json",
            },
            json={"children": blocks},
            timeout=10,
        )
    except Exception as e:
        print(f"  [NOTION] error: {e}")


def _notion_bullet(text: str, url: str = None):
    rt = {"type": "text", "text": {"content": text}}
    if url:
        rt["text"]["link"] = {"url": url}
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [rt]},
    }


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def notify_start(n_cities: int, balance: float, scan_min: int):
    msg = (
        f"🚀 *WeatherBet arrancado* — operando con *dinero REAL*\n"
        f"🌍 {n_cities} ciudades | ⏱ Scan cada {scan_min} min\n"
        f"🕐 _{_ts()}_"
    )
    _send(msg)


def notify_buy(city_name: str, date: str, bucket: str, entry: float,
               ev: float, cost: float, src: str, horizon: str,
               high_conf: bool = False, ensemble_agree: float = None,
               market_lag: float = None):
    url = f"https://polymarket.com/event/highest-temperature-in-{city_name.lower().replace(' ', '-')}-on-{date}"
    hc_badge = "⚡ " if high_conf else ""
    hc_line  = ""
    if high_conf and ensemble_agree is not None:
        hc_line = (
            f"\n⚡ *Alta confianza* — acuerdo {ensemble_agree:.0%}"
            + (f" | ventaja {market_lag:+.0%}" if market_lag is not None else "")
        )
    msg = (
        f"{hc_badge}📈 *BUY — {city_name}*\n"
        f"📅 {date} ({horizon}) | Bucket: `{bucket}`\n"
        f"💵 Entry: ${entry:.3f} | Cost: ${cost:.2f}\n"
        f"📊 EV: {ev:+.2f} | Src: {src.upper()}"
        f"{hc_line}\n"
        f"🔗 [Ver en Polymarket]({url})\n"
        f"🕐 _{_ts()}_"
    )
    _send(msg)
    _notion_append([
        _notion_bullet(
            f"{'⚡ ' if high_conf else ''}📈 BUY {city_name} {date} {bucket} | entry ${entry:.3f} | EV {ev:+.2f} | {src.upper()}",
            url,
        )
    ])


def notify_close(city_name: str, date: str, reason: str,
                 entry: float, exit_price: float, pnl: float):
    emoji = "✅" if pnl >= 0 else "❌"
    pnl_str = f"{'+'if pnl>=0 else ''}{pnl:.2f}"
    msg = (
        f"{emoji} *{reason.upper()} — {city_name}*\n"
        f"📅 {date}\n"
        f"💵 Entry: ${entry:.3f} → Exit: ${exit_price:.3f}\n"
        f"💰 PnL: *{pnl_str}*\n"
        f"🕐 _{_ts()}_"
    )
    _send(msg)
    _notion_append([
        _notion_bullet(
            f"{emoji} {reason.upper()} {city_name} {date} | entry ${entry:.3f} exit ${exit_price:.3f} | PnL {pnl_str}"
        )
    ])


def notify_resolve(city_name: str, date: str, outcome: str, pnl: float,
                   balance: float = None):
    # Nota: 'balance' es track paper para learning. NO se muestra para evitar
    # confusión con el balance USDC real en Polymarket (bridge manda heartbeat
    # cada 3h con el saldo real + valor de posiciones abiertas).
    emoji = "🏆" if outcome == "win" else "💔"
    pnl_str = f"{'+'if pnl>=0 else ''}{pnl:.2f}"
    outcome_text = "PREDICCIÓN CORRECTA" if outcome == "win" else "PREDICCIÓN FALLIDA"
    msg = (
        f"{emoji} *{outcome_text} — {city_name}*\n"
        f"📅 {date}\n"
        f"💵 Resultado posición: *{pnl_str}$*\n"
        f"🕐 _{_ts()}_"
    )
    _send(msg)
    _notion_append([
        _notion_bullet(
            f"{emoji} {outcome.upper()} {city_name} {date} | PnL {pnl_str}$"
        )
    ])


def notify_new_markets(events: list):
    """Send summary of newly discovered Polymarket weather events."""
    if not events:
        return
    lines = [f"🆕 *{len(events)} nuevos mercados detectados*\n"]
    for ev in events[:5]:
        city = ev.get("city_name", "?")
        date = ev.get("date", "?")
        unit = ev.get("unit", "")
        fc   = ev.get("forecast")
        fc_s = f"{fc}°{unit}" if fc is not None else "—"
        lines.append(f"  • {city} {date} — forecast {fc_s}")
    if len(events) > 5:
        lines.append(f"  ...y {len(events)-5} más")
    lines.append(f"\n🕐 _{_ts()}_")
    _send("\n".join(lines))

    blocks = [
        {
            "object": "block",
            "type": "heading_3",
            "heading_3": {
                "rich_text": [{"type": "text", "text": {
                    "content": f"📊 {len(events)} nuevos mercados — {_ts()}"
                }}]
            },
        }
    ] + [
        _notion_bullet(
            f"{ev.get('city_name','?')} {ev.get('date','?')} | forecast {ev.get('forecast','—')}°{ev.get('unit','')}",
            f"https://polymarket.com/event/highest-temperature-in-{ev.get('city_name','').lower().replace(' ','-')}-on-{ev.get('date','')}",
        )
        for ev in events
    ] + [{"object": "block", "type": "divider", "divider": {}}]
    _notion_append(blocks)


def notify_status(balance: float, start: float, wins: int, losses: int,
                  open_pos: int):
    # Track de rendimiento del algoritmo (sin mostrar balance paper para
    # evitar confusión con el saldo real en Polymarket que manda el bridge).
    total   = wins + losses
    wr      = f"{wins/total:.0%}" if total else "—"
    msg = (
        f"📊 *Estado del algoritmo*\n"
        f"📈 Trades históricos: {total} | W: {wins} | L: {losses} | WR: {wr}\n"
        f"🔓 Posiciones abiertas: {open_pos}\n"
        f"💡 _El saldo real de Polymarket se envía en el heartbeat del bridge_\n"
        f"🕐 _{_ts()}_"
    )
    _send(msg)


def notify_error(component: str, error_type: str, detail: str,
                 critical: bool = False, context: dict = None):
    """Alerta genérica de fallo. Llamar desde TODOS los puntos de error.

    Args:
        component: "weatherbot", "bridge", "supervisor", "watchdog", "scan", etc.
        error_type: Texto corto del tipo: "Excepción", "BUY fallido", "SCAN error", etc.
        detail: Mensaje del error (truncado a 250 chars)
        critical: Si True, usa 🚨 + tag CRÍTICO. Si False, usa ⚠️.
        context: Dict opcional con campos extra a mostrar (city, date, etc.)
    """
    icon = "🚨" if critical else "⚠️"
    tag  = " *CRÍTICO*" if critical else ""
    lines = [
        f"{icon} *Error en {component}{tag}*",
        "",
        f"*Tipo:* {error_type}",
    ]
    if context:
        for k, v in context.items():
            lines.append(f"*{k}:* {v}")
    lines.append("")
    lines.append(f"*Detalle:*\n```\n{str(detail)[:250]}\n```")
    lines.append("")
    lines.append(f"_{_ts()}_")
    _send("\n".join(lines))


def notify_shutdown(reason: str, balance: float, wins: int, losses: int):
    total = wins + losses
    wr    = f"{wins/total:.0%}" if total else "—"
    msg = (
        f"🔒 *WeatherBet deteniéndose*\n"
        f"*Razón:* {reason}\n"
        f"*Trades históricos:* {total} | WR {wr}\n"
        f"🕐 _{_ts()}_"
    )
    _send(msg)
    _notion_append([{
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": [{"type": "text", "text": {
                "content": f"🔒 Sesión cerrada: {reason} — {_ts()}"
            }}],
            "icon": {"emoji": "⚠️"},
            "color": "red_background",
        },
    }])
