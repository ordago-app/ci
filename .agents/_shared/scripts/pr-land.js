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
 *   10  CI red, the merge result fails `integrationCheck`, or the merge queue removed
 *       this head twice (output printed) — fix, re-run
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
 * - **Staleness is semantic, not chronological.** Rebase only when the base
 *   changed a file this PR also changes, and do it before any wait rather than
 *   after green + approved. A base that moved only through the shared blast
 *   radius is answered by `integrationCheck` on the merge result, locally — see
 *   `baseMovement`.
 * - **A merge queue replaces all of the above.** When the base branch requires
 *   one (observed from GitHub, not configured), the loop never rebases and never
 *   runs the local check: it hands a green PR to the queue, which tests the real
 *   merge result in CI and merges it. See the queue section of decide.js.
 * - **A lane that never ran is not a lane that passed.** GitHub reports "skipped
 *   by a path filter" and "skipped because my dependency died" with the same
 *   word, and only the second can merge an unvalidated diff. See `checksVerdict`.
 *
 * All repo-specific values are DATA, in `.agents/land.config.json`. This file is
 * shared verbatim across repos via the `agent-skills` submodule; if you are
 * editing it for one repo, that value belongs in that repo's config instead.
 */
'use strict';

const { execFileSync, execSync, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
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
//     "ciGates": [                               // one per path-filtered workflow
//       { "workflow": "ci.yml", "paths": ["src/", "package.json"],  // or ["**"]
//         "requiredLanes": ["Lint + Unit"] }     // skipped/missing == failed
//     ],
//     "hardStop": [{ "pattern": "^firestore\\.rules$", "why": "security rules" }],
//     "sharedBlastRadius": ["packages/shared/", "pnpm-lock.yaml"],
//     "rebaseRadius": ["firestore.rules"],       // a move here always rebases
//     "integrationCheck": { "command": "pnpm -s typecheck", "timeoutMs": 900000 }
//   }
//
// `pattern` is a JS regex SOURCE string (not /slashes/); add "flags" for /i.
//
// The legacy single-workflow shape — top-level `ciPaths` + `requiredLanes` — is
// still accepted and read as ONE unnamed gate. See normalizeCiGates().
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
  // One entry per path-filtered CI workflow. Empty by default, which reads as
  // "CI covers nothing" — the UNVERIFIED path, never a silent green. Naming a
  // gate or a required lane is a claim about a specific repo's workflows.
  ciGates: [],
  hardStop: [],
  hardStopTrailerSource: '^Breaking-Client:',
  sharedBlastRadius: [],
  // Paths whose movement on the base only the FULL CI can judge (security rules,
  // anything whose behaviour lives in an emulator). A move here always rebases,
  // integrationCheck or not.
  rebaseRadius: [],
  // A local command run against the MERGE RESULT (base tip + this head) when the
  // base moved only through `sharedBlastRadius`. null keeps the old answer to
  // such a move — a rebase, which re-runs every CI lane and the review.
  integrationCheck: null,
  pollIntervalMs: 20_000,
  checksTimeoutMs: 90 * 60 * 1000,
  queueTimeoutMs: 90 * 60 * 1000,
  reviewTimeoutMs: 20 * 60 * 1000,
};

const CONFIG_FILENAME = '.agents/land.config.json';

const isStringList = (v) => Array.isArray(v) && v.every((x) => typeof x === 'string' && x.length > 0);

/**
 * The repo's CI, as a list of gates: `{ workflow, paths, requiredLanes }`.
 *
 * Why a list. A repo with more than one path-filtered workflow cannot be
 * described by one path list and one lane list. Declare only the first
 * workflow's paths and a diff touching only the second reads as UNVERIFIED and
 * merges on review while that workflow is still running. Union both workflows'
 * paths into one list and the first workflow's required lane is demanded on a
 * diff that never dispatches it — every such PR wedges. A required lane is a
 * claim about ONE workflow, so it is enforced only when that workflow's own
 * filter matches the diff.
 *
 * The legacy shape (top-level `ciPaths` / `requiredLanes`) is exactly one gate,
 * so it is read as one, with unchanged behaviour. Declaring both shapes is
 * rejected rather than merged: which of the two was meant is not guessable.
 */
