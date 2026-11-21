// Unit tests for scripts/plans-map.js.
//
// The failure this exists to prevent: a plans map that silently stops describing
// reality. The two ways that happens are a metadata block the parser mis-reads, and
// a `Gate:` value that says nothing actionable — both produce a green run and a
// lying index.
//
// Run with: node --test scripts/__tests__/plans-map.test.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const require = createRequire(import.meta.url);
const {
  parseBlock,
  parseGate,
  label,
  render,
  lastRealTouch,
  isSweep,
  isShippingFile,
  collect,
  declaresLanded,
  releaseCycleDates,
  SWEEP_FANOUT,
  LANDED_MARKER,
} = require('../plans-map.js');

const SCRIPT = new URL('../plans-map.js', import.meta.url).pathname;

/** A throwaway consumer repo. The script resolves the repo from its cwd. */
function fixture({ git = true, files = {} } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'plans-map-'));
  for (const [rel, body] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(root, rel)), { recursive: true });
    fs.writeFileSync(path.join(root, rel), body);
  }
  const sh = (...args) => spawnSync('git', args, { cwd: root, encoding: 'utf8' });
  if (git) {
    sh('init', '-q');
    sh('config', 'user.email', 't@example.com');
    sh('config', 'user.name', 't');
    sh('config', 'commit.gpgsign', 'false');
  }
  const run = (...args) => spawnSync(process.execPath, [SCRIPT, ...args], { cwd: root, encoding: 'utf8' });
  const commitAll = (msg, date) =>
    spawnSync('git', ['commit', '-qam', msg], {
      cwd: root,
      env: { ...process.env, GIT_AUTHOR_DATE: date, GIT_COMMITTER_DATE: date },
    });
  return { root, sh, run, commitAll, cleanup: () => fs.rmSync(root, { recursive: true, force: true }) };
}

// --- the block ------------------------------------------------------------

test('parseBlock reads every field under the title', () => {
  const { fields, title } = parseBlock(
    ['# Some plan', '', '**Priority:** high', '**Landed:** beta', '**Gate:** release:0.38.0', '**Next:** do the thing', '**Due:** 2026-11-30', '', '## Context', 'body'].join('\n')
  );
  assert.equal(title, 'Some plan');
  assert.deepEqual(fields, { Priority: 'high', Landed: 'beta', Gate: 'release:0.38.0', Next: 'do the thing', Due: '2026-11-30' });
});

test('parseBlock ignores metadata-looking lines in the body', () => {
  // Plans quote each other's blocks as examples. A quoted example is not this
  // plan's own state, so only the head — above the first `##` — counts.
  const { fields } = parseBlock(
    ['# A plan about plans', '', '**Priority:** low', '', '## Design', '**Priority:** high', '**Gate:** release:9.9.9'].join('\n')
  );
  assert.deepEqual(fields, { Priority: 'low' });
});

test('label keeps an em-dash rationale but rejects an undecided value', () => {
  assert.equal(label('low — defense-in-depth, not a live incident.'), 'low');
  assert.equal(label('medium'), 'medium');
  // "low-medium" is a plan refusing to choose; it must not silently read as `low`.
  assert.equal(label('low-medium'), 'low-medium');
});

test('parseGate requires a release gate to name a version', () => {
  assert.deepEqual(parseGate('release:0.38.0'), { kind: 'release', detail: '0.38.0' });
  assert.deepEqual(parseGate('release:0.38'), { kind: 'release', detail: '0.38' });
  // The whole point of the field is that a release cut can clear it automatically.
  assert.equal(parseGate('release:the next one').kind, 'invalid');
  assert.equal(parseGate('waiting').kind, 'invalid');
  assert.equal(parseGate('none').kind, 'none');
});

test('parseGate lets a soak gate name a trigger that has no version yet', () => {
  const gate = parseGate('soak: the next hard-version upgrade');
  assert.equal(gate.kind, 'soak');
  assert.equal(gate.detail, 'the next hard-version upgrade');
});

test('parseGate accepts a trailing recheck date on any gate, and only a date', () => {
  // agent-plans v2: "Any gate may end with ` (recheck YYYY-MM-DD)`".
  assert.deepEqual(parseGate('blocked:vendor reply (recheck 2026-10-15)'), {
    kind: 'blocked',
    detail: 'vendor reply',
    recheck: '2026-10-15',
  });
  assert.deepEqual(parseGate('none (recheck 2026-10-15)'), { kind: 'none', recheck: '2026-10-15' });
  // The recheck must not let a vague release gate through.
  assert.equal(parseGate('release:soon (recheck 2026-10-15)').kind, 'invalid');
});

