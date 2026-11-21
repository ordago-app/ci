#!/usr/bin/env node
// Should an orchestrator start one more worker right now?
//
//   node scripts/agent-capacity.js                    # verdict + one line per check
//   node scripts/agent-capacity.js --json             # the same, machine-readable
//   node scripts/agent-capacity.js --max-queue-min 30 # override one limit for this call
//
// Exit 0 = admit, 2 = do not admit, 1 = a signal could not be read. A signal that
// cannot be read is an error, never a pass: admitting blind is how a batch
// oversubscribes the one thing it cannot buy more of.
//
// Why a verdict and not a worker count: a fixed "5–6 workers" is right on a quiet
// day and wrong on a busy one. What actually binds is (1) self-hosted CI lanes, if
// the repo has them — a worker whose PR only queues adds rebases and review
// rounds, not throughput — and (2) this machine, which runs every worker's
// typecheck, unit suites and local emulators. Usage quota is the third ceiling
// and has no readable signal; react to a worker dying on it.
//
// Limits come from the consuming repo's .agents/orchestrate.config.json, found
// from the working directory (consumers reach this file through a symlink):
//   maxConcurrentEmulatorSuites   — measured ceiling of concurrent emulator suites
//   capacity.minAvailableMb       — RAM one more worker needs (default 3000)
//   capacity.ciQueue              — { runnerLabel, maxMinutes } to read a
//                                   self-hosted queue; omit it when CI runs on
//                                   hosted runners, which never queue on you.

'use strict';

