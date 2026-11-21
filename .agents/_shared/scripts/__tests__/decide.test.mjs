import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { decide, EXIT, ACTION } = require('../decide.js');
const { baseMovement } = require('../pr-land.js');

// The point of extracting decide(): the ENTIRE decision table is now testable
// with no network, no git, no gh. The pipeline version could only be tested by
// running it against a real pull request, which is why all ten of its bugs were
// found in production — one of them by successfully merging.

/** A healthy PR one step from merging. Each test perturbs one field. */
const base = () => ({
  baseBranch: 'develop',
  requireApprovingReview: true,
  maxReviewRounds: 5,
  roundsExhausted: 'handoff',
  branch: {
    name: 'feat/x', headSha: 'abc', isProtected: false, dirty: false, dirtyFiles: '',
    rebaseConflict: false, ahead: 2, pushed: true, remoteBehind: false, inWorktree: true,
  },
  pr: { number: 7, state: 'open', url: 'https://example/7', isDraft: false, headSha: 'abc', reviews: [], mergeable: 'MERGEABLE' },
  checks: { state: 'green', failures: [] },
  review: { state: 'approved', rounds: 1, body: '' },
  remoteBranchExists: true,
  files: ['src/a.ts'],
  ciWillRun: true,
  gated: [],
  base: { overlap: [], blast: [], needsRebase: false, needsIntegrationCheck: false },
  baseTip: 'tip',
  integration: { state: 'not-needed', output: '', verdictFile: null },
  checksDeadlinePassed: false,
  reviewDeadlinePassed: false,
});

const withState = (patch) => ({ ...base(), ...patch });

// --- a PR that cannot be merged at all --------------------------------------
//
// The failure these cover is not "the gate let something through" but "the gate
// reported the wrong cause": a conflicting PR dispatches no runs, so the checks
// wait timed out into a confident claim about runner starvation.

test('a conflicting PR is reported as conflicting, not waited on', () => {
  const s = withState({
    pr: { ...base().pr, mergeable: 'CONFLICTING' },
    checks: { state: 'none', failures: [] },
  });
  const d = decide(s);
  assert.equal(d.exit, EXIT.PREFLIGHT);
  assert.match(d.why, /conflicts with develop/);
  assert.match(d.detail, /git rebase origin\/develop/);
});

test('a conflicting PR is caught before the checks deadline can misattribute it', () => {
  const s = withState({
    pr: { ...base().pr, mergeable: 'CONFLICTING' },
    checks: { state: 'none', failures: [] },
    checksDeadlinePassed: true,
  });
  const d = decide(s);
  assert.equal(d.exit, EXIT.PREFLIGHT);
  assert.doesNotMatch(d.detail, /starved of runners/);
});

test('UNKNOWN mergeability is not a verdict — GitHub computes it lazily', () => {
  const s = withState({
    pr: { ...base().pr, mergeable: 'UNKNOWN' },
    checks: { state: 'pending', failures: [] },
  });
  assert.equal(decide(s).action, ACTION.WAIT_CHECKS);
});

test('a merged PR is terminal even though GitHub reports it UNKNOWN afterwards', () => {
  const s = withState({
    pr: { ...base().pr, state: 'merged', mergeable: 'UNKNOWN' },
    remoteBranchExists: false,
  });
  assert.equal(decide(s).exit, EXIT.MERGED);
});

test('a conflicting PR still reports the conflict rather than a stale approval', () => {
  const s = withState({
    pr: { ...base().pr, mergeable: 'CONFLICTING' },
    checks: { state: 'green', failures: [] },
  });
  assert.equal(decide(s).exit, EXIT.PREFLIGHT);
});

// --- the happy path ---------------------------------------------------------

test('green, approved, current and ungated → merge', () => {
  assert.equal(decide(base()).action, ACTION.MERGE);
});

