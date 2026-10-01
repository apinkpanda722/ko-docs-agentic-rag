"""Run the local-only streamable-HTTP MCP demo server."""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from kodocs.mcp_server.server import create_app
from kodocs.mcp_server.tokens import TOKEN_HASH_PATH
from kodocs.tools import build_tool_deps

_HOST = "127.0.0.1"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="로컬 MCP 데모 서버를 기동합니다.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--opensearch-url", default="http://127.0.0.1:9200")
    parser.add_argument("--model-path")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger(__name__)
    started = time.perf_counter()
    deps = build_tool_deps(
        root=args.root,
        base_url=args.opensearch_url,
        model_path_or_id=args.model_path,
    )
    logger.info("BGE-M3 임베더 준비 시간: %.3f초", time.perf_counter() - started)
    app = create_app(deps, token_hash_path=args.root / TOKEN_HASH_PATH)
    logger.info("MCP 서버를 %s:%d에서 기동합니다.", _HOST, args.port)
    uvicorn.run(app, host=_HOST, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
