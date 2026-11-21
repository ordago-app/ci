#!/usr/bin/env node
// Generates docs/plans/_plans-map.md — the index of in-flight plans — from the
// metadata block that `managing-plans-lifecycle` (agent-plans v2) puts under every
// plan's title.
//
//   node scripts/plans-map.js            # rewrite the map
//   node scripts/plans-map.js --check    # fail if a block is invalid or the map is stale
//   node scripts/plans-map.js --validate # fail ONLY if a block is invalid; never reads the map
//
// Run from anywhere inside the consuming repo: the root is the git toplevel of the
// working directory, never this file's location, because consumers reach this file
// through a symlink into the `.agents/_shared` submodule.
//
// Why this exists: the orchestrator's batch survey and the release cut both ask
// "what is actionable now", and both used to answer it by reading prose across
// every plan in docs/plans/ongoing/. A mandated `## Status` header meant to make
// that cheap was present in 2 of 23 plans — plan state changes with no diff (a
// release ships and a plan becomes unblocked), so a convention with no enforcement
// decays. Hence: a parsed block, a generated index, a lint.
//
// Two facts are DERIVED and must never be written by hand — the stage from the
// folder, `Advanced` from git. A hand-written "last updated" is the field that goes
// stale first.
//
// `Advanced` walks PAST sweep commits — see SWEEP_FANOUT. Deriving it from the
// file's last git touch was defeated the first time a convention rollout touched
// 27 of 30 plans in one commit: every plan read "this cycle" and the column could
// no longer answer the one question it exists for. A commit that edits many plan
// files at once is mechanical by construction, so it is not evidence that any one
// of those plans advanced.

const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

function repoRoot() {
  try {
    return execFileSync('git', ['rev-parse', '--show-toplevel'], { encoding: 'utf8' }).trim();
  } catch {
    return process.cwd();
  }
}

const REPO_ROOT = repoRoot();
const PLANS_DIR = path.join(REPO_ROOT, 'docs', 'plans');
const MAP_PATH = path.join(PLANS_DIR, '_plans-map.md');
const CHANGELOG = path.join(REPO_ROOT, 'CHANGELOG.md');
const INSTRUCTION_FILES = ['AGENTS.md', 'CLAUDE.md'];

/**
 * `Landed` is required in ongoing/ only where the repo deploys, and the repo says so
 * machine-readably with this exact line in its instructions (agent-plans v2). A
 * library or a docs repo has no environment for the field to name.
 */
const LANDED_MARKER = '<!-- plans:landed -->';

function declaresLanded(root = REPO_ROOT) {
  return INSTRUCTION_FILES.some((name) => {
    const p = path.join(root, name);
    return fs.existsSync(p) && fs.readFileSync(p, 'utf8').includes(LANDED_MARKER);
  });
}

/**
 * Stage → which fields the block must carry. The full block is required only where
 * the value is: the plans someone might pick up this cycle. Requiring it across the
 * ideas backlog would buy noise, not signal.
 */
function stages(landed = declaresLanded()) {
  const inFlight = ['Priority', ...(landed ? ['Landed'] : []), 'Gate', 'Next'];
  return {
    ideas: { dir: 'ideas', required: ['Priority'] },
    ready: { dir: 'ready', required: ['Priority'] },
    ongoing: { dir: 'ongoing', required: inFlight },
    soak: { dir: path.join('ongoing', 'soak'), required: inFlight },
  };
}

const FIELDS = ['Priority', 'Landed', 'Gate', 'Next', 'Due'];
const PRIORITIES = ['high', 'medium', 'low'];
const LANDED = ['none', 'dev', 'beta', 'prod', 'n/a'];
// The stage comes from the folder and `Advanced` from git, so a state line written
// beside the block is a second source of the same fact and it is the one that rots.
// The spellings are the ones actually found in a sweep of 45 such lines across 40
// plans, not a guess — a pattern anchored on Status/Stage alone left ten in place
// while reading as coverage.
const HANDWRITTEN_STATE = /^\*\*(Status|Stage|Estado|Updated|Last reviewed|Re-verified)(\s*\([^)]*\))?:\*\*/;
const SEMVER = /^\d+\.\d+(\.\d+)?$/;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const RECHECK = /\s*\(recheck (\d{4}-\d{2}-\d{2})\)$/;