const { execFile, execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { promisify } = require('node:util');

const execFileAsync = promisify(execFile);

const LIMITS = {
  // A self-hosted job queued longer than this is a sustained backlog, not a busy
  // moment. A new worker's PR reaches CI 30-90 min after dispatch, so a queue that
  // will have drained by then must not refuse: on 2026-10-02 a batch profitably
  // dispatched two workers while four PRs sat in CI-wait.
  ciQueueMaxMinutes: 30,
  // Headroom one more worker needs: its session plus a typecheck or a jest run.
  // Provisional — tune it from a measured batch rather than trusting it.
  minAvailableMb: 3000,
  // Measured: two concurrent local emulator suites fit in 16 GB WSL2, a third does not.
  maxLocalEmulatorSuites: 2,
};

function repoRoot() {
  try {
    return execFileSync('git', ['rev-parse', '--show-toplevel'], { encoding: 'utf8' }).trim();
  } catch {
    return process.cwd();
  }
}

/** Limits and CI settings from the repo's orchestrate config; absent keys keep the defaults. */
function readConfig(root = repoRoot()) {
  const file = path.join(root, '.agents', 'orchestrate.config.json');
  const cfg = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : {};
  const capacity = cfg.capacity || {};
  const limits = { ...LIMITS };
  if (Number.isFinite(cfg.maxConcurrentEmulatorSuites)) limits.maxLocalEmulatorSuites = cfg.maxConcurrentEmulatorSuites;
  if (Number.isFinite(capacity.minAvailableMb)) limits.minAvailableMb = capacity.minAvailableMb;
  const ciQueue = capacity.ciQueue || null;
  if (ciQueue && Number.isFinite(ciQueue.maxMinutes)) limits.ciQueueMaxMinutes = ciQueue.maxMinutes;
  return { limits, runnerLabel: ciQueue ? ciQueue.runnerLabel || 'self-hosted' : null };
}

const QUEUED_JOB_STATES = new Set(['queued', 'pending', 'requested']);

/**
 * Pure verdict over already-gathered signals.
 * `ci` is null when the repo declares no self-hosted queue.
 * @param {{ci: null | {queuedSelfHosted: number, oldestQueuedMinutes: number|null, oldestQueuedJob: string|null},
 *          memAvailableMb: number, localEmulatorSuites: number}} signals
 */
function decide(signals, limits = LIMITS) {
  const { ci } = signals;
  const checks = [
    ci === null
      ? { name: 'ci', ok: true, detail: 'no self-hosted queue configured (capacity.ciQueue) — not measured' }
      : {
          name: 'ci',
          ok: ci.oldestQueuedMinutes === null || ci.oldestQueuedMinutes <= limits.ciQueueMaxMinutes,
          detail:
            ci.oldestQueuedMinutes === null
              ? 'no self-hosted job of ours is queued'
              : `${ci.queuedSelfHosted} self-hosted job(s) queued, oldest ${ci.oldestQueuedMinutes} min ` +
                `(limit ${limits.ciQueueMaxMinutes}): ${ci.oldestQueuedJob}`,
        },
    {
      name: 'ram',
      ok: signals.memAvailableMb >= limits.minAvailableMb,
      detail: `${signals.memAvailableMb} MB available (need ${limits.minAvailableMb})`,
    },
    {
      name: 'emulators',
      ok: signals.localEmulatorSuites < limits.maxLocalEmulatorSuites,
      detail:
        `${signals.localEmulatorSuites} local Firestore emulator(s) running ` +
        `(max ${limits.maxLocalEmulatorSuites} concurrent suites)`,
    },
  ];
  return { admit: checks.every((c) => c.ok), checks };
}

async function ghJson(apiPath) {
  const { stdout } = await execFileAsync('gh', ['api', apiPath], { maxBuffer: 32 * 1024 * 1024 });
  return JSON.parse(stdout);
}

async function currentRepo() {
  const { stdout } = await execFileAsync('gh', ['repo', 'view', '--json', 'nameWithOwner', '-q', '.nameWithOwner']);
  return stdout.trim();
}

/** @param {(apiPath: string) => Promise<any>} fetchJson injected so the parsing is testable without GitHub */
async function readCiQueue(fetchJson = ghJson, now = Date.now(), { repo, runnerLabel = 'self-hosted' } = {}) {
  const REPO = repo || (await currentRepo());
  // A job sits queued inside a run that is itself `queued` or already
  // `in_progress` (its other jobs started), so both run states are read.
  const [queuedRuns, activeRuns] = await Promise.all(
    ['queued', 'in_progress'].map((status) =>
      fetchJson(`repos/${REPO}/actions/runs?status=${status}&per_page=100`)
    )
  );
  const runs = [...queuedRuns.workflow_runs, ...activeRuns.workflow_runs];
  const jobLists = await Promise.all(
    runs.map((run) => fetchJson(`repos/${REPO}/actions/runs/${run.id}/jobs?filter=latest&per_page=100`))
  );

  let oldest = null;
  let queuedSelfHosted = 0;
  for (const { jobs } of jobLists) {
    for (const job of jobs) {
      if (!QUEUED_JOB_STATES.has(job.status) || !job.labels.includes(runnerLabel)) continue;
      queuedSelfHosted += 1;
      const minutes = Math.floor((now - Date.parse(job.created_at)) / 60000);
      if (!oldest || minutes > oldest.minutes) oldest = { minutes, name: `${job.name} — ${job.html_url}` };
    }
  }
  return {
    queuedSelfHosted,
    oldestQueuedMinutes: oldest ? oldest.minutes : null,
    oldestQueuedJob: oldest ? oldest.name : null,
  };
}

function readMemAvailableMb(meminfo = fs.readFileSync('/proc/meminfo', 'utf8')) {
  const m = meminfo.match(/^MemAvailable:\s+(\d+)\s+kB$/m);
  if (!m) throw new Error('MemAvailable not found in /proc/meminfo');
  return Math.floor(Number(m[1]) / 1024);
}

/** One Firestore emulator JVM per running suite, whichever worktree started it. */
function countEmulatorSuites(psArgs) {
  return psArgs.split('\n').filter((line) => /java\b.*cloud-firestore-emulator/.test(line)).length;
}

async function readLocalEmulatorSuites() {
  // `ps -eo args`, not `pgrep -f`: pgrep matches the calling shell too.
  const { stdout } = await execFileAsync('ps', ['-eo', 'args=']);
  return countEmulatorSuites(stdout);
}

function parseArgs(argv, base = LIMITS) {
  const limits = { ...base };
  const flagFor = {
    '--max-queue-min': 'ciQueueMaxMinutes',
    '--min-available-mb': 'minAvailableMb',
    '--max-emulator-suites': 'maxLocalEmulatorSuites',
  };
  let json = false;
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--json') json = true;
    else if (flagFor[arg]) {
      const value = Number(argv[(i += 1)]);
      if (!Number.isFinite(value)) throw new Error(`${arg} needs a number`);
      limits[flagFor[arg]] = value;
    } else throw new Error(`unknown argument ${arg}`);
  }
  return { json, limits };
}

async function main() {
  const config = readConfig();
  const { json, limits } = parseArgs(process.argv.slice(2), config.limits);
  const [ci, localEmulatorSuites] = await Promise.all([
    config.runnerLabel ? readCiQueue(ghJson, Date.now(), { runnerLabel: config.runnerLabel }) : null,
    readLocalEmulatorSuites(),
  ]);
  const signals = { ci, memAvailableMb: readMemAvailableMb(), localEmulatorSuites };
  const verdict = decide(signals, limits);

  const report = json
    ? JSON.stringify({ ...verdict, signals, limits }, null, 2)
    : [`admit: ${verdict.admit ? 'yes' : 'no'}`, ...verdict.checks.map((c) => `  ${c.ok ? 'ok ' : 'NO '} ${c.name}: ${c.detail}`)].join('\n');
  process.stdout.write(`${report}\n`);
  process.exitCode = verdict.admit ? 0 : 2;
}

if (require.main === module) {
  main().catch((err) => {
    process.stderr.write(`agent-capacity: ${err.message}\n`);
    process.exitCode = 1;
  });
}

module.exports = { decide, LIMITS, readConfig, readCiQueue, readMemAvailableMb, countEmulatorSuites, parseArgs };
