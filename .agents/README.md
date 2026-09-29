# Project agent setup

Claude Code and Codex share `../AGENTS.md` and `skills/`.
`.claude/skills` is a relative symlink to `.agents/skills`; Git stores the link,
not a second copy of the skills. Preserve symlinks when cloning on Windows.

Claude Code must be 2.1.281 or newer. Its default AGENTS.md fallback requires
that no parent/project `CLAUDE.md` or `CLAUDE.local.md` shadows it. If you keep
local Claude instructions, select `claude-md-and-agents-md` in `/config`.
Tool-specific permissions, hooks and MCP configuration remain tool-specific.

## Upstream skills

The two skills `managing-plans-lifecycle` and `superpowers-plans-bridge` are
unmodified copies from [agent-plans](https://github.com/alvaro-francisco-gil/agent-plans/tree/0976c3043d5496f9e3ca869fbe6e5056b38af9bb),
version 1.0.2, commit `0976c3043d5496f9e3ca869fbe6e5056b38af9bb`.
The MIT licence is in `skills/LICENSE.agent-plans`.

Update both files together from a reviewed upstream commit and update this pin.
Do not fork their instructions locally: repository-specific policy belongs in
`AGENTS.md`. These files replace the project-level plans-lifecycle plugin install;
if you separately installed that plugin globally, disable it for this project to
avoid exposing two copies.

Existing shared skills may link into `.agents/_shared`. Run
`git submodule update --init --recursive` after cloning and in new worktrees.

References: [Claude instructions](https://code.claude.com/docs/en/memory#read-agentsmd),
[Claude skills](https://code.claude.com/docs/en/skills),
[Codex skills](https://learn.chatgpt.com/docs/build-skills).
