"""Delete old LangGraph checkpoints (Step N retention). Run it from a scheduled job, for example once a night.

    uv run python scripts\prune_checkpoints.py            (threads older than 30 days)
    uv run python scripts\prune_checkpoints.py --days 7
"""
from __future__ import annotations

import argparse

from shoppilot.agents.checkpoint import RETENTION_DAYS, close_checkpointer, open_checkpointer, prune_checkpoints


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete checkpoint threads older than N days.")
    parser.add_argument("--days", type=int, default=RETENTION_DAYS)
    args = parser.parse_args()

    saver = open_checkpointer()
    try:
        deleted = prune_checkpoints(saver, days=args.days)
    finally:
        close_checkpointer(saver)
    print(f"deleted {deleted} checkpoint thread(s) older than {args.days} days")


if __name__ == "__main__":
    main()
