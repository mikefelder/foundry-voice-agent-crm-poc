"""Voice CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys

from crm_companion.config import get_settings
from crm_companion.voice.session import run_session

__all__ = ["main"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Talk to the CRM Sales Companion.")
    parser.add_argument("--agent", help="override AGENT_NAME")
    args = parser.parse_args()

    settings = get_settings()
    if args.agent:
        settings = settings.model_copy(update={"agent_name": args.agent})

    try:
        return asyncio.run(run_session(settings))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
