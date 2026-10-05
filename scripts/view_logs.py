#!/usr/bin/env python3
"""Logs dekhne ka tool. Examples:
  python scripts/view_logs.py                    # app.log ki aakhri 30 lines (rang ke saath)
  python scripts/view_logs.py --errors           # sirf ERROR/CRITICAL, traceback ke saath
  python scripts/view_logs.py --level WARNING    # WARNING aur upar
  python scripts/view_logs.py --request-id ab12  # ek request ki puri kahani
  python scripts/view_logs.py --ticket T-1042
  python scripts/view_logs.py --raw-errors       # logs/error.log seedha print
  python scripts/view_logs.py -n 100 --follow    # live (tail -f)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
COL = {"DEBUG": "\033[36m", "INFO": "\033[32m", "WARNING": "\033[33m", "ERROR": "\033[31m", "CRITICAL": "\033[1;41m"}
RESET = "\033[0m"


def fmt(e: dict, color: bool) -> str:
    lvl = e.get("level", "?")
    tag = f"{COL.get(lvl, '')}{lvl:<8}{RESET}" if color else f"{lvl:<8}"
    ctx = " ".join(f"{k}={e[k]}" for k in ("request_id", "ticket_id", "thread_id") if e.get(k, "-") != "-")
    out = f"{e.get('ts', '')[11:23]} {tag} {e.get('logger', '')}: {e.get('msg', '')}"
    if ctx:
        out += f"  [{ctx}]"
    if e.get("extra"):
        out += f"  {json.dumps(e['extra'], ensure_ascii=False)}"
    if exc := e.get("exception"):
        out += f"\n    >>> {exc['type']}: {exc['message']}\n    where: {e.get('where')}\n"
        out += "\n".join("    " + line for line in exc["traceback"].splitlines())
    return out


def keep(e: dict, a: argparse.Namespace) -> bool:
    if LEVELS.get(e.get("level", "INFO"), 20) < LEVELS[a.level]:
        return False
    if a.request_id and a.request_id not in e.get("request_id", ""):
        return False
    if a.ticket and a.ticket != e.get("ticket_id"):
        return False
    return True


def main() -> int:
    p = argparse.ArgumentParser(description="ShopPilot log viewer")
    p.add_argument("--dir", default="logs")
    p.add_argument("-n", type=int, default=30, help="aakhri N lines")
    p.add_argument("--level", default="INFO", choices=LEVELS)
    p.add_argument("--errors", action="store_true", help="sirf ERROR+")
    p.add_argument("--request-id")
    p.add_argument("--ticket")
    p.add_argument("--raw-errors", action="store_true", help="logs/error.log print karo")
    p.add_argument("--follow", "-f", action="store_true")
    p.add_argument("--no-color", action="store_true")
    a = p.parse_args()
    if a.errors:
        a.level = "ERROR"
    color = sys.stdout.isatty() and not a.no_color

    if a.raw_errors:
        f = Path(a.dir) / "error.log"
        print(f.read_text(encoding="utf-8") if f.exists() else "error.log abhi nahi bani (koi error nahi aaya).")
        return 0

    path = Path(a.dir) / "app.log"
    if not path.exists():
        print(f"{path} nahi mili. Pehle app chalao ya: python scripts/demo_logging.py")
        return 1

    def parse(line: str) -> dict | None:
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None

    events = [e for e in map(parse, path.read_text(encoding="utf-8").splitlines()) if e and keep(e, a)]
    for e in events[-a.n:]:
        print(fmt(e, color))
    if a.follow:
        with path.open(encoding="utf-8") as fh:
            fh.seek(0, 2)
            while True:
                if line := fh.readline():
                    if (e := parse(line)) and keep(e, a):
                        print(fmt(e, color), flush=True)
                else:
                    time.sleep(0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
