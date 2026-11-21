import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const require = createRequire(import.meta.url);
const {
  hardStopHits, ciCovers, baseMovement, loadConfig, DEFAULTS, EXIT, CONFIG,
  reviewFor, integrationVerdictPath, readIntegrationVerdictFile, judgeIntegrationVerdict,
  integrationScratchRoot, runIntegrationCheck, killProcessesUnder, INTEGRATION_CHECK_CANNOT_RUN,
} = require('../pr-land.js');
import { execSync } from 'node:child_process';

// These three predicates decide whether a PR may merge without a human, so they
// are asserted rather than smoke-tested: a false negative auto-merges a security
// rules change. They are pure and take an explicit config, so the shared script
// is testable without any repo's config on disk.

/** A representative consumer config — shape, not any one repo's values. */
const cfg = loadConfig('/nonexistent-so-defaults-apply');
Object.assign(cfg, {
  ciGates: [{ workflow: 'ci.yml', paths: ['src/', 'functions/', 'package.json', 'pnpm-lock.yaml'], requiredLanes: [] }],
  sharedBlastRadius: ['packages/shared/', 'pnpm-lock.yaml'],
  hardStop: [
    { pattern: /^firestore\.rules$/, why: 'security rules' },
    { pattern: /^scripts\/backfills\//, why: 'data backfill' },
  ],
});

test('defaults fail closed: no hard-stop rules, but review still required', () => {
  assert.deepEqual(DEFAULTS.hardStop, []);
  assert.equal(DEFAULTS.requireApprovingReview, true);
});

/** Writes a throwaway repo root carrying just `.agents/land.config.json`. */
function repoWithConfig(config) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'land-cfg-'));
  fs.mkdirSync(path.join(root, '.agents'), { recursive: true });
  fs.writeFileSync(path.join(root, '.agents/land.config.json'), JSON.stringify(config));
  return root;
}

test('mergeMethod defaults to a merge commit and is overridable', () => {
  assert.equal(DEFAULTS.mergeMethod, 'merge');
  assert.equal(loadConfig(repoWithConfig({ mergeMethod: 'squash' })).mergeMethod, 'squash');
});

test('an unusable mergeMethod is rejected at load, not at the merge', () => {
  // The merge is the one step where a config typo must not surface: by then the
  // push, the CI wait and the review have all already succeeded.
  assert.throws(() => loadConfig(repoWithConfig({ mergeMethod: 'sqush' })), /mergeMethod/);
});

test('loadConfig compiles pattern strings into regexes', () => {
  const c = loadConfig('/nonexistent');
  c.hardStop = [{ pattern: '^a/b$', why: 'x' }];
  const compiled = loadConfig('/nonexistent');
  assert.ok(compiled.hardStopTrailer instanceof RegExp);
  assert.equal(compiled.hardStopTrailer.test('Breaking-Client: yes'), true);
});

test('hard stop: configured paths never self-merge', () => {
  assert.equal(hardStopHits(['firestore.rules'], '', cfg).length, 1);
  assert.equal(hardStopHits(['scripts/backfills/0012-foo.js'], '', cfg).length, 1);
});

test('hard stop: a Breaking-Client trailer gates regardless of paths', () => {
  const msg = 'feat(api): drop legacy field\n\nBreaking-Client: removes the v1 shape\n';
  assert.equal(hardStopHits(['README.md'], msg, cfg).length, 1);
  assert.equal(hardStopHits(['README.md'], 'feat: harmless\n', cfg).length, 0);
});

test('hard stop: ordinary source and doc changes are not gated', () => {
  assert.deepEqual(hardStopHits(['src/screens/Home.tsx'], '', cfg), []);
  assert.deepEqual(hardStopHits(['docs/plans/ideas/whatever.md'], '', cfg), []);
});

test('vacuous green: docs-only diffs dispatch no CI run', () => {
  assert.equal(ciCovers(['docs/x.md', 'README.md'], cfg), false);
  assert.equal(ciCovers(['.maestro/flows/login.yaml'], cfg), false);
});

test('vacuous green: code diffs are covered by CI', () => {
  assert.equal(ciCovers(['src/services/foo.ts'], cfg), true);
  assert.equal(ciCovers(['docs/x.md', 'functions/index.ts'], cfg), true);
});

test('vacuous green: an unconfigured repo reports NO coverage, never silent green', () => {
  const bare = loadConfig('/nonexistent');
  assert.equal(ciCovers(['src/anything.ts'], bare), false);
});

test('staleness: a base move that misses this diff does not force a rebase', () => {
  const m = baseMovement(['src/A.tsx'], ['functions/index.ts'], cfg);
  assert.equal(m.needsRebase, false);
  assert.equal(m.needsIntegrationCheck, false);
});

test('staleness: direct file overlap forces a rebase', () => {
  const m = baseMovement(['functions/index.ts'], ['functions/index.ts'], cfg);
  assert.equal(m.needsRebase, true);
  assert.deepEqual(m.overlap, ['functions/index.ts']);
});

test('staleness: without an integrationCheck, a blast-radius move still rebases', () => {
  const m = baseMovement(['pnpm-lock.yaml'], ['src/A.tsx'], cfg);
  assert.equal(m.needsRebase, true);
  assert.equal(m.needsIntegrationCheck, false);
  assert.deepEqual(m.blast, ['pnpm-lock.yaml']);
});

test('staleness: with an integrationCheck, a blast-only move is checked locally instead', () => {
  const withCheck = { ...cfg, integrationCheck: { command: 'true', timeoutMs: 1000 } };
  const m = baseMovement(['packages/shared/src/x.ts'], ['src/A.tsx'], withCheck);
  assert.equal(m.needsRebase, false);
  assert.equal(m.needsIntegrationCheck, true);
});

test('staleness: overlap wins over the local check — the same file on both sides rebases', () => {
  const withCheck = { ...cfg, integrationCheck: { command: 'true', timeoutMs: 1000 } };
  const m = baseMovement(['packages/shared/src/x.ts'], ['packages/shared/src/x.ts'], withCheck);
  assert.equal(m.needsRebase, true);
  assert.equal(m.needsIntegrationCheck, false);
});