// --- terminal states are checked FIRST --------------------------------------
//
// This ordering is the fix for the bug that reported a successfully merged PR as
// exit 40: a merged branch is legitimately 0 commits ahead, and its worktree may
// be anything at all. Preconditions must not run on an already-finished PR.

test('a merged PR whose remote branch is gone → exit 0', () => {
  const s = withState({ pr: { ...base().pr, state: 'merged' }, remoteBranchExists: false });
  assert.equal(decide(s).exit, EXIT.MERGED);
});

test('a merged PR whose remote branch remains → delete it, do not report failure', () => {
  const s = withState({ pr: { ...base().pr, state: 'merged' }, remoteBranchExists: true });
  assert.equal(decide(s).action, ACTION.DELETE_REMOTE);
});

test('merged wins over every precondition — 0 ahead, dirty, in a worktree', () => {
  const s = withState({
    pr: { ...base().pr, state: 'merged' },
    remoteBranchExists: false,
    branch: { ...base().branch, ahead: 0, dirty: true, dirtyFiles: 'M x' },
  });
  assert.equal(decide(s).exit, EXIT.MERGED, 'a landed PR must never be reported as a failure');
});

test('a closed-unmerged PR hands off rather than merging around the human', () => {
  const s = withState({ pr: { ...base().pr, state: 'closed' } });
  const d = decide(s);
  assert.equal(d.exit, EXIT.NEEDS_HUMAN);
  assert.match(d.detail, /do not merge around a human decision/);
});

// --- preconditions ----------------------------------------------------------

test('refuses to land from a protected branch', () => {
  const s = withState({ branch: { ...base().branch, isProtected: true, name: 'develop' } });
  assert.equal(decide(s).exit, EXIT.PREFLIGHT);
});

test('refuses on a dirty tree', () => {
  assert.equal(decide(withState({ branch: { ...base().branch, dirty: true } })).exit, EXIT.PREFLIGHT);
});

test('refuses mid-rebase', () => {
  assert.equal(decide(withState({ branch: { ...base().branch, rebaseConflict: true } })).exit, EXIT.PREFLIGHT);
});

test('no PR and nothing ahead → nothing to land', () => {
  const s = withState({ pr: { state: 'none', reviews: [] }, branch: { ...base().branch, ahead: 0 } });
  assert.equal(decide(s).exit, EXIT.PREFLIGHT);
});

// --- getting onto a PR ------------------------------------------------------

test('unpushed branch → push', () => {
  assert.equal(decide(withState({ branch: { ...base().branch, pushed: false } })).action, ACTION.PUSH);
});

test('remote behind local → push', () => {
  assert.equal(decide(withState({ branch: { ...base().branch, remoteBehind: true } })).action, ACTION.PUSH);
});

test('pushed but no PR → open one', () => {
  assert.equal(decide(withState({ pr: { state: 'none', reviews: [] } })).action, ACTION.OPEN_PR);
});

test('a draft PR hands off — drafts skip CI and review by design', () => {
  assert.equal(decide(withState({ pr: { ...base().pr, isDraft: true } })).exit, EXIT.NEEDS_HUMAN);
});

// --- verification -----------------------------------------------------------

test('red CI → exit 10, and says not to assume it is your code', () => {
  const s = withState({ checks: { state: 'red', failures: [{ name: 'Lint', link: 'l' }] } });
  const d = decide(s);
  assert.equal(d.exit, EXIT.CI_RED);
  assert.match(d.detail, /infrastructure failure/i);
});

test('pending CI → wait', () => {
  assert.equal(decide(withState({ checks: { state: 'pending', failures: [] } })).action, ACTION.WAIT_CHECKS);
});

test('no checks yet, but CI should run → wait, never read as green', () => {
  assert.equal(decide(withState({ checks: { state: 'none', failures: [] } })).action, ACTION.WAIT_CHECKS);
});

test('vacuous green: no CI lane matches, so `none` is the steady state → proceed on review', () => {
  const s = withState({ ciWillRun: false, checks: { state: 'none', failures: [] } });
  assert.equal(decide(s).action, ACTION.MERGE, 'an unverified PR still lands on an approving review');
});

