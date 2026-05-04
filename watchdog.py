#!/usr/bin/env python3
"""
watchdog.py — Guardián externo del supervisor WeatherBet
=========================================================
Ejecutado por Windows Task Scheduler cada 5 minutos.
Si el supervisor no está corriendo, lo reinicia automáticamente y
ENVÍA ALERTA A TELEGRAM. No interfiere si ya está activo.
"""
import subprocess
import sys
import os
from pathlib import Path
from datetime import datetime, timezone

# Cargar .env desde polymarket-trading-bot (donde están los tokens TG)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / "polymarket-trading-bot" / ".env")
except Exception:
    pass

import urllib.request
import urllib.parse
import json

TG_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_TOKEN2 = os.getenv("TELEGRAM_BOT_TOKEN_2", "")
TG_CHAT   = os.getenv("TELEGRAM_CHAT_ID", "")

BASE     = Path(__file__).parent
PID_FILE = BASE / "logs" / "supervisor.pid"
LOG_FILE = BASE / "logs" / "watchdog.log"


def tg(text: str):
    """Manda mensaje a Telegram con failover entre tokens. No bloquea si falla."""
    if not TG_CHAT:
        return
    body = json.dumps({"chat_id": TG_CHAT, "text": text, "parse_mode": "Markdown"}).encode()
    headers = {"Content-Type": "application/json"}
    for token in filter(None, [TG_TOKEN, TG_TOKEN2]):
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=body, headers=headers, method="POST"
            )
            urllib.request.urlopen(req, timeout=8).read()
            return  # éxito → no probar el segundo token
        except Exception:
            continue


def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def log(msg: str):
    line = f"[{ts()}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    # Rotar log si supera 500KB
    if LOG_FILE.stat().st_size > 500_000:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
        LOG_FILE.write_text("\n".join(lines[-200:]) + "\n", encoding="utf-8")


def is_running(pid: int) -> bool:
    try:
        result = subprocess.run(
            f'wmic process where ProcessId={pid} get ProcessId',
            shell=True, capture_output=True, timeout=8
        )
        return str(pid).encode() in result.stdout
    except Exception:
        return False


def supervisor_alive() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        return is_running(pid)
    except (ValueError, OSError):
        return False


def start_supervisor():
    launcher = BASE / "start_weatherbet.py"
    result = subprocess.run(
        [sys.executable, str(launcher)],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        cwd=str(BASE)
    )
    return result.returncode == 0, result.stdout.strip()


def logs_stale() -> tuple:
    """Detecta si bot_v2 o bridge están COLGADOS (vivos pero sin escribir).

    Retorna (stale: bool, detail: str).
    Considera stale si el log no se ha actualizado en >5 min (el bridge
    escribe cada 60s "Esperando 60s..."; el weatherbot al menos cada 10
    min en el ciclo de monitor).
    """
    import time
    bridge_log     = BASE / "logs" / "bridge.log"
    weatherbot_log = BASE / "logs" / "weatherbot.log"
    now = time.time()
    detail = []
    stale = False
    for name, p in [("bridge", bridge_log), ("weatherbot", weatherbot_log)]:
        if not p.exists():
            continue
        age_s = now - p.stat().st_mtime
        # Bridge debería escribir cada 60s. Si lleva >300s sin actividad → colgado.
        # weatherbot escribe en cada monitor (10 min) → si lleva >900s → colgado.
        threshold = 300 if name == "bridge" else 900
        if age_s > threshold:
            stale = True
            detail.append(f"{name} log sin actividad hace {age_s:.0f}s (umbral {threshold}s)")
    return stale, " | ".join(detail)


def force_restart_hung():
    """Mata supervisor + hijos colgados y los relanza."""
    # 1. Leer PID del supervisor y matar TODO el árbol
    try:
        if PID_FILE.exists():
            sup_pid = int(PID_FILE.read_text().strip())
            subprocess.run(["taskkill", "/PID", str(sup_pid), "/T", "/F"],
                           capture_output=True, timeout=8)
    except Exception:
        pass

    # 2. Matar hijos huérfanos por nombre (bot_v2.py, weatherbot_bridge.py)
    try:
        r = subprocess.run(
            'wmic process where "name like \'python%\'" get ProcessId,CommandLine /FORMAT:CSV',
            shell=True, capture_output=True, text=True, timeout=10
        )
        for line in r.stdout.splitlines():
            if any(s in line for s in ["bot_v2.py", "weatherbot_bridge.py"]):
                parts = line.split(",")
                if len(parts) >= 4:
                    pid = parts[3].strip()
                    if pid.isdigit():
                        subprocess.run(["taskkill", "/PID", pid, "/F"],
                                       capture_output=True, timeout=5)
    except Exception:
        pass

    # 3. Esperar y limpiar PID file
    import time
    time.sleep(2)
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def main():
    # Caso 1: supervisor caído (PID muerto)
    if not supervisor_alive():
        log("ALERTA — Supervisor caido. Reiniciando...")
        tg(
            f"🔴 *WeatherBet — Supervisor CAIDO*\n\n"
            f"El proceso supervisor ha muerto silenciosamente.\n"
            f"El watchdog está intentando relanzarlo automáticamente ahora.\n\n"
            f"_{ts()}_"
        )
    else:
        # Caso 2: supervisor vivo pero hijos colgados (no escriben logs)
        stale, detail = logs_stale()
        if not stale:
            pid = int(PID_FILE.read_text().strip())
            log(f"OK — Supervisor activo (PID {pid})")
            return

        # Hijos colgados → reinicio forzado
        log(f"ALERTA — Bot COLGADO ({detail}). Forzando reinicio...")
        tg(
            f"🟠 *WeatherBet — Bot COLGADO*\n\n"
            f"Los procesos están vivos pero no escriben en los logs:\n"
            f"_{detail}_\n\n"
            f"El watchdog está forzando reinicio del árbol completo.\n\n"
            f"_{ts()}_"
        )
        force_restart_hung()

    ok, out = start_supervisor()
    if ok:
        log(f"Supervisor relanzado correctamente. {out}")
        # Confirmación tras reinicio exitoso
        new_pid = "?"
        try:
            if PID_FILE.exists():
                new_pid = PID_FILE.read_text().strip()
        except Exception:
            pass
        tg(
            f"✅ *WeatherBet — Supervisor REANUDADO*\n\n"
            f"El watchdog ha relanzado el supervisor con éxito.\n"
            f"*Nuevo PID:* {new_pid}\n"
            f"El bot vuelve a operar normalmente.\n\n"
            f"_{ts()}_"
        )
    else:
        log(f"ERROR al relanzar supervisor: {out}")
        # CRÍTICO: ni siquiera arrancó. Necesita intervención manual.
        tg(
            f"🚨 *WeatherBet — FALLO CRÍTICO al relanzar*\n\n"
            f"El watchdog NO pudo relanzar el supervisor.\n"
            f"*Salida del intento de arranque:*\n```\n{out[:300]}\n```\n\n"
            f"⚠️ *Requiere intervención manual.* El bot NO está operando.\n\n"
            f"_{ts()}_"
        )


if __name__ == "__main__":
    main()
