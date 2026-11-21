// Tests for scripts/agent-reap.sh: it reaps only a clean worktree whose HEAD is on
// origin/<base>, and never with `git worktree remove --force`.
//
// Run with: node --test scripts/__tests__/agent-reap.test.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SCRIPT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'agent-reap.sh');
const git = (cwd, ...args) => {
  const r = spawnSync('git', ['-c', 'user.email=t@x', '-c', 'user.name=t', ...args], { cwd, encoding: 'utf8' });
  assert.equal(r.status, 0, `git ${args.join(' ')}: ${r.stderr}`);
  return r.stdout.trim();
};

// A main checkout with an origin, and a worker worktree on its own branch.
function fixture() {
  const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'reap-')));
  const origin = path.join(tmp, 'origin.git');
  const root = path.join(tmp, 'main');
  git(tmp, 'init', '-q', '--bare', '-b', 'develop', origin);
  git(tmp, 'clone', '-q', origin, root);
  git(root, 'checkout', '-q', '-b', 'develop');
  git(root, 'commit', '-q', '--allow-empty', '-m', 'base');
  git(root, 'push', '-q', 'origin', 'develop');
  const wt = path.join(root, '.claude', 'worktrees', 'w1-thing');
  git(root, 'worktree', 'add', '-q', '-b', 'feat/thing', wt, 'origin/develop');
  fs.writeFileSync(path.join(wt, 'a.txt'), 'a\n');
  git(wt, 'add', 'a.txt');
  git(wt, 'commit', '-q', '-m', 'work');
  return { tmp, root, wt };
}

const reap = (cwd, ...args) => spawnSync('bash', [SCRIPT, ...args], { cwd, encoding: 'utf8' });
const merge = ({ root }) => {
  git(root, 'merge', '-q', '--no-ff', '-m', 'merge', 'feat/thing');
  git(root, 'push', '-q', 'origin', 'develop');
};

test('refuses a worktree whose work is not on origin/<base>, and touches nothing', () => {
  const f = fixture();
  try {
    const r = reap(f.root, 'w1-thing');
    assert.equal(r.status, 2, r.stdout + r.stderr);
    assert.match(r.stderr, /not on origin\/develop/);
    assert.ok(fs.existsSync(f.wt));
  } finally {
    fs.rmSync(f.tmp, { recursive: true, force: true });
  }
});

test('refuses a dirty worktree even when its commits are merged', () => {
  const f = fixture();
  try {
    merge(f);
    fs.writeFileSync(path.join(f.wt, 'b.txt'), 'uncommitted\n');
    const r = reap(f.root, 'w1-thing');
    assert.equal(r.status, 2, r.stdout + r.stderr);
    assert.match(r.stderr, /uncommitted changes/);
    assert.ok(fs.existsSync(path.join(f.wt, 'b.txt')));
  } finally {
    fs.rmSync(f.tmp, { recursive: true, force: true });
  }
});

test('dry run of a merged, clean worktree removes, prunes and deletes the branch with -d, never --force', () => {
  const f = fixture();
  try {
    merge(f);
    const r = reap(f.root, '--dry-run', 'w1-thing');
    assert.equal(r.status, 0, r.stderr);
    const out = r.stdout;
    assert.match(out, /rm -rf .*w1-thing/);
    assert.match(out, /worktree prune/);
    assert.match(out, /branch -d feat\/thing/);
    assert.doesNotMatch(out, /--force|branch -D|worktree remove/);
    assert.ok(fs.existsSync(f.wt), 'a dry run removes nothing');
  } finally {
    fs.rmSync(f.tmp, { recursive: true, force: true });
  }
});

test('a real reap of a merged, clean worktree leaves no worktree and no branch', () => {
  const f = fixture();
  try {
    merge(f);
    const r = reap(f.root, '--session', 'no-such-session-reap-test', 'w1-thing');
    assert.equal(r.status, 0, r.stderr);
    assert.ok(!fs.existsSync(f.wt));
    assert.doesNotMatch(git(f.root, 'worktree', 'list'), /w1-thing/);
    assert.equal(git(f.root, 'branch', '--list', 'feat/thing'), '');
  } finally {
    fs.rmSync(f.tmp, { recursive: true, force: true });
  }
});

// The cleanup half. Each worker carries the two scripts a real worktree has, as stubs that
// log who ran them and from where; they are committed, so the worktree stays clean.
function withCleanupStubs(f, names) {
  const log = path.join(f.tmp, 'cleanup.log');
  for (const name of names) {
    const wt = path.join(f.root, '.claude', 'worktrees', name);
    if (!fs.existsSync(wt)) git(f.root, 'worktree', 'add', '-q', '-b', `feat/${name}`, wt, 'origin/develop');
    fs.mkdirSync(path.join(wt, 'scripts'), { recursive: true });
    fs.writeFileSync(path.join(wt, 'scripts', 'pr-land-bg.sh'), '#!/usr/bin/env bash\necho "lander $1 $PWD" >> "$REAP_TEST_LOG"\n', { mode: 0o755 });
    fs.writeFileSync(path.join(wt, 'scripts', 'agent-env.sh'), 'echo "slot $1 $PWD" >> "$REAP_TEST_LOG"\n');
    git(wt, 'add', 'scripts');
    git(wt, 'commit', '-q', '-m', `stubs for ${name}`);
    git(f.root, 'merge', '-q', '--no-ff', '-m', `merge ${name}`, git(wt, 'rev-parse', '--abbrev-ref', 'HEAD'));
  }
  git(f.root, 'push', '-q', 'origin', 'develop');
  const run = (...args) => spawnSync('bash', [SCRIPT, '--session', 'no-such-session-reap-test', ...args],
    { cwd: f.root, encoding: 'utf8', env: { ...process.env, REAP_TEST_LOG: log } });
  const lines = () => (fs.existsSync(log) ? fs.readFileSync(log, 'utf8').trim().split('\n').filter(Boolean) : []);
  return { run, lines };
}

test('a reap stops the lander, then frees the slot, in the selected worktree only, before deleting it', () => {
  const f = fixture();
  try {
    const { run, lines } = withCleanupStubs(f, ['w1-thing', 'w2-other']);
    const other = path.join(f.root, '.claude', 'worktrees', 'w2-other');
    const r = run('w1-thing');
    assert.equal(r.status, 0, r.stderr);
    // Each stub ran from inside w1-thing, which proves the worktree still existed when it ran.
    assert.deepEqual(lines(), [`lander --kill ${f.wt}`, `slot --clean ${f.wt}`]);
    assert.ok(!fs.existsSync(f.wt));
    assert.ok(fs.existsSync(path.join(other, 'scripts', 'agent-env.sh')), 'the other worker is untouched');
    assert.notEqual(git(f.root, 'branch', '--list', 'feat/w2-other'), '');
  } finally {
    fs.rmSync(f.tmp, { recursive: true, force: true });
  }
});

test('a dry run and a refused reap run neither cleanup script', () => {
  const f = fixture();
  try {
    const { run, lines } = withCleanupStubs(f, ['w1-thing']);
    assert.equal(run('--dry-run', 'w1-thing').status, 0);
    fs.writeFileSync(path.join(f.wt, 'b.txt'), 'uncommitted\n');
    assert.equal(run('w1-thing').status, 2);
    assert.deepEqual(lines(), []);
    assert.ok(fs.existsSync(f.wt));
  } finally {
    fs.rmSync(f.tmp, { recursive: true, force: true });
  }
});