test('CI that never settles hands off rather than waiting forever', () => {
  const s = withState({ checks: { state: 'pending', failures: [] }, checksDeadlinePassed: true });
  const d = decide(s);
  assert.equal(d.exit, EXIT.NEEDS_HUMAN);
  assert.match(d.detail, /starved of runners/);
});

test('a required lane that never reported is named at the deadline, not guessed at', () => {
  // The likeliest cause is a gate whose paths drifted from its workflow's filter,
  // so that is what the message points at first.
  const missing = 'Lint + typecheck + unit (ordago-console)';
  const s = withState({ checks: { state: 'pending', failures: [], missing: [missing] }, checksDeadlinePassed: true });
  const d = decide(s);
  assert.equal(d.exit, EXIT.NEEDS_HUMAN);
  assert.ok(d.detail.includes(missing));
  assert.match(d.detail, /mirror/);
});

// --- review -----------------------------------------------------------------

test('changes requested → exit 20 with the findings', () => {
  const s = withState({ review: { state: 'changes_requested', rounds: 1, body: 'finding' } });
  const d = decide(s);
  assert.equal(d.exit, EXIT.CHANGES_REQUESTED);
  assert.match(d.detail, /Fix the cause, not the symptom/);
});

test('rounds exhausted hands off instead of looping', () => {
  const s = withState({ review: { state: 'changes_requested', rounds: 5, body: 'x' } });
  assert.equal(decide(s).exit, EXIT.NEEDS_HUMAN);
});

test('roundsExhausted "merge" lands on green once the budget is spent', () => {
  const s = withState({
    roundsExhausted: 'merge',
    review: { state: 'changes_requested', rounds: 3, body: 'still objecting' },
    maxReviewRounds: 3,
  });
  const d = decide(s);
  assert.equal(d.action, ACTION.MERGE);
  assert.match(d.why, /budget spent \(3\/3 rounds\)/);
});

test('roundsExhausted "merge" still spends every round first', () => {
  const s = withState({
    roundsExhausted: 'merge',
    review: { state: 'changes_requested', rounds: 2, body: 'finding' },
    maxReviewRounds: 3,
  });
  assert.equal(decide(s).exit, EXIT.CHANGES_REQUESTED);
});

test('a spent review budget buys nothing against red CI', () => {
  const s = withState({
    roundsExhausted: 'merge',
    review: { state: 'changes_requested', rounds: 3, body: 'x' },
    maxReviewRounds: 3,
    checks: { state: 'red', failures: [{ name: 'Lint', why: 'failure', link: 'https://example/run' }] },
  });
  assert.equal(decide(s).exit, EXIT.CI_RED);
});

test('a spent review budget buys nothing against the hard-stop gate', () => {
  const s = withState({
    roundsExhausted: 'merge',
    review: { state: 'changes_requested', rounds: 3, body: 'x' },
    maxReviewRounds: 3,
    gated: ['firestore.rules — Firestore security rules'],
  });
  assert.equal(decide(s).exit, EXIT.NEEDS_HUMAN);
});

test('awaiting review → wait', () => {
  assert.equal(decide(withState({ review: { state: 'none', rounds: 0, body: '' } })).action, ACTION.WAIT_REVIEW);
});

test('no review before the deadline names BOTH causes, not just the flattering one', () => {
  const s = withState({ review: { state: 'none', rounds: 0, body: '' }, reviewDeadlinePassed: true });
  const d = decide(s);
  assert.equal(d.exit, EXIT.NEEDS_HUMAN);
  assert.match(d.detail, /NO reviewer wired/);
  assert.match(d.detail, /has not posted yet/);
});

test('a repo with no reviewer requirement merges on green alone', () => {
  const s = withState({ requireApprovingReview: false, review: { state: 'none', rounds: 0, body: '' } });
  assert.equal(decide(s).action, ACTION.MERGE);
});

