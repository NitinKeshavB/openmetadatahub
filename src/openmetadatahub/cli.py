"""Command line: offline helpers that need no warehouse, plus the API server."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .ai import discover_odh
from .contracts import contract_to_tests, diff_contracts, load_contract
from .dq import describe_catalog


def _dump(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="openmetadatahub")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("metrics", help="list the built-in data quality metrics")

    c = sub.add_parser("contract-tests", help="compile a data contract into test cases")
    c.add_argument("contract", type=Path)
    c.add_argument("--namespace", help="catalog.schema for unqualified physicalNames")

    d = sub.add_parser("contract-diff", help="detect breaking changes between two contract versions")
    d.add_argument("old", type=Path)
    d.add_argument("new", type=Path)

    a = sub.add_parser("ai-discover", help="inventory AI assets from exported Kubernetes manifests")
    a.add_argument("manifests", type=Path)

    s = sub.add_parser("serve", help="run the HTTP API (no warehouse attached)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)

    args = p.parse_args(argv)
    if args.cmd == "metrics":
        _dump(describe_catalog())
    elif args.cmd == "contract-tests":
        _dump([t.model_dump(mode="json") for t in contract_to_tests(load_contract(args.contract), args.namespace)])
    elif args.cmd == "contract-diff":
        diff = diff_contracts(load_contract(args.old), load_contract(args.new))
        _dump(diff.model_dump(mode="json"))
        return 0 if diff.version_ok else 1
    elif args.cmd == "ai-discover":
        _dump([x.model_dump(mode="json") for x in discover_odh(args.manifests.read_text())])
    elif args.cmd == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
