// Tests for scripts/ideas-review-order.js.
//
// The failure this exists to prevent: a rolling ideas review that re-reads the
// backlog from the top every run, because the reviews it already did left no
// trace — a "still valid" verdict changes no file, and a multi-idea rewrite is a
// sweep the history walk skips. The `Reviewed-Idea:` trailer is that trace.
//
// Run with: node --test scripts/__tests__/ideas-review-order.test.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const { orderForReview, parseReviewTrailers } = require('../ideas-review-order.js');
const SCRIPTS = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

test('a review trailer newer than the last edit is what counts', () => {
  const ordered = orderForReview([
    { slug: 'reviewed-lately', priority: 'high', touched: '2026-06-01', reviewed: '2026-09-30' },
    { slug: 'never-reviewed', priority: 'low', touched: '2026-08-01', reviewed: null },
    { slug: 'edited-after-review', priority: 'low', touched: '2026-09-15', reviewed: '2026-07-01' },
  ]);
  assert.deepEqual(
    ordered.map((i) => [i.slug, i.lastReviewed]),
    [
      ['never-reviewed', '2026-08-01'],
      ['edited-after-review', '2026-09-15'],
      ['reviewed-lately', '2026-09-30'],
    ]
  );
});

test('an idea being edited right now sorts last, never first', () => {
  const ordered = orderForReview([
    { slug: 'in-the-working-tree', priority: 'high', touched: null, reviewed: '2026-01-01' },
    { slug: 'old', priority: 'low', touched: '2026-02-01', reviewed: null },
  ]);
  assert.deepEqual(ordered.map((i) => i.slug), ['old', 'in-the-working-tree']);
});

test('parses repeated trailers, keeping each slug at its newest date', () => {
  const log = '\x012026-09-30\x1fa\x1eb\n\x012026-08-22\x1fb\x1ec\n';
  assert.deepEqual([...parseReviewTrailers(log)], [
    ['a', '2026-09-30'],
    ['b', '2026-09-30'],
    ['c', '2026-08-22'],
  ]);
});

function git(root, ...args) {
  const r = spawnSync('git', args, {
    cwd: root,
    encoding: 'utf8',
    env: { ...process.env, GIT_AUTHOR_DATE: process.env.FAKE_DATE, GIT_COMMITTER_DATE: process.env.FAKE_DATE },
  });
  assert.equal(r.status, 0, r.stderr);
  return r.stdout;
}

test('end to end: a VALID verdict recorded only in a trailer moves an idea back in the queue', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'ideas-order-'));
  try {
    fs.mkdirSync(path.join(root, 'scripts'));
    fs.mkdirSync(path.join(root, 'docs', 'plans', 'ideas'), { recursive: true });
    for (const f of ['plans-map.js', 'ideas-review-order.js']) {
      fs.copyFileSync(path.join(SCRIPTS, f), path.join(root, 'scripts', f));
    }
    git(root, 'init', '-q');
    git(root, 'config', 'user.email', 't@example.com');
    git(root, 'config', 'user.name', 't');
    for (const slug of ['alpha', 'beta']) {
      fs.writeFileSync(path.join(root, 'docs', 'plans', 'ideas', `${slug}.md`), `# ${slug}\n\n**Priority:** medium\n`);
    }
    process.env.FAKE_DATE = '2026-06-01T12:00:00Z';
    git(root, 'add', 'docs');
    git(root, 'commit', '-q', '-m', 'docs(plans): two ideas');
    process.env.FAKE_DATE = '2026-09-30T12:00:00Z';
    // A review that changed nothing: an empty commit whose only content is the trailer.
    git(
      root,
      'commit',
      '-q',
      '--allow-empty',
      '-m',
      'docs(plans): review ideas — no changes\n\nReviewed-Idea: alpha\nCo-Authored-By: x <x@example.com>'
    );

    const r = spawnSync(process.execPath, [path.join(root, 'scripts', 'ideas-review-order.js'), '--json'], {
      cwd: root,
      encoding: 'utf8',
    });
    assert.equal(r.status, 0, r.stderr);
    const ordered = JSON.parse(r.stdout);
    assert.deepEqual(
      ordered.map((i) => [i.slug, i.lastReviewed]),
      [
        ['beta', '2026-06-01'],
        ['alpha', '2026-09-30'],
      ]
    );
  } finally {
    delete process.env.FAKE_DATE;
    fs.rmSync(root, { recursive: true, force: true });
  }
});
