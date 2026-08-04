"""Silent one-shot registration with LM Studio.

The first time ``lmstudio-agent-mcp serve`` runs after installation, this
module writes (or updates) ``~/.lmstudio/mcp.json`` so the server appears
in LM Studio's MCP list without the user having to run ``lmstudio-mcp-setup``
manually.

Failures are swallowed on purpose: a broken autoregister should never stop
the server from starting. Errors are only logged to stderr.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from threading import Lock
from typing import Optional

from .cli import find_mcp_json, register_with_lm_studio

_LOG = logging.getLogger("lmstudio_agent_mcp.autoregister")

_disable_env = "LMSTUDIO_AGENT_NO_AUTOREGISTER"
_done_marker: Optional[Path] = None
_lock = Lock()
_already_tried = False


def _marker_path() -> Path:
    """Marker file that records the autoregister attempt for this version."""
    base = Path(
        os.environ.get("LMSTUDIO_AGENT_AUTOREGISTER_MARKER_DIR")
        or str(Path.home() / ".lmstudio_agent_mcp")
    )
    base.mkdir(parents=True, exist_ok=True)
    return base / "autoregister.done"


def try_autoregister(force: bool = False) -> bool:
    """Best-effort: register the server with LM Studio if not already done.

    Returns True if a registration was performed (or already present),
    False on failure or when disabled.
    """
    global _already_tried
    if os.environ.get(_disable_env):
        return False
    if not force and _already_tried:
        return True
    with _lock:
        if not force and _already_tried:
            return True
        marker = _marker_path()
        if not force and marker.exists():
            _already_tried = True
            return True
        try:
            result = register_with_lm_studio()
            marker.write_text(
                f"version={result.get('server_name')}\n",
                encoding="utf-8",
            )
            _already_tried = True
            return True
        except OSError as exc:
            # Filesystem not writable (e.g. permission denied) — silent.
            _LOG.debug("autoregister skipped: %s", exc)
            return False
        except Exception as exc:  # pragma: no cover - defensive
            _LOG.debug("autoregister failed: %s", exc)
            return False
