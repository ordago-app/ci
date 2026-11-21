// Tests for scripts/agent-env.sh + scripts/lib/agent-slots.sh, driven through bash
// against a throwaway repo with real git worktrees.
//
// The failure this exists to prevent: two parallel agents whose emulators share a
// port. That does not fail as "port in use" — the second suite evicts or talks to
// the first one's emulators, and the symptom is "the tests failed".
//
// Run with: node --test scripts/__tests__/agent-env.test.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const AGENT_ENV = new URL('../agent-env.sh', import.meta.url).pathname;

const FIREBASE_JSON = {
  firestore: { rules: 'firestore.rules' },
  emulators: {
    auth: { port: 9099 },
    firestore: { port: 8080 },
    storage: { port: 9199 },
    functions: { port: 5001 },
    hosting: { port: 5000 },
    ui: { enabled: true, port: 4000 },
    singleProjectMode: true,
  },
};

function repo({ config } = {}) {
  const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'agent-env-')));
  const root = path.join(tmp, 'repo');
  fs.mkdirSync(root);
  const git = (...args) => {
    const r = spawnSync('git', args, { cwd: root, encoding: 'utf8' });
    if (r.status !== 0) throw new Error(`git ${args.join(' ')}: ${r.stderr}`);
    return r.stdout.trim();
  };
  git('init', '-q', '-b', 'main');
  git('config', 'user.email', 't@example.com');
  git('config', 'user.name', 't');
  git('config', 'commit.gpgsign', 'false');
  fs.writeFileSync(path.join(root, 'firebase.json'), JSON.stringify(FIREBASE_JSON, null, 2));
  if (config) {
    fs.mkdirSync(path.join(root, '.agents'));
    fs.writeFileSync(path.join(root, '.agents', 'orchestrate.config.json'), JSON.stringify(config));
  }
  git('add', '.');
  git('commit', '-qm', 'init');

  const slotsFile = path.join(tmp, 'slots.json');
  const addWorktree = (name, dir = '.claude/worktrees') => {
    git('worktree', 'add', '-q', path.join(dir, name), '-b', name);
    return path.join(root, dir, name);
  };
  /** Source agent-env.sh in `cwd`, then run `after` in the same shell. */
  const source = (cwd, args = '', after = ':') =>
    spawnSync('bash', ['-c', `source "${AGENT_ENV}" ${args}; rc=$?; ${after}; exit $rc`], {
      cwd,
      encoding: 'utf8',
      env: { ...process.env, AGENT_SLOTS_FILE: slotsFile },
    });
  const slots = () => JSON.parse(fs.readFileSync(slotsFile, 'utf8')).slots;
  return { tmp, root, addWorktree, source, slots, cleanup: () => fs.rmSync(tmp, { recursive: true, force: true }) };
}

test('two worktrees get different slots and disjoint emulator ports', () => {
  const r = repo();
  try {
    const a = r.addWorktree('a');
    const b = r.addWorktree('b');
    assert.equal(r.source(a, '', 'echo "SLOT=$AGENT_SLOT"').stdout.match(/SLOT=(\d+)/)[1], '1');
    assert.equal(r.source(b, '', 'echo "SLOT=$AGENT_SLOT"').stdout.match(/SLOT=(\d+)/)[1], '2');

    const portsOf = (wt) => {
      const cfg = JSON.parse(fs.readFileSync(path.join(wt, 'firebase.agent.json'), 'utf8')).emulators;
      return [
        ...Object.values(cfg)
          .filter((v) => typeof v === 'object')
          .map((v) => v.port),
        cfg.firestore.websocketPort,
      ];
    };
    const pa = portsOf(a);
    const pb = portsOf(b);
    assert.equal(new Set(pa).size, pa.length, 'a slot must not reuse a port internally');
    assert.deepEqual(pa.filter((p) => pb.includes(p)), [], 'two slots must not share a port');
    for (const p of [...pa, ...pb]) assert.ok(p >= 20000 && p < 60000, `port ${p} outside the slot range`);

    // Everything outside `emulators` is untouched, and the defaults are never used.
    const cfgA = JSON.parse(fs.readFileSync(path.join(a, 'firebase.agent.json'), 'utf8'));
    assert.deepEqual(cfgA.firestore, FIREBASE_JSON.firestore);
    assert.equal(cfgA.emulators.singleProjectMode, true);
    assert.ok(!pa.includes(8080) && !pa.includes(9099));
  } finally {
    r.cleanup();
  }
});

test('re-sourcing is idempotent and exports the slot ports in the same shell', () => {
  const r = repo();
  try {
    const a = r.addWorktree('a');
    r.source(a);
    const out = r.source(a, '', 'echo "SLOT=$AGENT_SLOT FS=$FIREBASE_EMULATOR_FIRESTORE_PORT"').stdout;
    assert.match(out, /SLOT=1 FS=20180/);
    assert.deepEqual(Object.values(r.slots()), [1]);
  } finally {
    r.cleanup();
  }
});

