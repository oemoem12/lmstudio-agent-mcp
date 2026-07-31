"""LM Studio Agent MCP — local agent tools for LM Studio via MCP."""

try:
    from importlib.metadata import version as _v
    __version__ = _v("lmstudio-agent-mcp")
except Exception:  # pragma: no cover
    __version__ = "0.0.0+local"

from .server import mcp  # re-exported for `python -m lmstudio_agent_mcp`

__all__ = ["mcp", "__version__"]
