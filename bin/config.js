#!/usr/bin/env node
'use strict';

// Print (and optionally write) the LM Studio MCP config snippet via the
// installed Python package. Passes through any CLI flags.

const { spawnSync } = require('child_process');
const PYTHON_BIN = process.env.LMSTUDIO_AGENT_PYTHON || 'python3';

const result = spawnSync(PYTHON_BIN, ['-m', 'lmstudio_agent_mcp', 'config', ...process.argv.slice(2)], {
  stdio: 'inherit',
  env: process.env,
});

process.exit(result.status ?? 1);
