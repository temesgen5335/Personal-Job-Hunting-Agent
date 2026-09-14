"""The MCP operator server: the fourth renderer of the assistant mechanism.

`build_server()` (Task 11) bridges the governed toolbox to MCP tools over stdio. Nothing
in this package is a second access path: every read and write goes through
`GuardedToolBox.execute()` on the `Operator`'s single owning thread.
"""