// --- Landed is required only where the repo declares it --------------------

test('Landed is required in ongoing/ only when the instructions carry the marker', () => {
  const plan = '# P\n\n**Priority:** high\n**Gate:** none\n**Next:** go\n';
  const without = fixture({ git: false, files: { 'AGENTS.md': '# rules\n', 'docs/plans/ongoing/p.md': plan } });
  const withMarker = fixture({
    git: false,
    files: { 'AGENTS.md': `# rules\n${LANDED_MARKER}\n`, 'docs/plans/ongoing/p.md': plan },
  });
  try {
    assert.equal(declaresLanded(without.root), false);
    assert.deepEqual(collect(without.root).errors, []);

    assert.equal(declaresLanded(withMarker.root), true);
    assert.deepEqual(collect(withMarker.root).errors, ['docs/plans/ongoing/p.md: missing `**Landed:**`']);
  } finally {
    without.cleanup();
    withMarker.cleanup();
  }
});

test('Due must be a date', () => {
  const f = fixture({
    git: false,
    files: { 'docs/plans/ready/p.md': '# P\n\n**Priority:** high\n**Due:** end of Q4\n' },
  });
  try {
    assert.match(collect(f.root).errors.join('\n'), /Due must be a YYYY-MM-DD date/);
  } finally {
    f.cleanup();
  }
});

// --- Advanced: sweeps are walked past ---------------------------------------
//
// The failure this half exists to prevent: one mechanical commit resetting every
// plan's clock at once. It happened — a rollout touched 27 of 30 in-flight plans,
// and the column could no longer answer "what stalled".

const commit = (over) => ({ sha: 'aaa1111', date: '2026-08-20', subject: 's', shipped: false, files: [], ...over });
const planFiles = (n) => Array.from({ length: n }, (_, i) => `docs/plans/ongoing/p${i}.md`);
const MINE = 'docs/plans/ongoing/p0.md';

test('a docs-only commit is a sweep past a lower fan-out than a shipping one', () => {
  assert.equal(isSweep(commit({ files: planFiles(SWEEP_FANOUT.prose) })), false);
  assert.equal(isSweep(commit({ files: planFiles(SWEEP_FANOUT.prose + 1) })), true);
  const shipped = (n) => commit({ shipped: true, files: planFiles(n) });
  assert.equal(isSweep(shipped(SWEEP_FANOUT.prose + 1)), false);
  assert.equal(isSweep(shipped(SWEEP_FANOUT.shipping)), false);
  assert.equal(isSweep(shipped(SWEEP_FANOUT.shipping + 1)), true);
});

test('prose is not shipping, wherever in the tree it lives', () => {
  // The trap: a convention rollout edits AGENTS.md alongside the plans it rolls out.
  assert.equal(isShippingFile('AGENTS.md'), false);
  assert.equal(isShippingFile('CHANGELOG.md'), false);
  assert.equal(isShippingFile('docs/ops/ci.md'), false);
  assert.equal(isShippingFile('docs/plans/ideas/x.md'), false);
  assert.equal(isShippingFile('scripts/plans-map.js'), true);
  assert.equal(isShippingFile('.github/workflows/plans-map.yml'), true);
  assert.equal(isShippingFile('packages/shared/src/services/x.ts'), true);
});

test('lastRealTouch walks past a sweep to the last real edit, and says it did', () => {
  const history = [
    commit({ sha: 'sweep11', date: '2026-08-27', subject: 'give every plan a block', files: planFiles(30) }),
    commit({ sha: 'real222', date: '2026-06-01', files: [MINE] }),
  ];
  const { date, skipped } = lastRealTouch(MINE, history);
  assert.equal(date, '2026-06-01');
  assert.deepEqual(skipped.map((c) => c.sha), ['sweep11']);
});

test('lastRealTouch falls back to the birth commit when every touch was a sweep', () => {
  const history = [
    commit({ sha: 'sweep22', date: '2026-08-27', files: planFiles(30) }),
    commit({ sha: 'born333', date: '2026-05-01', files: planFiles(30) }),
  ];
  const { date, skipped } = lastRealTouch(MINE, history);
  assert.equal(date, '2026-05-01');
  assert.deepEqual(skipped.map((c) => c.sha), ['sweep22']);
});

test('lastRealTouch treats a plan being edited right now as newest possible', () => {
  const history = [commit({ sha: 'old4444', date: '2026-05-01', files: [MINE] })];
  assert.deepEqual(lastRealTouch(MINE, history, new Set([MINE])), { date: null, skipped: [] });
  assert.equal(lastRealTouch(MINE, history, new Set()).date, '2026-05-01');
});

