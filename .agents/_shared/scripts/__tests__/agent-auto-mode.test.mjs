// Run with: node --test scripts/__tests__/agent-auto-mode.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { mergeAutoMode: merge, tagFor } = require('../agent-auto-mode.js');

const TAG = '[myrepo] ';
const mergeAutoMode = (settings, template) => merge(settings, template, TAG);
const template = { allow: ['a', 'b'], soft_deny: [], environment: ['env'] };

test('installs the template tagged, after $defaults, keeping personal entries', () => {
  const { settings } = mergeAutoMode(
    { theme: 'dark', autoMode: { allow: ['$defaults', 'mine'], soft_deny: ['$defaults', 'no rm'] } },
    template,
  );
  assert.equal(settings.theme, 'dark');
  assert.deepEqual(settings.autoMode.allow, ['$defaults', 'mine', `${TAG}a`, `${TAG}b`]);
  assert.deepEqual(settings.autoMode.soft_deny, ['$defaults', 'no rm']);
  assert.deepEqual(settings.autoMode.environment, ['$defaults', `${TAG}env`]);
});

test('a re-run replaces only tagged entries, so a template edit propagates', () => {
  const installed = mergeAutoMode({}, template).settings;
  const { settings, changes } = mergeAutoMode(installed, { ...template, allow: ['a', 'c'] });
  assert.deepEqual(settings.autoMode.allow, ['$defaults', `${TAG}a`, `${TAG}c`]);
  assert.deepEqual(changes.allow.added, [`${TAG}c`]);
  assert.deepEqual(changes.allow.dropped, [`${TAG}b`]);
});

test('installing twice changes nothing', () => {
  const once = mergeAutoMode({}, template).settings;
  const { settings, changes } = mergeAutoMode(once, template);
  assert.deepEqual(settings, once);
  for (const list of Object.values(changes)) assert.deepEqual(list, { added: [], dropped: [] });
});

test('the tag is the template\'s own, else the repo directory name', () => {
  assert.equal(tagFor({ tag: 'cultuvilla-app' }, '/x/whatever'), '[cultuvilla-app] ');
  assert.equal(tagFor({}, '/home/me/githubs/ordago-apps'), '[ordago-apps] ');
});

test('two repos\' policies coexist: installing one never drops the other\'s entries', () => {
  const a = merge({}, { allow: ['from a'] }, '[a] ').settings;
  const both = merge(a, { allow: ['from b'] }, '[b] ').settings;
  assert.deepEqual(both.autoMode.allow, ['$defaults', '[a] from a', '[b] from b']);
});
