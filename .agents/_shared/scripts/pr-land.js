#!/usr/bin/env node
/**
 * `pnpm pr:land` — the autonomous landing loop.
 *
 * NOT a merge command, and no longer a pipeline. A **reconciler**: each iteration
 * observes the world, asks `decide.js` what the world now needs, and performs
 * exactly one action. It never remembers what it did, so it cannot be wrong about
 * it. Kill it anywhere and re-run — it resumes, because it never assumed where it
 * was.
 *
 *   0   merged and the remote branch is gone
 *   10  CI red (failures printed) — fix, re-run
 *   20  review requested changes (findings printed) — fix the cause, re-run
 *   30  hard-stop, draft, closed, a deadline, or (unless the repo sets
 *       `roundsExhausted: "merge"`) rounds exhausted — hand to a human
 *   40  preflight failed (dirty tree, protected branch, conflict) — resolve, re-run
 *
 * Why a reconciler. The pipeline version accumulated ten bugs in its first day and
 * not one was in the logic deciding whether a PR may merge. Every one was in
 * PERCEPTION — reading a tool's exit code as a statement about the world.
 * `gh pr checks` exits non-zero for "no checks yet", "network down" and "checks
 * failed" alike. `gh pr merge --delete-branch` exited non-zero *after a successful
 * merge*, because a local checkout failed afterwards, and reported a landed PR as
 * a failure.
 *
 * The rule that kills the whole class: **never infer domain state from a tool's
 * exit code — query it.** Here that is structural rather than a discipline each
 * author has to remember. A failed observation is *no new information*, not a
 * verdict; the loop simply looks again. Idempotence is not a feature that was
 * added, it is the shape: "already merged" is just an observation whose gap to the
 * desired state is empty.
 *
 * Three guards are judgement rather than perception, so they live here:
 *
 * - **Vacuous green is not green.** CI is usually path-filtered, so a PR touching
 *   only docs or infra dispatches no run at all. Such a PR is marked UNVERIFIED in
 *   its body and rests on review alone.
 * - **Staleness is semantic, not chronological.** Rebase only when the base's
 *   changed paths actually intersect this PR's, or touch a shared blast radius.
 * - **A lane that never ran is not a lane that passed.** GitHub reports "skipped
 *   by a path filter" and "skipped because my dependency died" with the same
 *   word, and only the second can merge an unvalidated diff. See `checksVerdict`.
 *
 * All repo-specific values are DATA, in `.agents/land.config.json`. This file is
 * shared verbatim across repos via the `agent-skills` submodule; if you are
 * editing it for one repo, that value belongs in that repo's config instead.
 */
'use strict';

