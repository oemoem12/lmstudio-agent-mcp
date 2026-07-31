#!/usr/bin/env node
'use strict';

const { spawn } = require('child_process');
const path = require('path');

const PYTHON_BIN = process.env.LMSTUDIO_AGENT_PYTHON || 'python3';

// Run the installed package as a module so the same code path is used as a
// direct `python -m lmstudio_agent_mcp` invocation. This keeps the npm wrapper
// in sync with `pip install` and avoids shipping a duplicate server script.
const args = ['-m', 'lmstudio_agent_mcp', 'serve'];

const proc = spawn(PYTHON_BIN, args, {
  stdio: 'inherit',
  env: process.env,
});

proc.on('error', (err) => {
  if (err.code === 'ENOENT') {
    console.error(
      'Error: Python is not found at "' + PYTHON_BIN + '".\n' +
      'Please install Python 3.10+ and set LMSTUDIO_AGENT_PYTHON if needed.\n' +
      'Then run: pip install lmstudio-agent-mcp'
    );
  } else {
    console.error('Error starting MCP server:', err.message);
  }
  process.exit(1);
});

proc.on('exit', (code) => {
  process.exit(code ?? 1);
});