// --- render -----------------------------------------------------------------

const plan = (over) => ({
  stage: 'ongoing',
  slug: 'x',
  relPath: 'docs/plans/ongoing/x.md',
  priority: 'medium',
  landed: 'dev',
  gate: { kind: 'none' },
  next: 'do it',
  due: null,
  touched: '2026-08-20',
  cyclesStale: 0,
  skipped: [],
  ...over,
});

const CYCLES = [{ date: '2026-08-23', version: 'v0.37.0' }, { date: '2026-08-15', version: 'v0.36.0' }];

test('render groups a plan under the release that unblocks it', () => {
  const out = render([plan({ slug: 'gated', gate: { kind: 'release', detail: '0.38.0' } })], CYCLES);
  assert.match(out.split('## Waiting on release 0.38.0')[1].split('##')[0], /gated/);
});

test('render calls out plans nobody advanced for two cycles', () => {
  const out = render([plan({ slug: 'forgotten', cyclesStale: 3 })], CYCLES);
  assert.match(out, /Not advanced in 2\+ cycles:.*forgotten/);
  assert.match(out, /forgotten.*⚠️/);
});

test('render keeps ideas out of the tables', () => {
  const out = render([plan({ stage: 'ideas', slug: 'someday' })], CYCLES);
  assert.doesNotMatch(out.split('## Actionable now')[1].split('##')[0], /someday/);
});

test('render emits NO aggregate totals — they were the highest-conflict line', () => {
  const out = render([plan({ stage: 'ideas', slug: 'someday' }), plan({ slug: 'in-flight' })], CYCLES);
  assert.doesNotMatch(out, /plans in flight/);
  assert.match(out, /Current cycle \*\*v0\.37\.0/);
});

test('render gives the stuck sections a column for why, recheck included', () => {
  const out = render(
    [plan({ slug: 'stuck', gate: { kind: 'blocked', detail: 'needs an operator', recheck: '2026-10-01' } })],
    CYCLES,
    '2026-10-02'
  );
  const blocked = out.split('## Blocked')[1];
  assert.match(blocked, /Waiting on/);
  assert.match(blocked, /needs an operator \(recheck 2026-10-01\)/);
  assert.match(out, /Recheck date passed:\*\* stuck \(2026-10-01\)/);
});

test('render escapes a pipe in a Next line instead of breaking the table', () => {
  const out = render([plan({ slug: 'piped', next: 'run `a | b`' })], CYCLES);
  assert.match(out, /run `a \\\| b`/);
});

test('a plan edited in this very commit reads the same before and after it lands', () => {
  const untracked = render([plan({ slug: 'fresh', touched: null, cyclesStale: 0 })], CYCLES);
  const committed = render([plan({ slug: 'fresh', touched: '2026-08-27', cyclesStale: 0 })], CYCLES);
  assert.equal(untracked, committed);
});

test('render makes a walked-past sweep visible rather than magic', () => {
  const swept = commit({ sha: 'sweep33', date: '2026-08-27', subject: 'bulk retire', files: planFiles(30) });
  const out = render([plan({ slug: 'walked', cyclesStale: 1, skipped: [swept] })], CYCLES);
  assert.match(out, /walked.*1 cycle ago \\\*/);
  assert.match(out, /`sweep33`.*30.*bulk retire/);
});

// --- release cycles ---------------------------------------------------------

test('release cycles come from the commit that introduced each CHANGELOG heading', () => {
  const f = fixture({ files: { 'CHANGELOG.md': '# Changelog\n\n## [Unreleased]\n' } });
  try {
    f.sh('add', '.');
    f.commitAll('init', '2026-08-01T12:00:00');
    fs.writeFileSync(path.join(f.root, 'CHANGELOG.md'), '# Changelog\n\n## [Unreleased]\n\n## v1.0.0 — 2026-08-10\n');
    f.commitAll('1.0.0', '2026-08-10T12:00:00');
    fs.writeFileSync(
      path.join(f.root, 'CHANGELOG.md'),
      '# Changelog\n\n## [Unreleased]\n\n## [1.1.0] - 2026-09-01\n\n## v1.0.0 — 2026-08-10\n'
    );
    f.commitAll('1.1.0', '2026-09-01T12:00:00');

    // Through the CLI: the module's git calls are bound to the repo it was loaded
    // from, and the pickaxe needs this fixture's history.
    fs.mkdirSync(path.join(f.root, 'docs', 'plans', 'ongoing'), { recursive: true });
    fs.writeFileSync(
      path.join(f.root, 'docs', 'plans', 'ongoing', 'p.md'),
      '# P\n\n**Priority:** high\n**Gate:** none\n**Next:** go\n'
    );
    f.sh('add', '.');
    f.commitAll('plan', '2026-09-02T12:00:00');
    const r = f.run();
    assert.equal(r.status, 0, r.stderr);
    const map = fs.readFileSync(path.join(f.root, 'docs', 'plans', '_plans-map.md'), 'utf8');
    assert.match(map, /Current cycle \*\*v1\.1\.0\*\* \(cut 2026-09-01\)/);
  } finally {
    f.cleanup();
  }
});

