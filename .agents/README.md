# Project agent setup

Claude Code and Codex share `../AGENTS.md` and `skills/`.
`.claude/skills` is a relative symlink to `.agents/skills`; Git stores the link,
not a second copy of the skills. Preserve symlinks when cloning on Windows.

Claude Code must be 2.1.281 or newer. Its default AGENTS.md fallback requires
that no parent/project `CLAUDE.md` or `CLAUDE.local.md` shadows it. If you keep
local Claude instructions, select `claude-md-and-agents-md` in `/config`.
Tool-specific permissions, hooks and MCP configuration remain tool-specific.

## Upstream skills

The skill `managing-plans-lifecycle` is an unmodified copy from
[agent-plans](https://github.com/alvaro-francisco-gil/agent-plans/tree/ae4235530d9d199372a373d01a977aba63ce1d59),
version 3.0.0, commit `ae4235530d9d199372a373d01a977aba63ce1d59`.
The MIT licence is in `skills/LICENSE.agent-plans`.

Refresh it with the workspace's `vendor-agent-plans.py`, never by hand, and do not fork its
instructions locally: repository-specific policy belongs in `AGENTS.md`. This copy replaces
a plans-lifecycle plugin install; if you also installed that plugin globally, disable it for
this project to avoid exposing two copies.

References: [Claude instructions](https://code.claude.com/docs/en/memory#read-agentsmd),
[Claude skills](https://code.claude.com/docs/en/skills),
[Codex skills](https://learn.chatgpt.com/docs/build-skills).
