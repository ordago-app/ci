'use strict';

/**
 * The decision table. PURE — no network, no filesystem, no clock of its own.
 *
 * This file exists because of how `pr-land` failed in practice. Ten bugs in its
 * first day, and not one was in the logic deciding whether a PR may merge. Every
 * one was in PERCEPTION: reading a tool's exit code as if it described the world.
 * `gh pr checks` returns non-zero for "no checks yet", "network down" and "checks
 * failed" alike; `gh pr merge --delete-branch` returned non-zero *after a
 * successful merge* because a local checkout failed afterwards.
 *
 * So the loop is level-triggered, not edge-triggered. It does not remember what
 * it did; each iteration observes the world, asks this function what the world
 * now needs, and does exactly one thing. "Already merged" is not a special case —
 * it is simply an observation whose gap to the desired state is empty.
 *
 * Desired state: the branch is merged into the base, and its remote ref is gone.
 *
 * Keeping this pure is the point: the whole table is testable without a network,
 * which is exactly what the pipeline version made impossible.
 */

const EXIT = { MERGED: 0, CI_RED: 10, CHANGES_REQUESTED: 20, NEEDS_HUMAN: 30, PREFLIGHT: 40 };

const ACTION = {
  PUSH: 'push',
  OPEN_PR: 'open-pr',
  WAIT_CHECKS: 'wait-checks',
  WAIT_REVIEW: 'wait-review',
  REBASE: 'rebase',
  MERGE: 'merge',
  DELETE_REMOTE: 'delete-remote-branch',
};

/**
 * @param {object} s observed state — see observe() in pr-land.js
 * @returns {{action: string, why: string} | {exit: number, why: string, detail?: string}}
 */
