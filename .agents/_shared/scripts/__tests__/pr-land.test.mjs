import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const require = createRequire(import.meta.url);
const { hardStopHits, ciCovers, needsRebase, loadConfig, DEFAULTS, EXIT } = require('../pr-land.js');

// These three predicates decide whether a PR may merge without a human, so they
// are asserted rather than smoke-tested: a false negative auto-merges a security
// rules change. They are pure and take an explicit config, so the shared script
// is testable without any repo's config on disk.

/** A representative consumer config — shape, not any one repo's values. */
const cfg = loadConfig('/nonexistent-so-defaults-apply');
Object.assign(cfg, {
  ciPaths: ['src/', 'functions/', 'package.json', 'pnpm-lock.yaml'],
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
  assert.equal(needsRebase(['src/A.tsx'], ['functions/index.ts'], cfg).rebase, false);
});

test('staleness: direct file overlap forces a rebase', () => {
  const r = needsRebase(['functions/index.ts'], ['functions/index.ts'], cfg);
  assert.equal(r.rebase, true);
  assert.deepEqual(r.overlap, ['functions/index.ts']);
});

test('staleness: shared blast radius forces a rebase without file overlap', () => {
  const r = needsRebase(['pnpm-lock.yaml'], ['src/A.tsx'], cfg);
  assert.equal(r.rebase, true);
  assert.deepEqual(r.overlap, []);
  assert.deepEqual(r.blast, ['pnpm-lock.yaml']);
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

test('ciPaths ["**"] means the repo has no path filter and CI always runs', () => {
  const all = loadConfig('/nonexistent');
  all.ciPaths = ['**'];
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

const { checksVerdict, runIdOf } = require('../pr-land.js');

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
  const v = checksVerdict(pr777, { ciWillRun: true }, cfg);
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
  const v = checksVerdict(pr777, { ciWillRun: true }, cfg);
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
  ], { ciWillRun: true }, cfg);
  assert.equal(v.state, 'red');
  assert.deepEqual(named(v), ['Emulators · Vitest (functions) + E2E (shared)']);
});

test('an all-green run with only by-design skips is still green', () => {
  const v = checksVerdict([
    { name: 'Next.js lint + build', state: 'SUCCESS', link: WEB_RUN + '1' },
    { name: 'Build image + deploy to Cloud Run', state: 'SKIPPED', link: WEB_RUN + '2' },
    { name: 'Lint + Unit (app · shared · functions)', state: 'SUCCESS', link: CI_RUN + '1' },
  ], { ciWillRun: true }, cfg);
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
  ], { ciWillRun: true }, cfg);
  assert.deepEqual(named(v), ['Lint + Unit (app · shared · functions)']);
});

test('pending is still pending when the only skips are legitimate', () => {
  const v = checksVerdict([
    { name: 'Emulators · Vitest (functions) + E2E (shared)', state: 'QUEUED', link: CI_RUN + '1' },
    { name: 'Build image + deploy to Cloud Run', state: 'SKIPPED', link: WEB_RUN + '2' },
  ], { ciWillRun: true }, cfg);
  assert.equal(v.state, 'pending');
});

test('a diff that dispatched nothing is `none`, not red', () => {
  // A docs-only PR: every check skipped, no cause anywhere. This must stay the
  // UNVERIFIED path gated on review, not become an unmergeable PR.
  const v = checksVerdict([
    { name: 'Build image + deploy to Cloud Run', state: 'SKIPPED', link: WEB_RUN + '2' },
    { name: 'Emulators · Vitest (functions) + E2E (shared)', state: 'SKIPPED', link: CI_RUN + '2' },
  ], { ciWillRun: false }, cfg);
  assert.equal(v.state, 'none');
  assert.deepEqual(v.failures, []);
});

test('NEUTRAL is still ignored, and an empty list is still `none`', () => {
  assert.equal(checksVerdict([], {}, cfg).state, 'none');
  assert.equal(
    checksVerdict([{ name: 'advisory', state: 'NEUTRAL', link: CI_RUN + '1' }], {}, cfg).state,
    'none',
  );
});

test('requiredLanes is opt-in: unconfigured repos keep exactly today’s behaviour', () => {
  const stale = [
    // The residual case the run-level signal cannot see: the failed upstream was
    // re-run green, leaving its dependent skipped from the earlier attempt. The
    // run now looks clean, so only a named lane catches it.
    { name: 'Lint + Unit (app · shared · functions)', state: 'SUCCESS', link: CI_RUN + '1' },
    { name: 'Emulators · Vitest (functions) + E2E (shared)', state: 'SKIPPED', link: CI_RUN + '2' },
  ];
  assert.equal(checksVerdict(stale, { ciWillRun: true }, cfg).state, 'green');

  const guarded = { ...cfg, requiredLanes: ['Emulators · Vitest (functions) + E2E (shared)'] };
  const v = checksVerdict(stale, { ciWillRun: true }, guarded);
  assert.equal(v.state, 'red');
  assert.match(v.failures[0].why, /required lane/);

  // And a required lane is only required when CI was meant to run at all.
  assert.equal(checksVerdict(stale, { ciWillRun: false }, guarded).state, 'green');
});