function normalizeCiGates(overrides) {
  const legacy = 'ciPaths' in overrides || 'requiredLanes' in overrides;
  if (overrides.ciGates === undefined) {
    if (!legacy) return [];
    return [{ workflow: null, paths: overrides.ciPaths || [], requiredLanes: overrides.requiredLanes || [] }];
  }
  if (legacy) {
    throw new Error(`${CONFIG_FILENAME}: declare ciGates OR the legacy ciPaths/requiredLanes, not both`);
  }
  if (!Array.isArray(overrides.ciGates)) {
    throw new Error(`${CONFIG_FILENAME}: ciGates must be an array`);
  }
  return overrides.ciGates.map((gate, i) => {
    const at = `${CONFIG_FILENAME}: ciGates[${i}]`;
    if (typeof gate?.workflow !== 'string' || !gate.workflow) {
      throw new Error(`${at}.workflow must name the workflow file whose filter it mirrors`);
    }
    // An empty path list is a gate that can never match — its required lanes
    // would be dead config that reads as protection.
    if (!isStringList(gate.paths) || gate.paths.length === 0) {
      throw new Error(`${at}.paths must be a non-empty list of path prefixes (or ["**"])`);
    }
    const requiredLanes = gate.requiredLanes ?? [];
    if (!isStringList(requiredLanes)) {
      throw new Error(`${at}.requiredLanes must be a list of check names`);
    }
    return { workflow: gate.workflow, paths: gate.paths, requiredLanes };
  });
}

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
  merged.ciGates = normalizeCiGates(overrides);
  // One representation from here on, so nothing downstream can read the legacy
  // keys and disagree with the gates.
  delete merged.ciPaths;
  delete merged.requiredLanes;
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
  if (merged.integrationCheck !== null) {
    const ic = merged.integrationCheck;
    if (!ic || typeof ic.command !== 'string' || !ic.command.trim()) {
      throw new Error(`${CONFIG_FILENAME}: integrationCheck must be null or { "command": "<shell command>" }`);
    }
    merged.integrationCheck = { timeoutMs: 15 * 60 * 1000, maxAgeMs: 30 * 60 * 1000, ...ic };
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

/** `["**"]` means that workflow has no path filter and always runs. */
function gateCovers(gate, files) {
  if (gate.paths.includes('**')) return files.length > 0;
  return touches(files, gate.paths).length > 0;
}

/** The gates whose workflow this diff dispatches. */
function gatesFor(files, cfg = CONFIG) {
  return cfg.ciGates.filter((gate) => gateCovers(gate, files));
}

function ciCovers(files, cfg = CONFIG) {
  return gatesFor(files, cfg).length > 0;
}

/** Required lanes of the gates this diff dispatches — and of no other gate. */
function requiredLanesFor(files, cfg = CONFIG) {
  return [...new Set(gatesFor(files, cfg).flatMap((gate) => gate.requiredLanes))];
}

const gateLabel = (gate) => gate.workflow || 'CI';

/**
 * What the base's movement since the merge-base means for this PR — the `base`
 * field of the observed state, in exactly the shape decide() reads.
 *
 * The rule every branch below obeys: a REBASE may only be triggered by something
 * a rebase makes go away. A rebase restarts CI and asks for a fresh review, and
 * the base moves several times an hour, so a trigger that survives the rebase
 * turns the landing loop into one that never lands. That is how this function
 * once made a PR that edits package.json rebase on every base move (6 force-
 * pushes in 40 minutes, no merge). Everything the PR's OWN diff raises is
 * therefore answered locally, at the merge gate, by `integrationCheck` — or, in
 * a repo without one, not before the merge at all (the base's own post-merge CI
 * is then the only answer, as it always was).
 *
 * - Same file changed on both sides → rebase. After it, the overlap is gone.
 * - The BASE moved `rebaseRadius` → rebase. Paths only the full CI can judge;
 *   after the rebase the move is part of the tested base.
 * - The BASE moved `sharedBlastRadius` → integration check at scope "shared"
 *   (a rebase, in a repo without one: it is removable).
 * - The PR changes `sharedBlastRadius` while the base moved anything → integration
 *   check at scope "shared". It was tested against the consumers as they were
 *   when it branched.
 * - The PR changes `rebaseRadius` while the base moved anything → integration
 *   check at scope "wide": the repo's command must cover what those paths reach.
 *
 * "Anything", not "anything a CI gate covers": each gate mirrors ONE workflow's
 * filter, and a consumer with no gate of its own (a web app, a console) is outside
 * all of them while importing the shared code all the same. A check the other side did
 * not need costs minutes of local time, once; a skipped one costs a broken base.
 *
 * `forced` and `blast` list the triggering files, from whichever side.
 */
function baseMovement(baseChangedFiles, prFiles, cfg = CONFIG, { queue = false } = {}) {
  // A merge queue tests the real merge result before merging, so nothing here
  // needs answering on the branch: a textual conflict still surfaces, as
  // CONFLICTING mergeability, and is the only thing that still needs a rebase.
  if (queue) {
    return { overlap: [], forced: [], blast: [], scope: 'shared', needsRebase: false, needsIntegrationCheck: false };
  }
  const overlap = baseChangedFiles.filter((f) => prFiles.includes(f));
  const baseMovedCode = baseChangedFiles.length > 0;
  const prMovedCode = prFiles.length > 0;
  const radius = cfg.rebaseRadius || [];
  const hasCheck = Boolean(cfg.integrationCheck);

  const forced = prMovedCode ? touches(baseChangedFiles, radius) : [];
  const baseBlast = prMovedCode ? touches(baseChangedFiles, cfg.sharedBlastRadius) : [];
  const prBlast = baseMovedCode ? touches(prFiles, cfg.sharedBlastRadius) : [];
  const prWide = baseMovedCode ? touches(prFiles, radius) : [];
  const blast = [...new Set([...baseBlast, ...prBlast, ...prWide])].filter((f) => !forced.includes(f));
  const settled = overlap.length === 0 && forced.length === 0;
  return {
    overlap,
    forced,
    blast,
    scope: prWide.length > 0 ? 'wide' : 'shared',
    needsRebase: !settled || (!hasCheck && baseBlast.length > 0),
    needsIntegrationCheck: settled && hasCheck && blast.length > 0,
  };
}

/**
 * Reviews bound to THIS commit — an approval of an older head is not an approval.
 *
 * `rounds` counts disagreements: every CHANGES_REQUESTED on the PR. An approval
 * ends the review conversation, so the review a later CI-fix push buys is a fresh
 * look, not another round of argument. Counting approvals let a PR the reviewer
 * never objected to exhaust its budget and, under `roundsExhausted: "merge"`,
 * land its next head unreviewed.
 */
function reviewFor(reviews, headSha) {
  const all = reviews || [];
  const rounds = all.filter((r) => r.state === 'CHANGES_REQUESTED').length;
  const mine = all.filter((r) => (r.commit?.oid || r.commit_id) === headSha);
  const changes = mine.filter((r) => r.state === 'CHANGES_REQUESTED');
  if (changes.length) {
    return { state: 'changes_requested', rounds, body: changes.map((r) => r.body).join('\n---\n') };
  }
  if (mine.some((r) => r.state === 'APPROVED')) return { state: 'approved', rounds, body: '' };
  return { state: 'none', rounds, body: '' };
}

// ---------------------------------------------------------------------------
// The integration check's verdict, kept where observe() can read it back.
//
// The loop is level-triggered, so the check cannot hand its result to the next
// decision directly — it writes it down, per head, with the base tip and scope it
// judged. The check takes minutes and the base moves every few, so a verdict that
// died with every base move would starve: a PASS keeps holding while the base
// moves only through paths it cannot care about.
// ---------------------------------------------------------------------------

function integrationVerdictPath(commonDir, headSha) {
  return path.join(commonDir, 'pr-land', 'integration', `${headSha}.json`);
}

/** The recorded verdict, or null. A torn write from a killed run is no verdict. */
function readIntegrationVerdictFile(file) {
  if (!fs.existsSync(file)) return null;
  try {
    const v = JSON.parse(fs.readFileSync(file, 'utf8'));
    return typeof v.baseTip === 'string' ? v : null;
  } catch {
    return null;
  }
}

/**
 * Does a recorded verdict still answer the question for the base as it is NOW?
 * Pure. `movedSince` is what the base changed between the verdict's tip and now
 * (null when that could not be read — no answer, so look again).
 *
 * - A narrower verdict never answers a wider question (shared ⊂ wide).
 * - Older than `integrationCheck.maxAgeMs`, it answers nothing: the base has
 *   moved on under it however unrelated each single move looked.
 * - On the same tip, the verdict stands — pass, fail, or `error` (the check could
 *   not run: a missing tool, a timeout — never a statement about the merge).
 * - Past it, a PASS holds while nothing moved in the PR's own files or in
 *   rebaseRadius; anything else — including an old FAIL, which a new base may
 *   fix — is re-checked.
 */
function judgeIntegrationVerdict(v, { baseTip, scope, movedSince, prFiles, now = Date.now() }, cfg = CONFIG) {
  const pending = { state: 'pending', output: '' };
  if (!v) return pending;
  if (scope === 'wide' && v.scope !== 'wide') return pending;
  const maxAgeMs = cfg.integrationCheck ? cfg.integrationCheck.maxAgeMs : Infinity;
  if (typeof v.at !== 'number' || now - v.at > maxAgeMs) return pending;
  const out = String(v.output || '');
  if (v.baseTip === baseTip) return { state: v.error ? 'error' : v.ok ? 'pass' : 'fail', output: out };
  if (!v.ok || movedSince === null) return pending;
  // NOT sharedBlastRadius: shared code moves on most merges, and a check that
  // takes longer than the gap between them would then never settle. A shared
  // move that lands while the check runs is the one gap left, and the base's
  // own post-merge CI (never cancelled) is what covers it. A move into the PR's
  // own files or rebaseRadius is re-judged — and the latter rebases anyway.
  const relevant = [
    ...movedSince.filter((f) => prFiles.includes(f)),
    ...touches(movedSince, cfg.rebaseRadius || []),
  ];
  return relevant.length ? pending : { state: 'pass', output: out };
}

// ---------------------------------------------------------------------------
// OBSERVE — one full read of the world. Never throws on a flaky call; an
// unreadable field stays `unknown` and the loop looks again.
// ---------------------------------------------------------------------------

/**
 * Which of the PRs ever opened from this branch NAME is this branch's PR.
 *
 * A name is not an identity: a new branch can reuse the name of one merged
 * months ago, and that old PR answers `--head <name>` too. Taken at its word it
 * reads as "already merged", and the loop exits 0 without pushing anything. So
 * a merged PR only counts while HEAD carries no commit it did not merge; new
 * work under the name means a new PR. An open PR always wins: it is the live one.
 *
 * @param {Array<{number?: number, state?: string, baseRefName?: string, headRefOid?: string}>} prs `gh pr list` rows, newest first
 * @param {{baseBranch: string, carriesWorkBeyond: (pr: object) => boolean}} opts
 */
function pickBranchPr(prs, { baseBranch, carriesWorkBeyond }) {
  // Filter by BASE too. `--head develop` also matches release PRs (develop →
  // beta), and treating one of those as "this branch's PR" is how the loop
  // concluded that develop was merged and should be deleted.
  const list = prs.filter((p) => p.baseRefName === baseBranch);
  const open = list.find((p) => String(p.state).toLowerCase() === 'open');
  if (open) return open;
  const latest = list[0];
  if (!latest) return null;
  if (String(latest.state).toLowerCase() === 'merged' && carriesWorkBeyond(latest)) return null;
  return latest;
}

/**
 * Does HEAD hold commits that neither the PR's head nor the base has? When that
 * cannot be known it answers false, which keeps the previous reading of a
 * merged PR.
 */
function headCarriesWorkBeyond(pr) {
  const present = () => sh(`git cat-file -e ${pr.headRefOid}^{commit}`, { allowFail: true }) !== null;
  // A squash-merged head is reachable from no branch; GitHub keeps it under the PR ref.
  if (!present()) sh(`git fetch --quiet origin pull/${pr.number}/head`, { allowFail: true });
  if (!present()) return false;
  const count = sh(`git rev-list --count HEAD ^${pr.headRefOid} ^origin/${CONFIG.baseBranch}`, { allowFail: true });
  return count !== null && Number(count) > 0;
}

function observePr(branch) {
  const raw = gh(
    ['pr', 'list', '--head', branch, '--state', 'all', '--limit', '20',
     '--json', 'number,state,url,isDraft,headRefOid,reviews,baseRefName'],
    { allowFail: true },
  );
  if (failed(raw)) return null; // unknown, NOT absent
  const p = pickBranchPr(JSON.parse(raw || '[]'), {
    baseBranch: CONFIG.baseBranch,
    carriesWorkBeyond: headCarriesWorkBeyond,
  });
  if (!p) return { state: 'none', reviews: [] };
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

/**
 * The merge queue as this PR sees it, from one GraphQL read. Pure.
 *
 * `enabled` is observed, never configured: if the base requires a queue, the
 * queue is the only way in, and a loop that tried a direct merge there would
 * spin. `failures` are the removals of THIS head — anything older than its
 * commit belongs to a head that no longer exists.
 */
function queueStateFrom(node) {
  if (!node) return null;
  const head = node.commits?.nodes?.[0]?.commit;
  const headAt = head?.committedDate ? Date.parse(head.committedDate) : 0;
  const removals = (node.timelineItems?.nodes || [])
    .filter((e) => e && e.createdAt && Date.parse(e.createdAt) > headAt)
    .map((e) => ({ at: e.createdAt, reason: e.reason || '' }));
  // The queue also "removes" the PR it merges, with reason MERGED, a moment
  // before the PR itself reads as merged. That removal is a success, never a
  // failure: counted as one, the loop re-enqueued a closed PR and exited 40.
  const isMerge = (r) => /^merged$/i.test(r.reason.trim());
  return {
    id: node.id,
    enabled: Boolean(node.isMergeQueueEnabled),
    inQueue: Boolean(node.isInMergeQueue),
    position: node.mergeQueueEntry?.position ?? null,
    merged: removals.some(isMerge),
    failures: removals.filter((r) => !isMerge(r)),
  };
}

const QUEUE_QUERY = `query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      id isMergeQueueEnabled isInMergeQueue
      mergeQueueEntry { position }
      commits(last: 1) { nodes { commit { committedDate } } }
      timelineItems(itemTypes: [REMOVED_FROM_MERGE_QUEUE_EVENT], last: 20) {
        nodes { ... on RemovedFromMergeQueueEvent { createdAt reason } }
      }
    }
  }
}`;

function observeQueue(number) {
  const raw = gh(
    ['api', 'graphql', '-f', `query=${QUEUE_QUERY}`, '-F', 'owner={owner}', '-F', 'repo={repo}', '-F', `number=${number}`],
    { allowFail: true },
  );
  if (failed(raw)) return null; // unknown, NOT "no queue"
  try {
    return queueStateFrom(JSON.parse(raw).data.repository.pullRequest);
  } catch {
    return null;
  }
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
function skipBlocks(check, causedRuns, requiredLanes) {
  const run = runIdOf(check.link);
  if (run && causedRuns.has(run)) {
    return 'a job in its own run failed or was cancelled — this lane never ran';
  }
  // Belt and braces for the case the run-level signal cannot see: a lane the
  // repo names as required, skipped on a diff CI is supposed to cover, in a run
  // that now looks clean (a single failed job re-run green leaves its dependent
  // skipped from the earlier attempt). `requiredLanes` holds only the lanes of
  // gates this diff matches, so a lane of a workflow the diff never dispatches
  // is not in it and cannot wedge the PR.
  if (requiredLanes.includes(check.name)) {
    return 'a required lane, skipped on a diff its workflow covers — it did not run';
  }
  return null;
}

/**
 * The merge gate's reading of the check list.
 *
 * Skipped checks used to be filtered out BEFORE failures and pending were
 * computed, which made a cascaded skip invisible to the gate rather than merely
 * absent from a badge row. See the note above.
 *
 * `requiredLanes` is what `requiredLanesFor(files)` returns: the lanes this
 * diff owes, already narrowed to the workflows it dispatches.
 *
 * A required lane ABSENT from the list is pending, not passed. With more than
 * one workflow on a PR, their checks register independently, and a fast
 * workflow can report all-green before a slower one has registered anything —
 * without this, that moment reads as a mergeable PR.
 */
function checksVerdict(checks, { requiredLanes = [] } = {}) {
  const skipped = checks.filter((c) => c.state === 'SKIPPED');
  const live = checks.filter((c) => c.state !== 'SKIPPED' && c.state !== 'NEUTRAL');

  const causedRuns = new Set(
    checks.filter((c) => CASCADE_CAUSES.includes(c.state)).map((c) => runIdOf(c.link)).filter(Boolean),
  );
  const blockingSkips = skipped
    .map((c) => {
      const why = skipBlocks(c, causedRuns, requiredLanes);
      return why ? { ...c, why } : null;
    })
    .filter(Boolean);

  // Every check skipped and none of them suspect is still "none": a PR whose
  // diff dispatched nothing has not been verified, and `ciWillRun` decides what
  // that means.
  const missing = requiredLanes.filter((lane) => !checks.some((c) => c.name === lane));
  if (!live.length && !blockingSkips.length) return { state: 'none', failures: [], missing };

  const failures = [...live.filter((c) => HARD_FAILURE.includes(c.state)), ...blockingSkips];
  if (failures.length) return { state: 'red', failures, missing };
  const pending = live.filter((c) => STILL_RUNNING.includes(c.state));
  return { state: pending.length || missing.length ? 'pending' : 'green', failures: [], missing };
}

function observeChecks(pr, requiredLanes) {
  const raw = gh(['pr', 'checks', String(pr), '--json', 'name,state,link'], { allowFail: true });
  if (failed(raw)) {
    // "no checks reported" is a real answer: none have registered yet.
    if (/no checks reported/i.test(raw.stderr)) return { state: 'none', failures: [], missing: requiredLanes };
    return null; // unknown
  }
  return checksVerdict(JSON.parse(raw || '[]'), { requiredLanes });
}

function observeIntegration(commonDir, headSha, baseTip, scope, prFiles) {
  const verdictFile = integrationVerdictPath(commonDir, headSha);
  const v = readIntegrationVerdictFile(verdictFile);
  let movedSince = [];
  if (v && v.baseTip !== baseTip) {
    const raw = sh(`git diff --name-only ${v.baseTip} ${baseTip}`, { allowFail: true });
    movedSince = raw === null ? null : raw.split('\n').filter(Boolean);
  }
  return { ...judgeIntegrationVerdict(v, { baseTip, scope, movedSince, prFiles }), verdictFile };
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
  // Computed before the checks are read, because which workflows were *meant*
  // to run is what makes a required lane's absence meaningful.
  const ciGatesHit = gatesFor(files);
  const ciWillRun = ciGatesHit.length > 0;
  const checks = pr.number
    ? observeChecks(pr.number, requiredLanesFor(files))
    : { state: 'none', failures: [], missing: [] };

  const gitDir = sh('git rev-parse --git-dir', { allowFail: true }) || '';
  const common = sh('git rev-parse --git-common-dir', { allowFail: true }) || '';
  const queue = pr.number && pr.state === 'open'
    ? observeQueue(pr.number)
    : { enabled: false, inQueue: false, position: null, merged: false, failures: [] };
  const movement = baseMovement(baseChanged, files, CONFIG, { queue: Boolean(queue && queue.enabled) });

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
    queue,
    review: reviewFor(pr.reviews, headSha),
    remoteBranchExists: Boolean(remoteSha),
    files,
    ciWillRun,
    ciWorkflows: ciGatesHit.map(gateLabel),
    gated: hardStopHits(files, sh(`git log origin/${CONFIG.baseBranch}..HEAD --format=%B`, { allowFail: true }) || ''),
    base: movement,
    baseTip,
    integration: movement.needsIntegrationCheck
      ? observeIntegration(path.resolve(common || '.git'), headSha, baseTip, movement.scope, files)
      : { state: 'not-needed', output: '', verdictFile: null },
    checksDeadlinePassed: Date.now() > deadlines.checks,
    queueDeadlinePassed: Boolean(deadlines.queue) && Date.now() > deadlines.queue,
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

/**
 * spawnSync's timeout kills the shell and nothing under it, so a check that runs
 * out of time leaves its test workers running — holding RAM in a directory about
 * to be deleted, under the next check. Anything still working in `dir` is killed.
 * Linux only (/proc); elsewhere a no-op.
 */
function killProcessesUnder(dir) {
  let pids;
  try {
    pids = fs.readdirSync('/proc').filter((p) => /^\d+$/.test(p));
  } catch {
    return;
  }
  for (const pid of pids) {
    if (Number(pid) === process.pid) continue;
    try {
      const cwd = fs.readlinkSync(`/proc/${pid}/cwd`);
      if (cwd === dir || cwd.startsWith(`${dir}/`)) process.kill(Number(pid), 'SIGKILL');
    } catch {
      // Gone already, or not ours to inspect.
    }
  }
}

/**
 * Builds the merge result (base tip + this head) in a throwaway worktree, runs the
 * repo's integrationCheck there, and records the verdict for observe() to read.
 *
 * A worktree, not the current checkout: the branch must stay exactly what was
 * reviewed, and the merge commit made here is never pushed — GitHub makes the
 * real one. Reaped with rm + prune, never `worktree remove --force`, because a
 * repo with a submodule makes the refusal routine (see reportReap).
 */
/**
 * The command's own exit code for "I could not run" (a missing tool, a failed
 * install) as opposed to "the merge result is broken". Recorded as `error`, so the
 * author is never told to fix code that has nothing wrong with it.
 */
const INTEGRATION_CHECK_CANNOT_RUN = 3;

/**
 * Rebases HEAD onto `upstream` and moves every initialized submodule to the commit
 * the rebased tree records. A rebase moves a submodule's recorded commit but never
 * its checkout, so without the sync a base that bumped the submodule leaves the
 * tree dirty and the next run stops at preflight (exit 40) over a change nobody made.
 */
function rebaseOnto(upstream) {
  try {
    sh(`git rebase ${upstream}`);
  } catch (err) {
    sh('git rebase --abort', { allowFail: true });
    throw new Error(`rebase conflicted — resolve by hand, then re-run.\n${err.message}`);
  }
  try {
    sh('git submodule update');
  } catch (err) {
    throw new Error(`rebased, but \`git submodule update\` failed — run it, then re-run.\n${err.message}`);
  }
}

/**
 * One check at a time per clone. Each is a full install plus every typecheck and
 * test runner, and parallel workers landing together (all worktrees of one clone)
 * would otherwise run them side by side on a dev box with no RAM budget. The lock
 * holds the owner's pid, so one left by a killed run is recognised and taken over.
 */
function withIntegrationLock(common, fn) {
  const lock = path.join(common, 'pr-land', 'integration.lock');
  fs.mkdirSync(path.dirname(lock), { recursive: true });
  let announced = false;
  for (;;) {
    try {
      fs.writeFileSync(lock, String(process.pid), { flag: 'wx' });
      break;
    } catch {
      const owner = Number(fs.readFileSync(lock, 'utf8'));
      let alive = false;
      try {
        process.kill(owner, 0);
        alive = true;
      } catch {
        // No such process: a killed run's lock.
      }
      if (!alive) {
        fs.rmSync(lock, { force: true });
        continue;
      }
      if (!announced) log(`  waiting for another integration check (pid ${owner}) to finish`);
      announced = true;
      sleep(CONFIG.pollIntervalMs);
    }
  }
  try {
    return fn();
  } finally {
    fs.rmSync(lock, { force: true });
  }
}

function runIntegrationCheck(s) {
  const common = path.resolve(sh('git rev-parse --git-common-dir'));
  return withIntegrationLock(common, () => runIntegrationCheckLocked(s, common));
}

/**
 * Where the integration check builds its merge result: `$PR_LAND_SCRATCH_DIR`, else
 * `$XDG_CACHE_HOME/pr-land`, else `~/.cache/pr-land`. Never `os.tmpdir()`: /tmp is
 * often a RAM-backed tmpfs, and a full install plus every test runner's temp files
 * filled one (ENOSPC mid-land). Never the git common dir either: Jest ignores every
 * path containing `/.git/`, so a check built there would find no tests.
 */
function integrationScratchRoot(env = process.env) {
  if (env.PR_LAND_SCRATCH_DIR) return path.resolve(env.PR_LAND_SCRATCH_DIR);
  // The XDG spec says a relative value is invalid and must be ignored.
  const xdg = env.XDG_CACHE_HOME && path.isAbsolute(env.XDG_CACHE_HOME) ? env.XDG_CACHE_HOME : null;
  return path.join(xdg || path.join(os.homedir(), '.cache'), 'pr-land');
}

function runIntegrationCheckLocked(s, common) {
  const verdictFile = integrationVerdictPath(common, s.branch.headSha);
  const scope = s.base.scope;
  const scratchRoot = integrationScratchRoot();
  fs.mkdirSync(scratchRoot, { recursive: true });
  // `tree` holds the merge result; `tmp` is the check's TMPDIR, so the test
  // runners' own temp files land on the same disk and go with it.
  const dir = fs.mkdtempSync(path.join(scratchRoot, 'integration-'));
  const tree = path.join(dir, 'tree');
  const tmp = path.join(dir, 'tmp');
  fs.mkdirSync(tmp);
  let ok = false;
  let error = false;
  let output = '';
  try {
    sh(`git worktree add --detach "${tree}" ${s.baseTip}`);
    const merged = sh(
      `git -C "${tree}" -c user.name=pr-land -c user.email=pr-land@localhost merge --no-ff --no-edit ${s.branch.headSha}`,
      { allowFail: true },
    );
    if (merged === null) {
      output = `${s.branch.headSha.slice(0, 12)} does not merge cleanly onto ${s.baseTip.slice(0, 12)}.`;
    } else {
      // `2>&1` inside the shell, not stdout+stderr concatenated afterwards: tools
      // like Jest write their report to stderr, so the concatenation put a passing
      // suite's last lines at the tail and dropped the step that actually failed.
      const r = spawnSync(`${CONFIG.integrationCheck.command} 2>&1`, {
        cwd: tree,
        // "shared" or "wide" — see baseMovement. The command decides what each covers.
        env: { ...process.env, PR_LAND_INTEGRATION_SCOPE: scope, TMPDIR: tmp, TMP: tmp, TEMP: tmp },
        shell: true,
        encoding: 'utf8',
        timeout: CONFIG.integrationCheck.timeoutMs,
        maxBuffer: 64 * 1024 * 1024,
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      ok = r.status === 0;
      // A timeout or a signal is the machine's verdict, not the merge's.
      error = !ok && (Boolean(r.error) || r.signal !== null || r.status === INTEGRATION_CHECK_CANNOT_RUN);
      output = String(r.stdout || '').split('\n').slice(-60).join('\n');
      if (r.error) output += `\n${r.error.message}`;
    }
  } finally {
    killProcessesUnder(dir);
    fs.rmSync(dir, { recursive: true, force: true });
    sh('git worktree prune', { allowFail: true });
  }
  fs.mkdirSync(path.dirname(verdictFile), { recursive: true });
  fs.writeFileSync(
    verdictFile,
    JSON.stringify({ baseTip: s.baseTip, scope, ok, error, at: Date.now(), output, command: CONFIG.integrationCheck.command }),
  );
  return ok;
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
        ? `CI covers this diff (${s.ciWorkflows.join(', ')}).`
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
    case ACTION.WAIT_QUEUE:
      return sleep(CONFIG.pollIntervalMs);

    case ACTION.REBASE: {
      if (DRY_RUN) return log('  [dry-run] would rebase');
      rebaseOnto(`origin/${CONFIG.baseBranch}`);
      sh(`git push --force-with-lease origin ${s.branch.name}`);
      return log('  rebased and pushed — CI and review re-run against the new head');
    }

    case ACTION.INTEGRATION_CHECK: {
      if (DRY_RUN) return log(`  [dry-run] would run \`${CONFIG.integrationCheck.command}\` on the merge result`);
      log(`  running \`${CONFIG.integrationCheck.command}\` (scope ${s.base.scope}) on the merge result — minutes, not a CI lane`);
      const ok = runIntegrationCheck(s);
      return log(ok ? '  the merge result passes' : '  the merge result FAILS');
    }

    case ACTION.MERGE:
      if (DRY_RUN) return log(`  [dry-run] would merge #${s.pr.number}`);
      // NOT --delete-branch: that makes gh check out the base branch locally
      // afterwards, which fails when another worktree holds it — the normal state
      // under this contract. The remote ref is deleted as its own reconciled step.
      gh(['pr', 'merge', String(s.pr.number), `--${CONFIG.mergeMethod}`]);
      return log(`  merged #${s.pr.number}`);

    case ACTION.ENQUEUE: {
      if (DRY_RUN) return log(`  [dry-run] would add #${s.pr.number} to the merge queue`);
      // expectedHeadOid: a push racing this call must not enqueue a head nobody
      // has seen green. GitHub refuses the mutation instead.
      gh([
        'api', 'graphql',
        '-f', 'query=mutation($id: ID!, $head: GitObjectID!) { enqueuePullRequest(input: { pullRequestId: $id, expectedHeadOid: $head }) { mergeQueueEntry { position } } }',
        '-f', `id=${s.queue.id}`, '-f', `head=${s.branch.headSha}`,
      ]);
      return log(`  queued #${s.pr.number} — the queue now runs CI on the merge result`);
    }

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

  // Per head, not per invocation: a rebase or a long integration check inside
  // this same run starts a new CI run, and a deadline set at startup would then
  // expire on a run that was only just dispatched — reported, wrongly, as "CI
  // never settled".
  const deadlines = {};
  let deadlineHead = null;
  const resetDeadlines = () => {
    deadlines.checks = Date.now() + CONFIG.checksTimeoutMs;
    deadlines.review = Date.now() + CONFIG.reviewTimeoutMs;
  };
  resetDeadlines();
  let last = '';
  let unknownStreak = 0;
  // A reconciler must make progress. If the same non-waiting action repeats
  // without the observed state changing, the action is not reducing the gap and
  // looping is pointless — that is a livelock, and it is how a dry run spun
  // forever printing "would delete the remote branch".
  let repeat = { key: '', n: 0 };

  for (;;) {
    const s = observe(deadlines);
    // The queue's clock starts when the PR enters it, not when the head was pushed:
    // CI and review have already spent theirs.
    if (s.queue && s.queue.inQueue) {
      if (!deadlines.queue) deadlines.queue = Date.now() + CONFIG.queueTimeoutMs;
    } else {
      deadlines.queue = null;
    }
    if (s.branch.headSha !== deadlineHead) {
      if (deadlineHead !== null) {
        resetDeadlines();
        // observe() judged the flags against the previous head's deadlines.
        s.checksDeadlinePassed = Date.now() > deadlines.checks;
        s.reviewDeadlinePassed = Date.now() > deadlines.review;
      }
      deadlineHead = s.branch.headSha;
    }

    // An unreadable world is NOT a verdict about the PR — look again.
    if (s.pr.state === 'unknown' || s.checks.state === 'unknown' || s.queue === null) {
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

    const isWait = [ACTION.WAIT_CHECKS, ACTION.WAIT_REVIEW, ACTION.WAIT_QUEUE].includes(verdict.action);
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
  loadConfig, decide, hardStopHits, ciCovers, gatesFor, requiredLanesFor, baseMovement, touches, reviewFor,
  integrationVerdictPath, readIntegrationVerdictFile, judgeIntegrationVerdict,
  integrationScratchRoot, runIntegrationCheck, killProcessesUnder, INTEGRATION_CHECK_CANNOT_RUN,
  checksVerdict, runIdOf, queueStateFrom, pickBranchPr, headCarriesWorkBeyond, rebaseOnto,
};
