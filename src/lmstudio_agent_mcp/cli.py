"""CLI helpers for ``lmstudio-agent-mcp``.

Console scripts exposed by the package:

  - ``lmstudio-agent-mcp``   ``serve`` starts the MCP server (default if no
                             subcommand is given), ``setup`` auto-registers
                             the server with LM Studio, ``config`` prints or
                             saves the JSON snippet for LM Studio's MCP config
  - ``lmstudio-mcp-config``  shortcut for ``config`` without subcommand
  - ``lmstudio-mcp-setup``   shortcut for ``setup`` without subcommand

All three are also available as ``python -m lmstudio_agent_mcp ...``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
    use_console_script: bool = True,
) -> Dict[str, Any]:
    """Build the ``mcpServers`` snippet for LM Studio.

    Resolution order for the interpreter:
      1. ``python_executable`` argument if given
      2. ``LMSTUDIO_AGENT_PYTHON`` environment variable
      3. The Python interpreter currently running this process

    When ``use_console_script`` is True (the default), the command points at
    the installed ``lmstudio-agent-mcp`` console script so the snippet works
    out of the box without knowing the Python interpreter path.
    """
    env: Dict[str, str] = {}
    if skills_dir:
        env["LMSTUDIO_AGENT_SKILLS_DIR"] = str(skills_dir)
    if memory_file:
        env["LMSTUDIO_AGENT_MEMORY_FILE"] = str(memory_file)

    if use_console_script:
        script = _resolve_executable(["lmstudio-agent-mcp"])
        if script:
            server_entry: Dict[str, Any] = {"command": script}
            if env:
                server_entry["env"] = env
            return {"mcpServers": {server_name: server_entry}}

    interpreter = (
        python_executable
        or os.environ.get("LMSTUDIO_AGENT_PYTHON")
        or sys.executable
        or "python3"
    )
    args: List[str] = ["-m", "lmstudio_agent_mcp"]
    server_entry = {"command": interpreter, "args": args}
    if env:
        server_entry["env"] = env
    return {"mcpServers": {server_name: server_entry}}


def print_config(args: argparse.Namespace) -> int:
    config = build_config(
        server_name=args.name,
        python_executable=args.python,
        skills_dir=args.skills_dir,
        memory_file=args.memory_file,
        use_console_script=getattr(args, "use_console_script", True),
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
# Auto-registration with LM Studio
# ---------------------------------------------------------------------------

# Marker file written next to mcp.json so we can detect "already registered"
# without having to parse JSON.
_REGISTRATION_MARKER = ".lmstudio_agent_mcp_installed"


def _candidate_lm_studio_dirs() -> List[Path]:
    """Return the list of directories where LM Studio's ``mcp.json`` may live.

    The order reflects what real LM Studio installations use on each platform,
    but the first writable directory wins.
    """
    home = Path.home()
    candidates = [
        home / ".lmstudio",  # canonical LM Studio config dir
        home / ".config" / "LM Studio",
        home / "Library" / "Application Support" / "LM Studio",  # macOS
    ]
    # On Windows %APPDATA%/LM Studio
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "LM Studio")
    return [c for c in candidates if c is not None]


def find_mcp_json() -> Optional[Path]:
    """Return the first ``mcp.json`` found under a known LM Studio location."""
    for base in _candidate_lm_studio_dirs():
        candidate = base / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def find_or_create_mcp_json(prefer: Optional[Path] = None) -> Tuple[Path, bool]:
    """Locate or create ``mcp.json`` for LM Studio.

    Returns ``(path, created)``. If ``prefer`` is given it is used as the
    target when no existing file is found. Otherwise the first writable
    candidate directory is used.
    """
    existing = find_mcp_json()
    if existing is not None:
        return existing, False

    target_dir: Optional[Path] = prefer
    if target_dir is None:
        for d in _candidate_lm_studio_dirs():
            try:
                d.mkdir(parents=True, exist_ok=True)
                target_dir = d
                break
            except OSError:
                continue
    if target_dir is None:
        # Fall back to ~/.lmstudio even if it can't be created yet; the caller
        # will surface a clear OSError.
        target_dir = Path.home() / ".lmstudio"
        target_dir.mkdir(parents=True, exist_ok=True)

    target = target_dir / CONFIG_FILENAME
    return target, True


def register_with_lm_studio(
    *,
    server_name: str = "agent_mcp",
    python_executable: Optional[str] = None,
    skills_dir: Optional[str] = None,
    memory_file: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Register this MCP server in LM Studio's ``mcp.json``.

    Returns a dict describing what happened: target path, created, already
    registered, written, etc. Raises ``OSError`` only if the file system is
    truly unwritable.
    """
    target, created_file = find_or_create_mcp_json()
    config = build_config(
        server_name=server_name,
        python_executable=python_executable,
        skills_dir=skills_dir,
        memory_file=memory_file,
    )["mcpServers"]

    # Idempotency: if the marker file says we already registered, skip unless
    # the user forced it.
    marker = target.parent / _REGISTRATION_MARKER
    if marker.exists() and not force:
        try:
            existing_marker = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing_marker = {}
        if existing_marker.get("server_name") == server_name:
            return {
                "target": str(target),
                "created_file": created_file,
                "already_registered": True,
                "server_name": server_name,
            }

    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
    else:
        existing = {}

    existing.setdefault("mcpServers", {})
    if not isinstance(existing["mcpServers"], dict):
        existing["mcpServers"] = {}
    existing["mcpServers"].update(config)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    marker.write_text(
        json.dumps({"server_name": server_name, "version": __version__}, indent=2),
        encoding="utf-8",
    )

    return {
        "target": str(target),
        "created_file": created_file,
        "already_registered": False,
        "server_name": server_name,
        "entry": config[server_name],
    }


