"""Setuptools install command with a post-install hook.

When ``pip install`` (without ``--no-build-isolation``) runs the setup
phase, this subclass of the standard install command calls
:func:`lmstudio_agent_mcp.cli.register_with_lm_studio` after the package
has been installed. Failures are swallowed so the install itself never
fails just because ``~/.lmstudio`` is not writable.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

try:
    from setuptools.command.install import install as _install
except ImportError:  # pragma: no cover
    _install = None  # type: ignore[assignment]


_log = logging.getLogger("lmstudio_agent_mcp.install")


class PostInstallCommand(_install if _install is not None else object):  # type: ignore[misc]
    """``setuptools`` install command that registers with LM Studio when done."""

    def run(self) -> None:  # type: ignore[override]
        if _install is None:
            return
        super().run()
        # The wheel is on sys.path now (editable install) or the package is
        # already in site-packages (regular install). Try to autoregister.
        try:
            from lmstudio_agent_mcp.cli import register_with_lm_studio
            result = register_with_lm_studio()
            sys.stderr.write(
                f"\n[lmstudio-agent-mcp] Registered '{result['server_name']}' in {result['target']}\n"
                f"[lmstudio-agent-mcp] Restart LM Studio to load the new MCP server.\n\n"
            )
        except Exception as exc:  # pragma: no cover - defensive
            sys.stderr.write(
                f"\n[lmstudio-agent-mcp] Could not auto-register with LM Studio: {exc}\n"
                f"[lmstudio-agent-mcp] Run 'lmstudio-mcp-setup' later to register manually.\n\n"
            )