test('a subdirectory of a worktree resolves to that worktree', () => {
  const r = repo();
  try {
    const a = r.addWorktree('a');
    fs.mkdirSync(path.join(a, 'deep', 'er'), { recursive: true });
    r.source(path.join(a, 'deep', 'er'));
    assert.deepEqual(Object.keys(r.slots()), [a]);
  } finally {
    r.cleanup();
  }
});

test('the main checkout is a no-op and allocates nothing', () => {
  const r = repo();
  try {
    const res = r.source(r.root);
    assert.equal(res.status, 0);
    assert.match(res.stdout, /main checkout/);
    assert.equal(fs.existsSync(path.join(r.root, 'firebase.agent.json')), false);
  } finally {
    r.cleanup();
  }
});

test('--clean releases the slot and removes the generated config, and the slot is reused', () => {
  const r = repo();
  try {
    const a = r.addWorktree('a');
    r.source(a);
    const res = r.source(a, '--clean');
    assert.equal(res.status, 0, res.stderr);
    assert.deepEqual(r.slots(), {});
    assert.equal(fs.existsSync(path.join(a, 'firebase.agent.json')), false);

    const b = r.addWorktree('b');
    r.source(b);
    assert.equal(r.slots()[b], 1, 'the lowest free slot is handed out again');
  } finally {
    r.cleanup();
  }
});

test('the worktrees directory comes from the repo config', () => {
  const r = repo({ config: { worktreesDir: 'wt' } });
  try {
    const a = r.addWorktree('a', 'wt');
    r.source(a);
    assert.deepEqual(Object.keys(r.slots()), [a]);
  } finally {
    r.cleanup();
  }
});

test('worktreeSetup runs once, in the worktree, and reruns after a failure', () => {
  const r = repo({ config: { worktreeSetup: ['echo ran >> setup.log'] } });
  try {
    const a = r.addWorktree('a');
    r.source(a);
    r.source(a);
    assert.equal(fs.readFileSync(path.join(a, 'setup.log'), 'utf8'), 'ran\n');
  } finally {
    r.cleanup();
  }

  const failing = repo({ config: { worktreeSetup: ['echo try >> setup.log; false'] } });
  try {
    const a = failing.addWorktree('a');
    failing.source(a);
    failing.source(a);
    assert.equal(fs.readFileSync(path.join(a, 'setup.log'), 'utf8'), 'try\ntry\n');
  } finally {
    failing.cleanup();
  }
});

test('concurrent allocations never hand out the same slot', () => {
  const r = repo();
  try {
    const wts = ['a', 'b', 'c', 'd', 'e', 'f'].map((n) => r.addWorktree(n));
    const script = wts
      .map((wt) => `(cd "${wt}" && source "${AGENT_ENV}" >/dev/null) &`)
      .join('\n');
    spawnSync('bash', ['-c', `${script}\nwait`], {
      encoding: 'utf8',
      env: { ...process.env, AGENT_SLOTS_FILE: path.join(r.tmp, 'slots.json') },
    });
    const values = Object.values(r.slots()).sort();
    assert.deepEqual(values, [1, 2, 3, 4, 5, 6]);
  } finally {
    r.cleanup();
  }
});

test('sourcing through a consumer symlink still finds the library', () => {
  // Consumers link scripts/agent-env.sh → ../.agents/_shared/scripts/agent-env.sh;
  // resolving lib/ next to the link instead of the real file would fail to source.
  const r = repo();
  try {
    const a = r.addWorktree('a');
    fs.mkdirSync(path.join(a, 'scripts'), { recursive: true });
    fs.symlinkSync(AGENT_ENV, path.join(a, 'scripts', 'agent-env.sh'));
    const res = spawnSync('bash', ['-c', 'source scripts/agent-env.sh && echo "SLOT=$AGENT_SLOT"'], {
      cwd: a,
      encoding: 'utf8',
      env: { ...process.env, AGENT_SLOTS_FILE: path.join(r.tmp, 'slots.json') },
    });
    assert.equal(res.status, 0, res.stderr);
    assert.match(res.stdout, /SLOT=1/);
  } finally {
    r.cleanup();
  }
});

test('an emulator with no slot offset fails loudly instead of keeping its default port', () => {
  const r = repo();
  try {
    const a = r.addWorktree('a');
    const fb = JSON.parse(fs.readFileSync(path.join(a, 'firebase.json'), 'utf8'));
    fb.emulators.someNewEmulator = { port: 7777 };
    fs.writeFileSync(path.join(a, 'firebase.json'), JSON.stringify(fb));
    const res = r.source(a);
    assert.notEqual(res.status, 0);
    assert.match(res.stderr, /no slot offset for emulator\(s\): someNewEmulator/);
  } finally {
    r.cleanup();
  }
});