test('staleness: a rebaseRadius move rebases even when a local check is configured', () => {
  const withCheck = {
    ...cfg,
    sharedBlastRadius: ['packages/shared/', 'firestore.rules'],
    rebaseRadius: ['firestore.rules'],
    integrationCheck: { command: 'true', timeoutMs: 1000 },
  };
  const rules = baseMovement(['firestore.rules', 'packages/shared/x.ts'], ['src/A.tsx'], withCheck);
  assert.equal(rules.needsRebase, true, 'a typecheck cannot judge a rules change');
  assert.equal(rules.needsIntegrationCheck, false);
  assert.deepEqual(rules.forced, ['firestore.rules']);
  assert.deepEqual(rules.blast, ['packages/shared/x.ts'], 'a forced path is not double-counted');

  const shared = baseMovement(['packages/shared/x.ts'], ['src/A.tsx'], withCheck);
  assert.equal(shared.needsIntegrationCheck, true);
});

// The direction a base-only rule cannot see: the PR edits the shared code, the
// base edits its consumer. The PR's CI tested the new shared code against the OLD
// consumer, so the merged pair is exactly as unverified as the mirror case.
test('staleness is symmetric: a PR changing shared code is checked when the base moved a consumer', () => {
  const withCheck = { ...cfg, sharedBlastRadius: ['packages/shared/'], integrationCheck: { command: 'true', timeoutMs: 1000 } };
  const m = baseMovement(['functions/index.ts'], ['packages/shared/x.ts'], withCheck);
  assert.equal(m.needsIntegrationCheck, true);
  assert.deepEqual(m.blast, ['packages/shared/x.ts']);
});

// The same livelock, in a repo with no integrationCheck: the PR's own diff is a
// trigger no rebase removes, so it must not rebase for it. Without a local check
// there is nothing to run before the merge, and the base's post-merge CI answers.
test('without an integrationCheck, only the BASE side rebases — the PR side never does', () => {
  const noCheck = { ...cfg, sharedBlastRadius: ['packages/shared/'], rebaseRadius: ['pnpm-lock.yaml'] };
  for (const own of ['pnpm-lock.yaml', 'packages/shared/x.ts']) {
    const m = baseMovement(['src/A.tsx'], [own], noCheck);
    assert.equal(m.needsRebase, false, `${own} in the PR must not rebase on every base move`);
    assert.equal(m.needsIntegrationCheck, false);
  }
  assert.equal(baseMovement(['packages/shared/x.ts'], ['src/A.tsx'], noCheck).needsRebase, true, 'removable');
  assert.equal(baseMovement(['pnpm-lock.yaml'], ['src/A.tsx'], noCheck).needsRebase, true, 'removable');
});

// The livelock this rule exists to prevent. A PR that edits package.json used to
// rebase on EVERY base move — the trigger was its own diff, which no rebase
// removes — and on a base that moves faster than CI, it never landed.
test('LIVELOCK: a PR changing a rebaseRadius path never rebases for it — it is checked wide', () => {
  const withCheck = {
    ...cfg,
    sharedBlastRadius: ['packages/shared/'],
    rebaseRadius: ['package.json'],
    integrationCheck: { command: 'true', timeoutMs: 1000 },
  };
  const m = baseMovement(['src/A.tsx'], ['package.json', 'functions/index.ts'], withCheck);
  assert.equal(m.needsRebase, false, 'the trigger is the PR itself; a rebase cannot remove it');
  assert.equal(m.needsIntegrationCheck, true);
  assert.equal(m.scope, 'wide');
});

test('the BASE moving a rebaseRadius path still rebases — and after it, the trigger is gone', () => {
  const withCheck = { ...cfg, rebaseRadius: ['package.json'], integrationCheck: { command: 'true', timeoutMs: 1000 } };
  assert.equal(baseMovement(['package.json'], ['src/A.tsx'], withCheck).needsRebase, true);
  assert.equal(baseMovement([], ['src/A.tsx'], withCheck).needsRebase, false, 'post-rebase: nothing moved');
});

test('a shared-only move is checked at scope "shared"', () => {
  const withCheck = { ...cfg, sharedBlastRadius: ['packages/shared/'], integrationCheck: { command: 'true', timeoutMs: 1000 } };
  assert.equal(baseMovement(['packages/shared/x.ts'], ['src/A.tsx'], withCheck).scope, 'shared');
});

// Each ciGates entry mirrors ONE workflow's filter; a consumer with no gate of its
// own (a web app) sits outside all of them while importing the shared code. The review of #1123
// found web-only PRs merging past shared moves unchecked because of it.
test('staleness: any move on the other side counts — not only what a CI gate covers', () => {
  const withCheck = { ...cfg, sharedBlastRadius: ['packages/shared/'], integrationCheck: { command: 'true', timeoutMs: 1000 } };
  assert.equal(baseMovement(['packages/shared/x.ts'], ['apps/web/page.tsx'], withCheck).needsIntegrationCheck, true);
  assert.equal(baseMovement(['apps/web/page.tsx'], ['packages/shared/x.ts'], withCheck).needsIntegrationCheck, true);
});

test('staleness: with no ciGates declared, any change on the other side counts', () => {
  const bare = { ...loadConfig('/nonexistent'), sharedBlastRadius: ['packages/shared/'], integrationCheck: { command: 'true', timeoutMs: 1000 } };
  assert.equal(baseMovement(['docs/x.md'], ['packages/shared/x.ts'], bare).needsIntegrationCheck, true);
});

test('integrationCheck defaults off, and a malformed one is rejected at load', () => {
  assert.equal(DEFAULTS.integrationCheck, null);
  assert.throws(() => loadConfig(repoWithConfig({ integrationCheck: 'pnpm tsc' })), /integrationCheck/);
  assert.throws(() => loadConfig(repoWithConfig({ integrationCheck: { command: ' ' } })), /integrationCheck/);
  const c = loadConfig(repoWithConfig({ integrationCheck: { command: 'pnpm tsc' } }));
  assert.equal(c.integrationCheck.command, 'pnpm tsc');
  assert.ok(c.integrationCheck.timeoutMs > 0, 'a timeout is always set');
});

// --- review rounds ------------------------------------------------------------

test('only disagreements are rounds: approvals, carried or not, spend none', () => {
  const reviews = [
    { state: 'CHANGES_REQUESTED', body: 'fix x', commit: { oid: 'a' } },
    { state: 'APPROVED', body: 'lgtm', commit: { oid: 'b' } },
    { state: 'APPROVED', body: '<!-- ai-review:carried-approval -->\nsame diff as b', commit: { oid: 'c' } },
    { state: 'APPROVED', body: 'lgtm after the CI fix', commit: { oid: 'd' } },
    { state: 'COMMENTED', body: 'nit', commit: { oid: 'd' } },
  ];
  const r = reviewFor(reviews, 'd');
  assert.equal(r.state, 'approved');
  assert.equal(r.rounds, 1);
});

