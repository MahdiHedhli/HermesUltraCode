"""Run the read-only dashboard + API:

    python -m server --store gate_audit.sqlite3 [--host 127.0.0.1] [--port 9120]
                     [--bench results.json]

Binds loopback by default and prints the ephemeral session token to paste into the
dashboard. Refuses to bind a non-loopback host without a token (fail closed).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import ReadApiConfig
from core.store_sqlite import SqliteAuditStore
from server.read_api import ReadApiContext, run


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="HermesUltraCode read-only dashboard + API")
    ap.add_argument("--store", default="gate_audit.sqlite3")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9120)
    ap.add_argument("--bench", default=None, help="path to a bench results JSON to surface in metrics")
    ap.add_argument("--token", default="", help="fixed session token (else an ephemeral one is generated)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    benchmark = None
    if args.bench and os.path.isfile(args.bench):
        with open(args.bench, encoding="utf-8") as fh:
            benchmark = json.load(fh)

    ctx = ReadApiContext(
        store=SqliteAuditStore(args.store),
        config=ReadApiConfig(host=args.host, port=args.port, session_token=args.token),
        benchmark=benchmark,
        surfaced_config={"store": args.store, "host": args.host, "port": args.port},
    )
    run(ctx)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
