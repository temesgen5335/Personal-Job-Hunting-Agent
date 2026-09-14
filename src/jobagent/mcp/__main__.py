"""`python -m jobagent.mcp` — the MCP operator server on stdio.

stdout is the wire: the SDK re-points fd 1 at stderr while serving, and this module
sends every log line to stderr itself, so nothing but MCP messages reaches the client.
`--check` never serves — it builds the server, connects an in-memory client, lists the
surface and asserts no send/approve-shaped tool exists; that is `make mcp_check`.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]          # src/jobagent/mcp/__main__.py → repo
FORBIDDEN_WORDS = ("send", "submit", "approve", "apply_to", "ats", "purge", "delete", "save_cv")


def main(argv=None, *, settings_factory=None, chdir: bool = True) -> int:
    ap = argparse.ArgumentParser(prog="python -m jobagent.mcp",
                                 description="personalAgent MCP operator server (stdio).")
    ap.add_argument("--admin", action="store_true",
                    help="let config-changing (ADMIN) tools be confirmed from this client")
    ap.add_argument("--check", action="store_true",
                    help="build the server, list tools/resources/prompts, exit 0 — never serves")
    ap.add_argument("--db", default="", help="override JOBAGENT_DB_PATH for this process")
    args = ap.parse_args(argv)

    if chdir:
        # `.env`, data/ and artifacts/ resolve as they do for `make ask`, whatever
        # directory the client launched us from.
        os.chdir(REPO_ROOT)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")

    try:
        import mcp  # noqa: F401
    except ImportError:
        print("The `mcp` extra is not installed. Run: make install "
              "(or: uv pip install -e '.[mcp]')", file=sys.stderr)
        return 2

    from jobagent import __version__
    from jobagent.config import get_settings
    from jobagent.mcp import build_server
    from jobagent.mcp.operator import Operator

    settings = (settings_factory or get_settings)()
    if args.db:
        settings = settings.model_copy(update={"db_path": args.db})

    op = Operator(settings, admin=args.admin)
    server = build_server(settings, admin=args.admin, operator=op)

    if args.check:
        import anyio
        from mcp import Client

        async def survey():
            async with Client(server, raise_exceptions=True) as c:
                tools = [t.name for t in (await c.list_tools()).tools]
                resources = [str(r.uri) for r in (await c.list_resources()).resources]
                templates = [t.uri_template for t in (await c.list_resource_templates()).resource_templates]
                prompts = [p.name for p in (await c.list_prompts()).prompts]
                return tools, resources, templates, prompts

        try:
            tools, resources, templates, prompts = anyio.run(survey)
        finally:
            op.close(summary="check")
        bad = sorted(n for n in tools if any(w in n for w in FORBIDDEN_WORDS))
        print(f"personalagent MCP {__version__}: {len(tools)} tools, "
              f"{len(resources)} resources (+{len(templates)} templates), {len(prompts)} prompts; "
              f"admin={'on' if args.admin else 'off'}")
        print("tools:     " + ", ".join(tools))
        print("resources: " + ", ".join(resources + templates))
        print("prompts:   " + ", ".join(prompts))
        if bad:
            print(f"FORBIDDEN tool names present: {bad}", file=sys.stderr)
            return 1
        return 0

    try:
        server.run(transport="stdio")
    finally:
        op.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