test('an approved PR with a new unreviewed head still waits for its review', () => {
  // Three approvals across CI-fix pushes used to read as three spent rounds, so
  // under roundsExhausted "merge" the fourth head landed with no review at all.
  const reviews = ['a', 'b', 'c'].map((oid) => ({ state: 'APPROVED', body: 'lgtm', commit: { oid } }));
  const r = reviewFor(reviews, 'd');
  assert.deepEqual([r.state, r.rounds], ['none', 0]);
});

// --- the integration check's recorded verdict ---------------------------------

// --- the integration check's recorded verdict ---------------------------------

const vcfg = {
  ...cfg,
  sharedBlastRadius: ['packages/shared/'],
  rebaseRadius: ['package.json'],
  integrationCheck: { command: 'true', timeoutMs: 1000, maxAgeMs: 30 * 60 * 1000 },
};
const NOW = 1_000_000_000;
const judge = (v, over = {}) =>
  judgeIntegrationVerdict(
    { at: NOW - 60_000, ...v },
    { baseTip: 't2', scope: 'shared', movedSince: [], prFiles: ['src/A.tsx'], now: NOW, ...over },
    vcfg,
  );

test('verdict: none recorded, or unreadable, is pending', () => {
  assert.equal(judgeIntegrationVerdict(null, { baseTip: 't2', scope: 'shared', movedSince: [], prFiles: [] }, vcfg).state, 'pending');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'land-verdict-'));
  const file = integrationVerdictPath(dir, 'head1');
  assert.equal(readIntegrationVerdictFile(file), null);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, '{"ok": tr');
  assert.equal(readIntegrationVerdictFile(file), null, 'a torn write is not a verdict');
});

test('verdict: on the same tip it stands, pass or fail', () => {
  assert.equal(judge({ baseTip: 't2', scope: 'shared', ok: true }).state, 'pass');
  const f = judge({ baseTip: 't2', scope: 'shared', ok: false, output: 'TS2339' });
  assert.deepEqual([f.state, f.output], ['fail', 'TS2339']);
});

// The starvation this exists to prevent: the check takes minutes, the base moves
// every few, and a verdict keyed by the exact tip would be re-run forever.
test('verdict: a PASS survives base moves that cannot matter to it', () => {
  const v = { baseTip: 't1', scope: 'shared', ok: true };
  assert.equal(judge(v, { movedSince: ['docs/x.md', 'apps/other/B.tsx'] }).state, 'pass');
});

test('verdict: a PASS is re-checked when the base moved the PR files or rebaseRadius', () => {
  const v = { baseTip: 't1', scope: 'shared', ok: true };
  assert.equal(judge(v, { movedSince: ['src/A.tsx'] }).state, 'pending', "the PR's own file");
  assert.equal(judge(v, { movedSince: ['package.json'] }).state, 'pending', 'rebase radius');
  assert.equal(judge(v, { movedSince: null }).state, 'pending', 'an unreadable move is no answer');
});

// Shared code moves on most merges. Were it to void a PASS, a check longer than
// the gap between merges would re-run forever — the review of #1123 caught this.
test('verdict: a PASS survives shared-radius moves — or a long check never settles', () => {
  const v = { baseTip: 't1', scope: 'wide', ok: true };
  assert.equal(judge(v, { scope: 'wide', movedSince: ['packages/shared/x.ts'] }).state, 'pass');
});

test('killProcessesUnder reaps what a timed-out check left running in its directory', { skip: process.platform !== 'linux' }, async () => {
  const { spawn } = await import('node:child_process');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'land-reap-'));
  const child = spawn('sleep', ['60'], { cwd: dir, stdio: 'ignore' });
  const exited = new Promise((resolve) => child.on('exit', (_code, signal) => resolve(signal)));
  await new Promise((r) => setTimeout(r, 100));
  killProcessesUnder(dir);
  assert.equal(await exited, 'SIGKILL');
});

test('verdict: an old FAIL is re-checked on a new tip — the base may have fixed it', () => {
  assert.equal(judge({ baseTip: 't1', scope: 'shared', ok: false }, { movedSince: ['docs/x.md'] }).state, 'pending');
});

// A pass that holds across unrelated moves must not hold forever: a run killed
// and resumed hours later would merge into a tree that never checked together.
test('verdict: past maxAgeMs a PASS answers nothing, however unrelated the moves', () => {
  const v = { baseTip: 't1', scope: 'shared', ok: true, at: NOW - 31 * 60 * 1000 };
  assert.equal(judge(v, { movedSince: ['docs/x.md'] }).state, 'pending');
  assert.equal(judge({ ...v, at: undefined }).state, 'pending', 'a verdict with no time is no verdict');
});

test('verdict: a check that could not run is an error, never a fail about the merge', () => {
  const v = { baseTip: 't2', scope: 'shared', ok: false, error: true, output: 'jq is not installed' };
  assert.equal(judge(v).state, 'error');
  assert.equal(judge({ ...v, baseTip: 't1' }, { movedSince: ['docs/x.md'] }).state, 'pending', 'a new tip retries');
});

test('verdict: a shared-scope PASS never answers a wide question', () => {
  assert.equal(judge({ baseTip: 't2', scope: 'shared', ok: true }, { scope: 'wide' }).state, 'pending');
  assert.equal(judge({ baseTip: 't2', scope: 'wide', ok: true }, { scope: 'shared' }).state, 'pass');
});

/**
 * A real repo: base adds `lib.js` exporting `answer`; the branch adds a caller.
 * The base then renames the export — no file overlap, a clean textual merge, and
 * a broken result. That is exactly the move a blast-radius rebase used to buy a
 * whole CI run to find.
 */
function repoWithSemanticBreak() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'land-int-'));
  const git = (cmd) => execSync(`git -c user.name=t -c user.email=t@t ${cmd}`, { cwd: root, stdio: 'pipe' }).toString().trim();
  git('init -q -b develop');
  fs.writeFileSync(path.join(root, 'lib.js'), 'exports.answer = 42;\n');
  git('add . && git -c user.name=t -c user.email=t@t commit -qm base');
  git('checkout -qb feat');
  fs.writeFileSync(path.join(root, 'use.js'), "if (require('./lib.js').answer !== 42) process.exit(1);\n");
  git('add . && git -c user.name=t -c user.email=t@t commit -qm feat');
  const head = git('rev-parse HEAD');
  git('checkout -q develop');
  const cleanTip = git('rev-parse HEAD');
  fs.writeFileSync(path.join(root, 'lib.js'), 'exports.theAnswer = 42;\n');
  git('add . && git -c user.name=t -c user.email=t@t commit -qm rename');
  const brokenTip = git('rev-parse HEAD');
  git('checkout -q feat');
  return { root, head, cleanTip, brokenTip };
}

