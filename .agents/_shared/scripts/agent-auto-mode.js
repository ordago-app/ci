#!/usr/bin/env node
// Install the repo's auto-mode policy into YOUR Claude Code user settings.
//
//   node scripts/agent-auto-mode.js            # show what would change (writes nothing)
//   node scripts/agent-auto-mode.js --write    # back up settings.json, then apply
//   node scripts/agent-auto-mode.js --settings <path>   # another settings file
//
// Run it from inside the consuming repo: the template is that repo's
// .agents/auto-mode.json, found from the working directory, never from this
// file's location (consumers reach it through a symlink into the submodule).
//
// Why a script and not a checked-in .claude/settings.json: Claude Code's auto-mode
// classifier reads `autoMode` only from user, --settings and managed scopes, never
// from a repo's own settings files — a cloned repo must not be able to widen its
// own permissions. So the policy lives in .agents/auto-mode.json and each founder
// installs it into ~/.claude/settings.json.
//
// Entries are installed tagged `[<tag>] ` — the template's `tag`, else the
// repo's directory name — so several repos' policies sit side by side in one
// settings file. A re-run replaces exactly that repo's tagged entries, so an edit
// to the template propagates and a personal rule or another repo's is never
// touched. `$defaults` is kept first in every list.

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { execFileSync } = require('node:child_process');

const LISTS = ['allow', 'soft_deny', 'environment'];

function repoRoot() {
  try {
    return execFileSync('git', ['rev-parse', '--show-toplevel'], { encoding: 'utf8' }).trim();
  } catch {
    return process.cwd();
  }
}

/** `[<tag>] ` for a template: its own `tag`, else the repo's directory name. */
function tagFor(template, root) {
  const name = template.tag || path.basename(root);
  return `[${name}] `;
}

/**
 * The user's settings with every tagged entry replaced by the template's.
 * Pure: returns a new object, and reports per list what was added or dropped.
 */
function mergeAutoMode(settings, template, TAG = tagFor(template, process.cwd())) {
  const next = { ...settings, autoMode: { ...(settings.autoMode || {}) } };
  const changes = {};
  for (const list of LISTS) {
    const current = Array.isArray(next.autoMode[list]) ? next.autoMode[list] : [];
    const personal = current.filter((e) => e !== '$defaults' && !e.startsWith(TAG));
    const wanted = (template[list] || []).map((e) => TAG + e);
    const merged = ['$defaults', ...personal, ...wanted];
    changes[list] = {
      added: wanted.filter((e) => !current.includes(e)),
      dropped: current.filter((e) => e.startsWith(TAG) && !wanted.includes(e)),
    };
    next.autoMode[list] = merged;
  }
  return { settings: next, changes };
}

function arg(flag) {
  const i = process.argv.indexOf(flag);
  return i === -1 ? undefined : process.argv[i + 1];
}

function main() {
  const configDir = process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), '.claude');
  const file = arg('--settings') || path.join(configDir, 'settings.json');
  const write = process.argv.includes('--write');

  const root = repoRoot();
  const templateFile = path.join(root, '.agents', 'auto-mode.json');
  if (!fs.existsSync(templateFile)) throw new Error(`${templateFile} not found — run this from inside the repo that declares the policy`);
  const template = JSON.parse(fs.readFileSync(templateFile, 'utf8'));
  const TAG = tagFor(template, root);
  const settings = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : {};
  const { settings: next, changes } = mergeAutoMode(settings, template, TAG);

  let total = 0;
  for (const list of LISTS) {
    for (const e of changes[list].added) { console.log(`+ ${list}: ${e.slice(TAG.length, TAG.length + 110)}…`); total++; }
    for (const e of changes[list].dropped) { console.log(`- ${list}: ${e.slice(TAG.length, TAG.length + 110)}…`); total++; }
  }
  if (total === 0) { console.log(`${file}: already in step with ${templateFile}`); return; }
  if (!write) { console.log(`\n${total} change(s) to ${file}. Re-run with --write to apply (a backup is made first).`); return; }

  if (fs.existsSync(file)) {
    const backup = `${file}.bak-${new Date().toISOString().replace(/[:.]/g, '-')}`;
    fs.copyFileSync(file, backup);
    console.log(`backup: ${backup}`);
  } else {
    fs.mkdirSync(path.dirname(file), { recursive: true });
  }
  fs.writeFileSync(file, `${JSON.stringify(next, null, 2)}\n`);
  console.log(`wrote ${total} change(s) to ${file}. Untagged personal entries were left as they were — review them with /permissions → Auto mode.`);
}

if (require.main === module) {
  try {
    main();
  } catch (err) {
    process.stderr.write(`agent-auto-mode: ${err.message}\n`);
    process.exitCode = 1;
  }
}

module.exports = { mergeAutoMode, tagFor };
