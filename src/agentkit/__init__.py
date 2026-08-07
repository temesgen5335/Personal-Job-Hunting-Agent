"""agentkit — a domain-agnostic agent harness.

Nothing in this package may import a specific application's code. It knows about
models, tools, permissions, retrieval and loops; it knows nothing about job hunting,
and a test asserts both the import boundary and the vocabulary boundary.

A host application supplies its domain through a manifest: its tools, its knowledge
sources, its prompts and its policy.
"""