test('the integration check judges the MERGE RESULT, and leaves the branch alone', () => {
  const { root, head, cleanTip, brokenTip } = repoWithSemanticBreak();
  const cwd = process.cwd();
  const saved = CONFIG.integrationCheck;
  // The scope reaches the command, so a repo can widen what it runs.
  CONFIG.integrationCheck = { command: 'test "$PR_LAND_INTEGRATION_SCOPE" = shared && node use.js', timeoutMs: 30_000 };
  const savedScratch = process.env.PR_LAND_SCRATCH_DIR;
  process.env.PR_LAND_SCRATCH_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'land-scratch-'));
  try {
    process.chdir(root);
    const s = (baseTip) => ({ branch: { headSha: head }, baseTip, base: { scope: 'shared' } });
    assert.equal(runIntegrationCheck(s(cleanTip)), true);
    assert.equal(runIntegrationCheck(s(brokenTip)), false, 'the rename breaks the caller only once merged');

    const common = path.join(root, '.git');
    const v = readIntegrationVerdictFile(integrationVerdictPath(common, head));
    assert.deepEqual([v.ok, v.baseTip, v.scope], [false, brokenTip, 'shared'], 'the last run is recorded with what it judged');

    // The failing step must be what the tail shows, even when an earlier step
    // wrote its (passing) report to stderr — Jest does.
    CONFIG.integrationCheck = {
      command: 'echo "suite passed" >&2; echo "the real failure"; exit 1',
      timeoutMs: 30_000,
    };
    assert.equal(runIntegrationCheck(s(cleanTip)), false);
    const tail = readIntegrationVerdictFile(integrationVerdictPath(common, head)).output.trim().split('\n');
    assert.equal(tail.at(-1), 'the real failure');
    assert.equal(readIntegrationVerdictFile(integrationVerdictPath(common, head)).error, false);

    // Exit 3 is "could not run" — recorded as an error, not as a broken merge.
    CONFIG.integrationCheck = { command: `echo "jq missing"; exit ${INTEGRATION_CHECK_CANNOT_RUN}`, timeoutMs: 30_000 };
    runIntegrationCheck(s(cleanTip));
    assert.equal(readIntegrationVerdictFile(integrationVerdictPath(common, head)).error, true);

    // A lock left by a killed run (its pid no longer exists) is taken over.
    const lock = path.join(common, 'pr-land', 'integration.lock');
    fs.writeFileSync(lock, '999999999');
    CONFIG.integrationCheck = { command: 'node use.js', timeoutMs: 30_000 };
    assert.equal(runIntegrationCheck(s(cleanTip)), true);
    assert.equal(fs.existsSync(lock), false, 'the lock is released after the run');
    assert.equal(execSync('git rev-parse HEAD', { cwd: root }).toString().trim(), head, 'the branch did not move');
    assert.equal(execSync('git status --porcelain', { cwd: root }).toString(), '', 'the checkout is untouched');
    assert.equal(execSync('git worktree list', { cwd: root }).toString().trim().split('\n').length, 1, 'the scratch worktree is reaped');
  } finally {
    process.chdir(cwd);
    CONFIG.integrationCheck = saved;
    if (savedScratch === undefined) delete process.env.PR_LAND_SCRATCH_DIR;
    else process.env.PR_LAND_SCRATCH_DIR = savedScratch;
  }
});

test('the integration scratch root is on disk, never os.tmpdir() or the git dir', () => {
  const home = os.homedir();
  assert.equal(integrationScratchRoot({}), path.join(home, '.cache', 'pr-land'));
  assert.equal(integrationScratchRoot({ XDG_CACHE_HOME: '/srv/cache' }), '/srv/cache/pr-land');
  assert.equal(integrationScratchRoot({ XDG_CACHE_HOME: 'relative' }), path.join(home, '.cache', 'pr-land'), 'a relative XDG value is ignored');
  assert.equal(integrationScratchRoot({ PR_LAND_SCRATCH_DIR: '/big/disk', XDG_CACHE_HOME: '/srv/cache' }), '/big/disk');
  assert.ok(!integrationScratchRoot({ TMPDIR: '/tmp' }).startsWith(os.tmpdir() + path.sep));
});

test('the integration check builds and runs under the scratch root, with its TMPDIR there, and leaves nothing', () => {
  const { root, head, cleanTip } = repoWithSemanticBreak();
  const scratch = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'land-scratch-')));
  const report = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'land-report-')), 'seen');
  const cwd = process.cwd();
  const saved = CONFIG.integrationCheck;
  const savedScratch = process.env.PR_LAND_SCRATCH_DIR;
  process.env.PR_LAND_SCRATCH_DIR = path.join(scratch, 'pr-land');
  CONFIG.integrationCheck = {
    command: `{ pwd -P; echo "$TMPDIR"; test -d "$TMPDIR" && echo tmp-exists; } > '${report}' && node use.js`,
    timeoutMs: 30_000,
  };
  try {
    process.chdir(root);
    assert.equal(runIntegrationCheck({ branch: { headSha: head }, baseTip: cleanTip, base: { scope: 'shared' } }), true);
    const [ranIn, tmpdir, tmpExists] = fs.readFileSync(report, 'utf8').trim().split('\n');
    const under = path.join(scratch, 'pr-land') + path.sep;
    assert.ok(ranIn.startsWith(under), `ran in ${ranIn}, not under ${under}`);
    assert.ok(tmpdir.startsWith(under), `TMPDIR ${tmpdir} is not under ${under}`);
    assert.equal(tmpExists, 'tmp-exists');
    assert.deepEqual(fs.readdirSync(path.join(scratch, 'pr-land')), [], 'the scratch tree is deleted after the run');
  } finally {
    process.chdir(cwd);
    CONFIG.integrationCheck = saved;
    if (savedScratch === undefined) delete process.env.PR_LAND_SCRATCH_DIR;
    else process.env.PR_LAND_SCRATCH_DIR = savedScratch;
  }
});

test('exit codes are stable — agents branch on these', () => {
  assert.deepEqual(EXIT, {
    MERGED: 0,
    CI_RED: 10,
    CHANGES_REQUESTED: 20,
    NEEDS_HUMAN: 30,
    PREFLIGHT: 40,
  });
});

test('paths ["**"] means the workflow has no path filter and always runs', () => {
  const all = loadConfig(repoWithConfig({ ciGates: [{ workflow: 'ci.yml', paths: ['**'] }] }));
  assert.equal(ciCovers(['docs/anything.md'], all), true);
  assert.equal(ciCovers([], all), false, 'an empty diff still covers nothing');
});

