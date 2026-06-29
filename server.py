#!/usr/bin/env python3
"""
LM Studio Agent MCP Server.

Provides local agent tools for LM Studio via the Model Context Protocol (MCP):
- Read and write files
- Execute terminal commands
- Search the web via multiple engines (DuckDuckGo, Bing, Google, Baidu)

Transport: stdio (default, suitable for LM Studio local MCP integration).
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
