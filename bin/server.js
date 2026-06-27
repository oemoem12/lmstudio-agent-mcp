#!/usr/bin/env node
'use strict';

const { spawn } = require('child_process');
const path = require('path');

const PYTHON_BIN = process.env.LMSTUDIO_AGENT_PYTHON || 'python3';
const SERVER_SCRIPT = path.join(__dirname, '..', 'server.py');

const proc = spawn(PYTHON_BIN, [SERVER_SCRIPT], {
  stdio: 'inherit',
  env: process.env,
});

proc.on('error', (err) => {
  if (err.code === 'ENOENT') {
    console.error(
      'Error: Python is not found at "' + PYTHON_BIN + '".\n' +
      'Please install Python 3.10+ and set LMSTUDIO_AGENT_PYTHON if needed.\n' +
      'Then run: npm run setup'
    );
  } else {
    console.error('Error starting MCP server:', err.message);
  }
  process.exit(1);
});

proc.on('exit', (code) => {
  process.exit(code ?? 1);
});