// ---------------------------------------------------------------------------
// The skip discriminator.
//
// Fixtures are the real `gh pr checks --json name,state,link` payloads from
// ordago-app/ordago-apps#777 at head 898853d, recorded 2026-08-28. That PR is
// the useful one because it carries BOTH kinds of skip on one head SHA:
//
//   · "Emulators · Vitest (functions) + E2E (shared)" and "request-review" —
//     `needs: [changes, lint-and-unit]` in develop-tests.yml, skipped because
//     lint-and-unit FAILED. These lanes never ran and must block.
//   · "Build image + deploy to Cloud Run" — `if: github.event_name == 'push'`
//     in web-build.yml, skipped by design on every PR. Must NOT block, or every
//     PR in every consuming repo wedges.
//
// Both are `conclusion: skipped` with a null runner and no steps. The run each
// sits in is what separates them.
// ---------------------------------------------------------------------------

const { checksVerdict, runIdOf, gatesFor, requiredLanesFor } = require('../pr-land.js');

const CI_RUN = 'https://github.com/ordago-app/ordago-apps/actions/runs/33126802372/job/';
const WEB_RUN = 'https://github.com/ordago-app/ordago-apps/actions/runs/33126802218/job/';

/** ordago-apps#777 @ 898853d — the mixed-skip fixture. */
const pr777 = [
  { name: 'Container build (build stage only)', state: 'SUCCESS', link: WEB_RUN + '98713102462' },
  { name: 'Build image + deploy to Cloud Run', state: 'SKIPPED', link: WEB_RUN + '98713103662' },
  { name: 'Emulators · Vitest (functions) + E2E (shared)', state: 'SKIPPED', link: CI_RUN + '98711028084' },
  { name: 'request-review', state: 'SKIPPED', link: CI_RUN + '98711028301' },
  { name: 'Lint + Unit (app · shared · functions)', state: 'FAILURE', link: CI_RUN + '98706784606' },
  { name: 'Next.js lint + build', state: 'SUCCESS', link: WEB_RUN + '98706784156' },
  { name: 'Detect affected areas', state: 'SUCCESS', link: CI_RUN + '98706784727' },
];

const named = (v) => v.failures.map((c) => c.name).sort();

test('runIdOf reads the run out of a check link', () => {
  assert.equal(runIdOf(CI_RUN + '98711028084'), '33126802372');
  assert.equal(runIdOf(''), null);
  assert.equal(runIdOf(undefined), null);
});

test('a dependency-cascade skip blocks; a path-filtered skip in a clean run does not', () => {
  const v = checksVerdict(pr777, {});
  assert.equal(v.state, 'red');
  assert.deepEqual(named(v), [
    'Emulators · Vitest (functions) + E2E (shared)',
    'Lint + Unit (app · shared · functions)',
    'request-review',
  ]);
  // The legitimate skip sits in a run with no failed sibling and stays silent.
  assert.ok(!named(v).includes('Build image + deploy to Cloud Run'));
});

test('a blocking skip says why, so the log does not read as a mystery', () => {
  const v = checksVerdict(pr777, {});
  const em = v.failures.find((c) => c.name.startsWith('Emulators'));
  assert.match(em.why, /failed or was cancelled/);
  // A genuine failure is reported as itself, not dressed up as a skip.
  assert.equal(v.failures.find((c) => c.name.startsWith('Lint')).why, undefined);
});

test('the incident: a skipped required suite alone is red, not green', () => {
  // The counterfactual that made #772 dangerous — the starved upstream had it
  // been re-run green is removed here, leaving only the cancelled cause. Before
  // this guard the gate saw one SUCCESS and called the PR mergeable.
  const v = checksVerdict([
    { name: 'Lint + Unit (app · shared · functions)', state: 'CANCELLED', link: CI_RUN + '1' },
    { name: 'Emulators · Vitest (functions) + E2E (shared)', state: 'SKIPPED', link: CI_RUN + '2' },
    { name: 'Detect affected areas', state: 'SUCCESS', link: CI_RUN + '3' },
  ], {});
  assert.equal(v.state, 'red');
  assert.deepEqual(named(v), ['Emulators · Vitest (functions) + E2E (shared)']);
});

test('an all-green run with only by-design skips is still green', () => {
  const v = checksVerdict([
    { name: 'Next.js lint + build', state: 'SUCCESS', link: WEB_RUN + '1' },
    { name: 'Build image + deploy to Cloud Run', state: 'SKIPPED', link: WEB_RUN + '2' },
    { name: 'Lint + Unit (app · shared · functions)', state: 'SUCCESS', link: CI_RUN + '1' },
  ], {});
  assert.equal(v.state, 'green');
  assert.deepEqual(v.failures, []);
});

test('a skip is judged by its own run, not by any red anywhere on the PR', () => {
  // Cross-run contamination would be the wedge: `needs:` cannot span workflows,
  // so a failure in the CI run says nothing about a skip in the Web run.
  const v = checksVerdict([
    { name: 'Lint + Unit (app · shared · functions)', state: 'FAILURE', link: CI_RUN + '1' },
    { name: 'Build image + deploy to Cloud Run', state: 'SKIPPED', link: WEB_RUN + '2' },
    { name: 'Next.js lint + build', state: 'SUCCESS', link: WEB_RUN + '1' },
  ], {});
  assert.deepEqual(named(v), ['Lint + Unit (app · shared · functions)']);
});

test('pending is still pending when the only skips are legitimate', () => {
  const v = checksVerdict([
    { name: 'Emulators · Vitest (functions) + E2E (shared)', state: 'QUEUED', link: CI_RUN + '1' },
    { name: 'Build image + deploy to Cloud Run', state: 'SKIPPED', link: WEB_RUN + '2' },
  ], {});
  assert.equal(v.state, 'pending');
});

test('a diff that dispatched nothing is `none`, not red', () => {
  // A docs-only PR: every check skipped, no cause anywhere. This must stay the
  // UNVERIFIED path gated on review, not become an unmergeable PR.
  const v = checksVerdict([
    { name: 'Build image + deploy to Cloud Run', state: 'SKIPPED', link: WEB_RUN + '2' },
    { name: 'Emulators · Vitest (functions) + E2E (shared)', state: 'SKIPPED', link: CI_RUN + '2' },
  ], {});
  assert.equal(v.state, 'none');
  assert.deepEqual(v.failures, []);
});

