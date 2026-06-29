# Agent MCP Server for LM Studio

A lightweight MCP (Model Context Protocol) server that provides local agent capabilities for LM Studio: file I/O, terminal execution, and web search.

## Features

| Tool | Description |
|---|---|
| `agent_read_file` | Read text files with offset/limit pagination and encoding support |
| `agent_write_file` | Write or append to files, with optional parent directory creation |
| `agent_execute_command` | Execute shell commands with pipes, redirections, custom working directory, environment variables, and configurable timeout |
| `agent_web_search` | Search the web via DuckDuckGo, Bing, Google, or Baidu (switchable), returning titles, URLs, and snippets |

## Requirements

- Python 3.10+
- Dependencies listed in `requirements.txt`

## Installation

```bash
cd lmstudio_agent_mcp
pip install -r requirements.txt
```

## Usage with LM Studio

Add the following to your LM Studio MCP server configuration:

```json
{
  "mcpServers": {
    "agent_mcp": {
      "command": "python3",
      "args": ["/absolute/path/to/lmstudio_agent_mcp/server.py"]
    }
  }
}
```

If you are using a virtual environment, replace `python3` with the absolute path to your venv's Python binary (e.g. `/path/to/venv/bin/python`).

## Usage with Other MCP Clients

The server uses **stdio** transport by default. Start it directly:

```bash
python3 server.py
```

For remote access, you can switch to streamable HTTP:

```python
# Add to the bottom of server.py
if __name__ == "__main__":
    mcp.run(transport="streamable_http", port=8000)
```

## Tool Reference

### agent_read_file

Read the contents of a text file.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | string | *(required)* | Absolute or relative path to the file |
| `offset` | int | `0` | Number of lines to skip from the beginning |
| `limit` | int \| null | `200` | Maximum number of lines to return (`null` = unlimited) |
| `encoding` | string | `"utf-8"` | Text encoding |

### agent_write_file

Write text content to a file.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `path` | string | *(required)* | Absolute or relative path to the file |
| `content` | string | *(required)* | Text content to write |
| `encoding` | string | `"utf-8"` | Text encoding |
| `append` | bool | `false` | If `true`, append instead of overwrite |
| `create_dirs` | bool | `true` | If `true`, create parent directories when missing |

### agent_execute_command

Execute a terminal command.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `command` | string | *(required)* | Shell command to execute |
| `working_directory` | string \| null | `null` | Working directory (defaults to server cwd) |
| `timeout` | float | `60.0` | Maximum execution time in seconds (1-600) |
| `env` | object \| null | `null` | Additional environment variables to set |
| `shell` | bool | `true` | Execute through system shell (required for pipes/redirects) |

### agent_web_search

Search the web using multiple search engines.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | *(required)* | Search query (1-500 chars) |
| `engine` | string | `"duckduckgo"` | Search engine: `duckduckgo`, `bing`, `google`, or `baidu` |
| `num_results` | int | `5` | Maximum results to return (1-20) |
| `region` | string \| null | `null` | Region/locale code (e.g. `wt-wt`, `us-en`, `zh-cn`) |

## Security Notes

- File paths are resolved to absolute paths; `~` expansion is supported
- Large files (>10 MiB) are rejected to prevent memory exhaustion
- Command execution has a configurable timeout (max 600s)
- **Do not expose this server to untrusted clients** — `agent_execute_command` can run arbitrary shell commands

## License

MIT
