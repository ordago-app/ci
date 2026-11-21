// Tests for scripts/pr-land-bg.sh against a real, private tmux server.
//
// A tmux server keeps the working directory it started in for good. Once that
// directory is deleted (a worktree reaped by its own lander), `-c` no longer
// reaches new panes, and every lander started from that server dies on
// `uv_cwd: ENOENT` before it opens a PR.
//
// Run with: node --test scripts/__tests__/pr-land-bg.test.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SCRIPT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'pr-land-bg.sh');
const hasTmux = spawnSync('tmux', ['-V']).status === 0;

test('lands from the worktree even when the tmux server lost its own directory', { skip: !hasTmux }, async (t) => {
  const base = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'pr-land-bg-')));
  const socketDir = path.join(base, 'tmux');
  const bin = path.join(base, 'bin');
  const repo = path.join(base, 'repo');
  const gone = path.join(base, 'gone');
  const seen = path.join(base, 'seen');
  for (const dir of [socketDir, bin, repo, gone]) fs.mkdirSync(dir);

  const { TMUX: _ignored, ...parentEnv } = process.env;
  const env = { ...parentEnv, TMUX_TMPDIR: socketDir, PATH: `${bin}:${process.env.PATH}` };
  t.after(() => {
    spawnSync('tmux', ['kill-server'], { env });
    fs.rmSync(base, { recursive: true, force: true });
  });

  fs.writeFileSync(path.join(bin, 'pnpm'), `#!/bin/sh\npwd -P > '${seen}'\n`, { mode: 0o755 });
  spawnSync('git', ['init', '-q'], { cwd: repo });
  spawnSync('git', ['-c', 'user.email=t@x', '-c', 'user.name=t', 'commit', '-q', '--allow-empty', '-m', 'x'], { cwd: repo });

  assert.equal(spawnSync('tmux', ['new-session', '-d', '-s', 'keeper'], { cwd: gone, env }).status, 0);
  fs.rmdirSync(gone);

  const run = spawnSync('bash', [SCRIPT], { cwd: repo, env, encoding: 'utf8' });
  assert.equal(run.status, 0, run.stderr);

  const deadline = Date.now() + 10_000;
  while (!fs.existsSync(seen) && Date.now() < deadline) await new Promise((r) => setTimeout(r, 100));
  assert.equal(fs.readFileSync(seen, 'utf8').trim(), repo);
});
