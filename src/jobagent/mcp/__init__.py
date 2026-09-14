"""The MCP operator server: the fourth renderer of the assistant mechanism.

`build_server()` (Task 11) bridges the governed toolbox to MCP tools over stdio. Nothing
in this package is a second access path: every read and write goes through
`GuardedToolBox.execute()` on the `Operator`'s single owning thread.
"""

from contextlib import asynccontextmanager

from jobagent import __version__

SERVER_NAME = "personalagent"


def build_server(settings=None, *, admin: bool = False, deps=None, operator=None):
    """Assemble the server. Imports the SDK here so `jobagent.mcp` stays importable —
    and its absence reportable — without the `mcp` extra."""
    from mcp.server.mcpserver import MCPServer

    from jobagent.config import get_settings
    from jobagent.mcp.bridge import annotations_for, make_tool_fn, title_for
    from jobagent.mcp.operator import Operator
    from jobagent.mcp.prompts import INSTRUCTIONS, register_prompts
    from jobagent.mcp.resources import register_resources

    settings = settings or get_settings()
    owns_operator = operator is None            # close only what this call created
    op = operator or Operator(settings, admin=admin, deps=deps)
    if op.assistant is None:
        op.start()

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield {"operator": op}
        finally:
            if owns_operator:                   # a passed-in operator is the caller's to close
                op.close()

    server = MCPServer(SERVER_NAME, title="personalAgent", version=__version__,
                       instructions=INSTRUCTIONS, lifespan=lifespan)
    for spec in op.specs():                      # registration order → deterministic tools/list
        policy = op.policy_for(spec.name)
        server.add_tool(make_tool_fn(op, spec, policy), name=spec.name, title=title_for(spec.name),
                        description=spec.description, annotations=annotations_for(policy, spec.name))
    register_resources(server, op)
    register_prompts(server, op)
    return server
