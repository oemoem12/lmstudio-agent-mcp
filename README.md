# Agent MCP Server for LM Studio

A lightweight MCP (Model Context Protocol) server that provides local agent capabilities for LM Studio: file I/O, terminal execution, multi-engine web search, persistent key/value memory, and a pluggable skill system.

- 📦 **[PyPI](https://pypi.org/project/lmstudio-agent-mcp/)** — `pip install lmstudio-agent-mcp`
- 🔌 **Auto-registers with LM Studio** — no manual config editing required
- 🛠 11 tools, ~14 kB wheel, no heavy dependencies

## Features

| Tool | Description |
|---|---|
| `agent_read_file` | Read text files with offset/limit pagination and encoding support |
| `agent_write_file` | Write or append to files, with optional parent directory creation |
| `agent_execute_command` | Execute shell commands with pipes, redirections, custom working directory, environment variables, and configurable timeout |
| `agent_web_search` | Search the web via DuckDuckGo, Bing, Google, or Baidu (switchable), returning titles, URLs, and snippets |
| `agent_memory_save` | Persist a key/value memory entry with category and tags |
| `agent_memory_load` | Load a single memory entry by key |
| `agent_memory_list` | List memory entries, optionally filtered by category/tag |
| `agent_memory_delete` | Delete a single memory entry |
| `agent_memory_search` | Full-text search across key, value, and tags (substring or regex) |
| `agent_list_skills` | Discover skills available in the configured skills directory |
| `agent_run_skill` | Invoke a discovered skill (Python, shell, or markdown) |

## Requirements

- Python 3.10+
- Dependencies listed in `requirements.txt`

## Installation

```bash
pip install lmstudio-agent-mcp
```

After installation, three console scripts are available:

| Script | Purpose |
|---|---|
| `lmstudio-agent-mcp` | Start the MCP server (`serve` is the default subcommand) |
| `lmstudio-mcp-setup` | Register this server with LM Studio's `mcp.json` |
| `lmstudio-mcp-config` | Print or write the LM Studio MCP config snippet |

## Auto-registration with LM Studio

The package registers itself with LM Studio automatically — no copy/paste required.

1. **Editable / source install** — a setuptools `cmdclass` hook appends an
   `agent_mcp` entry to `~/.lmstudio/mcp.json` immediately after install.
2. **Wheel install (e.g. from PyPI)** — the first time `lmstudio-agent-mcp`
   starts it writes the entry silently. To trigger the registration right
   after install run:

   ```bash
   lmstudio-mcp-setup
   ```

The function is idempotent: re-running it is a no-op. Pass `--force` to
overwrite an existing entry. To opt out, set the environment variable
`LMSTUDIO_AGENT_NO_AUTOREGISTER=1` before starting the server.

A marker file `~/.lmstudio/.lmstudio_agent_mcp_installed` is written next to
`mcp.json` so the registration is not repeated unnecessarily. The generated
snippet uses the installed `lmstudio-agent-mcp` console script as the command
so no Python interpreter path is baked in:

```json
{
  "mcpServers": {
    "agent_mcp": {
      "command": "/home/<you>/.local/bin/lmstudio-agent-mcp"
    }
  }
}
```

To print or write the config snippet manually:

```bash
lmstudio-mcp-config
# with overrides:
lmstudio-mcp-config --python /path/to/python --skills-dir ~/my_skills --memory-file ~/my_memory.json
# write directly (merges into existing mcp.json if present):
lmstudio-mcp-config --write ~/.lmstudio/mcp.json
# use the python module form instead of the console script:
lmstudio-mcp-config --no-console-script
```

### Other install methods

```bash
# editable install (development)
git clone https://github.com/oemoem12/lmstudio-agent-mcp.git
cd lmstudio-agent-mcp
pip install -e .

# npm wrapper (thin shell around the Python package)
npm install -g lmstudio-agent-mcp
```

## Usage with LM Studio

Restart LM Studio after running `lmstudio-mcp-setup`. The server will appear in
the MCP list as `agent_mcp`. The CLI also generates a JSON snippet for
`mcp.json` automatically; if you prefer to add it by hand:

```json
{
  "mcpServers": {
    "agent_mcp": {
      "command": "/usr/bin/python3",
      "args": ["-m", "lmstudio_agent_mcp"]
    }
  }
}
```

The CLI automatically detects the current Python interpreter. Use `--python` to
override it (e.g. for a virtualenv) and `--skills-dir` / `--memory-file` to
customize where the server looks for skills and where it stores memory.

## Usage with Other MCP Clients

The server uses **stdio** transport by default. Start it directly:

```bash
python3 -m lmstudio_agent_mcp
```

For remote access, switch to streamable HTTP:

```python
import lmstudio_agent_mcp
lmstudio_agent_mcp.mcp.run(transport="streamable_http", port=8000)
```

## Configuration

The server reads the following environment variables on startup:

| Variable | Default | Purpose |
|---|---|---|
| `LMSTUDIO_AGENT_MEMORY_FILE` | `~/.lmstudio_agent_mcp/memory.json` | Path to the persistent memory store |
| `LMSTUDIO_AGENT_SKILLS_DIR` | `~/.agents/skills/` | Directory scanned for user-defined skills |
| `LMSTUDIO_AGENT_NO_AUTOREGISTER` | `0` | Set to `1` to disable silent autoregistration on first serve |

The skills directory also accepts `SKILL.md` (with optional `scripts/`,
`reference/`, etc. siblings) in addition to `main.py` / `run.py` directories.

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

### agent_memory_save

Persist a key/value memory entry to disk for cross-session recall.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `key` | string | *(required)* | Unique identifier (1-200 chars) |
| `value` | string | *(required)* | Content to remember |
| `category` | string | `"general"` | Logical bucket for filtering |
| `tags` | string[] | `[]` | Tags for retrieval filtering |
| `overwrite` | bool | `true` | If `false`, fail when key already exists |

### agent_memory_load

Load a single memory entry by key.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `key` | string | *(required)* | Key of the entry to load |

### agent_memory_list

List memory entries, optionally filtered by category and/or tag.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `category` | string \| null | `null` | Restrict to one category |
| `tag` | string \| null | `null` | Restrict to entries carrying this tag |
| `limit` | int | `100` | Maximum entries to return (1-1000) |

### agent_memory_delete

Delete a single memory entry.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `key` | string | *(required)* | Key of the entry to delete |

### agent_memory_search

Full-text search across key, value, and tags.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | *(required)* | Substring or regex to search for (1-500 chars) |
| `use_regex` | bool | `false` | Treat the query as a regular expression |
| `category` | string \| null | `null` | Restrict the search to one category |
| `limit` | int | `20` | Maximum matches to return (1-200) |

### agent_list_skills

Discover skills available in the configured skills directory.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `skills_dir` | string \| null | `null` | Override the skills directory |
| `pattern` | string \| null | `null` | Glob pattern to filter skill names (e.g. `trans*`) |

### agent_run_skill

Invoke a discovered skill by name.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `name` | string | *(required)* | Skill name (subdirectory or filename without extension) |
| `input` | string | `""` | Primary input passed as the first argument |
| `args` | object | `{}` | Additional keyword arguments forwarded to the skill |
| `skills_dir` | string \| null | `null` | Override the skills directory |
| `timeout` | float | `60.0` | Maximum execution time in seconds (1-600) |

## Writing Skills

Place skills under the directory pointed to by `LMSTUDIO_AGENT_SKILLS_DIR` (default `~/.agents/skills/`). Three skill types are supported:

### Python skill (subdirectory)

```
skills/
└── summarize/
    ├── SKILL.md        # optional description (first paragraph is used)
    └── main.py         # must define `def run(input, **kwargs)`
```

```python
# skills/summarize/main.py
def run(input: str, **kwargs) -> str:
    max_words = int(kwargs.get("max_words", 50))
    words = input.split()
    return " ".join(words[:max_words])
```

### Python skill (single file)

```python
# skills/translate.py
def run(input: str, **kwargs) -> str:
    target = kwargs.get("target", "zh")
    return f"[{target}] {input}"
```

### Shell skill

```bash
# skills/count_lines.sh  (must be executable)
#!/usr/bin/env bash
echo "Lines: $(wc -l < "$1")"
```

The `input` parameter becomes `$1`; `args` become additional positional arguments.

### Markdown skill

```markdown
<!-- skills/cheatsheet.md -->
# Cheatsheet

Useful commands ...
```

A markdown skill simply returns the file contents when invoked.

## Example: Memory + Skill Workflow

```python
# 1) Save user preferences
agent_memory_save(key="user.lang", value="zh-CN", category="user", tags=["lang"])

# 2) Later, recall them
agent_memory_load(key="user.lang")

# 3) Run a custom skill
agent_run_skill(name="summarize", input="long text ...", args={"max_words": 20})
```

## Security Notes

- File paths are resolved to absolute paths; `~` expansion is supported
- Large files (>10 MiB) are rejected to prevent memory exhaustion
- Command execution has a configurable timeout (max 600s)
- The memory file is rewritten atomically (temp file + rename) to prevent corruption
- **Do not expose this server to untrusted clients** — `agent_execute_command` and `agent_run_skill` (Python/shell) can run arbitrary code

## License

MIT
