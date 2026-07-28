"""Service layer: run queue, event streaming, database-backed config, HTTP API.

The CLI runs a workflow in-process and exits. The server keeps a queue, a run
history and an event log, so several runs can be observed and replayed — while
sharing exactly the same agents, workflows and config objects.
"""

from .app import create_app, serve

__all__ = ["create_app", "serve"]