test('the merge reason never claims a review that was not required', () => {
  // The stated reason is the only record of what bar a merge actually cleared.
  // Two repos here run different bars, so a fixed "green and approved" string
  // would log a review that never happened in one of them.
  const noReview = withState({ requireApprovingReview: false, review: { state: 'none', rounds: 0, body: '' } });
  assert.doesNotMatch(decide(noReview).why, /approved/);
  assert.match(decide(noReview).why, /no review required/);
  assert.match(decide(base()).why, /approved/);
});

test('a gated PR reports the same bar it actually cleared', () => {
  const s = withState({ requireApprovingReview: false, review: { state: 'none', rounds: 0, body: '' }, gated: ['security rules'] });
  const d = decide(s);
  assert.equal(d.exit, EXIT.NEEDS_HUMAN);
  assert.doesNotMatch(d.why, /approved/);
});

// --- integration and the gate green cannot answer ---------------------------

test('base moved into this diff → rebase', () => {
  const s = withState({ base: { overlap: ['src/a.ts'], blast: [], needsRebase: true, needsIntegrationCheck: false } });
  assert.equal(decide(s).action, ACTION.REBASE);
});

test('base moved elsewhere → no rebase, green still holds', () => {
  assert.equal(decide(base()).action, ACTION.MERGE);
});

// The contract between the two files. decide() read `base.needsRebase` while
// pr-land's helper returned `rebase`, so REBASE was unreachable from 2026-08-21
// until this test existed: each side's tests built their own idea of the shape.
// This one feeds decide() the helper's REAL output.
test('CONTRACT: decide() acts on what baseMovement() actually returns', () => {
  const cfg = { sharedBlastRadius: ['packages/shared/'], integrationCheck: { command: 'true', timeoutMs: 1 } };
  const overlap = withState({ base: baseMovement(['src/a.ts'], ['src/a.ts'], cfg) });
  assert.equal(decide(overlap).action, ACTION.REBASE);
  const blast = withState({
    base: baseMovement(['packages/shared/x.ts'], ['src/a.ts'], cfg),
    integration: { state: 'pending', output: '', verdictFile: '/v' },
  });
  assert.equal(decide(blast).action, ACTION.INTEGRATION_CHECK);
});

const overlapping = { overlap: ['src/a.ts'], blast: [], needsRebase: true, needsIntegrationCheck: false };

test('a PR that must rebase does it BEFORE waiting on CI, not after it is green', () => {
  const s = withState({ base: overlapping, checks: { state: 'pending', failures: [] } });
  assert.equal(decide(s).action, ACTION.REBASE);
});

test('a PR that must rebase does it before waiting on review', () => {
  const s = withState({ base: overlapping, review: { state: 'none', rounds: 1, body: '' } });
  assert.equal(decide(s).action, ACTION.REBASE);
});

test('findings are reported while CI is still running — the fix push supersedes that run', () => {
  const s = withState({
    checks: { state: 'pending', failures: [] },
    review: { state: 'changes_requested', rounds: 1, body: 'null deref in foo()' },
  });
  const d = decide(s);
  assert.equal(d.exit, EXIT.CHANGES_REQUESTED);
  assert.match(d.detail, /null deref/);
});

test('findings go back to the author before a rebase', () => {
  const s = withState({ base: overlapping, review: { state: 'changes_requested', rounds: 1, body: 'x' } });
  assert.equal(decide(s).exit, EXIT.CHANGES_REQUESTED);
});

test('an approval alone still waits for CI', () => {
  const s = withState({ checks: { state: 'pending', failures: [] } });
  assert.equal(decide(s).action, ACTION.WAIT_CHECKS);
});

test('red CI goes back to the author before any rebase', () => {
  const s = withState({ base: overlapping, checks: { state: 'red', failures: [] } });
  assert.equal(decide(s).exit, EXIT.CI_RED);
});

