// Tests for scripts/agent-dispatch.sh, through --dry-run and its refusals.
//
// The failures this exists to prevent are the dispatch recipe's silent ones: a
// worktree cut from a stale local `develop`, a worker launched without the
// submodule its skills and `pr:land` live in, and a second worker dispatched
// over the first one's worktree. The live tmux/claude half is not unit-testable;
// its smoke test is a real dispatch into a throwaway session.
//
// Run with: node --test scripts/__tests__/agent-dispatch-shell.test.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SCRIPT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'agent-dispatch.sh');

function fixtureRepo() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dispatch-'));
  spawnSync('git', ['init', '-q'], { cwd: root });
  spawnSync('git', ['-c', 'user.email=t@x', '-c', 'user.name=t', 'commit', '-q', '--allow-empty', '-m', 'x'], { cwd: root });
  const prompt = path.join(root, 'prompt.txt');
  fs.writeFileSync(prompt, 'do the thing\n');
  return { root, prompt };
}

const dispatch = (cwd, ...args) => spawnSync('bash', [SCRIPT, ...args], { cwd, encoding: 'utf8' });

test('a new worker is cut from a freshly fetched origin/develop, with its submodule, in its own window', () => {
  const { root, prompt } = fixtureRepo();
  try {
    const r = dispatch(root, '--dry-run', 'w1-thing', 'feat/thing', prompt);
    assert.equal(r.status, 0, r.stderr);
    const lines = r.stdout.trim().split('\n');
    const at = (re) => lines.findIndex((l) => re.test(l));
    const worktree = path.join(fs.realpathSync(root), '.claude', 'worktrees', 'w1-thing');

    assert.ok(at(/fetch --quiet origin develop/) >= 0, 'fetches first');
    assert.ok(at(/worktree add/) > at(/fetch/), 'fetch precedes the worktree');
    assert.match(lines[at(/worktree add/)], /-b feat\/thing origin\/develop$/);
    assert.ok(lines[at(/worktree add/)].includes(worktree));
    assert.ok(at(/submodule update --init/) > at(/worktree add/));
    const window = lines[at(/new-window/)];
    assert.match(window, /-n w1-thing/);
    assert.match(window, /--permission-mode auto/);
    assert.match(window, /crossSessionInbound/);
    assert.match(window, /sleep\\? 100000/, 'the window outlives the worker');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('base branch, worktrees dir and fleet session come from the repo config', () => {
  const { root, prompt } = fixtureRepo();
  try {
    fs.mkdirSync(path.join(root, '.agents'));
    fs.writeFileSync(path.join(root, '.agents', 'land.config.json'), JSON.stringify({ baseBranch: 'main' }));
    fs.writeFileSync(
      path.join(root, '.agents', 'orchestrate.config.json'),
      JSON.stringify({ project: 'myrepo', worktreesDir: 'wt' })
    );
    const r = dispatch(root, '--dry-run', 'w1-thing', 'feat/thing', prompt);
    assert.equal(r.status, 0, r.stderr);
    assert.match(r.stdout, /fetch --quiet origin main/);
    assert.match(r.stdout, new RegExp(`${path.join(fs.realpathSync(root), 'wt', 'w1-thing')} -b feat/thing origin/main`));
    assert.match(r.stdout, /new-session -d -s myrepo-fleet/);
    assert.match(r.stdout, /new-window -d -t myrepo-fleet:/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('refuses a bad name, a missing prompt, and an existing worktree', () => {
  const { root, prompt } = fixtureRepo();
  try {
    assert.notEqual(dispatch(root, '--dry-run', 'W1 Thing', 'b', prompt).status, 0);
    assert.notEqual(dispatch(root, '--dry-run', 'w1', 'b', path.join(root, 'nope.txt')).status, 0);
    fs.mkdirSync(path.join(root, '.claude', 'worktrees', 'w1-taken'), { recursive: true });
    const taken = dispatch(root, '--dry-run', 'w1-taken', 'b', prompt);
    assert.notEqual(taken.status, 0);
    assert.match(taken.stderr, /already exists — use --resume/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

/**
 * A clone whose `origin` has a `develop`, plus a fake `tmux` on PATH, so the
 * real (not dry-run) path runs: fetch, worktree add, submodule update, launch.
 */
function liveFixture() {
  const base = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'dispatch-live-')));
  const g = (cwd, ...args) => {
    const r = spawnSync('git', ['-c', 'user.email=t@x', '-c', 'user.name=t', ...args], { cwd, encoding: 'utf8' });
    assert.equal(r.status, 0, r.stderr);
  };
  g(base, 'init', '-q', '--bare', 'origin.git');
  g(base, 'init', '-q', '-b', 'develop', 'seed');
  g(path.join(base, 'seed'), 'commit', '-q', '--allow-empty', '-m', 'x');
  g(path.join(base, 'seed'), 'push', '-q', path.join(base, 'origin.git'), 'develop');
  g(base, 'clone', '-q', path.join(base, 'origin.git'), 'repo');

  const bin = path.join(base, 'bin');
  fs.mkdirSync(bin);
  fs.writeFileSync(
    path.join(bin, 'tmux'),
    [
      '#!/usr/bin/env bash',
      'case "$1" in',
      '  list-windows) for w in ${FAKE_TMUX_WINDOWS:-}; do echo "$w"; done ;;',
      '  new-window) [[ -z "${FAKE_TMUX_FAIL_NEW_WINDOW:-}" ]] ;;',
      '  capture-pane) printf "%b" "${FAKE_TMUX_PANE:-}" ;;',
      '  send-keys) echo "$@" >> "${FAKE_TMUX_KEYS_LOG:-/dev/null}" ;;',
      '  *) exit 0 ;;',
      'esac',
      '',
    ].join('\n'),
    { mode: 0o755 }
  );
  const prompt = path.join(base, 'prompt.txt');
  fs.writeFileSync(prompt, 'do the thing\n');
  const repo = path.join(base, 'repo');
  const run = (env, ...args) =>
    spawnSync('bash', [SCRIPT, ...args], {
      cwd: repo,
      encoding: 'utf8',
      env: { ...process.env, PATH: `${bin}:${process.env.PATH}`, ...env },
    });
  const branchExists = (b) => spawnSync('git', ['show-ref', '--verify', '--quiet', `refs/heads/${b}`], { cwd: repo }).status === 0;
  const worktreeCount = () =>
    spawnSync('git', ['worktree', 'list', '--porcelain'], { cwd: repo, encoding: 'utf8' }).stdout.split('\n').filter((l) => l.startsWith('worktree ')).length;
  return { base, repo, prompt, run, branchExists, worktreeCount };
}

test('a window-name collision is refused before anything is created, so the retry is not refused too', () => {
  const f = liveFixture();
  try {
    const r = f.run({ FAKE_TMUX_WINDOWS: 'fleet w1-thing' }, 'w1-thing', 'feat/thing', f.prompt);
    assert.notEqual(r.status, 0);
    assert.match(r.stderr, /window .*w1-thing already exists/);
    assert.equal(fs.existsSync(path.join(f.repo, '.claude', 'worktrees', 'w1-thing')), false);
    assert.equal(f.branchExists('feat/thing'), false);
    assert.equal(f.worktreeCount(), 1);
  } finally {
    fs.rmSync(f.base, { recursive: true, force: true });
  }
});

test('a failure after the worktree exists rolls back what this run created', () => {
  const f = liveFixture();
  try {
    const r = f.run({ FAKE_TMUX_FAIL_NEW_WINDOW: '1' }, 'w2-thing', 'feat/other', f.prompt);
    assert.notEqual(r.status, 0);
    assert.match(r.stderr, /rolling back/);
    assert.equal(fs.existsSync(path.join(f.repo, '.claude', 'worktrees', 'w2-thing')), false);
    assert.equal(f.branchExists('feat/other'), false);
    assert.equal(f.worktreeCount(), 1);
  } finally {
    fs.rmSync(f.base, { recursive: true, force: true });
  }
});

test('a worker that showed it was working and then died is reported as exited, not dispatched', () => {
  const f = liveFixture();
  try {
    const pane = 'esc to interrupt\\nError: something broke\\n[worker exited 1]\\n';
    const r = f.run({ FAKE_TMUX_PANE: pane, AGENT_DISPATCH_POLL_SECONDS: '0' }, 'w3-thing', 'feat/third', f.prompt);
    assert.equal(r.status, 1, r.stdout + r.stderr);
    assert.match(r.stderr, /exited during startup/);
    assert.doesNotMatch(r.stdout, /dispatched/);
  } finally {
    fs.rmSync(f.base, { recursive: true, force: true });
  }
});

test('answers the startup dialogs but never a tool-permission prompt', () => {
  const f = liveFixture();
  try {
    const keys = path.join(f.base, 'keys.log');
    const send = (name, pane) =>
      f.run({ FAKE_TMUX_PANE: pane, FAKE_TMUX_KEYS_LOG: keys, AGENT_DISPATCH_POLL_SECONDS: '0' }, name, `feat/${name}`, f.prompt);
    assert.match(send('w4-permission', 'Do you want to proceed?\\n❯ 1. Yes\\n').stderr, /never showed it was working/);
    assert.equal(fs.existsSync(keys), false, 'Enter must not answer a permission prompt');
    send('w5-settings', 'Settings Warning\\n❯ Continue\\n');
    assert.match(fs.readFileSync(keys, 'utf8'), /Enter/);
  } finally {
    fs.rmSync(f.base, { recursive: true, force: true });
  }
});

test('--resume continues the worker in its existing worktree and creates nothing', () => {
  const { root } = fixtureRepo();
  try {
    assert.match(dispatch(root, '--dry-run', '--resume', 'w2-gone').stderr, /no worktree/);
    fs.mkdirSync(path.join(root, '.claude', 'worktrees', 'w2-back'), { recursive: true });
    const r = dispatch(root, '--dry-run', '--resume', 'w2-back');
    assert.equal(r.status, 0, r.stderr);
    assert.doesNotMatch(r.stdout, /worktree add|fetch/);
    assert.match(r.stdout, /claude --continue -n w2-back/);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('a worker changes into its worktree itself, since a tmux server can ignore -c', () => {
  const { root, prompt } = fixtureRepo();
  const r = dispatch(root, '--dry-run', 'w9-cwd', 'feat/cwd', prompt);
  // --dry-run prints through %q, which backslash-escapes the launch command.
  const launch = `${r.stdout}${r.stderr}`.replaceAll('\\', '');
  assert.match(launch, /WORKTREE=\S*w9-cwd/);
  assert.match(launch, /cd "\$WORKTREE" &&/);
});
