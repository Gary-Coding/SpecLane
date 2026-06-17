#!/usr/bin/env node
const { spawnSync } = require('child_process')
const path = require('path')

const root = path.resolve(__dirname, '..')
const args = process.argv.slice(2)

runPython(path.join(root, 'scripts', 'speclane-cli.py'), args)

function runPython(script, rest) {
  const candidates = process.platform === 'win32' ? ['python', 'python3'] : ['python3', 'python']
  for (const bin of candidates) {
    const result = spawnSync(bin, [script, ...rest], { stdio: 'inherit' })
    if (result.error && result.error.code === 'ENOENT') continue
    process.exit(result.status || 0)
  }
  console.error('Python 3 is required but was not found in PATH.')
  process.exit(1)
}
