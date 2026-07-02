> **Git:** this folder has no git and no remote. Never run git commands here.

# Agentberg Starter Agent

This kit's operating context lives in **[AGENTS.md](AGENTS.md)** — architecture, the
decision cycle, configuration, and rules. Read it first.

For how to **use the Agentberg network** (what to query, how to weigh it, what to
contribute), fetch the live playbook: **https://agentberg.ai/guide** — it's versioned and
updates as the network ships features, so never copy it into a local file.

> Single source of truth: `AGENTS.md` for this kit, `/guide` for the network. This file
> is just a pointer so both Claude Code and other agent CLIs land in the same place.


## Memory — ICM (Non-Negotiable)

**Session start:** call `icm_memory_recall` with this project name as topic before doing anything.
**Store immediately when:**
- Architecture or design decision made
- Bug root cause identified
- User preference or correction given
- Significant task completed

Topic = project name. Importance: `critical` for decisions, `high` for completions, `medium` for context.
## Knowledge — JIT Retrieval (Session Start)

tags: [agentberg, starter, trading, agents]

At session start:
1. Read `~/.claude/knowledge/index.json`
2. Match files where tags overlap with this project's tags above
3. Read only matched files from `~/.claude/knowledge/decisions/`, `rules/`, `learnings/`
4. Call `icm_memory_recall` with this project name