test('NEUTRAL is still ignored, and an empty list is still `none`', () => {
  assert.equal(checksVerdict([]).state, 'none');
  assert.equal(
    checksVerdict([{ name: 'advisory', state: 'NEUTRAL', link: CI_RUN + '1' }]).state,
    'none',
  );
});

test('requiredLanes is opt-in: an empty list keeps the run-level rule alone', () => {
  const stale = [
    // The residual case the run-level signal cannot see: the failed upstream was
    // re-run green, leaving its dependent skipped from the earlier attempt. The
    // run now looks clean, so only a named lane catches it.
    { name: 'Lint + Unit (app · shared · functions)', state: 'SUCCESS', link: CI_RUN + '1' },
    { name: 'Emulators · Vitest (functions) + E2E (shared)', state: 'SKIPPED', link: CI_RUN + '2' },
  ];
  assert.equal(checksVerdict(stale).state, 'green');

  const v = checksVerdict(stale, { requiredLanes: ['Emulators · Vitest (functions) + E2E (shared)'] });
  assert.equal(v.state, 'red');
  assert.match(v.failures[0].why, /required lane/);
});

// ---------------------------------------------------------------------------
// ciGates — one gate per path-filtered workflow.
//
// The shape of ordago-app/ordago-apps, where it was needed: develop-tests.yml
// covers the app/shared/functions tree, console-ci.yml covers only
// apps/ordago-console/. With one ciPaths list, PRs #1053 and #1054 (console
// only) read as UNVERIFIED and merged on review while console CI still ran —
// and adding the console path to that list would have demanded the
// develop-tests lane on diffs that never dispatch it.
// ---------------------------------------------------------------------------

const MAIN_LANE = 'Lint + Unit (app · shared · functions)';
const CONSOLE_LANE = 'Lint + typecheck + unit (ordago-console)';
const CONSOLE_RUN = 'https://github.com/ordago-app/ordago-apps/actions/runs/40000000001/job/';

const gated = loadConfig(repoWithConfig({
  ciGates: [
    { workflow: 'develop-tests.yml', paths: ['apps/ordago-app/', 'packages/shared/', 'pnpm-lock.yaml'], requiredLanes: [MAIN_LANE] },
    { workflow: 'console-ci.yml', paths: ['apps/ordago-console/', 'pnpm-lock.yaml'], requiredLanes: [CONSOLE_LANE] },
  ],
}));

const forDiff = (files) => ({ requiredLanes: requiredLanesFor(files, gated) });

test('ciGates: a console-only diff is covered, and owes only the console lane', () => {
  const files = ['apps/ordago-console/src/page.tsx'];
  assert.equal(ciCovers(files, gated), true);
  assert.deepEqual(gatesFor(files, gated).map((g) => g.workflow), ['console-ci.yml']);
  assert.deepEqual(requiredLanesFor(files, gated), [CONSOLE_LANE]);
});

test('ciGates: a console-only diff waits for the console lane before it registers', () => {
  // Nothing reported yet, and then only a hosted sibling: neither is green.
  assert.equal(checksVerdict([], forDiff(['apps/ordago-console/a.ts'])).state, 'none');
  const early = checksVerdict([
    { name: 'Container build (build stage only)', state: 'SUCCESS', link: CONSOLE_RUN + '1' },
  ], forDiff(['apps/ordago-console/a.ts']));
  assert.equal(early.state, 'pending');
  assert.deepEqual(early.missing, [CONSOLE_LANE]);
});

test('ciGates: a console-only diff is not wedged by the develop-tests lane it never dispatches', () => {
  const v = checksVerdict([
    { name: CONSOLE_LANE, state: 'SUCCESS', link: CONSOLE_RUN + '1' },
    { name: 'Container build (build stage only)', state: 'SUCCESS', link: CONSOLE_RUN + '2' },
  ], forDiff(['apps/ordago-console/a.ts']));
  assert.equal(v.state, 'green');
  assert.deepEqual(v.missing, []);
});

test('ciGates: a mixed diff owes both lanes, and is pending until both report', () => {
  const files = ['apps/ordago-console/a.ts', 'packages/shared/src/b.ts'];
  assert.deepEqual(requiredLanesFor(files, gated).sort(), [CONSOLE_LANE, MAIN_LANE].sort());

  const oneDone = checksVerdict([
    { name: MAIN_LANE, state: 'SUCCESS', link: CI_RUN + '1' },
  ], forDiff(files));
  assert.equal(oneDone.state, 'pending', 'a fast workflow green is not the PR green');
  assert.deepEqual(oneDone.missing, [CONSOLE_LANE]);

  const both = checksVerdict([
    { name: MAIN_LANE, state: 'SUCCESS', link: CI_RUN + '1' },
    { name: CONSOLE_LANE, state: 'SUCCESS', link: CONSOLE_RUN + '1' },
  ], forDiff(files));
  assert.equal(both.state, 'green');
});

test('ciGates: a path shared by two gates dispatches both, and owes both lanes', () => {
  assert.deepEqual(requiredLanesFor(['pnpm-lock.yaml'], gated).sort(), [CONSOLE_LANE, MAIN_LANE].sort());
});

test('ciGates: a skipped required lane of a matching gate still fails', () => {
  const v = checksVerdict([
    { name: CONSOLE_LANE, state: 'SKIPPED', link: CONSOLE_RUN + '1' },
    { name: 'Container build (build stage only)', state: 'SUCCESS', link: CONSOLE_RUN + '2' },
  ], forDiff(['apps/ordago-console/a.ts']));
  assert.equal(v.state, 'red');
  assert.deepEqual(named(v), [CONSOLE_LANE]);
  assert.match(v.failures[0].why, /required lane/);
});

test('ciGates: a skipped lane of a gate the diff does not match is not required', () => {
  // The app-only diff: console-ci.yml never dispatches, so its lane showing up
  // SKIPPED (or not at all) says nothing about this PR.
  const v = checksVerdict([
    { name: MAIN_LANE, state: 'SUCCESS', link: CI_RUN + '1' },
    { name: CONSOLE_LANE, state: 'SKIPPED', link: CONSOLE_RUN + '1' },
  ], forDiff(['apps/ordago-app/screens/Home.tsx']));
  assert.equal(v.state, 'green');
});

test('ciGates: a docs-only diff matches no gate and owes nothing — still UNVERIFIED', () => {
  assert.equal(ciCovers(['docs/x.md'], gated), false);
  assert.deepEqual(requiredLanesFor(['docs/x.md'], gated), []);
});

