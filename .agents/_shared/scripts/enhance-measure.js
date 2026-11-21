#!/usr/bin/env node
// The evidence an enhancement pass starts from, as one JSON document.
//
//   node scripts/enhance-measure.js                  # whole repo, last 90 days of origin/<base>
//   node scripts/enhance-measure.js --since 365      # a quiet repo: widen the window
//   node scripts/enhance-measure.js --scope services/github-review
//   node scripts/enhance-measure.js --ref HEAD --no-prs
//
// `enhance-repo` reads this instead of having every agent re-derive it with
// `git log` pipelines: churn, fix density and co-change are deterministic, and
// paying an agent to compute them in every repo is the largest avoidable cost of
// a pass. The output also carries what triage needs to cut the repo into units —
// the plan backlog by title, the repo's `## Approval` and `## Enhance` sections,
// its hard stops, and size signals for deciding whether to fan out.
//
// Read-only: it never fetches, writes, or asks `gh` to write. Run `git fetch`
// first if `origin/<base>` should be current.

'use strict';

const { execFileSync } = require('node:child_process');

function repoRoot() {
  try {
    return execFileSync('git', ['rev-parse', '--show-toplevel'], { encoding: 'utf8' }).trim();
  } catch {
    return process.cwd();
  }
}

function git(root, args) {
  return execFileSync('git', args, { cwd: root, encoding: 'utf8', maxBuffer: 256 * 1024 * 1024 });
}

