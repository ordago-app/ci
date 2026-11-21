// Unit tests for scripts/agent-capacity.js.
//
// The failure this exists to prevent: an orchestrator admitting one more worker
// into lanes or RAM that cannot take it, because one signal was misread as
// "fine". Every check must be able to say no on its own.
//
// Run with: node --test scripts/__tests__/agent-capacity.test.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { decide, LIMITS, readConfig, readCiQueue, readMemAvailableMb, countEmulatorSuites, parseArgs } = require('../agent-capacity.js');

const idle = {
  ci: { queuedSelfHosted: 0, oldestQueuedMinutes: null, oldestQueuedJob: null },
  memAvailableMb: 8000,
  localEmulatorSuites: 0,
};

test('admits when every signal has headroom', () => {
  const v = decide(idle);
  assert.equal(v.admit, true);
  assert.deepEqual(
    v.checks.map((c) => c.name),
    ['ci', 'ram', 'emulators']
  );
});

test('each check alone can refuse', () => {
  const backlog = { ...idle, ci: { queuedSelfHosted: 3, oldestQueuedMinutes: LIMITS.ciQueueMaxMinutes + 1, oldestQueuedJob: 'Lint + Unit' } };
  const lowRam = { ...idle, memAvailableMb: LIMITS.minAvailableMb - 1 };
  const emulators = { ...idle, localEmulatorSuites: LIMITS.maxLocalEmulatorSuites };
  for (const [label, signals, failing] of [
    ['ci backlog', backlog, 'ci'],
    ['low ram', lowRam, 'ram'],
    ['emulators full', emulators, 'emulators'],
  ]) {
    const v = decide(signals);
    assert.equal(v.admit, false, label);
    assert.deepEqual(
      v.checks.filter((c) => !c.ok).map((c) => c.name),
      [failing],
      label
    );
  }
});

test('the CI refusal names the job that is waiting, so the leader can judge a stranded one', () => {
  const v = decide({ ...idle, ci: { queuedSelfHosted: 1, oldestQueuedMinutes: 90, oldestQueuedJob: 'Android E2E — https://x' } });
  assert.match(v.checks[0].detail, /Android E2E/);
});

test('a limit override applies to this call only', () => {
  const { limits } = parseArgs(['--max-queue-min', '120']);
  assert.equal(limits.ciQueueMaxMinutes, 120);
  assert.equal(LIMITS.ciQueueMaxMinutes, 30);
  assert.throws(() => parseArgs(['--max-queue-min', 'lots']), /needs a number/);
  assert.throws(() => parseArgs(['--bogus']), /unknown argument/);
});

test('reads MemAvailable, and refuses to guess when it is absent', () => {
  assert.equal(readMemAvailableMb('MemTotal:  12247040 kB\nMemAvailable:    7168000 kB\n'), 7000);
  assert.throws(() => readMemAvailableMb('MemTotal: 1 kB\n'), /MemAvailable not found/);
});

test('counts emulator JVMs, not processes that merely mention one', () => {
  const ps = [
    '/usr/bin/java -Duser.language=en -jar /home/u/.cache/firebase/emulators/cloud-firestore-emulator-v1.19.7.jar --host 127.0.0.1',
    '/usr/bin/java -jar /home/u/.cache/firebase/emulators/cloud-firestore-emulator-v1.19.7.jar --port 18080',
    'grep cloud-firestore-emulator',
    'node scripts/run-tests-with-emulators.js',
  ].join('\n');
  assert.equal(countEmulatorSuites(ps), 2);
});

test('reads the queue from both queued and in-progress runs, counting only waiting self-hosted jobs', async () => {
  const now = Date.parse('2026-10-03T10:00:00Z');
  const ago = (min) => new Date(now - min * 60000).toISOString();
  const job = (status, labels, minutesAgo, name) => ({ status, labels, created_at: ago(minutesAgo), name, html_url: `https://x/${name}` });
  const responses = {
    'runs?status=queued': { workflow_runs: [{ id: 1 }] },
    'runs?status=in_progress': { workflow_runs: [{ id: 2 }] },
    'runs/1/jobs': { jobs: [job('queued', ['self-hosted', 'repo-ci'], 12, 'lint')] },
    'runs/2/jobs': {
      jobs: [
        job('in_progress', ['self-hosted', 'repo-ci-medium'], 90, 'running-not-waiting'),
        job('completed', ['self-hosted', 'repo-ci'], 120, 'done'),
        job('queued', ['ubuntu-latest'], 200, 'hosted-not-ours-to-count'),
        job('pending', ['self-hosted', 'repo-ci-light'], 40, 'oldest-waiting'),
      ],
    },
  };
  const fetchJson = async (apiPath) => {
    const key = Object.keys(responses).find((k) => apiPath.includes(k));
    assert.ok(key, `unexpected API call ${apiPath}`);
    return responses[key];
  };
  const ci = await readCiQueue(fetchJson, now, { repo: 'o/r' });
  assert.equal(ci.queuedSelfHosted, 2);
  assert.equal(ci.oldestQueuedMinutes, 40);
  assert.match(ci.oldestQueuedJob, /oldest-waiting/);
});

test('an idle queue reads as no waiting job, not as zero minutes', async () => {
  const ci = await readCiQueue(async (p) => (p.includes('/jobs') ? { jobs: [] } : { workflow_runs: [] }), Date.now(), { repo: 'o/r' });
  assert.deepEqual(ci, { queuedSelfHosted: 0, oldestQueuedMinutes: null, oldestQueuedJob: null });
});

test('a repo with no self-hosted queue is not refused on CI, and says it did not measure', () => {
  const v = decide({ ...idle, ci: null });
  assert.equal(v.admit, true);
  assert.match(v.checks.find((c) => c.name === 'ci').detail, /not measured/);
});

test('limits come from the repo\'s orchestrate config', async () => {
  const fs = await import('node:fs');
  const os = await import('node:os');
  const path = await import('node:path');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'capacity-cfg-'));
  try {
    assert.deepEqual(readConfig(root), { limits: LIMITS, runnerLabel: null });
    fs.mkdirSync(path.join(root, '.agents'));
    fs.writeFileSync(
      path.join(root, '.agents', 'orchestrate.config.json'),
      JSON.stringify({ maxConcurrentEmulatorSuites: 1, capacity: { minAvailableMb: 5000, ciQueue: { runnerLabel: 'my-ci', maxMinutes: 10 } } })
    );
    assert.deepEqual(readConfig(root), {
      limits: { ciQueueMaxMinutes: 10, minAvailableMb: 5000, maxLocalEmulatorSuites: 1 },
      runnerLabel: 'my-ci',
    });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