test('legacy ciPaths/requiredLanes load as one gate with unchanged behaviour', () => {
  const legacy = loadConfig(repoWithConfig({ ciPaths: ['src/'], requiredLanes: [MAIN_LANE] }));
  assert.deepEqual(legacy.ciGates, [{ workflow: null, paths: ['src/'], requiredLanes: [MAIN_LANE] }]);
  assert.equal('ciPaths' in legacy, false, 'one representation after load');

  assert.equal(ciCovers(['src/a.ts'], legacy), true);
  assert.equal(ciCovers(['docs/a.md'], legacy), false);
  assert.deepEqual(requiredLanesFor(['src/a.ts'], legacy), [MAIN_LANE]);
  assert.deepEqual(requiredLanesFor(['docs/a.md'], legacy), [], 'no CI, no required lane');

  const skipped = checksVerdict([
    { name: 'other', state: 'SUCCESS', link: CI_RUN + '1' },
    { name: MAIN_LANE, state: 'SKIPPED', link: CI_RUN + '2' },
  ], { requiredLanes: requiredLanesFor(['src/a.ts'], legacy) });
  assert.equal(skipped.state, 'red');

  const legacyAll = loadConfig(repoWithConfig({ ciPaths: ['**'] }));
  assert.equal(ciCovers(['anything.md'], legacyAll), true);
});

test('ciGates: malformed or ambiguous config is rejected at load', () => {
  const bad = (c) => () => loadConfig(repoWithConfig(c));
  assert.throws(bad({ ciGates: [{ workflow: 'a.yml', paths: ['x/'] }], ciPaths: ['y/'] }), /not both/);
  assert.throws(bad({ ciGates: {} }), /must be an array/);
  assert.throws(bad({ ciGates: [{ paths: ['x/'] }] }), /workflow/);
  assert.throws(bad({ ciGates: [{ workflow: 'a.yml', paths: [] }] }), /paths/);
  assert.throws(bad({ ciGates: [{ workflow: 'a.yml', paths: ['x/'], requiredLanes: 'Lint' }] }), /requiredLanes/);
  assert.deepEqual(loadConfig(repoWithConfig({ ciGates: [{ workflow: 'a.yml', paths: ['x/'] }] })).ciGates[0].requiredLanes, []);
});

// --- the merge queue, as observed --------------------------------------------

test('queue state: only removals after the current head count against it', () => {
  const { queueStateFrom } = require('../pr-land.js');
  const q = queueStateFrom({
    id: 'PR_1',
    isMergeQueueEnabled: true,
    isInMergeQueue: false,
    mergeQueueEntry: null,
    commits: { nodes: [{ commit: { committedDate: '2026-10-06T10:00:00Z' } }] },
    timelineItems: {
      nodes: [
        { createdAt: '2026-10-06T09:00:00Z', reason: 'an older head' },
        { createdAt: '2026-10-06T10:30:00Z', reason: 'this head' },
        {},
      ],
    },
  });
  assert.equal(q.enabled, true);
  assert.deepEqual(q.failures.map((f) => f.reason), ['this head']);
});

test('queue state: the removal that merged the PR is a merge, not a failure', () => {
  const { queueStateFrom } = require('../pr-land.js');
  const q = queueStateFrom({
    id: 'PR_1',
    isMergeQueueEnabled: true,
    isInMergeQueue: false,
    mergeQueueEntry: null,
    commits: { nodes: [{ commit: { committedDate: '2026-10-06T10:00:00Z' } }] },
    timelineItems: { nodes: [{ createdAt: '2026-10-06T10:30:00Z', reason: 'MERGED' }] },
  });
  assert.equal(q.merged, true);
  assert.deepEqual(q.failures, []);
});

test('queue state: an unreadable PR is unknown, never "no queue"', () => {
  const { queueStateFrom } = require('../pr-land.js');
  assert.equal(queueStateFrom(null), null);
});

// A branch NAME is not an identity: an old merged PR from a reused name answers
// `gh pr list --head <name>` too, and read as this branch's PR it reported
// "already merged" and exited 0 without pushing the new work.
const { pickBranchPr } = require('../pr-land.js');

test('pickBranchPr: a merged PR the branch has moved past is not this branch\'s PR', () => {
  const old = { number: 895, state: 'MERGED', baseRefName: 'develop', headRefOid: 'old' };
  assert.equal(pickBranchPr([old], { baseBranch: 'develop', carriesWorkBeyond: () => true }), null);
});

test('pickBranchPr: a merged PR that holds all of HEAD is still the landed one', () => {
  const landed = { number: 1441, state: 'MERGED', baseRefName: 'develop', headRefOid: 'head' };
  assert.equal(pickBranchPr([landed], { baseBranch: 'develop', carriesWorkBeyond: () => false }), landed);
});

test('pickBranchPr: an open PR wins over a newer-looking concluded one', () => {
  const merged = { number: 900, state: 'MERGED', baseRefName: 'develop', headRefOid: 'a' };
  const open = { number: 901, state: 'OPEN', baseRefName: 'develop', headRefOid: 'b' };
  const never = () => { throw new Error('an open PR needs no ancestry check'); };
  assert.equal(pickBranchPr([merged, open], { baseBranch: 'develop', carriesWorkBeyond: never }), open);
});

test('pickBranchPr: a closed PR is left for decide() to hand to a human', () => {
  const closed = { number: 902, state: 'CLOSED', baseRefName: 'develop', headRefOid: 'c' };
  assert.equal(pickBranchPr([closed], { baseBranch: 'develop', carriesWorkBeyond: () => true }), closed);
});

test('pickBranchPr: PRs into another base are ignored', () => {
  const release = { number: 903, state: 'MERGED', baseRefName: 'beta', headRefOid: 'd' };
  assert.equal(pickBranchPr([release], { baseBranch: 'develop', carriesWorkBeyond: () => false }), null);
});

// The ancestry half, against real repos. The old PR's head is pushed only as
// `refs/pull/7/head` — what a squash merge leaves behind — so the clone has to
// fetch it before it can tell whether HEAD moved past it.
const { headCarriesWorkBeyond } = require('../pr-land.js');

// The fixture's base is whatever base the loaded config names: pr-land.js reads the config of
// the directory the tests run from, and a consumer's copy runs them from a repo whose base is `main`.
const BASE = require('../pr-land.js').CONFIG.baseBranch;

