"""The `pulsar mcp` server: Pulsar's read-only tools for an agent harness.

A Model Context Protocol server on standard input and output. It runs the
public `pulsar` commands as subprocesses and imports no lifecycle code: nothing
from model_library/, release_spec/ or serving_guard/.
"""
