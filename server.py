#!/usr/bin/env python3
"""
LM Studio Agent MCP Server.

Provides local agent tools for LM Studio via the Model Context Protocol (MCP):
- Read and write files
- Execute terminal commands
- Search the web via multiple engines (DuckDuckGo, Bing, Google, Baidu)
- Persistent key/value memory with tagging and search
- Load and invoke user-defined skills from a local directory

Transport: stdio (default, suitable for LM Studio local MCP integration).
"""

from __future__ import annotations

import asyncio
import fnmatch
import importlib.util
import inspect
import json
import os
import re
import shlex
import sys
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, field_validator
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Server setup
# ---------------------------------------------------------------------------

mcp = FastMCP("agent_mcp")

DEFAULT_TIMEOUT = 60.0
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MiB

# ---------------------------------------------------------------------------
# Persistent storage paths
# ---------------------------------------------------------------------------
# Memory file: JSON-based key/value store, persisted across server restarts.
# Override with the LMSTUDIO_AGENT_MEMORY_FILE environment variable.
_DEFAULT_MEMORY_DIR = Path.home() / ".lmstudio_agent_mcp"
MEMORY_FILE: Path = Path(
    os.environ.get("LMSTUDIO_AGENT_MEMORY_FILE", _DEFAULT_MEMORY_DIR / "memory.json")
).expanduser().resolve()

# Skills directory: each subdirectory or file is treated as a discoverable skill.
# Override with the LMSTUDIO_AGENT_SKILLS_DIR environment variable.
_DEFAULT_SKILLS_DIR = Path.cwd() / "skills"
SKILLS_DIR: Path = Path(
    os.environ.get("LMSTUDIO_AGENT_SKILLS_DIR", _DEFAULT_SKILLS_DIR)
).expanduser().resolve()

# Process-wide lock guarding the memory file from concurrent write corruption.
_memory_lock = RLock()


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def _resolve_path(path: str) -> Path:
    """Resolve a path to an absolute path and guard against traversal attacks."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    return p.resolve()


def _format_error(message: str, detail: Optional[str] = None) -> str:
    payload: Dict[str, Any] = {"success": False, "error": message}
    if detail:
        payload["detail"] = detail
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _format_success(data: Dict[str, Any]) -> str:
    return json.dumps({"success": True, **data}, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------

class SearchEngine(str, Enum):
    """Supported search engines."""

    DUCKDUCKGO = "duckduckgo"
    BING = "bing"
    GOOGLE = "google"
    BAIDU = "baidu"


class ReadFileInput(BaseModel):
    """Input model for reading a file."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(..., description="Absolute or relative path to the file to read.", min_length=1)
    offset: int = Field(default=0, description="Number of lines to skip from the beginning.", ge=0)
    limit: Optional[int] = Field(default=200, description="Maximum number of lines to return (null = unlimited).", ge=1)
    encoding: str = Field(default="utf-8", description="Text encoding to use when reading the file.")