# ---------------------------------------------------------------------------
# Subcommand entry points
# ---------------------------------------------------------------------------

def setup_main(argv: Optional[List[str]] = None) -> int:
    """Entry point for the ``lmstudio-mcp-setup`` console script.

    Registers this MCP server with LM Studio's ``mcp.json`` so the user does
    not have to copy/paste config by hand.
    """
    parser = argparse.ArgumentParser(
        prog="lmstudio-mcp-setup",
        description="Register the agent MCP server with LM Studio's mcp.json.",
    )
    parser.add_argument("--name", default="agent_mcp", help="Server name (default: agent_mcp).")
    parser.add_argument("--python", default=None, help="Path to the Python interpreter to invoke.")
    parser.add_argument("--skills-dir", default=None, help="Override LMSTUDIO_AGENT_SKILLS_DIR.")
    parser.add_argument("--memory-file", default=None, help="Override LMSTUDIO_AGENT_MEMORY_FILE.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-register even if a previous registration marker exists.",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress the success message.")
    parser.add_argument("--version", action="version", version=f"lmstudio-agent-mcp {__version__}")
    args = parser.parse_args(argv)

    result = register_with_lm_studio(
        server_name=args.name,
        python_executable=args.python,
        skills_dir=args.skills_dir,
        memory_file=args.memory_file,
        force=args.force,
    )
    if not args.quiet:
        action = "Already registered" if result["already_registered"] else "Registered"
        target = result["target"]
        print(f"{action} '{result['server_name']}' in {target}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for the ``lmstudio-agent-mcp`` console script."""
    parser = argparse.ArgumentParser(
        prog="lmstudio-agent-mcp",
        description=(
            "LM Studio Agent MCP server. Without a subcommand the server is "
            "started. Use 'setup' to register with LM Studio, 'config' to "
            "print or write the config snippet, and 'uninstall' to remove "
            "the registration."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Start the MCP server (stdio transport).")
    serve.set_defaults(func=_run_serve, command="serve")

    setup = sub.add_parser("setup", help="Register this server with LM Studio's mcp.json.")
    setup.add_argument("--name", default="agent_mcp", help="Server name (default: agent_mcp).")
    setup.add_argument("--python", default=None, help="Path to the Python interpreter to invoke.")
    setup.add_argument("--skills-dir", default=None, help="Override LMSTUDIO_AGENT_SKILLS_DIR.")
    setup.add_argument("--memory-file", default=None, help="Override LMSTUDIO_AGENT_MEMORY_FILE.")
    setup.add_argument("--force", action="store_true", help="Re-register even if already registered.")
    setup.add_argument("--quiet", action="store_true", help="Suppress the success message.")
    setup.set_defaults(func=_run_setup, command="setup")

    cfg = sub.add_parser(
        "config",
        help="Print or save the LM Studio MCP config snippet.",
    )
    cfg.add_argument("--name", default="agent_mcp", help="Server name (default: agent_mcp).")
    cfg.add_argument("--python", default=None, help="Path to the Python interpreter to invoke.")
    cfg.add_argument("--skills-dir", default=None, help="Override LMSTUDIO_AGENT_SKILLS_DIR.")
    cfg.add_argument("--memory-file", default=None, help="Override LMSTUDIO_AGENT_MEMORY_FILE.")
    cfg.add_argument("--no-console-script", dest="use_console_script",
                    action="store_false",
                    help="Use 'python -m lmstudio_agent_mcp' instead of the console script.")
    cfg.add_argument(
        "--write", metavar="PATH",
        help="Write the config to PATH. When the file exists, --merge updates it (default).",
    )
    cfg.add_argument(
        "--no-merge", dest="merge", action="store_false",
        help="Overwrite the target file instead of merging into an existing config.",
    )
    cfg.set_defaults(func=print_config, merge=True, use_console_script=True)

    # Top-level --version works regardless of subcommand.
    parser.add_argument("--version", action="version", version=f"lmstudio-agent-mcp {__version__}")
    # When no subcommand is given, default to starting the server.
    parser.set_defaults(func=_run_serve, command="serve")

    args = parser.parse_args(argv)
    return args.func(args)


def config_main(argv: Optional[List[str]] = None) -> int:
    """Entry point for the ``lmstudio-mcp-config`` console script."""
    parser = argparse.ArgumentParser(
        prog="lmstudio-mcp-config",
        description="Print or save the LM Studio MCP config snippet for lmstudio-agent-mcp.",
    )
    parser.add_argument("--name", default="agent_mcp", help="Server name (default: agent_mcp).")
    parser.add_argument("--python", default=None, help="Path to the Python interpreter to invoke.")
    parser.add_argument("--skills-dir", default=None, help="Override LMSTUDIO_AGENT_SKILLS_DIR.")
    parser.add_argument("--memory-file", default=None, help="Override LMSTUDIO_AGENT_MEMORY_FILE.")
    parser.add_argument("--no-console-script", dest="use_console_script",
                    action="store_false",
                    help="Use 'python -m lmstudio_agent_mcp' instead of the console script.")
    parser.add_argument(
        "--write", metavar="PATH",
        help="Write the config to PATH. When the file exists, --merge updates it (default).",
    )
    parser.add_argument(
        "--no-merge", dest="merge", action="store_false",
        help="Overwrite the target file instead of merging into an existing config.",
    )
    parser.add_argument("--version", action="version", version=f"lmstudio-agent-mcp {__version__}")
    parser.set_defaults(merge=True, use_console_script=True)

    args = parser.parse_args(argv)
    return print_config(args)


# ---------------------------------------------------------------------------
# Internal handlers
# ---------------------------------------------------------------------------

def _run_serve(_: argparse.Namespace) -> int:
    from . import _autoregister
    from .server import mcp

    _autoregister.try_autoregister()  # silent, one-shot
    mcp.run()
    return 0


def _run_setup(args: argparse.Namespace) -> int:
    return setup_main([
        *(["--name", args.name] if args.name else []),
        *(["--python", args.python] if args.python else []),
        *(["--skills-dir", args.skills_dir] if args.skills_dir else []),
        *(["--memory-file", args.memory_file] if args.memory_file else []),
        *(["--force"] if args.force else []),
        *(["--quiet"] if args.quiet else []),
    ])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