const { execFileSync, execSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { decide, EXIT, ACTION } = require('./decide.js');

// ---------------------------------------------------------------------------
// Config
//
// Defaults are deliberately conservative — an unconfigured repo gets an EMPTY
// hardStop list and requireApprovingReview:true, so it stalls at the review gate
// rather than silently auto-merging something it never declared.
//
//   {
//     "baseBranch": "develop",
//     "protectedBranches": ["beta", "main"],
//     "reviewLabel": "ai-review",
//     "requireApprovingReview": true,
//     "maxReviewRounds": 5,
//     "roundsExhausted": "handoff",              // or "merge" — land on green at the cap
//     "mergeMethod": "merge",                    // or "squash" / "rebase"
//     "ciPaths": ["src/", "package.json"],       // or ["**"] when CI has no filter
//     "requiredLanes": ["Emulators · Vitest"],   // skipped == failed for these
//     "hardStop": [{ "pattern": "^firestore\\.rules$", "why": "security rules" }],
//     "sharedBlastRadius": ["packages/shared/", "pnpm-lock.yaml"]
//   }
//
// `pattern` is a JS regex SOURCE string (not /slashes/); add "flags" for /i.
// ---------------------------------------------------------------------------

const DEFAULTS = {
  baseBranch: 'develop',
  protectedBranches: ['beta', 'main', 'master'],
  reviewLabel: 'ai-review',
  requireApprovingReview: true,
  maxReviewRounds: 5,
  // What running the review budget out MEANS here: "handoff" (a human takes the
  // open findings) or "merge" (the cap ends the review conversation and the PR
  // lands on CI green alone). See the review section of decide.js — "merge"
  // never relaxes the hard-stop gate.
  roundsExhausted: 'handoff',
  // How this repo integrates a PR. A repo whose history is squashed and one
  // whose history keeps merge commits are both correct; which one is a property
  // of the repo, so it is data here rather than a value baked into the loop.
  mergeMethod: 'merge',
  ciPaths: [],
  // Lanes whose absence is itself a failure when CI covers the diff. Empty by
  // default: naming one is a claim about a specific repo's workflows.
  requiredLanes: [],
  hardStop: [],
  hardStopTrailerSource: '^Breaking-Client:',
  sharedBlastRadius: [],
  pollIntervalMs: 20_000,
  checksTimeoutMs: 90 * 60 * 1000,
  reviewTimeoutMs: 20 * 60 * 1000,
};

const CONFIG_FILENAME = '.agents/land.config.json';

function loadConfig(repoRoot = process.cwd()) {
  const file = path.join(repoRoot, CONFIG_FILENAME);
  let overrides = {};
  if (fs.existsSync(file)) {
    try {
      overrides = JSON.parse(fs.readFileSync(file, 'utf8'));
    } catch (err) {
      throw new Error(`${CONFIG_FILENAME} is not valid JSON: ${err.message}`);
    }
  }
  const merged = { ...DEFAULTS, ...overrides };
  merged.hardStop = (merged.hardStop || []).map((rule) => ({
    why: rule.why,
    pattern: rule.pattern instanceof RegExp ? rule.pattern : new RegExp(rule.pattern, rule.flags || ''),
  }));
  merged.hardStopTrailer = new RegExp(merged.hardStopTrailerSource, 'm');
  // Caught here rather than at the merge, which is the one moment a config typo
  // must not surface: everything up to it has already succeeded.
  if (!['merge', 'squash', 'rebase'].includes(merged.mergeMethod)) {
    throw new Error(`${CONFIG_FILENAME}: mergeMethod must be "merge", "squash" or "rebase" (got ${JSON.stringify(merged.mergeMethod)})`);
  }
  // A typo here would silently read as "handoff" — the safe half of the choice,
  // which is exactly why nobody would notice the repo never adopted the other.
  if (!['handoff', 'merge'].includes(merged.roundsExhausted)) {
    throw new Error(`${CONFIG_FILENAME}: roundsExhausted must be "handoff" or "merge" (got ${JSON.stringify(merged.roundsExhausted)})`);
  }
  merged.configFound = fs.existsSync(file);
  return merged;
}

function repoRoot() {
  try {
    return execSync('git rev-parse --show-toplevel', { encoding: 'utf8' }).trim();
  } catch {
    return process.cwd();
  }
}

const CONFIG = loadConfig(repoRoot());
const DRY_RUN = process.argv.includes('--dry-run');

// ---------------------------------------------------------------------------
// IO. Every call here can fail without that meaning anything about the PR.
// ---------------------------------------------------------------------------

function sh(cmd, { allowFail = false } = {}) {
  try {
    return execSync(cmd, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  } catch (err) {
    if (allowFail) return null;
    throw new Error(`command failed: ${cmd}\n${err.stderr || err.message}`);
  }
}

function gh(args, { allowFail = false } = {}) {
  try {
    return execFileSync('gh', args, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  } catch (err) {
    if (allowFail) return { __failed: true, stderr: String(err.stderr || err.message) };
    throw new Error(`gh ${args.join(' ')} failed:\n${err.stderr || err.message}`);
  }
}

const failed = (r) => Boolean(r && r.__failed);
const log = (m) => console.log(m);
const sleep = (ms) => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);

const touches = (files, prefixes) =>
  files.filter((f) => prefixes.some((p) => (p.endsWith('/') ? f.startsWith(p) : f === p)));

// ---------------------------------------------------------------------------
// Pure predicates
// ---------------------------------------------------------------------------

function hardStopHits(files, commitMessages = '', cfg = CONFIG) {
  const hits = [];
  for (const f of files) {
    for (const rule of cfg.hardStop) if (rule.pattern.test(f)) hits.push(`${f} — ${rule.why}`);
  }
  if (cfg.hardStopTrailer.test(commitMessages)) {
    hits.push('commit carries a Breaking-Client: trailer — walls installed clients');
  }
  return hits;
}

/** `["**"]` means the repo's CI has no path filter and always runs. */
function ciCovers(files, cfg = CONFIG) {
  if ((cfg.ciPaths || []).includes('**')) return files.length > 0;
  return touches(files, cfg.ciPaths).length > 0;
}

function needsRebase(baseChangedFiles, prFiles, cfg = CONFIG) {
  const overlap = baseChangedFiles.filter((f) => prFiles.includes(f));
  const blast = touches(baseChangedFiles, cfg.sharedBlastRadius);
  return { overlap, blast, rebase: overlap.length > 0 || blast.length > 0 };
}

/** Reviews bound to THIS commit — an approval of an older head is not an approval. */
function reviewFor(reviews, headSha) {
  const all = reviews || [];
  const mine = all.filter((r) => (r.commit?.oid || r.commit_id) === headSha);
  const changes = mine.filter((r) => r.state === 'CHANGES_REQUESTED');
  if (changes.length) {
    return { state: 'changes_requested', rounds: all.length, body: changes.map((r) => r.body).join('\n---\n') };
  }
  if (mine.some((r) => r.state === 'APPROVED')) return { state: 'approved', rounds: all.length, body: '' };
  return { state: 'none', rounds: all.length, body: '' };
}

// ---------------------------------------------------------------------------
// OBSERVE — one full read of the world. Never throws on a flaky call; an
// unreadable field stays `unknown` and the loop looks again.
// ---------------------------------------------------------------------------

function observePr(branch) {
  const raw = gh(
    ['pr', 'list', '--head', branch, '--state', 'all', '--limit', '20',
     '--json', 'number,state,url,isDraft,headRefOid,reviews,baseRefName'],
    { allowFail: true },
  );
  if (failed(raw)) return null; // unknown, NOT absent
  // Filter by BASE too. `--head develop` also matches release PRs (develop →
  // beta), and treating one of those as "this branch's PR" is how the loop
  // concluded that develop was merged and should be deleted.
  const list = JSON.parse(raw || '[]').filter((p) => p.baseRefName === CONFIG.baseBranch);
  if (!list.length) return { state: 'none', reviews: [] };
  const p = list[0];
  const state = String(p.state || '').toLowerCase(); // open | merged | closed
  return {
    number: p.number,
    state,
    url: p.url,
    isDraft: p.isDraft,
    headSha: p.headRefOid,
    reviews: p.reviews || [],
    // Only an open PR has a mergeability worth asking about, and a merged one
    // answers UNKNOWN anyway.
    mergeable: state === 'open' ? observeMergeable(p.number) : 'UNKNOWN',
  };
}

/**
 * Can this PR still be merged into its base at all? MERGEABLE | CONFLICTING | UNKNOWN.
 *
 * A SECOND call, and it has to be. GitHub computes mergeability lazily, in the
 * background, and answers UNKNOWN until it has; `gh pr list` mostly does not
 * trigger that computation while `gh pr view` on one PR does. Measured on a live
 * conflicting PR: the list query said UNKNOWN and the view said CONFLICTING.
 * Folding this into the list query would therefore read as "no conflict here"
 * on exactly the PRs it exists to catch.
 *
 * UNKNOWN is returned as UNKNOWN and never as a verdict — see decide().
 */
function observeMergeable(number) {
  const raw = gh(['pr', 'view', String(number), '--json', 'mergeable'], { allowFail: true });
  if (failed(raw)) return 'UNKNOWN';
  return JSON.parse(raw || '{}').mergeable || 'UNKNOWN';
}

// A `gh pr checks` state of SKIPPED is two utterly different facts wearing one
// word, and telling them apart is the whole of the guard below:
//
//   (a) NOT APPLICABLE — a path filter matched nothing, or an `if:` evaluated
//       false. Routine and constant: CI here is path-filtered, and lanes like
//       "Build image + deploy to Cloud Run" (`if: github.event_name == 'push'`)
//       are skipped by design on every PR. Blocking on these would wedge nearly
//       every PR in every consuming repo.
//   (b) DEPENDENCY CASCADE — a `needs:` upstream failed or was cancelled, so
//       GitHub skipped this job. The lane never ran. This is the one shape of
//       "not red" that can merge a diff no test ever looked at.
//
// The job API does not say which. Verified against the live incident: the
// cascaded skip (Emulators, run 33126802372) and the path-filtered skip (Cloud
// Run deploy, run 33126802218) on the same head SHA are byte-identical in
// `/actions/jobs/<id>` — conclusion `skipped`, `runner_name` null, `steps` [],
// degenerate timestamps. Nothing local to the job distinguishes them.
//
// What does distinguish them is the run they sit in. `needs:` cannot cross
// workflows, so a cascade's cause is always a sibling job in the SAME run.
// Hence: a skipped check is a cascade suspect iff its own run contains a job
// that reached a terminal non-success conclusion. On the fixtures that
// separates the two cases exactly — the CI run held the failure and both
// cascaded skips; the Web run held only successes and the legitimate skip.
//
// This is deliberately coarser than walking `needs:` through the workflow YAML.
// Its imprecision is one-directional and cheap: a run holding an unrelated
// failure *and* an unrelated legitimate skip over-blocks — but such a run is
// already red, so the verdict does not change. It buys that with no YAML
// parsing, no display-name matching (`name: Audit ${{ matrix.env }}` does not
// match anything), and no extra API call: `gh pr checks` already reports the
// run in each `link`.

/** Terminal, not success. Any of these in a run explains a sibling's skip. */
const CASCADE_CAUSES = ['FAILURE', 'TIMED_OUT', 'ACTION_REQUIRED', 'CANCELLED'];
/** States that are a verdict against the diff itself. */
const HARD_FAILURE = ['FAILURE', 'TIMED_OUT', 'ACTION_REQUIRED'];
const STILL_RUNNING = ['PENDING', 'QUEUED', 'IN_PROGRESS'];

/** The run a check belongs to, read off its own link. */
function runIdOf(link) {
  const m = /\/actions\/runs\/(\d+)\b/.exec(String(link || ''));
  return m ? m[1] : null;
}

/**
 * Why this SKIPPED check must block, or null if it is a legitimate skip.
 * Pure, and takes the whole check list, so it is asserted against recorded
 * fixtures rather than smoke-tested against a live PR.
 */
function skipBlocks(check, causedRuns, ciWillRun, cfg) {
  const run = runIdOf(check.link);
  if (run && causedRuns.has(run)) {
    return 'a job in its own run failed or was cancelled — this lane never ran';
  }
  // Belt and braces for the case the run-level signal cannot see: a lane the
  // repo names as required, skipped on a diff CI is supposed to cover, in a run
  // that now looks clean (a single failed job re-run green leaves its dependent
  // skipped from the earlier attempt). Opt-in and empty by default, so an
  // unconfigured repo keeps today's behaviour exactly.
  if (ciWillRun && (cfg.requiredLanes || []).includes(check.name)) {
    return 'a required lane, skipped on a diff CI covers — it did not run';
  }
  return null;
}

/**
 * The merge gate's reading of the check list.
 *
 * Skipped checks used to be filtered out BEFORE failures and pending were
 * computed, which made a cascaded skip invisible to the gate rather than merely
 * absent from a badge row. See the note above.
 */
function checksVerdict(checks, { ciWillRun = false } = {}, cfg = CONFIG) {
  const skipped = checks.filter((c) => c.state === 'SKIPPED');
  const live = checks.filter((c) => c.state !== 'SKIPPED' && c.state !== 'NEUTRAL');

  const causedRuns = new Set(
    checks.filter((c) => CASCADE_CAUSES.includes(c.state)).map((c) => runIdOf(c.link)).filter(Boolean),
  );
  const blockingSkips = skipped
    .map((c) => {
      const why = skipBlocks(c, causedRuns, ciWillRun, cfg);
      return why ? { ...c, why } : null;
    })
    .filter(Boolean);

  // Every check skipped and none of them suspect is still "none": a PR whose
  // diff dispatched nothing has not been verified, and `ciWillRun` decides what
  // that means.
  if (!live.length && !blockingSkips.length) return { state: 'none', failures: [] };

  const failures = [...live.filter((c) => HARD_FAILURE.includes(c.state)), ...blockingSkips];
  if (failures.length) return { state: 'red', failures };
  const pending = live.filter((c) => STILL_RUNNING.includes(c.state));
  return { state: pending.length ? 'pending' : 'green', failures: [] };
}

function observeChecks(pr, ciWillRun) {
  const raw = gh(['pr', 'checks', String(pr), '--json', 'name,state,link'], { allowFail: true });
  if (failed(raw)) {
    // "no checks reported" is a real answer: none have registered yet.
    if (/no checks reported/i.test(raw.stderr)) return { state: 'none', failures: [] };
    return null; // unknown
  }
  return checksVerdict(JSON.parse(raw || '[]'), { ciWillRun });
}

function observe(deadlines) {
  const branchName = sh('git rev-parse --abbrev-ref HEAD');
  const headSha = sh('git rev-parse HEAD');
  sh(`git fetch origin ${CONFIG.baseBranch} --prune`, { allowFail: true });

  const mergeBase = sh(`git merge-base origin/${CONFIG.baseBranch} HEAD`, { allowFail: true }) || headSha;
  const files = (sh(`git diff --name-only ${mergeBase} HEAD`, { allowFail: true }) || '').split('\n').filter(Boolean);
  const baseTip = sh(`git rev-parse origin/${CONFIG.baseBranch}`, { allowFail: true }) || mergeBase;
  const baseChanged = (sh(`git diff --name-only ${mergeBase} ${baseTip}`, { allowFail: true }) || '')
    .split('\n').filter(Boolean);

  // ls-remote, NOT `git rev-parse origin/<branch>`: the tracking ref is a local
  // cache that `git fetch origin <base> --prune` does not prune, so a branch
  // deleted on the server still looks present. Observing means asking the
  // remote, not reading what we happen to remember.
  const remoteLine = sh(`git ls-remote --heads origin ${branchName}`, { allowFail: true });
  const remoteSha = remoteLine ? remoteLine.split(/\s+/)[0] : null;
  const pr = observePr(branchName) || { state: 'unknown', reviews: [] };
  // Computed before the checks are read, because whether CI was *meant* to run
  // is what makes a required lane's absence meaningful.
  const ciWillRun = ciCovers(files);
  const checks = pr.number ? observeChecks(pr.number, ciWillRun) : { state: 'none', failures: [] };

  const gitDir = sh('git rev-parse --git-dir', { allowFail: true }) || '';
  const common = sh('git rev-parse --git-common-dir', { allowFail: true }) || '';

  return {
    baseBranch: CONFIG.baseBranch,
    requireApprovingReview: CONFIG.requireApprovingReview,
    maxReviewRounds: CONFIG.maxReviewRounds,
    roundsExhausted: CONFIG.roundsExhausted,
    branch: {
      name: branchName,
      headSha,
      isProtected: [CONFIG.baseBranch, ...CONFIG.protectedBranches, 'HEAD'].includes(branchName),
      dirty: Boolean(sh('git status --porcelain', { allowFail: true })),
      dirtyFiles: sh('git status --short', { allowFail: true }) || '',
      rebaseConflict:
        fs.existsSync(path.join(common || '.git', 'rebase-merge')) ||
        fs.existsSync(path.join(common || '.git', 'rebase-apply')),
      ahead: Number(sh(`git rev-list --count origin/${CONFIG.baseBranch}..HEAD`, { allowFail: true }) || 0),
      pushed: Boolean(remoteSha),
      remoteBehind: Boolean(remoteSha) && remoteSha !== headSha,
      inWorktree: gitDir !== common,
    },
    pr,
    checks: checks || { state: 'unknown', failures: [] },
    review: reviewFor(pr.reviews, headSha),
    remoteBranchExists: Boolean(remoteSha),
    files,
    ciWillRun,
    gated: hardStopHits(files, sh(`git log origin/${CONFIG.baseBranch}..HEAD --format=%B`, { allowFail: true }) || ''),
    base: needsRebase(baseChanged, files),
    checksDeadlinePassed: Date.now() > deadlines.checks,
    reviewDeadlinePassed: Date.now() > deadlines.review,
  };
}

// ---------------------------------------------------------------------------
// ACT — exactly one action per iteration.
// ---------------------------------------------------------------------------

function ensureLabel() {
  if (!CONFIG.reviewLabel) return null;
  const made = gh(
    ['label', 'create', CONFIG.reviewLabel, '--description', 'Request an automated review', '--force'],
    { allowFail: true },
  );
  if (failed(made)) {
    log(`  ⚠ could not ensure the "${CONFIG.reviewLabel}" label — opening without it.`);
    log('    An unlabelled PR gets no review, so it will stop at exit 30.');
    return null;
  }
  return CONFIG.reviewLabel;
}

function act(action, s) {
  switch (action) {
    case ACTION.PUSH:
      if (DRY_RUN) return log('  [dry-run] would push');
      sh(`git push -u origin ${s.branch.name} --force-with-lease`);
      return log(`  pushed ${s.branch.name}`);

    case ACTION.OPEN_PR: {
      if (DRY_RUN) return log('  [dry-run] would open a PR');
      const label = ensureLabel();
      const note = s.ciWillRun
        ? 'CI covers this diff.'
        : "⚠ **UNVERIFIED BY CI** — no changed path matches this repo's CI filter, so no run " +
          'was dispatched. This PR is gated on review alone.';
      const body = [sh(`git log origin/${CONFIG.baseBranch}..HEAD --format='- %s'`), '', note].join('\n');
      const url = gh([
        'pr', 'create', '--base', CONFIG.baseBranch,
        ...(label ? ['--label', label] : []),
        '--title', sh('git log -1 --format=%s'), '--body', body,
      ]);
      return log(`  opened ${url}`);
    }

    case ACTION.WAIT_CHECKS:
    case ACTION.WAIT_REVIEW:
      return sleep(CONFIG.pollIntervalMs);

    case ACTION.REBASE: {
      if (DRY_RUN) return log('  [dry-run] would rebase');
      try {
        sh(`git rebase origin/${CONFIG.baseBranch}`);
      } catch (err) {
        sh('git rebase --abort', { allowFail: true });
        throw new Error(`rebase conflicted — resolve by hand, then re-run.\n${err.message}`);
      }
      sh(`git push --force-with-lease origin ${s.branch.name}`);
      return log('  rebased and pushed — CI and review re-run against the new head');
    }

    case ACTION.MERGE:
      if (DRY_RUN) return log(`  [dry-run] would merge #${s.pr.number}`);
      // NOT --delete-branch: that makes gh check out the base branch locally
      // afterwards, which fails when another worktree holds it — the normal state
      // under this contract. The remote ref is deleted as its own reconciled step.
      gh(['pr', 'merge', String(s.pr.number), `--${CONFIG.mergeMethod}`]);
      return log(`  merged #${s.pr.number}`);

    case ACTION.DELETE_REMOTE: {
      if (DRY_RUN) return log('  [dry-run] would delete the remote branch');
      const r = gh(['api', '-X', 'DELETE', `repos/{owner}/{repo}/git/refs/heads/${s.branch.name}`], { allowFail: true });
      return log(failed(r) ? '  remote branch already gone' : `  deleted remote ${s.branch.name}`);
    }

    default:
      throw new Error(`unknown action: ${action}`);
  }
}

// ---------------------------------------------------------------------------
// Terminal reporting
// ---------------------------------------------------------------------------

/**
 * A process cannot remove the directory it is running in, so this reports the
 * cleanup rather than performing it. Saying so explicitly matters: worktrees
 * accumulate one per landed PR, and "the script cleans up" was claimed before it
 * was true — 14 had piled up in one repo by the time anyone checked.
 */
function reportReap(s) {
  if (!s.branch.inWorktree) {
    sh(`git branch -d ${s.branch.name}`, { allowFail: true });
    return log(`  deleted local ${s.branch.name}`);
  }
  const common = sh('git rev-parse --git-common-dir', { allowFail: true }) || '';
  const wt = sh('git rev-parse --show-toplevel', { allowFail: true });
  const root = sh(`git -C "${common}/.." rev-parse --show-toplevel`, { allowFail: true });
  // `git worktree remove` refuses outright on a worktree containing a submodule,
  // and this contract puts one in every adopting repo — so the refusal is the
  // NORMAL case here, not the exceptional one. That is exactly what makes
  // `--force` the wrong answer: reached for every single time, it stops reading
  // as "override a safety check" and starts reading as "the reap command", and
  // it discards uncommitted work without saying so. Delete the directory, prune
  // the registration, and let `branch -d` do the merged-ness check it exists for.
  if (sh('git status --porcelain', { allowFail: true })) {
    log('  this worktree is stale but NOT clean — it has uncommitted changes:');
    log(`    git -C ${wt} status --short`);
    return log('  reap it by hand once you have decided what those changes are.');
  }
  log('  this worktree is now stale — reap it from the main checkout:');
  log(`    rm -rf ${wt} && git -C ${root} worktree prune && git -C ${root} branch -d ${s.branch.name}`);
}

function finish(verdict, s) {
  if (verdict.exit === EXIT.MERGED) {
    console.log(`\n✓ ${verdict.why}`);
    if (s) reportReap(s);
    console.log('→ exit 0');
  } else {
    console.error(`\n✗ ${verdict.why}`);
    if (verdict.detail) console.error(verdict.detail);
    console.error(`\n→ exit ${verdict.exit}`);
  }
  process.exit(verdict.exit);
}

// ---------------------------------------------------------------------------
// The loop: observe → decide → act, forever, until decide() says stop.
// ---------------------------------------------------------------------------

function main() {
  if (DRY_RUN) log('[dry-run] no push, no PR, no merge\n');
  if (!CONFIG.configFound) {
    log(`⚠ no ${CONFIG_FILENAME} — running on defaults: no hard-stop list, no CI path map.`);
    log('  The defaults fail closed, not open, but add one before relying on this.\n');
  }

  const deadlines = {
    checks: Date.now() + CONFIG.checksTimeoutMs,
    review: Date.now() + CONFIG.reviewTimeoutMs,
  };
  let last = '';
  let unknownStreak = 0;
  // A reconciler must make progress. If the same non-waiting action repeats
  // without the observed state changing, the action is not reducing the gap and
  // looping is pointless — that is a livelock, and it is how a dry run spun
  // forever printing "would delete the remote branch".
  let repeat = { key: '', n: 0 };

  for (;;) {
    const s = observe(deadlines);

    // An unreadable world is NOT a verdict about the PR — look again.
    if (s.pr.state === 'unknown' || s.checks.state === 'unknown') {
      if (++unknownStreak > 10) {
        finish({
          exit: EXIT.NEEDS_HUMAN,
          why: 'could not read the PR state 10 times running — the API, not this PR, is the problem',
        });
      }
      log(`  ⚠ could not read state (${unknownStreak}/10), retrying`);
      sleep(CONFIG.pollIntervalMs);
      continue;
    }
    unknownStreak = 0;

    const verdict = decide(s);
    if (verdict.exit !== undefined) finish(verdict, s);

    if (DRY_RUN) {
      log(`\n▸ next action: ${verdict.action}`);
      log(`  ${verdict.why}`);
      log('\n[dry-run] stopping here — a dry run cannot converge, because its actions are no-ops.');
      process.exit(0);
    }

    const isWait = verdict.action === ACTION.WAIT_CHECKS || verdict.action === ACTION.WAIT_REVIEW;
    if (!isWait) {
      const key = `${verdict.action}|${JSON.stringify(s.pr)}|${s.remoteBranchExists}|${s.branch.headSha}`;
      repeat = key === repeat.key ? { key, n: repeat.n + 1 } : { key, n: 1 };
      if (repeat.n > 3) {
        finish({
          exit: EXIT.NEEDS_HUMAN,
          why: `"${verdict.action}" ran 3 times without changing anything observable`,
          detail: '  The action is not reducing the gap to the desired state. Investigate by hand\n' +
                  '  rather than letting the loop spin.',
        }, s);
      }
    }

    if (verdict.why !== last) {
      log(`\n▸ ${verdict.action}`);
      log(`  ${verdict.why}`);
      last = verdict.why;
    }
    act(verdict.action, s);
  }
}

if (require.main === module) {
  try {
    main();
  } catch (err) {
    finish({ exit: EXIT.PREFLIGHT, why: err.message });
  }
}

module.exports = {
  CONFIG, DEFAULTS, EXIT, ACTION,
  loadConfig, decide, hardStopHits, ciCovers, needsRebase, touches, reviewFor,
  checksVerdict, runIdOf,
};
