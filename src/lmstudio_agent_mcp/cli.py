"""CLI helpers for `lmstudio-agent-mcp`.

Two commands are exposed via console scripts:

  - ``lmstudio-agent-mcp``   start the MCP server (stdio transport)
  - ``lmstudio-mcp-config``  print the JSON snippet for LM Studio's MCP config

Both also work as ``python -m lmstudio_agent_mcp ...``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__


# ---------------------------------------------------------------------------
# Config snippet generation
# ---------------------------------------------------------------------------

CONFIG_FILENAME = "mcp.json"


def _resolve_executable(candidates: List[str]) -> Optional[str]:
    """Return the first existing executable from the candidate list."""
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    return None


def build_config(
    *,
    server_name: str = "agent_mcp",
    python_executable: Optional[str] = None,
    skills_dir: Optional[str] = None,
    memory_file: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the ``mcpServers`` snippet for LM Studio.

    Resolution order for the interpreter:
      1. ``python_executable`` argument if given
      2. ``LMSTUDIO_AGENT_PYTHON`` environment variable
      3. The Python interpreter currently running this process
    """
    interpreter = (
        python_executable
        or os.environ.get("LMSTUDIO_AGENT_PYTHON")
        or sys.executable
    )
    if not interpreter:
        interpreter = _resolve_executable(["python3", "python"]) or "python3"

    args: List[str] = ["-m", "lmstudio_agent_mcp"]

    env: Dict[str, str] = {}
    if skills_dir:
        env["LMSTUDIO_AGENT_SKILLS_DIR"] = str(skills_dir)
    if memory_file:
        env["LMSTUDIO_AGENT_MEMORY_FILE"] = str(memory_file)

    server_entry: Dict[str, Any] = {"command": interpreter, "args": args}
    if env:
        server_entry["env"] = env
    return {"mcpServers": {server_name: server_entry}}


def print_config(args: argparse.Namespace) -> int:
    config = build_config(
        server_name=args.name,
        python_executable=args.python,
        skills_dir=args.skills_dir,
        memory_file=args.memory_file,
    )
    text = json.dumps(config, indent=2, ensure_ascii=False)
    if args.write:
        target = Path(args.write).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and args.merge:
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
            if not isinstance(existing, dict):
                existing = {}
            existing.setdefault("mcpServers", {})
            existing["mcpServers"].update(config["mcpServers"])
            text = json.dumps(existing, indent=2, ensure_ascii=False)
        target.write_text(text + "\n", encoding="utf-8")
        print(f"Wrote {target}", file=sys.stderr)
    else:
        print(text)
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lmstudio-agent-mcp",
        description="LM Studio Agent MCP server and configuration helper.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Start the MCP server (stdio transport).")
    serve.set_defaults(func=_run_serve)

    cfg = sub.add_parser("config", help="Print or save the LM Studio MCP config snippet.")
    cfg.add_argument("--name", default="agent_mcp", help="Server name (default: agent_mcp).")
    cfg.add_argument("--python", default=None, help="Path to the Python interpreter to invoke.")
    cfg.add_argument("--skills-dir", default=None, help="Override LMSTUDIO_AGENT_SKILLS_DIR.")
    cfg.add_argument("--memory-file", default=None, help="Override LMSTUDIO_AGENT_MEMORY_FILE.")
    cfg.add_argument(
        "--write",
        metavar="PATH",
        help="Write the config to PATH. When the file exists, --merge updates it (default).",
    )
    cfg.add_argument(
        "--no-merge",
        dest="merge",
        action="store_false",
        help="Overwrite the target file instead of merging into an existing config.",
    )
    cfg.add_argument("--version", action="version", version=f"lmstudio-agent-mcp {__version__}")
    cfg.set_defaults(func=print_config, merge=True)

    # Top-level --version works regardless of subcommand.
    parser.add_argument("--version", action="version", version=f"lmstudio-agent-mcp {__version__}")

    args = parser.parse_args(argv)
    return args.func(args)


def config_main(argv: Optional[List[str]] = None) -> int:
    """Entry point for the ``lmstudio-mcp-config`` console script.

    This is a thin wrapper that calls :func:`print_config` directly, so
    ``lmstudio-mcp-config --write /path/to/mcp.json`` works without specifying
    a subcommand.
    """
    parser = argparse.ArgumentParser(
        prog="lmstudio-mcp-config",
        description="Print or save the LM Studio MCP config snippet for lmstudio-agent-mcp.",
    )
    parser.add_argument("--name", default="agent_mcp", help="Server name (default: agent_mcp).")
    parser.add_argument("--python", default=None, help="Path to the Python interpreter to invoke.")
    parser.add_argument("--skills-dir", default=None, help="Override LMSTUDIO_AGENT_SKILLS_DIR.")
    parser.add_argument("--memory-file", default=None, help="Override LMSTUDIO_AGENT_MEMORY_FILE.")
    parser.add_argument(
        "--write",
        metavar="PATH",
        help="Write the config to PATH. When the file exists, --merge updates it (default).",
    )
    parser.add_argument(
        "--no-merge",
        dest="merge",
        action="store_false",
        help="Overwrite the target file instead of merging into an existing config.",
    )
    parser.add_argument("--version", action="version", version=f"lmstudio-agent-mcp {__version__}")
    parser.set_defaults(merge=True)

    args = parser.parse_args(argv)
    return print_config(args)


def _run_serve(_: argparse.Namespace) -> int:
    from .server import mcp

    mcp.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