test('releaseCycleDates tolerates a repo with no changelog', () => {
  // Falls back to `v*` tags; a repo with neither is "unreleased", not an error.
  const cycles = releaseCycleDates('/nonexistent/CHANGELOG.md');
  assert.ok(Array.isArray(cycles));
  for (const c of cycles) assert.match(c.date, /^\d{4}-\d{2}-\d{2}$/);
});

// --- CLI contract -------------------------------------------------------------
// `--validate` must catch the author error (a bad block) WITHOUT the staleness
// comparison, which on a PR branch is usually caused by a different PR landing.

test('--validate survives a shallow clone, which is what CI hands it', () => {
  const f = fixture({ files: { 'docs/plans/ready/sample.md': '# Sample\n\n**Priority:** high\n\nbody\n' } });
  try {
    f.sh('commit', '-q', '--allow-empty', '-m', 'x');
    // What makes `git rev-parse --is-shallow-repository` answer true.
    fs.writeFileSync(path.join(f.root, '.git', 'shallow'), '');
    assert.equal(f.sh('rev-parse', '--is-shallow-repository').stdout.trim(), 'true');

    const r = f.run('--validate');
    assert.equal(r.status, 0, `--validate must not need history; stderr: ${r.stderr}`);
    assert.match(r.stdout, /plan metadata is valid/);

    const full = f.run();
    assert.equal(full.status, 1);
    assert.match(full.stderr, /shallow clone/);
  } finally {
    f.cleanup();
  }
});

test('--validate passes on a stale map, where --check fails', () => {
  const f = fixture({ files: { 'docs/plans/ready/sample.md': '# Sample\n\n**Priority:** high\n' } });
  try {
    f.sh('add', '.');
    f.commitAll('init', '2026-09-01T12:00:00');
    assert.equal(f.run().status, 0);
    assert.equal(f.run('--check').status, 0, 'a fresh map must be in sync');

    const mapPath = path.join(f.root, 'docs', 'plans', '_plans-map.md');
    fs.appendFileSync(mapPath, '\n<!-- deliberately stale -->\n');
    const check = f.run('--check');
    assert.notEqual(check.status, 0);
    assert.match(check.stderr, /out of date/);

    const validate = f.run('--validate');
    assert.equal(validate.status, 0, '--validate must ignore staleness entirely');
  } finally {
    f.cleanup();
  }
});

test('the CLI resolves the repo from its cwd, not from its own location', () => {
  // Consumers reach this script through a symlink into a submodule; resolving from
  // __dirname would validate the submodule's (empty) docs/plans and pass vacuously.
  const f = fixture({ files: { 'docs/plans/ready/bad.md': '# Bad\n\n**Priority:** urgent\n' } });
  try {
    const r = f.run('--validate');
    assert.equal(r.status, 1);
    assert.match(r.stderr, /docs\/plans\/ready\/bad\.md: Priority must be one of/);
  } finally {
    f.cleanup();
  }
});

test('--validate rejects a hand-written state line, in every spelling found', () => {
  const f = fixture({ git: false, files: { 'docs/plans/ideas/a-plan.md': '# A plan\n\n**Priority:** medium\n\nBody.\n' } });
  const planPath = path.join(f.root, 'docs', 'plans', 'ideas', 'a-plan.md');
  try {
    assert.equal(f.run('--validate').status, 0);
    for (const line of [
      '**Status:** idea — not started',
      '**Stage:** ideas — evidenced, not located',
      '**Estado:** idea — sin empezar',
      '**Updated:** 2026-08-22',
      '**Re-verified:** 2026-08-22',
      '**Status (2026-08-22):** still true',
    ]) {
      fs.writeFileSync(planPath, `# A plan\n\n**Priority:** medium\n${line}\n\nBody.\n`);
      const res = f.run('--validate');
      assert.equal(res.status, 1, `must reject ${line}`);
      assert.match(res.stderr, /hand-written state line/);
    }
  } finally {
    f.cleanup();
  }
});
