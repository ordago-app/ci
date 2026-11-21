#!/usr/bin/env node
// Which ideas/ plans are most overdue a review against the code, oldest first.
//
//   node scripts/ideas-review-order.js            # every idea, oldest-reviewed first
//   node scripts/ideas-review-order.js --limit 40 # the next review batch
//   node scripts/ideas-review-order.js --json
//
// An idea was last reviewed at the later of:
//   - its last real edit — the same sweep-aware walk the plans map uses for
//     `Advanced`, so a convention rollout touching every plan does not read as
//     a review of each; and
//   - the last commit naming it in a `Reviewed-Idea: <slug>` trailer.
//
// The trailer is what makes a rolling review possible. A review whose verdict is
// "still valid" rightly changes nothing in the file, and a review that rewrites
// many ideas at once is a sweep the walk skips — so without the trailer both
// leave no trace, and every run re-reads the whole backlog from the top. The
// date lives in git, never in the file: a hand-written "last reviewed" line is
// the field that rots first (`HANDWRITTEN_STATE` in plans-map.js rejects it).

'use strict';

const { execFileSync } = require('node:child_process');
const { collect, planCommits, lastRealTouch, dirtyPlanFiles } = require('./plans-map');

// The consuming repo, from the working directory: consumers reach this file
// through a symlink into the submodule, where __dirname is the submodule.
function repoRoot() {
  try {
    return execFileSync('git', ['rev-parse', '--show-toplevel'], { encoding: 'utf8' }).trim();
  } catch {
    return process.cwd();
  }
}
const REPO_ROOT = repoRoot();
const TRAILER = 'Reviewed-Idea';

/** slug → date (YYYY-MM-DD) of the newest commit carrying `Reviewed-Idea: <slug>`. */
function parseReviewTrailers(log) {
  const reviewed = new Map();
  for (const chunk of log.split('\x01').slice(1)) {
    const [date, trailers = ''] = chunk.split('\x1f');
    for (const slug of trailers.split('\x1e').map((s) => s.trim()).filter(Boolean)) {
      // `git log` is newest first, so the first date seen for a slug is its latest.
      if (!reviewed.has(slug)) reviewed.set(slug, date.trim());
    }
  }
  return reviewed;
}

function readReviewTrailers() {
  const log = execFileSync(
    'git',
    [
      'log',
      `--grep=^${TRAILER}:`,
      '--date=short',
      `--format=%x01%ad%x1f%(trailers:key=${TRAILER},valueonly,separator=%x1e)`,
    ],
    { cwd: REPO_ROOT, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 }
  );
  return parseReviewTrailers(log);
}

/**
 * Pure ordering. `touched` is null for an idea with no committed history or one
 * being edited right now — both mean "newest possible", so it sorts last.
 * @param {{slug: string, priority: string|null, touched: string|null, reviewed: string|null}[]} ideas
 */
function orderForReview(ideas) {
  return ideas
    .map((idea) => {
      const candidates = [idea.touched, idea.reviewed].filter(Boolean).sort();
      const fresh = idea.touched === null;
      return { ...idea, lastReviewed: fresh ? null : candidates[candidates.length - 1] };
    })
    .sort((a, b) => {
      if (a.lastReviewed === b.lastReviewed) return a.slug.localeCompare(b.slug);
      if (a.lastReviewed === null) return 1;
      if (b.lastReviewed === null) return -1;
      return a.lastReviewed.localeCompare(b.lastReviewed);
    });
}

function parseArgs(argv) {
  let json = false;
  let limit = Infinity;
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--json') json = true;
    else if (argv[i] === '--limit') {
      limit = Number(argv[(i += 1)]);
      if (!Number.isInteger(limit) || limit < 1) throw new Error('--limit needs a positive integer');
    } else throw new Error(`unknown argument ${argv[i]}`);
  }
  return { json, limit };
}

function main() {
  const { json, limit } = parseArgs(process.argv.slice(2));
  const { plans, errors } = collect();
  if (errors.length > 0) throw new Error(`invalid plan metadata — run plans-map.js --validate:\n${errors.join('\n')}`);

  const commits = planCommits();
  const dirty = dirtyPlanFiles();
  const reviewed = readReviewTrailers();
  const ordered = orderForReview(
    plans
      .filter((p) => p.stage === 'ideas')
      .map((p) => ({
        slug: p.slug,
        relPath: p.relPath,
        priority: p.priority,
        touched: lastRealTouch(p.relPath, commits, dirty).date,
        reviewed: reviewed.get(p.slug) || null,
      }))
  ).slice(0, limit);

  if (json) {
    process.stdout.write(`${JSON.stringify(ordered, null, 2)}\n`);
    return;
  }
  for (const idea of ordered) {
    process.stdout.write(`${idea.lastReviewed || 'editing  '}  ${(idea.priority || '—').padEnd(6)}  ${idea.relPath}\n`);
  }
}

if (require.main === module) {
  try {
    main();
  } catch (err) {
    process.stderr.write(`ideas-review-order: ${err.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = { orderForReview, parseReviewTrailers, TRAILER };
