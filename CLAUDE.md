<!--
  This is a POINTER, not the contract.
  CLAUDE.md would be a symlink to AGENTS.md, but this OS blocks symlink creation
  (Administrator / Developer Mode required), so it is a one-line pointer instead.
  Do NOT add rules here. Edit AGENTS.md — it is the single source of truth.
-->
# Claude Code — read the canonical contract

The agent contract for this repo is **[AGENTS.md](AGENTS.md)**. Read it top to bottom before running or changing anything.

Tool-specific mechanism (Claude Code hooks, permissions, slash commands) will live under `.claude/` once the stack lands; it must never add a rule that isn't reflected in `AGENTS.md`.
