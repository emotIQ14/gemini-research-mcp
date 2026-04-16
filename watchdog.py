#!/usr/bin/env python3
"""
watchdog.py — Guardián externo del supervisor WeatherBet
=========================================================
Ejecutado por Windows Task Scheduler cada 5 minutos.
Si el supervisor no está corriendo, lo reinicia automáticamente.
No interfiere si ya está activo.
"""
import subprocess
import sys
import os
from pathlib import Path
from datetime import datetime, timezone

BASE     = Path(__file__).parent
PID_FILE = BASE / "logs" / "supervisor.pid"
LOG_FILE = BASE / "logs" / "watchdog.log"


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


def main():
    if supervisor_alive():
        pid = int(PID_FILE.read_text().strip())
        log(f"OK — Supervisor activo (PID {pid})")
        return

    log("ALERTA — Supervisor caido. Reiniciando...")
    ok, out = start_supervisor()
    if ok:
        log(f"Supervisor relanzado correctamente. {out}")
    else:
        log(f"ERROR al relanzar supervisor: {out}")


if __name__ == "__main__":
    main()