const STALE_CYCLES = 2;

/**
 * Fan-out above which a commit is a **sweep** — a convention rollout, a lifecycle
 * reorg, a bulk retire, a formatter pass — and is walked past when deriving
 * `Advanced`, because a mechanical edit is not progress.
 *
 * Two thresholds, because fan-out alone does not separate the cases: a commit that
 * moved the code and touched 7 plan files genuinely advanced them, while a prose
 * pass touching 8 advanced none. The discriminator is whether the commit shipped
 * anything (see isShippingFile): a pure-prose commit is bookkeeping once it stops
 * being about one or two plans, while one that also changes code can legitimately
 * close several plans at once. These are the numbers agent-plans v2 specifies.
 */
const SWEEP_FANOUT = { prose: 4, shipping: 8 };

/**
 * `**Priority:** low — defense-in-depth, not a live incident.` The label is the
 * machine-readable part; the trailing rationale is worth keeping, so an em-dash tail
 * is stripped rather than rejected. A value with no separator (`low-medium`) is
 * still an error — that is a plan refusing to decide, which is exactly what the
 * label exists to force.
 */
function label(raw) {
  const m = raw.match(/^([^\s—–]+)(?:\s+[—–-]\s+.*)?$/s);
  return m ? m[1] : raw;
}

function git(args, cwd = REPO_ROOT) {
  // 64 MB, not node's 1 MB default: a full-history `--name-only` walk grows with
  // the repo. Overflowing throws ENOBUFS, and a catch that read that as "no
  // history" once rendered every plan as freshly touched — a confidently wrong map
  // from a silent fallback.
  return execFileSync('git', args, { cwd, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 }).trim();
}

/** The only git failure worth tolerating: the map is generated outside a checkout. */
function isGitRepo() {
  try {
    git(['rev-parse', '--git-dir']);
    return true;
  } catch {
    return false;
  }
}

/**
 * A shallow clone has no history to walk, so `Advanced` would read "this cycle" for
 * every plan — the exact lie this file exists to prevent, arriving silently. The
 * regenerating workflow must check out with `fetch-depth: 0`; this refuses rather
 * than trusting that it stays that way.
 */
const SHALLOW_MESSAGE =
  'plans-map: the repository is a shallow clone, so no plan has a readable history and every\n' +
  'plan would render as advanced this cycle. Fetch full history (`git fetch --unshallow`, or\n' +
  '`fetch-depth: 0` in the workflow) and run again.';

function isShallowRepo() {
  return git(['rev-parse', '--is-shallow-repository']) === 'true';
}

/**
 * Release-cycle boundaries, newest first, as ISO dates.
 *
 * The commit that first introduced a `## vX.Y.Z` (or `## [X.Y.Z]`) heading into
 * CHANGELOG.md is the cycle boundary — a tag may be cut later, or never, by
 * whatever deploy pipeline the repo has. With no such headings, `v*` tags are the
 * fallback. Dates are de-duplicated on purpose: three hotfixes cut in one afternoon
 * are one cycle, and counting three would mark every untouched plan stale overnight.
 */