// --- the local integration check --------------------------------------------

const blastOnly = { overlap: [], blast: ['packages/shared/x.ts'], needsRebase: false, needsIntegrationCheck: true };

test('a blast-radius move is checked locally once the PR is green and approved', () => {
  const s = withState({ base: blastOnly, integration: { state: 'pending', output: '', verdictFile: '/v' } });
  assert.equal(decide(s).action, ACTION.INTEGRATION_CHECK);
});

test('the local check waits for CI and review — the base keeps moving until then', () => {
  const s = withState({
    base: blastOnly,
    integration: { state: 'pending', output: '', verdictFile: '/v' },
    checks: { state: 'pending', failures: [] },
  });
  assert.equal(decide(s).action, ACTION.WAIT_CHECKS);
});

test('a passing merge result merges without a rebase', () => {
  const s = withState({ base: blastOnly, integration: { state: 'pass', output: '', verdictFile: '/v' } });
  assert.equal(decide(s).action, ACTION.MERGE);
});

test('a check that could not run hands over — it says nothing about the PR', () => {
  const s = withState({ base: blastOnly, integration: { state: 'error', output: 'jq missing', verdictFile: '/v.json' } });
  const d = decide(s);
  assert.equal(d.exit, EXIT.NEEDS_HUMAN);
  assert.match(d.detail, /jq missing/);
  assert.doesNotMatch(d.detail, /git rebase/, 'never tell the author to fix code over a machine problem');
});

test('a failing merge result is red, with the output and how to retry', () => {
  const s = withState({ base: blastOnly, integration: { state: 'fail', output: 'TS2339 foo', verdictFile: '/v.json' } });
  const d = decide(s);
  assert.equal(d.exit, EXIT.CI_RED);
  assert.match(d.detail, /TS2339 foo/);
  assert.match(d.detail, /git rebase origin\/develop/);
  assert.match(d.detail, /\/v\.json/);
});

test('gated paths hand off even when green, approved and current', () => {
  const s = withState({ gated: ['firestore.rules — security rules'] });
  const d = decide(s);
  assert.equal(d.exit, EXIT.NEEDS_HUMAN);
  assert.match(d.detail, /firestore\.rules/);
});

test('the gate is checked AFTER rebase — a stale gated PR rebases first', () => {
  const s = withState({ gated: ['firestore.rules — security rules'], base: overlapping });
  assert.equal(decide(s).action, ACTION.REBASE);
});

test('a gated PR is handed over BEFORE the integration check — the check cannot change that outcome', () => {
  const s = withState({
    gated: ['firestore.rules — security rules'],
    base: blastOnly,
    integration: { state: 'pending', output: '', verdictFile: '/v' },
  });
  assert.equal(decide(s).exit, EXIT.NEEDS_HUMAN);
});

// --- purity -----------------------------------------------------------------

test('decide() does not mutate the state it is given', () => {
  const s = base();
  const snapshot = JSON.stringify(s);
  decide(s);
  assert.equal(JSON.stringify(s), snapshot);
});

// --- regressions from the reconciler's own first run ------------------------

test('SAFETY: a protected branch is refused even when a merged PR exists for it', () => {
  // Real incident. `--head develop` matched merged release PR #669 (develop →
  // beta), the terminal branch fired, and its follow-up action is a branch
  // DELETION — of develop. The protected guard must precede everything,
  // including the terminal check.
  const s = withState({
    branch: { ...base().branch, isProtected: true, name: 'develop' },
    pr: { ...base().pr, state: 'merged' },
    remoteBranchExists: true,
  });
  const d = decide(s);
  assert.equal(d.exit, EXIT.PREFLIGHT, 'must never reach the delete-branch action on a protected branch');
  assert.notEqual(d.action, ACTION.DELETE_REMOTE);
});