function decide(s) {
  // --- absolute guard, before ANYTHING else --------------------------------
  // Nothing about a base or release branch is ever safe to act on, including
  // the "clean up after a merge" path. This nearly deleted `develop`: a merged
  // release PR (develop → beta) made the terminal branch fire while standing on
  // develop, and its follow-up action is a branch deletion. A guard that can be
  // skipped by an earlier branch is not a guard.
  if (s.branch.isProtected) {
    return {
      exit: EXIT.PREFLIGHT,
      why: `refusing to operate on "${s.branch.name}"`,
      detail: 'Work happens on a feature branch, never on a base or release branch.',
    };
  }

  // --- terminal: the desired state is reached ------------------------------
  // Checked before the remaining preconditions. A merged branch is legitimately
  // zero commits ahead and its worktree may be anything; treating those as
  // preflight failures is what made a successful landing report exit 40.
  if (s.pr.state === 'merged') {
    if (s.remoteBranchExists) {
      return { action: ACTION.DELETE_REMOTE, why: `#${s.pr.number} is merged; its remote ref is still there` };
    }
    return { exit: EXIT.MERGED, why: `#${s.pr.number} is merged and its branch is gone` };
  }

  if (s.pr.state === 'closed') {
    return {
      exit: EXIT.NEEDS_HUMAN,
      why: `#${s.pr.number} was closed without merging`,
      detail: 'Someone closed this deliberately. Reopen it or start again — do not merge around a human decision.',
    };
  }

  // --- preconditions on the local checkout ---------------------------------
  if (s.branch.dirty) {
    return { exit: EXIT.PREFLIGHT, why: 'working tree is dirty', detail: s.branch.dirtyFiles };
  }
  if (s.branch.rebaseConflict) {
    return {
      exit: EXIT.PREFLIGHT,
      why: 'a rebase is in progress or conflicted',
      detail: 'Resolve it by hand, then re-run.',
    };
  }
  if (s.pr.state === 'none' && s.branch.ahead === 0) {
    return { exit: EXIT.PREFLIGHT, why: `no commits ahead of ${s.baseBranch} — nothing to land` };
  }

  // --- get the work onto a PR ----------------------------------------------
  if (!s.branch.pushed || s.branch.remoteBehind) {
    return { action: ACTION.PUSH, why: 'the remote branch is missing or behind local' };
  }
  if (s.pr.state === 'none') {
    return { action: ACTION.OPEN_PR, why: 'no pull request exists for this branch yet' };
  }
  if (s.pr.isDraft) {
    return {
      exit: EXIT.NEEDS_HUMAN,
      why: `#${s.pr.number} is a draft`,
      detail: 'Draft PRs skip CI and review by design. Mark it ready, then re-run.',
    };
  }

  // --- can this PR be merged at all? ----------------------------------------
  // Asked BEFORE the checks are waited on, because a conflicting PR dispatches
  // no workflow runs whatsoever: `pull_request` jobs check out
  // refs/pull/<n>/merge, and GitHub cannot build that ref while the merge
  // conflicts. The checks list therefore stays empty forever, and the wait below
  // eventually times out into "a lane may be starved of runners" — a specific,
  // confident, WRONG cause. That message cost an hour of chasing runner capacity
  // on a PR whose only problem was that its base had moved on.
  //
  // Only a DEFINITE conflict counts. Mergeability is computed lazily and reads
  // UNKNOWN until GitHub has done it, which is the normal answer in the seconds
  // after a push; treating that as trouble would fail nearly every PR. Unknown
  // means look again, and the loop does.
  //
  // Reported, never repaired. ACTION.REBASE below exists for a base that moved
  // into a still-mergeable diff; resolving conflicting hunks is judgement about
  // which side is right, and nothing here can supply that.
  if (s.pr.mergeable === 'CONFLICTING') {
    return {
      exit: EXIT.PREFLIGHT,
      why: `#${s.pr.number} conflicts with ${s.baseBranch}`,
      detail: [
        `  GitHub cannot build the merge commit, so CI will never run and no amount of`,
        `  waiting will change that.`,
        '',
        `    git fetch origin ${s.baseBranch} && git rebase origin/${s.baseBranch}`,
        '',
        `  Resolve the conflicts, push with --force-with-lease, then re-run.`,
        '',
        `  PR: ${s.pr.url}`,
      ].join('\n'),
    };
  }

  // --- verification ---------------------------------------------------------
  // "No CI ran" is never "CI passed". When the diff matches no CI path the
  // checks state stays `none` forever, and that is a legitimate steady state —
  // the PR is UNVERIFIED and rests on review alone. But when CI *should* run,
  // `none` means not-yet, and waiting is right.
  if (s.checks.state === 'red') {
    return {
      exit: EXIT.CI_RED,
      why: 'CI is red',
      detail:
        s.checks.failures
          .map((c) => `  · ${c.name}${c.why ? ` — ${c.why}` : ''}\n    ${c.link}`)
          .join('\n') +
        '\n\n  Read the log before assuming it is your code. An infrastructure failure — a broken\n' +
        '  runner cache, a starved lane — is not a regression to "fix".',
    };
  }
  if (s.ciWillRun && (s.checks.state === 'none' || s.checks.state === 'pending')) {
    if (s.checksDeadlinePassed) {
      return {
        exit: EXIT.NEEDS_HUMAN,
        why: 'CI never settled within the timeout',
        detail:
          'A conflicting PR is already ruled out above, so the run was dispatchable.\n' +
          '  Investigate by hand — a lane may be starved of runners.',
      };
    }
    return { action: ACTION.WAIT_CHECKS, why: `checks are ${s.checks.state}` };
  }

  // --- review ---------------------------------------------------------------
  // The cap is a budget, and each repo declares what running out of it MEANS.
  //   "handoff" — the cap OPENS a human conversation (the default: a reviewer
  //               still objecting after N rounds is a signal, not noise).
  //   "merge"   — the cap CLOSES the review conversation, and the PR then lands
  //               on CI green alone. For repos where each round costs real money
  //               and the marginal one stopped paying for itself. It buys a pass
  //               on the review bar and on NOTHING else: red CI still exits 10,
  //               and the hard-stop gate below still hands the PR to a human
  //               however many rounds were spent.
  const reviewSpent = s.roundsExhausted === 'merge' && s.review.rounds >= s.maxReviewRounds;

  if (!reviewSpent && s.review.state === 'changes_requested') {
    if (s.review.rounds >= s.maxReviewRounds) {
      return {
        exit: EXIT.NEEDS_HUMAN,
        why: `review rounds exhausted (${s.review.rounds}/${s.maxReviewRounds})`,
        detail: 'Summarise the open findings in a comment and hand off.',
      };
    }
    return {
      exit: EXIT.CHANGES_REQUESTED,
      why: 'the review requested changes',
      detail: `${s.review.body}\n\n  Fix the cause, not the symptom. Do not silence the finding.`,
    };
  }
  if (!reviewSpent && s.requireApprovingReview && s.review.state !== 'approved') {
    if (s.reviewDeadlinePassed) {
      return {
        exit: EXIT.NEEDS_HUMAN,
        why: 'no review landed on this commit within the timeout',
        detail: [
          '  Two very different situations produce this, and this script cannot tell them apart:',
          '',
          '  1. This repo has NO reviewer wired yet. Then this is the DESIGNED outcome, not a',
          '     failure — every step up to the merge is done and the PR is yours to merge.',
          '     Leave requireApprovingReview true: false would merge on CI alone, which removes',
          '     review rather than replacing it.',
          '  2. A reviewer IS wired and has not posted yet. Re-run in a few minutes.',
          '',
          `  PR: ${s.pr.url}`,
        ].join('\n'),
      };
    }
    return { action: ACTION.WAIT_REVIEW, why: 'waiting for an approving review on this commit' };
  }

  // --- integration ----------------------------------------------------------
  // Semantic, not chronological. A branch whose paths do not intersect what the
  // base changed is still validly green; re-running CI for it buys nothing and
  // costs the scarcest lane on the host.
  if (s.base.needsRebase) {
    return {
      action: ACTION.REBASE,
      why: `the base moved into this diff (${s.base.overlap.length} overlapping, ${s.base.blast.length} shared)`,
    };
  }

  // --- the gate that green cannot answer ------------------------------------
  if (s.gated.length) {
    return {
      exit: EXIT.NEEDS_HUMAN,
      why: `${bar(s, reviewSpent)}, but this PR is gated — a human merges it`,
      detail: s.gated.map((g) => `  · ${g}`).join('\n') + `\n\n  PR: ${s.pr.url}`,
    };
  }

  return { action: ACTION.MERGE, why: `${bar(s, reviewSpent)}, current, and ungated` };
}

/**
 * What was actually cleared, in the words of the bar this repo set. A repo with
 * `requireApprovingReview: false` merges on CI alone; saying "approved" there
 * would put a review in the log that never happened, and the log is how anyone
 * reconstructs why something merged.
 */
function bar(s, reviewSpent = false) {
  if (reviewSpent) {
    return `green, with the review budget spent (${s.review.rounds}/${s.maxReviewRounds} rounds)`;
  }
  return s.requireApprovingReview ? 'green and approved' : 'green (no review required here)';
}

module.exports = { decide, EXIT, ACTION };
