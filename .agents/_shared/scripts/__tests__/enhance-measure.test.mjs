// Tests for scripts/enhance-measure.js.
//
// The failures this exists to prevent, each seen on the first enhancement dry runs:
// a fix grep that only knew conventional commits and found 3 of homelab's fixes; a
// renamed component whose history split into two half-hot files plus a ghost; locale
// strings reported as the top co-change partner of every UI file.
//
// Run with: node --test scripts/__tests__/enhance-measure.test.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const require = createRequire(import.meta.url);
const m = require('../enhance-measure.js');

test('fix commits are found whatever the commit style', () => {
  for (const s of ['fix(api): x', 'github-review: fix the cap', 'Revert "x"', 'ci: stop the crash loop', 'hotfix', 'reta: fixed the date']) {
    assert.ok(m.isFix(s), s);
  }
  for (const s of ['feat: prefix parser', 'docs: suffix', 'mcp-gateway: tighten ACL']) assert.ok(!m.isFix(s), s);
});

test('lockfiles, locale strings, prose and tests are not source', () => {
  for (const p of ['web/package-lock.json', 'uv.lock', 'web/src/strings/es.json', 'locales/en.json', 'README.md', 'docs/x.md', 'a/__tests__/b.ts', 'tests/test_x.py', 'src/x.test.ts', 'docs/plans/_plans-map.md', 'drafts/MSA.docx', 'feedback/clip.jsonl']) {
    assert.ok(!m.isSource(p), p);
  }
  for (const p of ['ansible/playbooks/services.yml', 'services/x/Dockerfile', 'src/worker.py', 'web/src/components/A.astro']) {
    assert.ok(m.isSource(p), p);
  }
});

test('a renamed file keeps one history under its newest name, through a chain', () => {
  const log = [
    '\x01c3\x1fedit C', 'M\tsrc/C.ts',
    '\x01c2\x1frename B to C', 'R100\tsrc/B.ts\tsrc/C.ts',
    '\x01c1\x1frename A to B', 'R090\tsrc/A.ts\tsrc/B.ts',
    '\x01c0\x1fedit A', 'M\tsrc/A.ts', '',
  ].join('\n');
  const commits = m.parseLog(log);
  assert.deepEqual(commits.map((c) => c.files), [['src/C.ts'], ['src/C.ts'], ['src/C.ts'], ['src/C.ts']]);
});

test('a named test means a test named after the file, not any path containing its stem', () => {
  const tests = ['tests/test_ci_changed_services.py', 'src/__tests__/worker.test.ts', 'tests/test_job_store.py'];
  assert.equal(m.hasNamedTest('ansible/playbooks/services.yml', tests), false);
  assert.equal(m.hasNamedTest('src/worker.ts', tests), true);
  assert.equal(m.hasNamedTest('src/job_store.py', tests), true);
  assert.equal(m.hasNamedTest('src/index.ts', tests), null);
});

test('co-change marks partners in another module, and needs two shared commits', () => {
  const commits = [
    { files: ['services/a/w.py', 'services/a/s.py', 'ansible/p.yml'] },
    { files: ['services/a/w.py', 'services/a/s.py', 'ansible/p.yml'] },
    { files: ['services/a/w.py', 'services/b/once.py'] },
  ];
  assert.deepEqual(m.coChange(commits, 'services/a/w.py', () => true), [
    { path: 'services/a/s.py', n: 2, crossModule: false },
    { path: 'ansible/p.yml', n: 2, crossModule: true },
  ]);
});

test('reads ## sections and plan heads', () => {
  const md = '# R\n\n## Approval\n\nFree: refactors.\n\n## Enhance\n\nNever propose X.\n';
  assert.equal(m.section(md, 'Approval'), 'Free: refactors.');
  assert.equal(m.section(md, 'Enhance'), 'Never propose X.');
  assert.equal(m.section(md, 'Missing'), null);
  assert.deepEqual(m.planHead('# T\n\n**Priority:** high — why\n**Gate:** decision:q?\n'), {
    title: 'T', priority: 'high — why', gate: 'decision:q?',
  });
});

test('fan-out is offered only to a large repo, with the reason', () => {
  assert.deepEqual(m.sizeSignals({ commits: 30, sourceFiles: 40, hottestLines: 300 }), { fanoutEligible: false, reasons: [] });
  assert.deepEqual(m.sizeSignals({ commits: 321, sourceFiles: 40, hottestLines: 2120 }).reasons, ['321 commits in the window', 'a 2120-line hot file']);
});

test('measures a real repo end to end, read-only', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'enhance-measure-'));
  const g = (...a) => execFileSync('git', a, { cwd: dir, encoding: 'utf8' });
  const write = (p, s) => {
    fs.mkdirSync(path.dirname(path.join(dir, p)), { recursive: true });
    fs.writeFileSync(path.join(dir, p), s);
  };
  g('init', '-q', '-b', 'main');
  g('config', 'user.email', 't@t');
  g('config', 'user.name', 't');
  write('AGENTS.md', '# Repo\n\n## Approval\n\nRefactors that change nothing observable.\n\n## Enhance\n\nNever propose a rewrite.\n');
  write('docs/plans/ideas/old-idea.md', '# Old idea\n\n**Priority:** low\n');
  write('src/Old.ts', 'a\n');
  write('src/strings/en.json', '{}\n');
  g('add', '.');
  g('commit', '-qm', 'init');
  g('mv', 'src/Old.ts', 'src/New.ts');
  g('commit', '-qm', 'refactor: rename');
  for (let i = 0; i < 3; i++) {
    write('src/New.ts', `a\n${'x\n'.repeat(i + 1)}`);
    write('src/strings/en.json', `{"k":${i}}\n`);
    write('src/other.ts', `${i}\n`);
    g('add', '.');
    g('commit', '-qm', i === 0 ? 'ui: fix the thing' : `ui: change ${i}`);
  }
  const before = g('status', '--porcelain');

  const out = m.measure(dir, { since: 30, ref: 'HEAD', scope: null, top: 10, prs: false });

  assert.equal(g('status', '--porcelain'), before);
  assert.equal(out.window.commits, 5);
  assert.equal(out.window.fixCommits, 1);
  const hot = Object.fromEntries(out.hot.map((h) => [h.path, h]));
  assert.equal(hot['src/New.ts'].churn, 5, 'rename folded into one history: the add, the rename and three edits');
  assert.equal(hot['src/New.ts'].fixes, 1);
  assert.ok(!('src/Old.ts' in hot), 'no ghost of the old name');
  assert.ok(!('src/strings/en.json' in hot), 'locale strings are not source');
  assert.deepEqual(hot['src/New.ts'].coChange.map((c) => c.path), ['src/other.ts']);
  assert.equal(out.agents.approval, 'Refactors that change nothing observable.');
  assert.equal(out.agents.enhance, 'Never propose a rewrite.');
  assert.deepEqual(out.backlog.ideas, [{ path: 'docs/plans/ideas/old-idea.md', title: 'Old idea', priority: 'low', gate: null }]);
  assert.equal(out.size.fanoutEligible, false);
});