function repoWithOldPrHead() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'land-reuse-'));
  const run = (cwd, cmd) => execSync(`git -c user.name=t -c user.email=t@t ${cmd}`, { cwd, stdio: 'pipe' }).toString().trim();
  const origin = path.join(tmp, 'origin.git');
  const seed = path.join(tmp, 'seed');
  const clone = path.join(tmp, 'clone');
  run(tmp, `init -q --bare -b ${BASE} ${origin}`);
  run(tmp, `clone -q ${origin} ${seed}`);
  run(seed, `checkout -qb ${BASE}`);
  fs.writeFileSync(path.join(seed, 'a.txt'), 'base\n');
  run(seed, 'add . && git -c user.name=t -c user.email=t@t commit -qm base');
  run(seed, `push -q origin ${BASE}`);
  run(seed, 'checkout -qb old');
  fs.writeFileSync(path.join(seed, 'old.txt'), 'old\n');
  run(seed, 'add . && git -c user.name=t -c user.email=t@t commit -qm old');
  const oldHead = run(seed, 'rev-parse HEAD');
  run(seed, 'push -q origin HEAD:refs/pull/7/head');
  // file:// rather than a path: a local clone hardlinks every object, the PR ref's included.
  run(tmp, `clone -q -b ${BASE} file://${origin} ${clone}`);
  return { clone, oldHead, run };
}

function inRepo(dir, fn) {
  const cwd = process.cwd();
  process.chdir(dir);
  try { return fn(); } finally { process.chdir(cwd); }
}

test('headCarriesWorkBeyond: new work on a reused name, with the old head fetched from the PR ref', () => {
  const { clone, oldHead, run } = repoWithOldPrHead();
  run(clone, 'checkout -qb reused');
  fs.writeFileSync(path.join(clone, 'new.txt'), 'new\n');
  run(clone, 'add . && git -c user.name=t -c user.email=t@t commit -qm new');
  assert.throws(() => run(clone, `cat-file -e ${oldHead}^{commit}`), 'the old head starts out missing locally');
  assert.equal(inRepo(clone, () => headCarriesWorkBeyond({ number: 7, headRefOid: oldHead })), true);
});

test('headCarriesWorkBeyond: HEAD at the merged head carries nothing beyond it', () => {
  const { clone, oldHead, run } = repoWithOldPrHead();
  run(clone, 'fetch -q origin pull/7/head');
  run(clone, `checkout -qb landed ${oldHead}`);
  assert.equal(inRepo(clone, () => headCarriesWorkBeyond({ number: 7, headRefOid: oldHead })), false);
});

test('headCarriesWorkBeyond: an unreachable head keeps the old reading', () => {
  const { clone } = repoWithOldPrHead();
  const ghost = '0123456789abcdef0123456789abcdef01234567';
  assert.equal(inRepo(clone, () => headCarriesWorkBeyond({ number: 99, headRefOid: ghost })), false);
});

// A base that bumps a submodule: the rebase moves the recorded commit, and the
// checkout must follow it, or the next run's preflight reads the tree as dirty.
const { rebaseOnto } = require('../pr-land.js');

/** A repo whose `develop` bumped submodule `lib` to `bumpTo` (a real commit by default) after `feature` branched. */
function repoWithSubmoduleBump({ bumpTo } = {}) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'land-submodule-'));
  const run = (cwd, cmd) => execSync(`git -c user.name=t -c user.email=t@t -c protocol.file.allow=always ${cmd}`, { cwd, stdio: 'pipe' }).toString().trim();
  const commit = (cwd, msg) => run(cwd, `commit -qam ${msg}`);
  const lib = path.join(tmp, 'lib');
  run(tmp, `init -q -b main ${lib}`);
  fs.writeFileSync(path.join(lib, 'v.txt'), '1\n');
  run(lib, 'add .');
  commit(lib, 'v1');
  const repo = path.join(tmp, 'repo');
  run(tmp, `init -q -b develop ${repo}`);
  run(repo, `submodule add -q ${lib} lib`);
  commit(repo, 'base');
  run(repo, 'checkout -qb feature');
  fs.writeFileSync(path.join(repo, 'f.txt'), 'f\n');
  run(repo, 'add f.txt');
  commit(repo, 'feature');

  run(repo, 'checkout -q develop');
  if (bumpTo) {
    run(repo, `update-index --cacheinfo 160000,${bumpTo},lib`);
  } else {
    fs.writeFileSync(path.join(lib, 'v.txt'), '2\n');
    commit(lib, 'v2');
    run(path.join(repo, 'lib'), 'pull -q origin main');
    run(repo, 'add lib');
  }
  run(repo, 'commit -qm bump');
  const bumped = run(repo, 'rev-parse develop:lib');
  run(repo, 'checkout -q feature');
  run(repo, 'submodule update');
  return { repo, run, bumped };
}

/** Runs `fn` with the git config the rebase needs on a bare CI runner: an identity, and file:// submodules. */
function withRebaseGitEnv(fn) {
  const gitEnv = {
    GIT_CONFIG_COUNT: '3',
    GIT_CONFIG_KEY_0: 'protocol.file.allow', GIT_CONFIG_VALUE_0: 'always',
    GIT_CONFIG_KEY_1: 'user.name', GIT_CONFIG_VALUE_1: 't',
    GIT_CONFIG_KEY_2: 'user.email', GIT_CONFIG_VALUE_2: 't@t',
  };
  Object.assign(process.env, gitEnv);
  try {
    return fn();
  } finally {
    for (const key of Object.keys(gitEnv)) delete process.env[key];
  }
}

test('rebaseOnto: a base that bumped a submodule leaves the tree clean, at the recorded commit', () => {
  const { repo, run, bumped } = repoWithSubmoduleBump();
  withRebaseGitEnv(() => inRepo(repo, () => rebaseOnto('develop')));
  assert.equal(run(repo, 'status --porcelain'), '', 'no submodule left behind its recorded commit');
  assert.equal(run(path.join(repo, 'lib'), 'rev-parse HEAD'), bumped);
});

test('rebaseOnto: a submodule that cannot be updated is reported, with the rebase kept for the next run', () => {
  const unreachable = '0123456789abcdef0123456789abcdef01234567';
  const { repo, run } = repoWithSubmoduleBump({ bumpTo: unreachable });
  assert.throws(
    () => withRebaseGitEnv(() => inRepo(repo, () => rebaseOnto('develop'))),
    /rebased, but `git submodule update` failed — run it, then re-run/,
  );
  assert.equal(run(repo, 'rev-parse --abbrev-ref HEAD'), 'feature', 'the branch is not left mid-rebase');
  assert.equal(run(repo, 'merge-base --is-ancestor develop HEAD && echo yes'), 'yes', 'the rebase itself is kept');
  assert.equal(run(repo, 'rev-parse HEAD:lib'), unreachable, 'the tree records the base\'s pointer');
});