test('SAFETY: protected wins over every other terminal and precondition', () => {
  for (const patch of [
    { pr: { ...base().pr, state: 'closed' } },
    { branch: { ...base().branch, isProtected: true, dirty: true } },
    { checks: { state: 'red', failures: [] } },
  ]) {
    const s = withState({ ...patch, branch: { ...base().branch, ...(patch.branch || {}), isProtected: true } });
    assert.equal(decide(s).exit, EXIT.PREFLIGHT);
  }
});

// --- the merge queue ---------------------------------------------------------
//
// A base that requires a queue is observed, not configured, and changes what a
// green PR's last step is: never a direct merge, never a rebase to stay current.

const queued = (q = {}) => ({ id: 'PR_x', enabled: true, inQueue: false, position: null, merged: false, failures: [], ...q });

test('queue: a green, ungated PR is enqueued instead of merged', () => {
  const d = decide(withState({ queue: queued() }));
  assert.equal(d.action, ACTION.ENQUEUE);
});

test('queue: no queue on the base keeps the direct merge', () => {
  const d = decide(withState({ queue: { ...queued(), enabled: false } }));
  assert.equal(d.action, ACTION.MERGE);
});

test('queue: a PR already in the queue waits there', () => {
  const d = decide(withState({ queue: queued({ inQueue: true, position: 2 }) }));
  assert.equal(d.action, ACTION.WAIT_QUEUE);
  assert.match(d.why, /position 2/);
});

test('queue: stuck in the queue past the timeout goes to a human', () => {
  const d = decide(withState({ queue: queued({ inQueue: true }), queueDeadlinePassed: true }));
  assert.equal(d.exit, EXIT.NEEDS_HUMAN);
});

test('queue: one removal is retried — a single flaky lane must not hand back a sound PR', () => {
  const d = decide(withState({ queue: queued({ failures: [{ at: 't1', reason: 'CI failed' }] }) }));
  assert.equal(d.action, ACTION.ENQUEUE);
  assert.match(d.why, /CI failed/);
});

test('queue: a PR the queue merged waits for GitHub to say so — never re-enqueued', () => {
  const d = decide(withState({ queue: queued({ merged: true }) }));
  assert.equal(d.action, ACTION.WAIT_QUEUE);
  assert.equal(d.exit, undefined);
});

test('queue: a second removal of the same head is CI red, with the reasons', () => {
  const failures = [{ at: 't1', reason: 'CI failed' }, { at: 't2', reason: 'CI failed again' }];
  const d = decide(withState({ queue: queued({ failures }) }));
  assert.equal(d.exit, EXIT.CI_RED);
  assert.match(d.detail, /CI failed again/);
});

test('queue: a hard-stopped PR is never enqueued', () => {
  const d = decide(withState({ queue: queued(), gated: ['x — why'] }));
  assert.equal(d.exit, EXIT.NEEDS_HUMAN);
});

test('queue: red CI on the PR head is still red — the queue is not a second chance', () => {
  const d = decide(withState({ queue: queued(), checks: { state: 'red', failures: [] } }));
  assert.equal(d.exit, EXIT.CI_RED);
});

test('queue: a conflicting PR still needs its rebase — the queue cannot build it either', () => {
  const d = decide(withState({ queue: queued(), pr: { ...base().pr, mergeable: 'CONFLICTING' } }));
  assert.equal(d.exit, EXIT.PREFLIGHT);
});

test('queue: base movement never rebases or checks locally when a queue is active', () => {
  const cfg = { rebaseRadius: ['package.json'], sharedBlastRadius: ['packages/shared/'], integrationCheck: null };
  const m = baseMovement(['package.json', 'src/a.ts', 'packages/shared/x.ts'], ['src/a.ts'], cfg, { queue: true });
  assert.equal(m.needsRebase, false);
  assert.equal(m.needsIntegrationCheck, false);
  assert.equal(baseMovement(['src/a.ts'], ['src/a.ts'], cfg).needsRebase, true, 'without a queue, overlap still rebases');
});
