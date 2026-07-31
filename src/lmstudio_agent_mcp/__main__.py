"""Allow ``python -m lmstudio_agent_mcp ...`` to invoke the CLI."""

from .cli import main

raise SystemExit(main())