function tryGit(root, args) {
  try {
    // Probes that may fail (a missing file, an unset origin/HEAD): keep stderr quiet.
    return execFileSync('git', args, { cwd: root, encoding: 'utf8', maxBuffer: 256 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- classification

const FIX_RE = /\b(fix(e[sd])?|revert(s|ed)?|hotfix|crash(es|ed)?|broke|broken|regress(ion|ed)?)\b/i;
const CONVENTIONAL_RE = /^[a-z]+(\([^)]*\))?!?: /;

const NOISE_RES = [
  /(^|\/)(package-lock\.json|pnpm-lock\.yaml|yarn\.lock|uv\.lock|poetry\.lock|Cargo\.lock|Gemfile\.lock|go\.sum|composer\.lock)$/,
  /(^|\/)_plans-map\.md$/,
  /(^|\/)(dist|build|out|coverage|node_modules|\.next|\.astro)\//,
  /\.(min\.js|map|snap)$/,
  /\.(png|jpe?g|gif|webp|svg|ico|pdf|mp4|mov|mp3|wav|woff2?|ttf|zip|gz)$/i,
  // Office documents and data logs: their churn is drafting or capture, not code.
  /\.(docx?|xlsx?|pptx?|odt|ods|epub|jsonl)$/i,
  // Shared resources every feature touches: they co-change with everything,
  // so they are no evidence of a leaking seam.
  /(^|\/)(locales?|i18n|lang|strings)\/[^/]+\.(json|ya?ml|po)$/,
  /(^|\/)strings\.[a-z-]+\.json$/,
];
const PROSE_RE = /\.(md|mdx|txt|rst)$/i;
const TEST_RES = [
  /(^|\/)(tests?|__tests__|spec|e2e)\//,
  /\.(test|spec|vitest)\.[a-z]+$/,
  /(^|\/)test_[^/]+\.py$/,
  /_test\.(py|go)$/,
];

const isFix = (subject) => FIX_RE.test(subject);
const isNoise = (p) => NOISE_RES.some((re) => re.test(p));
const isProse = (p) => PROSE_RE.test(p);
const isTest = (p) => TEST_RES.some((re) => re.test(p));
/** A file whose churn says something about the code: not noise, not prose, not a test. */
const isSource = (p) => !isNoise(p) && !isProse(p) && !isTest(p);

/** The module a path belongs to: its first two directories (`services/github-review`). */
function moduleOf(p) {
  const dirs = p.split('/').slice(0, -1);
  return dirs.length ? dirs.slice(0, 2).join('/') : '.';
}

// ---------------------------------------------------------------- history

/**
 * Parse `git log --format=%x01%h%x1f%s --name-status -M`.
 * Returns commits newest first, with every path rewritten to its newest name, so a
 * renamed file's history counts as one file.
 */
function parseLog(log) {
  const commits = [];
  for (const chunk of log.split('\x01').slice(1)) {
    const [head, ...lines] = chunk.split('\n');
    const [sha, subject = ''] = head.split('\x1f');
    const files = [];
    const renames = [];
    for (const line of lines) {
      if (!line.trim()) continue;
      const parts = line.split('\t');
      const status = parts[0];
      if (status.startsWith('R') && parts.length >= 3) {
        renames.push([parts[1], parts[2]]);
        files.push(parts[2]);
      } else if (parts.length >= 2) {
        files.push(parts[parts.length - 1]);
      }
    }
    commits.push({ sha, subject, files, renames });
  }
  // Walk oldest → newest is not needed: newest first, a rename seen earlier in the
  // walk is a later rename, so resolve chains through the map as we go.
  const newest = new Map();
  const resolve = (p) => {
    const seen = new Set();
    while (newest.has(p) && !seen.has(p)) {
      seen.add(p);
      p = newest.get(p);
    }
    return p;
  };
  for (const c of commits) {
    c.files = [...new Set(c.files.map(resolve))];
    for (const [from, to] of c.renames) newest.set(from, resolve(to));
  }
  for (const c of commits) delete c.renames;
  return commits;
}

function countBy(commits, pick) {
  const counts = new Map();
  for (const c of commits) for (const f of c.files) if (pick(f)) counts.set(f, (counts.get(f) || 0) + 1);
  return counts;
}

/** Files that land in the same commits as `file`, with whether each sits in another module. */
function coChange(commits, file, keep, limit = 8) {
  const partners = new Map();
  for (const c of commits) {
    if (!c.files.includes(file)) continue;
    for (const f of c.files) if (f !== file && keep(f)) partners.set(f, (partners.get(f) || 0) + 1);
  }
  return [...partners]
    .filter(([, n]) => n >= 2)
    .sort((a, b) => b[1] - a[1])
    .slice(0, limit)
    .map(([path, n]) => ({ path, n, crossModule: moduleOf(path) !== moduleOf(file) }));
}

/**
 * Whether a test file is named after this file (`test_<stem>`, `<stem>.test.*`,
 * `<stem>_test.*`, `<stem>.spec.*`): a heuristic for "has a test of its own". Null
 * for stems too generic to tell.
 */
function hasNamedTest(file, testFiles) {
  const stem = file.split('/').pop().replace(/\.[^.]+$/, '').toLowerCase();
  if (stem.length < 3 || ['index', 'main', 'init', '__init__', 'mod', 'lib', 'app', 'config'].includes(stem)) return null;
  const names = new Set([`test_${stem}`, `${stem}_test`, `${stem}.test`, `${stem}.spec`, `${stem}.vitest`, `test-${stem}`]);
  return testFiles.some((t) => {
    const base = t.split('/').pop().toLowerCase();
    return [...names].some((n) => base === n || base.startsWith(`${n}.`));
  });
}

// ---------------------------------------------------------------- repo docs

/** The body of a `## <name>` section, up to the next `## ` heading. */
function section(markdown, name) {
  const re = new RegExp(`^## ${name}\\b[^\\n]*\\n([\\s\\S]*?)(?=^## |(?![\\s\\S]))`, 'm');
  const m = markdown.match(re);
  return m ? m[1].trim() : null;
}

/** Title and v2 block lines of a plan file. */
function planHead(text) {
  const title = (text.match(/^# (.+)$/m) || [])[1] || null;
  const field = (k) => ((text.match(new RegExp(`^\\*\\*${k}:\\*\\*\\s*(.+)$`, 'm')) || [])[1] || null);
  return { title, priority: field('Priority'), gate: field('Gate') };
}

// ---------------------------------------------------------------- size and fan-out

// A repo above any of these is large enough that one agent per unit beats one agent
// per repo: exploring a candidate there costs more than onboarding a fresh agent.
const LARGE = { commits: 120, sourceFiles: 250, hottestLines: 900 };

function sizeSignals({ commits, sourceFiles, hottestLines }) {
  const reasons = [];
  if (commits >= LARGE.commits) reasons.push(`${commits} commits in the window`);
  if (sourceFiles >= LARGE.sourceFiles) reasons.push(`${sourceFiles} source files`);
  if (hottestLines >= LARGE.hottestLines) reasons.push(`a ${hottestLines}-line hot file`);
  return { fanoutEligible: reasons.length > 0, reasons };
}

// ---------------------------------------------------------------- main

function parseArgs(argv) {
  const opts = { since: 90, ref: null, scope: null, top: 25, prs: true };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--since') opts.since = Number(argv[++i]);
    else if (a === '--ref') opts.ref = argv[++i];
    else if (a === '--scope') opts.scope = argv[++i];
    else if (a === '--top') opts.top = Number(argv[++i]);
    else if (a === '--no-prs') opts.prs = false;
    else throw new Error(`unknown argument: ${a}`);
  }
  return opts;
}

function readAt(root, ref, path) {
  return tryGit(root, ['show', `${ref}:${path}`]);
}

function baseRef(root, explicit) {
  if (explicit) return explicit;
  const land = readAt(root, 'HEAD', '.agents/land.config.json');
  if (land) {
    try {
      const base = JSON.parse(land).baseBranch;
      if (base && tryGit(root, ['rev-parse', '--verify', `origin/${base}`])) return `origin/${base}`;
    } catch {}
  }
  const head = tryGit(root, ['symbolic-ref', '--short', 'refs/remotes/origin/HEAD']);
  if (head) return head.trim();
  // Many clones never set origin/HEAD.
  for (const b of ['main', 'master', 'develop']) {
    if (tryGit(root, ['rev-parse', '--verify', '--quiet', `origin/${b}`])) return `origin/${b}`;
  }
  return 'HEAD';
}

function measure(root, opts) {
  const ref = baseRef(root, opts.ref);
  const sha = git(root, ['rev-parse', '--short', ref]).trim();
  const date = git(root, ['log', '-1', '--format=%cs', ref]).trim();
  const tracked = git(root, ['ls-tree', '-r', '--name-only', ref]).split('\n').filter(Boolean);
  const exists = new Set(tracked);
  const inScope = (p) => !opts.scope || p === opts.scope || p.startsWith(opts.scope.replace(/\/$/, '') + '/');

  const logArgs = ['log', ref, `--since=${opts.since}.days`, '--no-merges', '-M', '--name-status', '--format=%x01%h%x1f%s'];
  const commits = parseLog(git(root, logArgs)).map((c) => ({ ...c, files: c.files.filter((f) => exists.has(f)) }));
  const fixCommits = commits.filter((c) => isFix(c.subject));

  const keep = (f) => isSource(f) && inScope(f);
  const churn = countBy(commits, keep);
  const fixes = countBy(fixCommits, keep);
  const testFiles = tracked.filter(isTest);
  const sourceFiles = tracked.filter((f) => isSource(f) && inScope(f));

  const lines = (f) => {
    const text = readAt(root, ref, f);
    return text == null ? 0 : text.split('\n').length;
  };
  const hot = [...churn]
    .sort((a, b) => b[1] - a[1])
    .slice(0, opts.top * 2)
    .map(([path, n]) => ({ path, churn: n, fixes: fixes.get(path) || 0, lines: lines(path) }))
    .map((f) => ({ ...f, score: f.churn * Math.max(1, Math.log2(f.lines + 1)) * (1 + f.fixes / f.churn) }))
    .sort((a, b) => b.score - a.score)
    .slice(0, opts.top)
    .map((f) => ({
      ...f,
      score: Math.round(f.score),
      namedTest: hasNamedTest(f.path, testFiles),
      coChange: coChange(commits, f.path, keep),
      fixSubjects: fixCommits.filter((c) => c.files.includes(f.path)).slice(0, 5).map((c) => `${c.sha} ${c.subject}`),
    }));

  const modules = new Map();
  for (const [path, n] of churn) {
    const m = modules.get(moduleOf(path)) || { module: moduleOf(path), churn: 0, fixes: 0, files: 0 };
    m.churn += n;
    m.fixes += fixes.get(path) || 0;
    m.files += 1;
    modules.set(m.module, m);
  }

  const agents = readAt(root, ref, 'AGENTS.md') || '';
  let hardStop = [];
  const land = readAt(root, ref, '.agents/land.config.json');
  if (land) {
    try {
      hardStop = JSON.parse(land).hardStop || [];
    } catch {}
  }

  const docs = (dir) =>
    tracked
      .filter((p) => p.startsWith(dir) && p.endsWith('.md') && !/(README|_plans-map)\.md$/.test(p))
      .map((path) => ({ path, ...planHead(readAt(root, ref, path) || '') }));

  let prs = null;
  if (opts.prs) {
    const out = (() => {
      try {
        return execFileSync('gh', ['pr', 'list', '--state', 'open', '--limit', '100', '--json', 'number,title,headRefName,files'], {
          cwd: root,
          encoding: 'utf8',
          stdio: ['ignore', 'pipe', 'ignore'],
        });
      } catch {
        return null;
      }
    })();
    if (out) {
      prs = JSON.parse(out).map((p) => ({
        number: p.number,
        title: p.title,
        branch: p.headRefName,
        files: (p.files || []).map((f) => f.path).slice(0, 60),
      }));
    }
  }

  const conventional = commits.filter((c) => CONVENTIONAL_RE.test(c.subject)).length;
  const codeFiles = sourceFiles.length;
  const proseFiles = tracked.filter(isProse).length;

  return {
    repo: root.split('/').pop(),
    ref,
    sha,
    date,
    window: { days: opts.since, commits: commits.length, fixCommits: fixCommits.length },
    scope: opts.scope,
    commitStyle: {
      conventional: commits.length ? Math.round((100 * conventional) / commits.length) : null,
      sample: commits.slice(0, 5).map((c) => c.subject),
    },
    kindHint: /perimeter/i.test(agents) && proseFiles > codeFiles ? 'record' : codeFiles >= proseFiles ? 'code' : 'record',
    size: {
      trackedFiles: tracked.length,
      sourceFiles: codeFiles,
      proseFiles,
      ...sizeSignals({ commits: commits.length, sourceFiles: codeFiles, hottestLines: Math.max(0, ...hot.map((h) => h.lines)) }),
    },
    hot,
    modules: [...modules.values()].sort((a, b) => b.churn - a.churn).slice(0, 15),
    agents: {
      present: Boolean(agents),
      approval: section(agents, 'Approval'),
      enhance: section(agents, 'Enhance'),
      hardStop,
    },
    backlog: {
      ideas: docs('docs/plans/ideas/'),
      ready: docs('docs/plans/ready/'),
      ongoing: docs('docs/plans/ongoing/'),
      decisions: docs('docs/decisions/').map(({ path, title }) => ({ path, title })),
      incidents: docs('docs/incidents/').map(({ path, title }) => ({ path, title })),
    },
    openPrs: prs,
  };
}

if (require.main === module) {
  const opts = parseArgs(process.argv.slice(2));
  process.stdout.write(JSON.stringify(measure(repoRoot(), opts), null, 2) + '\n');
}

module.exports = { parseLog, isFix, isSource, isTest, isNoise, moduleOf, coChange, hasNamedTest, section, planHead, sizeSignals, measure };