function releaseCycleDates(changelog = CHANGELOG) {
  const seen = new Map();
  const add = (date, version) => {
    if (date && !seen.has(date)) seen.set(date, version);
  };

  const headings = fs.existsSync(changelog)
    ? fs
        .readFileSync(changelog, 'utf8')
        .split('\n')
        .map((line) => line.match(/^## \[?(v?\d+\.\d+(?:\.\d+)?)\]?(?:\s|$)/))
        .filter(Boolean)
        .map((m) => m[1])
    : [];

  for (const version of headings) {
    let out;
    try {
      // The pickaxe needs the heading's own spelling; try the bracketed form too.
      out =
        git(['log', '--reverse', '--format=%ad', '--date=short', `-S## ${version}`, '--', 'CHANGELOG.md']) ||
        git(['log', '--reverse', '--format=%ad', '--date=short', `-S## [${version}]`, '--', 'CHANGELOG.md']);
    } catch {
      continue;
    }
    add(out.split('\n')[0], version.startsWith('v') ? version : `v${version}`);
  }

  if (seen.size === 0 && isGitRepo()) {
    const tags = git(['for-each-ref', '--sort=-creatordate', '--format=%(creatordate:short) %(refname:short)', 'refs/tags/v*']);
    for (const line of tags.split('\n').filter(Boolean)) {
      const [date, tag] = line.split(' ');
      add(date, tag);
    }
  }

  return [...seen.entries()]
    .map(([date, version]) => ({ date, version }))
    .sort((a, b) => (a.date < b.date ? 1 : -1));
}

function isPlanFile(p) {
  return p.startsWith('docs/plans/') && p.endsWith('.md') && !path.posix.basename(p).startsWith('_');
}

/**
 * Did this file's change ship something? Prose did not — and that includes prose
 * outside `docs/`, which is the trap: a convention rollout edits `AGENTS.md`
 * alongside the plans it rolls out, and reading that as shipping work would hand
 * the sweep the higher threshold and reset every clock it touched.
 */
function isShippingFile(p) {
  return !p.startsWith('docs/') && !p.endsWith('.md');
}

/**
 * Every commit that touched a plan file, newest first, with the plan files it
 * touched and whether it shipped anything. One `git log` for the whole history
 * rather than one per plan — the walk needs each commit's fan-out and shape anyway.
 */
function planCommits() {
  if (!isGitRepo()) return [];
  if (isShallowRepo()) throw new Error(SHALLOW_MESSAGE);
  // Deliberately uncaught: a git that fails for any other reason must crash the
  // generator, not quietly yield an empty history that reads as "everything is
  // fresh". This walk is the whole basis of `Advanced`.
  return git(['log', '--format=%x01%h%x1f%ad%x1f%s', '--date=short', '--name-only'])
    .split('\x01')
    .slice(1)
    .map((chunk) => {
      const [header, ...rest] = chunk.split('\n');
      const [sha, date, subject] = header.split('\x1f');
      const files = rest.map((l) => l.trim()).filter(Boolean);
      return { sha, date, subject, shipped: files.some(isShippingFile), files: files.filter(isPlanFile) };
    })
    .filter((c) => c.files.length > 0);
}

function isSweep(commit) {
  return commit.files.length > (commit.shipped ? SWEEP_FANOUT.shipping : SWEEP_FANOUT.prose);
}

/**
 * Plan files with uncommitted changes. They are advancing *right now*, so the
 * history walk must not speak for them: otherwise a plan untouched for three cycles
 * reads "3 cycles ago" while the edit is in the working tree and "this cycle" the
 * instant it lands, so a map committed alongside it is stale on arrival.
 */
function dirtyPlanFiles() {
  if (!isGitRepo()) return new Set();
  return new Set(
    git(['status', '--porcelain', '--', 'docs/plans'])
      .split('\n')
      .map((l) => l.slice(3).trim())
      .filter(isPlanFile)
  );
}

/**
 * The date a plan last actually advanced, plus the sweeps walked past to reach it.
 * `null` when the plan has no committed history yet, or is being edited in the
 * working tree — both mean "newest possible".
 */
function lastRealTouch(relPath, commits, dirty = new Set()) {
  if (dirty.has(relPath)) return { date: null, skipped: [] };
  const touching = commits.filter((c) => c.files.includes(relPath));
  if (touching.length === 0) return { date: null, skipped: [] };

  const skipped = [];
  for (const commit of touching) {
    if (isSweep(commit)) {
      skipped.push(commit);
      continue;
    }
    return { date: commit.date, skipped };
  }

  // Every commit that ever touched this plan was a sweep: it has had no edit of its
  // own since it was written, so the honest date is when it entered the repo.
  const born = touching[touching.length - 1];
  return { date: born.date, skipped: skipped.filter((c) => c !== born) };
}

function parseBlock(text) {
  // The block sits between the `# Title` and the first `## Section`. Bounding the
  // search there keeps a `**Priority:**` quoted inside the body from being read as
  // the plan's own metadata.
  const head = text.split(/^## /m)[0];
  const fields = {};
  const re = new RegExp(`^\\*\\*(${FIELDS.join('|')}):\\*\\*[ \\t]*(.*)$`, 'gm');
  let m;
  while ((m = re.exec(head)) !== null) fields[m[1]] = m[2].trim();
  const titleMatch = text.match(/^#\s+(.+)$/m);
  return { fields, title: titleMatch ? titleMatch[1].trim() : null };
}

/**
 * `Gate:` must always name a specific trigger — a vague gate is how a plan sits
 * unnoticed for four cycles. Any gate may end with ` (recheck YYYY-MM-DD)`.
 */
function parseGate(raw) {
  const recheckMatch = raw.match(RECHECK);
  const recheck = recheckMatch ? recheckMatch[1] : null;
  const body = recheckMatch ? raw.slice(0, recheckMatch.index).trim() : raw.trim();
  const withRecheck = (gate) => (recheck ? { ...gate, recheck } : gate);

  if (body === 'none') return withRecheck({ kind: 'none' });
  const m = body.match(/^(release|soak|decision|blocked):\s*(.+)$/);
  if (!m) return { kind: 'invalid' };
  const [, kind, rest] = m;
  // `release:` must name a version — that is what lets the release cut clear it.
  // `soak:` cannot: a fleet-turnover gate has no version until the release that
  // raises the support floor is cut, so it takes free text naming the trigger.
  if (kind === 'release' && !SEMVER.test(rest.trim())) return { kind: 'invalid' };
  return withRecheck({ kind, detail: rest.trim() });
}

function collect(root = REPO_ROOT) {
  const plansDir = path.join(root, 'docs', 'plans');
  const plans = [];
  const errors = [];

  for (const [stage, { dir, required }] of Object.entries(stages(declaresLanded(root)))) {
    const abs = path.join(plansDir, dir);
    if (!fs.existsSync(abs)) continue;
    const files = fs
      .readdirSync(abs, { withFileTypes: true })
      .filter((e) => e.isFile() && e.name.endsWith('.md') && !e.name.startsWith('_'))
      .map((e) => e.name)
      .sort();

    for (const name of files) {
      const relPath = path.posix.join('docs/plans', dir.split(path.sep).join('/'), name);
      const raw = fs.readFileSync(path.join(abs, name), 'utf8');
      const { fields, title } = parseBlock(raw);
      const fail = (msg) => errors.push(`${relPath}: ${msg}`);

      if (!title) fail('no `# Title` heading');
      const handWritten = raw.split('\n').find((line) => HANDWRITTEN_STATE.test(line));
      if (handWritten) {
        fail(
          `hand-written state line \`${handWritten.trim().slice(0, 60)}\` — the folder gives ` +
            `the stage and git gives the last touch, so this is a second source that will rot. ` +
            `Fold what it says into the intro prose or the block's \`Next:\` and delete the line.`
        );
      }
      for (const field of required) {
        if (!fields[field]) fail(`missing \`**${field}:**\``);
      }
      const priority = fields.Priority ? label(fields.Priority) : null;
      if (priority && !PRIORITIES.includes(priority)) {
        fail(`Priority must be one of ${PRIORITIES.join(' | ')}, got "${fields.Priority}"`);
      }
      const landed = fields.Landed ? label(fields.Landed) : null;
      if (landed && !LANDED.includes(landed)) {
        fail(`Landed must be one of ${LANDED.join(' | ')}, got "${fields.Landed}"`);
      }
      const gate = fields.Gate ? parseGate(fields.Gate) : null;
      if (gate && gate.kind === 'invalid') {
        fail(
          'Gate must be `none`, `release:<x.y.z>`, `soak:<trigger>`, `decision:<question>` or ' +
            `\`blocked:<why>\`, optionally ending in \` (recheck YYYY-MM-DD)\`, got "${fields.Gate}"`
        );
      }
      if (fields.Due && !ISO_DATE.test(fields.Due)) {
        fail(`Due must be a YYYY-MM-DD date, got "${fields.Due}"`);
      }

      plans.push({
        stage,
        slug: name.replace(/\.md$/, ''),
        file: name,
        relPath,
        title,
        priority,
        landed,
        gate: gate || { kind: 'none' },
        gateRaw: fields.Gate || 'none',
        next: fields.Next || null,
        due: fields.Due || null,
      });
    }
  }
  return { plans, errors };
}

function annotateFreshness(plans, cycles, commits = planCommits(), dirty = dirtyPlanFiles()) {
  for (const plan of plans) {
    const { date, skipped } = lastRealTouch(plan.relPath, commits, dirty);
    plan.touched = date;
    plan.skipped = skipped;
    // Untracked file = created in the working tree right now. Newest possible.
    plan.cyclesStale = date === null ? 0 : cycles.filter((c) => c.date > date).length;
  }
}

function freshnessLabel(plan) {
  // A plan edited in the commit that also regenerates this map has no git date yet,
  // and gains one the instant the commit lands. Labelling that state separately
  // would make every such map stale the moment it was committed. Untracked and
  // just-committed are both "this cycle", which is the truth either way.
  // `*` = a sweep was walked past to reach this date, so it is deliberately older
  // than `git log -1` on the file. The footer names the commits.
  const swept = plan.skipped && plan.skipped.length > 0 ? ' \\*' : '';
  if (plan.touched === null) return 'this cycle';
  if (plan.cyclesStale === 0) return `this cycle${swept}`;
  return `${plan.cyclesStale} cycle${plan.cyclesStale === 1 ? '' : 's'} ago${swept}`;
}

const PRIORITY_RANK = { high: 0, medium: 1, low: 2 };

function sortRows(a, b) {
  if (b.cyclesStale !== a.cyclesStale) return b.cyclesStale - a.cyclesStale;
  const pa = PRIORITY_RANK[a.priority] ?? 3;
  const pb = PRIORITY_RANK[b.priority] ?? 3;
  if (pa !== pb) return pa - pb;
  return a.slug.localeCompare(b.slug);
}

function cell(text) {
  return String(text).replace(/\|/g, '\\|').replace(/\n/g, ' ');
}

function row(plan, withReason) {
  const stale = plan.cyclesStale >= STALE_CYCLES ? ' ⚠️' : '';
  const link = `[${plan.slug}](${plan.relPath.replace('docs/plans/', '')})`;
  const stage = plan.stage === 'ongoing' ? '' : ` \`${plan.stage}\``;
  const cells = [`${link}${stage}${stale}`, plan.priority || '—', plan.landed || '—', freshnessLabel(plan)];
  // The sections whose whole point is "why is this not moving" earn a column for the
  // answer. Everywhere else it would be an empty column on every row.
  if (withReason) {
    const recheck = plan.gate.recheck ? ` (recheck ${plan.gate.recheck})` : '';
    cells.push(`${plan.gate.detail || '—'}${recheck}`);
  }
  cells.push(plan.next || '—');
  return `| ${cells.map(cell).join(' | ')} |`;
}

function table(plans, withReason = false) {
  if (plans.length === 0) return '_None._\n';
  const cols = ['Plan', 'Pri', 'Landed', 'Advanced', ...(withReason ? ['Waiting on'] : []), 'Next'];
  const header = `| ${cols.join(' | ')} |\n|${cols.map(() => '---').join('|')}|\n`;
  return header + [...plans].sort(sortRows).map((p) => row(p, withReason)).join('\n') + '\n';
}

/**
 * `today` is a parameter, not `new Date()` inside, so the output is a pure function
 * of its inputs. Only the recheck and due callouts read it, and both are dated
 * facts a reader needs; a map that changes daily for no other reason would be noise.
 */
function render(plans, cycles, today = null) {
  const tracked = plans.filter((p) => p.stage !== 'ideas');
  const byKind = (kind) => tracked.filter((p) => p.gate.kind === kind);

  const releases = new Map();
  for (const plan of byKind('release')) {
    if (!releases.has(plan.gate.detail)) releases.set(plan.gate.detail, []);
    releases.get(plan.gate.detail).push(plan);
  }

  const current = cycles[0];
  const stale = tracked.filter((p) => p.cyclesStale >= STALE_CYCLES).sort(sortRows);
  const recheckDue = today ? tracked.filter((p) => p.gate.recheck && p.gate.recheck <= today) : [];
  const dated = tracked.filter((p) => p.due).sort((a, b) => (a.due < b.due ? -1 : 1));

  const out = [];
  out.push('# Plans map');
  out.push('');
  out.push('<!-- GENERATED by scripts/plans-map.js — do not edit by hand.');
  out.push('     Edit the `**Priority:** / **Landed:** / **Gate:** / **Next:**` block at the top of');
  out.push('     the plan itself, and let CI regenerate this file on the base branch. -->');
  out.push('');
  // Deliberately NO aggregate counts. A total changes whenever ANY plan is added or
  // removed, so every plan-touching PR edited this one line and collided with every
  // other — the single highest-conflict line in the repo it came from.
  out.push(
    `Current cycle **${current ? `v${current.version.replace(/^v/, '')}` : 'unreleased'}**${
      current ? ` (cut ${current.date})` : ''
    }`
  );
  out.push('');
  out.push(
    'Read this top-down: **Actionable now** is what a batch can pick up today; the release ' +
      'sections empty themselves when that version is cut; **Waiting on you** is the escalation ' +
      'list. ⚠️ marks a plan not advanced in ' +
      `${STALE_CYCLES}+ release cycles — the ones easiest to forget. ` +
      '`\\*` in **Advanced** means a sweep commit was walked past to reach that date — see ' +
      '[how `Advanced` is derived](#how-advanced-is-derived).'
  );
  out.push('');

  if (stale.length > 0) {
    out.push(`> **Not advanced in ${STALE_CYCLES}+ cycles:** ` + stale.map((p) => p.slug).join(' · '));
    out.push('');
  }
  if (recheckDue.length > 0) {
    out.push('> **Recheck date passed:** ' + recheckDue.map((p) => `${p.slug} (${p.gate.recheck})`).join(' · '));
    out.push('');
  }
  if (dated.length > 0) {
    out.push('> **Due:** ' + dated.map((p) => `${p.slug} ${p.due}`).join(' · '));
    out.push('');
  }

  out.push('## Actionable now');
  out.push('');
  out.push(table(byKind('none')));

  for (const [version, group] of [...releases.entries()].sort()) {
    out.push(`## Waiting on release ${version}`);
    out.push('');
    out.push(table(group));
  }
  out.push('## Soaking');
  out.push('');
  out.push(table(byKind('soak'), true));

  out.push('## Waiting on you');
  out.push('');
  out.push(table(byKind('decision'), true));

  out.push('## Blocked');
  out.push('');
  out.push(table(byKind('blocked'), true));

  const sweeps = [];
  for (const plan of plans) {
    for (const commit of plan.skipped || []) {
      if (!sweeps.some((c) => c.sha === commit.sha)) sweeps.push(commit);
    }
  }

  out.push('## How `Advanced` is derived');
  out.push('');
  out.push(
    '`Advanced` is the release cycle containing the last commit that touched the plan file — ' +
      'except that **sweeps are walked past**, because a mechanical edit is not progress. A ' +
      `pure-prose commit is a sweep once it touches more than ${SWEEP_FANOUT.prose} ` +
      `plan files; one that also ships code, more than ${SWEEP_FANOUT.shipping} — shipping work ` +
      'can legitimately close several plans at once, bookkeeping cannot. A `\\*` marks a row whose ' +
      'date was reached by walking past a sweep: it is older than `git log -1` on the file, on purpose.'
  );
  out.push('');
  if (sweeps.length > 0) {
    out.push('Sweeps walked past, newest first:');
    out.push('');
    out.push('| Commit | Date | Plan files | Subject |');
    out.push('|---|---|---|---|');
    for (const c of sweeps.sort((a, b) => (a.date < b.date ? 1 : -1))) {
      out.push(`| \`${c.sha}\` | ${c.date} | ${c.files.length} | ${cell(c.subject)} |`);
    }
    out.push('');
  }

  out.push('## Ideas');
  out.push('');
  out.push(
    'Proposals live in [`ideas/`](ideas/). Not listed here on purpose — they carry a ' +
      'priority but no gate, and an index of everything indexes nothing. Survey them when the ' +
      'in-flight list runs dry.'
  );
  out.push('');
  return out.join('\n');
}

function main() {
  const check = process.argv.includes('--check');
  // Validation without the staleness comparison. A PR gets this and nothing more: a
  // malformed block is an author error CI must catch BEFORE it reaches the base
  // branch (the generator exits before writing, so it cannot repair one post-merge),
  // while a stale map is regenerated on the base branch and is never the author's
  // problem — it is usually caused by a different PR landing.
  const validateOnly = process.argv.includes('--validate');

  // Parsing the blocks is pure file reading. Everything git-dependent — the shallow
  // guard, release cycles, freshness — belongs AFTER the validate-only exit, because
  // a PR job checks out shallow by default and `--validate` must survive that.
  const { plans, errors } = collect();

  if (errors.length > 0) {
    console.error('Invalid plan metadata blocks:\n');
    for (const e of errors) console.error(`  ${e}`);
    console.error(
      '\nEvery plan declares its state in a block under its title: `**Priority:**` everywhere,\n' +
        'plus `**Gate:** / **Next:**` (and `**Landed:**` where the repo declares\n' +
        `\`${LANDED_MARKER}\`) once it reaches ongoing/. See the managing-plans-lifecycle skill.`
    );
    process.exit(1);
  }

  if (validateOnly) {
    console.log(`plan metadata is valid (${plans.length} plans).`);
    return;
  }

  if (isGitRepo() && isShallowRepo()) {
    console.error(SHALLOW_MESSAGE);
    process.exit(1);
  }
  const cycles = releaseCycleDates();
  annotateFreshness(plans, cycles);

  // Recheck/due callouts only in the regenerated file, keyed to the last commit's
  // date rather than the wall clock, so `--check` on the same commit is reproducible.
  const today = isGitRepo() ? git(['log', '-1', '--format=%ad', '--date=short']) : null;
  const rendered = render(plans, cycles, today);
  const existing = fs.existsSync(MAP_PATH) ? fs.readFileSync(MAP_PATH, 'utf8') : null;

  if (check) {
    if (existing !== rendered) {
      console.error('docs/plans/_plans-map.md is out of date. Run `node scripts/plans-map.js` and commit it.');
      process.exit(1);
    }
    console.log(`plans map is in sync (${plans.length} plans).`);
    return;
  }

  fs.writeFileSync(MAP_PATH, rendered);
  console.log(`Wrote docs/plans/_plans-map.md (${plans.length} plans).`);
}

if (require.main === module) main();

module.exports = {
  parseBlock,
  parseGate,
  label,
  releaseCycleDates,
  declaresLanded,
  stages,
  collect,
  render,
  annotateFreshness,
  lastRealTouch,
  dirtyPlanFiles,
  isSweep,
  isPlanFile,
  isShippingFile,
  planCommits,
  isShallowRepo,
  isGitRepo,
  SWEEP_FANOUT,
  LANDED_MARKER,
};