class WriteFileInput(BaseModel):
    """Input model for writing a file."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    path: str = Field(..., description="Absolute or relative path to the file to write.", min_length=1)
    content: str = Field(..., description="Text content to write to the file.")
    encoding: str = Field(default="utf-8", description="Text encoding to use when writing the file.")
    append: bool = Field(default=False, description="If True, append to the file instead of overwriting.")
    create_dirs: bool = Field(default=True, description="If True, create parent directories when they do not exist.")


class ExecuteCommandInput(BaseModel):
    """Input model for executing a shell command."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    command: str = Field(..., description="Shell command to execute. Pipes and redirections are supported.", min_length=1)
    working_directory: Optional[str] = Field(default=None, description="Working directory for the command. Defaults to the server process cwd.")
    timeout: float = Field(default=DEFAULT_TIMEOUT, description="Maximum execution time in seconds.", ge=1.0, le=600.0)
    env: Optional[Dict[str, str]] = Field(default=None, description="Additional environment variables to set or override.")
    shell: bool = Field(default=True, description="Execute the command through the system shell (required for pipes/redirects).")

    @field_validator("working_directory")
    @classmethod
    def _validate_working_directory(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        resolved = _resolve_path(v)
        if not resolved.is_dir():
            raise ValueError(f"Working directory does not exist: {resolved}")
        return str(resolved)


class WebSearchInput(BaseModel):
    """Input model for web search."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(..., description="Search query string.", min_length=1, max_length=500)
    engine: SearchEngine = Field(default=SearchEngine.DUCKDUCKGO, description="Search engine to use: 'duckduckgo', 'bing', 'google', or 'baidu'.")
    num_results: int = Field(default=5, description="Maximum number of results to return.", ge=1, le=20)
    region: Optional[str] = Field(default=None, description="Optional region/locale code for search results (e.g. 'wt-wt', 'us-en', 'zh-cn').")


class MemorySaveInput(BaseModel):
    """Input model for saving a memory entry."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    key: str = Field(..., description="Unique key to identify this memory entry.", min_length=1, max_length=200)
    value: str = Field(..., description="Content to remember.")
    category: str = Field(default="general", description="Logical bucket for the entry (e.g. 'user_preference', 'project_note').", max_length=64)
    tags: List[str] = Field(default_factory=list, description="Optional list of tags for retrieval filtering.", max_length=32)
    overwrite: bool = Field(default=True, description="If False, fail when the key already exists.")


class MemoryLoadInput(BaseModel):
    """Input model for loading a single memory entry."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    key: str = Field(..., description="Key of the memory entry to load.", min_length=1, max_length=200)


class MemoryDeleteInput(BaseModel):
    """Input model for deleting a memory entry."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    key: str = Field(..., description="Key of the memory entry to delete.", min_length=1, max_length=200)


class MemoryListInput(BaseModel):
    """Input model for listing memory entries."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    category: Optional[str] = Field(default=None, description="Only return entries in this category.")
    tag: Optional[str] = Field(default=None, description="Only return entries carrying this tag.")
    limit: int = Field(default=100, description="Maximum number of entries to return.", ge=1, le=1000)


class MemorySearchInput(BaseModel):
    """Input model for full-text searching across memory entries."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    query: str = Field(..., description="Substring or regex to look for in keys, values, and tags.", min_length=1, max_length=500)
    use_regex: bool = Field(default=False, description="Treat the query as a regular expression.")
    category: Optional[str] = Field(default=None, description="Restrict the search to a single category.")
    limit: int = Field(default=20, description="Maximum number of matches to return.", ge=1, le=200)


class SkillListInput(BaseModel):
    """Input model for listing available skills."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    skills_dir: Optional[str] = Field(default=None, description="Override the skills directory (defaults to LMSTUDIO_AGENT_SKILLS_DIR or ./skills).")
    pattern: Optional[str] = Field(default=None, description="Optional glob pattern to filter skill names (e.g. 'trans*').")


class SkillRunInput(BaseModel):
    """Input model for invoking a skill."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(..., description="Skill name (subdirectory or file basename without extension).", min_length=1, max_length=200)
    input: str = Field(default="", description="Primary input passed to the skill as the first argument.")
    args: Dict[str, Any] = Field(default_factory=dict, description="Additional keyword arguments forwarded to the skill.")
    skills_dir: Optional[str] = Field(default=None, description="Override the skills directory.")
    timeout: float = Field(default=DEFAULT_TIMEOUT, description="Maximum execution time in seconds.", ge=1.0, le=600.0)


# ---------------------------------------------------------------------------
# Search engine implementations
# ---------------------------------------------------------------------------

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


async def _search_duckduckgo(query: str, num_results: int, region: Optional[str]) -> List[Dict[str, Optional[str]]]:
    """Search via DuckDuckGo HTML interface."""
    search_url = "https://html.duckduckgo.com/html/"
    payload: Dict[str, str] = {"q": query}
    if region:
        payload["kl"] = region

    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
        response = await client.post(search_url, data=payload, headers=_HEADERS)
        response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    results: List[Dict[str, Optional[str]]] = []

    for result in soup.select(".result"):
        title_tag = result.select_one(".result__a")
        snippet_tag = result.select_one(".result__snippet")
        if not title_tag:
            continue

        results.append({
            "title": title_tag.get_text(strip=True),
            "url": title_tag.get("href"),
            "snippet": snippet_tag.get_text(strip=True) if snippet_tag else "",
        })
        if len(results) >= num_results:
            break

    if not results:
        for link in soup.select("a.result__a"):
            title = link.get_text(strip=True)
            href = link.get("href")
            if title and href:
                results.append({"title": title, "url": href, "snippet": ""})
            if len(results) >= num_results:
                break

    return results


async def _search_bing(query: str, num_results: int, region: Optional[str]) -> List[Dict[str, Optional[str]]]:
    """Search via Bing HTML interface."""
    search_url = "https://www.bing.com/search"
    params: Dict[str, str] = {"q": query, "count": str(min(num_results, 50))}
    if region:
        params["cc"] = region

    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
        response = await client.get(search_url, params=params, headers=_HEADERS)
        response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    results: List[Dict[str, Optional[str]]] = []

    for li in soup.select("li.b_algo"):
        title_tag = li.select_one("h2 a")
        snippet_tag = li.select_one("p, .b_caption p")
        if not title_tag:
            continue

        results.append({
            "title": title_tag.get_text(strip=True),
            "url": title_tag.get("href"),
            "snippet": snippet_tag.get_text(strip=True) if snippet_tag else "",
        })
        if len(results) >= num_results:
            break

    return results


async def _search_google(query: str, num_results: int, region: Optional[str]) -> List[Dict[str, Optional[str]]]:
    """Search via Google HTML interface (limited results due to anti-scraping)."""
    search_url = "https://www.google.com/search"
    params: Dict[str, str] = {"q": query, "num": str(min(num_results, 20))}
    if region:
        params["hl"] = region

    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
        response = await client.get(search_url, params=params, headers=_HEADERS)
        response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    results: List[Dict[str, Optional[str]]] = []

    for div in soup.select("div.g"):
        title_tag = div.select_one("h3")
        snippet_tag = div.select_one("div.VwiC3b, div[style*='-webkit-line-clamp']")
        link_tag = div.select_one("a")
        if not title_tag:
            continue

        url = link_tag.get("href", "") if link_tag else ""
        if url.startswith("/url?q="):
            from urllib.parse import parse_qs, urlparse
            parsed = urlparse(url)
            qs = parse_qs(parsed.query)
            url = qs.get("q", [""])[0]

        results.append({
            "title": title_tag.get_text(strip=True),
            "url": url,
            "snippet": snippet_tag.get_text(strip=True) if snippet_tag else "",
        })
        if len(results) >= num_results:
            break

    return results


async def _search_baidu(query: str, num_results: int, region: Optional[str]) -> List[Dict[str, Optional[str]]]:
    """Search via Baidu HTML interface."""
    search_url = "https://www.baidu.com/s"
    params: Dict[str, str] = {"wd": query, "rn": str(min(num_results, 50))}

    baidu_headers = _HEADERS.copy()
    baidu_headers["Accept-Language"] = region or "zh-CN,zh;q=0.9"

    async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
        response = await client.get(search_url, params=params, headers=baidu_headers)
        response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    results: List[Dict[str, Optional[str]]] = []

    for div in soup.select("div.result, div.result-op"):
        title_tag = div.select_one("h3 a, h3.t a")
        snippet_tag = div.select_one("span.c-abstract, div.c-abstract")
        if not title_tag:
            continue

        results.append({
            "title": title_tag.get_text(strip=True),
            "url": title_tag.get("href"),
            "snippet": snippet_tag.get_text(strip=True) if snippet_tag else "",
        })
        if len(results) >= num_results:
            break

    return results


_ENGINE_MAP = {
    SearchEngine.DUCKDUCKGO: _search_duckduckgo,
    SearchEngine.BING: _search_bing,
    SearchEngine.GOOGLE: _search_google,
    SearchEngine.BAIDU: _search_baidu,
}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool(
    name="agent_read_file",
    annotations={
        "title": "Read File",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def agent_read_file(params: ReadFileInput) -> str:
    """Read the contents of a text file.

    Returns a JSON object with the file path, requested offset/limit, and the
    lines that were read. Large files are capped at MAX_FILE_SIZE bytes.

    Args:
        params (ReadFileInput): Validated read parameters.

    Returns:
        str: JSON string with keys 'success', 'path', 'offset', 'limit',
             'total_lines', 'truncated', and 'content'.
    """
    try:
        target = _resolve_path(params.path)

        if not target.exists():
            return _format_error("File not found.", str(target))
        if not target.is_file():
            return _format_error("Path is not a file.", str(target))

        size = target.stat().st_size
        if size > MAX_FILE_SIZE:
            return _format_error(
                f"File is too large to read ({size} bytes > {MAX_FILE_SIZE} bytes).",
                str(target),
            )

        try:
            text = target.read_text(encoding=params.encoding)
        except UnicodeDecodeError as exc:
            return _format_error(
                f"Could not decode file with encoding '{params.encoding}'. Try a different encoding.",
                str(exc),
            )

        lines = text.splitlines()
        total_lines = len(lines)
        start = min(params.offset, total_lines)
        end = total_lines if params.limit is None else min(start + params.limit, total_lines)
        selected = lines[start:end]

        return _format_success(
            {
                "path": str(target),
                "offset": start,
                "limit": params.limit,
                "total_lines": total_lines,
                "truncated": end < total_lines,
                "content": "\n".join(selected),
            }
        )
    except PermissionError as exc:
        return _format_error("Permission denied reading file.", str(exc))
    except OSError as exc:
        return _format_error("Failed to read file.", str(exc))


@mcp.tool(
    name="agent_write_file",
    annotations={
        "title": "Write File",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def agent_write_file(params: WriteFileInput) -> str:
    """Write text content to a file, optionally creating parent directories.

    Args:
        params (WriteFileInput): Validated write parameters.

    Returns:
        str: JSON string with keys 'success', 'path', 'bytes_written', and
             'operation' ('append' or 'overwrite').
    """
    try:
        target = _resolve_path(params.path)

        if params.create_dirs:
            target.parent.mkdir(parents=True, exist_ok=True)

        target.write_text(params.content, encoding=params.encoding)

        return _format_success(
            {
                "path": str(target),
                "bytes_written": len(params.content.encode(params.encoding)),
                "operation": "append" if params.append else "overwrite",
            }
        )
    except PermissionError as exc:
        return _format_error("Permission denied writing file.", str(exc))
    except OSError as exc:
        return _format_error("Failed to write file.", str(exc))


@mcp.tool(
    name="agent_execute_command",
    annotations={
        "title": "Execute Terminal Command",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def agent_execute_command(params: ExecuteCommandInput) -> str:
    """Execute a terminal command and return stdout, stderr, and exit code.

    The command runs in a subprocess. By default it is executed through the
    system shell so that pipes, redirections, and environment variables work.
    Use caution with untrusted input to avoid command injection.

    Args:
        params (ExecuteCommandInput): Validated command parameters.

    Returns:
        str: JSON string with keys 'success', 'command', 'exit_code',
             'stdout', 'stderr', and 'timed_out'.
    """
    cwd = _resolve_path(params.working_directory) if params.working_directory else None

    env = os.environ.copy()
    if params.env:
        env.update(params.env)

    try:
        if params.shell:
            proc = await asyncio.create_subprocess_shell(
                params.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *shlex.split(params.command),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=params.timeout
            )
            timed_out = False
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            stdout_bytes, stderr_bytes = await proc.communicate()
            timed_out = True

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        return _format_success(
            {
                "command": params.command,
                "exit_code": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "timed_out": timed_out,
            }
        )
    except FileNotFoundError as exc:
        return _format_error("Command not found.", str(exc))
    except PermissionError as exc:
        return _format_error("Permission denied executing command.", str(exc))
    except OSError as exc:
        return _format_error("Failed to execute command.", str(exc))


@mcp.tool(
    name="agent_web_search",
    annotations={
        "title": "Web Search",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def agent_web_search(params: WebSearchInput) -> str:
    """Search the web using the specified search engine and return results.

    Supported engines: duckduckgo (default), bing, google, baidu.
    Each result contains a title, URL, and short snippet. No API key is required.

    Args:
        params (WebSearchInput): Validated search parameters including engine selection.

    Returns:
        str: JSON string with keys 'success', 'query', 'engine', and 'results'.
    """
    search_fn = _ENGINE_MAP.get(params.engine)
    if not search_fn:
        return _format_error(f"Unsupported search engine: {params.engine}. Choose from: {', '.join(e.value for e in SearchEngine)}")

    try:
        results = await search_fn(params.query, params.num_results, params.region)
        return _format_success(
            {
                "query": params.query,
                "engine": params.engine.value,
                "count": len(results),
                "results": results,
            }
        )
    except httpx.HTTPStatusError as exc:
        return _format_error(
            f"Search request failed with status {exc.response.status_code}.",
            str(exc),
        )
    except httpx.RequestError as exc:
        return _format_error("Network error while performing search.", str(exc))
    except Exception as exc:
        return _format_error("Unexpected error while parsing search results.", str(exc))


# ---------------------------------------------------------------------------
# Memory & skill helpers
# ---------------------------------------------------------------------------

def _load_memory() -> Dict[str, Dict[str, Any]]:
    """Load the entire memory store from disk. Returns {} when missing or corrupt."""
    if not MEMORY_FILE.exists():
        return {}
    try:
        with MEMORY_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        return data
    except (json.JSONDecodeError, OSError):
        return {}


def _save_memory(data: Dict[str, Dict[str, Any]]) -> None:
    """Atomically persist the memory store to disk."""
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = MEMORY_FILE.with_suffix(MEMORY_FILE.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp_path, MEMORY_FILE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_tags(tags: List[str]) -> List[str]:
    seen: List[str] = []
    for raw in tags:
        if not isinstance(raw, str):
            continue
        t = raw.strip()
        if t and t not in seen:
            seen.append(t)
    return seen


def _entry_summary(key: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Return a compact view of a memory entry (value may be truncated for listings)."""
    value = entry.get("value", "")
    snippet = value if len(value) <= 200 else value[:200] + "..."
    return {
        "key": key,
        "category": entry.get("category", "general"),
        "tags": entry.get("tags", []),
        "snippet": snippet,
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
    }


# ---------------------------------------------------------------------------
# Skill discovery
# ---------------------------------------------------------------------------

def _resolve_skills_dir(override: Optional[str]) -> Path:
    base = Path(override).expanduser().resolve() if override else SKILLS_DIR
    return base


def _read_description(path: Path) -> str:
    """Read the first non-empty paragraph from a SKILL.md (or .md skill) file."""
    if not path.exists() or not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        cleaned = paragraph.strip()
        if cleaned:
            return cleaned[:500]
    return ""


def _discover_skills(skills_dir: Path) -> List[Dict[str, Any]]:
    """Walk a skills directory and produce a list of skill metadata records.

    A skill is either:
      - a subdirectory containing `main.py` (or `run.py`) with a callable
        `run(input, **kwargs)` function, plus an optional `SKILL.md`, or
      - a single `.py` / `.sh` / `.md` file at the top level.
    """
    if not skills_dir.exists() or not skills_dir.is_dir():
        return []

    skills: List[Dict[str, Any]] = []

    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        entry: Optional[Dict[str, Any]] = None
        for candidate in ("main.py", "run.py"):
            script = child / candidate
            if script.is_file():
                entry = {
                    "name": child.name,
                    "type": "python",
                    "path": str(script),
                    "description": _read_description(child / "SKILL.md"),
                }
                break
        if entry is not None:
            skills.append(entry)

    for child in sorted(skills_dir.iterdir()):
        if not child.is_file() or child.name.startswith("."):
            continue
        suffix = child.suffix.lower()
        if suffix == ".py":
            kind = "python"
        elif suffix == ".sh":
            kind = "shell"
        elif suffix == ".md":
            kind = "markdown"
        else:
            continue
        skills.append({
            "name": child.stem,
            "type": kind,
            "path": str(child),
            "description": _read_description(child) if suffix == ".md" else "",
        })

    return skills


def _invoke_python_skill(skill_path: Path, primary_input: str, extra_args: Dict[str, Any]) -> str:
    """Import a Python skill module and call its `run(input, **kwargs)` function."""
    spec = importlib.util.spec_from_file_location(f"skill_{uuid.uuid4().hex}", skill_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load Python skill from {skill_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)

    run_fn: Optional[Callable[..., Any]] = getattr(module, "run", None)
    if run_fn is None or not callable(run_fn):
        raise RuntimeError(f"Skill {skill_path} does not define a callable `run` function")

    sig = inspect.signature(run_fn)
    accepted: Dict[str, inspect.Parameter] = {
        name: param
        for name, param in sig.parameters.items()
        if param.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    call_kwargs = {k: v for k, v in extra_args.items() if k in accepted and k != "input"}

    if "input" in accepted or any(
        p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD for p in accepted.values()
    ):
        result = run_fn(primary_input, **call_kwargs)
    else:
        result = run_fn(**call_kwargs)
    return "" if result is None else str(result)


async def _invoke_skill(
    skill: Dict[str, Any],
    primary_input: str,
    extra_args: Dict[str, Any],
    timeout: float,
) -> Dict[str, Any]:
    """Dispatch execution based on the skill type and return a result record."""
    path = Path(skill["path"])
    skill_type = skill["type"]

    if skill_type == "python":
        try:
            stdout = await asyncio.wait_for(
                asyncio.to_thread(_invoke_python_skill, path, primary_input, extra_args),
                timeout=timeout,
            )
            return {"stdout": stdout, "stderr": "", "exit_code": 0, "timed_out": False}
        except asyncio.TimeoutError:
            return {"stdout": "", "stderr": f"Skill timed out after {timeout}s.", "exit_code": 124, "timed_out": True}
        except Exception as exc:
            return {"stdout": "", "stderr": f"{type(exc).__name__}: {exc}", "exit_code": 1, "timed_out": False}

    if skill_type == "shell":
        argv = [path.as_posix()]
        if primary_input:
            argv.append(primary_input)
        for key, value in extra_args.items():
            argv.append(f"--{key}")
            argv.append(str(value))
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                timed_out = False
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                stdout_b, stderr_b = await proc.communicate()
                timed_out = True
            return {
                "stdout": stdout_b.decode("utf-8", errors="replace"),
                "stderr": stderr_b.decode("utf-8", errors="replace"),
                "exit_code": proc.returncode,
                "timed_out": timed_out,
            }
        except FileNotFoundError as exc:
            return {"stdout": "", "stderr": f"Shell not found: {exc}", "exit_code": 127, "timed_out": False}

    if skill_type == "markdown":
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            return {"stdout": "", "stderr": f"Failed to read markdown: {exc}", "exit_code": 1, "timed_out": False}
        return {"stdout": content, "stderr": "", "exit_code": 0, "timed_out": False}

    return {"stdout": "", "stderr": f"Unsupported skill type: {skill_type}", "exit_code": 1, "timed_out": False}


# ---------------------------------------------------------------------------
# Memory tools
# ---------------------------------------------------------------------------

@mcp.tool(
    name="agent_memory_save",
    annotations={
        "title": "Save Memory",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def agent_memory_save(params: MemorySaveInput) -> str:
    """Persist a key/value memory entry to disk for cross-session recall.

    Each entry stores: key, value, category, tags, created_at, updated_at.
    Memory is kept in a JSON file and is safe to read concurrently.

    Args:
        params (MemorySaveInput): Validated save parameters.

    Returns:
        str: JSON with keys 'success', 'key', 'category', 'tags',
             'operation' ('created' or 'updated').
    """
    tags = _normalize_tags(params.tags)
    with _memory_lock:
        store = _load_memory()
        existed = params.key in store
        if existed and not params.overwrite:
            return _format_error(
                f"Memory key '{params.key}' already exists. Set overwrite=true to replace it."
            )
        now = _now_iso()
        existing = store.get(params.key) or {}
        store[params.key] = {
            "value": params.value,
            "category": params.category or "general",
            "tags": tags,
            "created_at": existing.get("created_at", now),
            "updated_at": now,
        }
        try:
            _save_memory(store)
        except OSError as exc:
            return _format_error("Failed to persist memory.", str(exc))
    return _format_success(
        {
            "key": params.key,
            "category": params.category or "general",
            "tags": tags,
            "operation": "updated" if existed else "created",
            "memory_file": str(MEMORY_FILE),
        }
    )


@mcp.tool(
    name="agent_memory_load",
    annotations={
        "title": "Load Memory",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def agent_memory_load(params: MemoryLoadInput) -> str:
    """Load a single memory entry by key.

    Args:
        params (MemoryLoadInput): Validated load parameters.

    Returns:
        str: JSON with keys 'success', 'key', and the full entry payload.
    """
    with _memory_lock:
        store = _load_memory()
    entry = store.get(params.key)
    if entry is None:
        return _format_error(f"Memory key '{params.key}' not found.")
    return _format_success({"key": params.key, **entry})


@mcp.tool(
    name="agent_memory_list",
    annotations={
        "title": "List Memories",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def agent_memory_list(params: MemoryListInput) -> str:
    """List memory entries, optionally filtered by category and/or tag.

    Args:
        params (MemoryListInput): Validated list parameters.

    Returns:
        str: JSON with keys 'success', 'count', and 'entries'.
    """
    with _memory_lock:
        store = _load_memory()

    entries: List[Dict[str, Any]] = []
    for key, entry in store.items():
        if params.category and entry.get("category") != params.category:
            continue
        if params.tag and params.tag not in (entry.get("tags") or []):
            continue
        entries.append(_entry_summary(key, entry))
        if len(entries) >= params.limit:
            break

    entries.sort(key=lambda e: e.get("updated_at") or "", reverse=True)
    return _format_success({"count": len(entries), "entries": entries, "memory_file": str(MEMORY_FILE)})


@mcp.tool(
    name="agent_memory_delete",
    annotations={
        "title": "Delete Memory",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def agent_memory_delete(params: MemoryDeleteInput) -> str:
    """Delete a single memory entry by key.

    Args:
        params (MemoryDeleteInput): Validated delete parameters.

    Returns:
        str: JSON with keys 'success', 'key', and 'deleted' (bool).
    """
    with _memory_lock:
        store = _load_memory()
        if params.key not in store:
            return _format_error(f"Memory key '{params.key}' not found.")
        del store[params.key]
        try:
            _save_memory(store)
        except OSError as exc:
            return _format_error("Failed to persist memory after delete.", str(exc))
    return _format_success({"key": params.key, "deleted": True})


@mcp.tool(
    name="agent_memory_search",
    annotations={
        "title": "Search Memories",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def agent_memory_search(params: MemorySearchInput) -> str:
    """Search memory entries by substring or regex across key, value, and tags.

    Args:
        params (MemorySearchInput): Validated search parameters.

    Returns:
        str: JSON with keys 'success', 'count', and 'matches'.
    """
    matcher: Callable[[str], bool]
    if params.use_regex:
        try:
            pattern = re.compile(params.query, re.IGNORECASE)
        except re.error as exc:
            return _format_error("Invalid regular expression.", str(exc))
        matcher = lambda text: bool(pattern.search(text))
    else:
        needle = params.query.lower()
        matcher = lambda text: needle in text.lower()

    with _memory_lock:
        store = _load_memory()

    matches: List[Dict[str, Any]] = []
    for key, entry in store.items():
        if params.category and entry.get("category") != params.category:
            continue
        haystack = " ".join([
            key,
            str(entry.get("value", "")),
            " ".join(entry.get("tags") or []),
        ])
        if matcher(haystack):
            matches.append(_entry_summary(key, entry))
            if len(matches) >= params.limit:
                break

    return _format_success(
        {"query": params.query, "regex": params.use_regex, "count": len(matches), "matches": matches}
    )


# ---------------------------------------------------------------------------
# Skill tools
# ---------------------------------------------------------------------------

@mcp.tool(
    name="agent_list_skills",
    annotations={
        "title": "List Skills",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def agent_list_skills(params: SkillListInput) -> str:
    """Discover and list skills available in the configured skills directory.

    A skill is one of:
      - `<skills_dir>/<name>/main.py` (or `run.py`) with a `run(input, **kwargs)` function
      - `<skills_dir>/<name>.py` — Python skill
      - `<skills_dir>/<name>.sh` — Shell skill
      - `<skills_dir>/<name>.md` — Markdown skill (returns the file contents)

    Args:
        params (SkillListInput): Validated list parameters.

    Returns:
        str: JSON with keys 'success', 'skills_dir', 'count', and 'skills'.
    """
    base = _resolve_skills_dir(params.skills_dir)
    if not base.exists():
        return _format_error(
            f"Skills directory does not exist: {base}. Create it or set LMSTUDIO_AGENT_SKILLS_DIR."
        )
    if not base.is_dir():
        return _format_error(f"Skills path is not a directory: {base}")

    discovered = _discover_skills(base)
    if params.pattern:
        discovered = [s for s in discovered if fnmatch.fnmatch(s["name"], params.pattern)]

    return _format_success(
        {"skills_dir": str(base), "count": len(discovered), "skills": discovered}
    )


@mcp.tool(
    name="agent_run_skill",
    annotations={
        "title": "Run Skill",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def agent_run_skill(params: SkillRunInput) -> str:
    """Invoke a discovered skill by name and return its result.

    Python skills are imported in-process and their `run(input, **kwargs)`
    function is called. Shell skills are executed via `subprocess`. Markdown
    skills simply return the file contents.

    Args:
        params (SkillRunInput): Validated skill invocation parameters.

    Returns:
        str: JSON with keys 'success', 'name', 'type', 'stdout', 'stderr',
             'exit_code', and 'timed_out'.
    """
    base = _resolve_skills_dir(params.skills_dir)
    if not base.exists() or not base.is_dir():
        return _format_error(
            f"Skills directory does not exist: {base}. Create it or set LMSTUDIO_AGENT_SKILLS_DIR."
        )

    discovered = _discover_skills(base)
    skill = next((s for s in discovered if s["name"] == params.name), None)
    if skill is None:
        names = ", ".join(s["name"] for s in discovered) or "(none)"
        return _format_error(
            f"Skill '{params.name}' not found in {base}. Available: {names}"
        )

    result = await _invoke_skill(skill, params.input, params.args, params.timeout)
    success = result["exit_code"] == 0 and not result["timed_out"]
    payload: Dict[str, Any] = {
        "name": skill["name"],
        "type": skill["type"],
        "path": skill["path"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
        "exit_code": result["exit_code"],
        "timed_out": result["timed_out"],
    }
    return _format_success(payload) if success else _format_error(
        f"Skill '{skill['name']}' failed with exit code {result['exit_code']}.",
        detail=result["stderr"] or result["stdout"],
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
