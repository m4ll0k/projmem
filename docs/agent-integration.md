# Using projmem with any LLM agent

projmem deliberately does NOT use MCP, plugins, or any client-specific
protocol. The integration model is the simplest one possible:

> the LLM runs `projmem` as a shell command and reads the JSON output.

Anything that can call a shell can use it: Claude Code, Cursor, Continue,
Aider, llama.cpp running locally, vLLM, Ollama, a custom Python loop
around the OpenAI API. No SDKs, no servers, no IDE extensions.

## The end-user setup

Two commands. That's the whole thing.

```bash
pip install projmem
projmem init                # builds the index AND drops AGENTS.md
# OR `projmem init --template all` to drop CLAUDE.md / AGENTS.md / .cursorrules
```

`projmem init` is idempotent — re-runs skip the index if one exists and
skip template files that are already on disk. Pass `--reindex` to
rebuild the structural index, `--force` to overwrite the templates,
`--no-index` to only refresh the templates.

After that, any LLM tool that reads its repo's instruction file (most do
by convention) will learn the projmem workflow on its next session.

## How agents discover projmem

LLM agents already read repo instruction files at session start:

| Tool | File it reads |
|---|---|
| Claude Code | `CLAUDE.md` |
| Cursor | `.cursorrules` |
| Continue, Aider, generic | `AGENTS.md` |

`projmem init --template all` drops all three. Each contains the same
core instructions, in the dialect that tool expects.

## What the templates instruct the agent to do

1. **First call on every task**: `projmem session <file_or_symbol>`
   Returns one bounded JSON blob with everything the agent needs to know:
   actionable doctor warnings, prior notes with claim verdicts, integrity
   score, recent activity on the target, dependency neighbors, and
   `next_steps_hint`.

2. **Honor the warning fields as blockers**:
   - `freshness_warning` → reindex before reading more
   - `ambiguity_warning` → use `primary_guess` or pin with `file#name`
   - `claim_overall_status: contradicted` → re-investigate

3. **Save concluded facts as structured claims**:
   ```bash
   projmem note add <target> --kind note "body" \
     --claims claims.json --truth-class FACT
   ```

4. **Before claiming done**: `projmem checklist`

The full agent-facing workflow is reproduced as `projmem usage` — any
agent can run it once to learn the surface without touching docs.

## Recipe: Claude Code

After `projmem index` and `projmem init --template claude`:

The next Claude Code session reads `CLAUDE.md`, sees the projmem
instructions, and starts using `projmem session` automatically. Verify
by asking Claude to "show me what we know about file X" — it should
call `projmem session X` first, then proceed.

## Recipe: Cursor

After `projmem init --template cursor`, Cursor reads `.cursorrules` on
each session. Same workflow as Claude Code.

## Recipe: Continue / Aider / generic agent

After `projmem init` (default `--template agents`), any tool that reads
`AGENTS.md` (which is now a community convention used by Continue,
Aider, and others) gets the projmem workflow.

## Recipe: Ollama / llama.cpp / vLLM (no instruction-file convention)

For local-model harnesses that don't auto-read `AGENTS.md`, include
the contents of `projmem usage` in your system prompt:

```python
import subprocess
usage = subprocess.run(["projmem", "usage"],
                       capture_output=True, text=True).stdout
system_prompt = f"""You are a code-aware assistant.

The user's repo has projmem installed. Use it as your persistent
memory layer. Here is the command catalog and recommended workflow:

{usage}
"""
```

Same workflow, just delivered through your prompt instead of the
agent's auto-read instruction file.

## Recipe: shell-script agent loop (smallest possible)

```bash
#!/bin/bash
# A trivial agent loop: read session bootstrap, ask the model, save
# concluded claims.
TARGET="$1"
projmem session "$TARGET" > /tmp/ctx.json
# ... feed /tmp/ctx.json to your model ...
# After the model concludes:
echo '[{"subject":"X","predicate":"defined-at","object":"Y:42",
        "truth_class":"FACT"}]' > /tmp/claims.json
projmem note add "$TARGET" --kind note "session conclusion" \
  --claims /tmp/claims.json
```

## Why no MCP

MCP is Anthropic-specific. Projmem is meant to work with any LLM,
including local models that don't speak MCP. The shell + JSON
interface achieves the same outcome with zero protocol adoption cost.
If a future agent SDK wants MCP, an MCP wrapper around `projmem`
commands is ~100 lines — but it's not in core because it's not needed.

## What the agent gets in return

Across sessions, your LLM:

- Sees prior conclusions verified or refuted against current code
- Knows when files drifted on disk after the index was built
- Sees source consumers separately from artifact / changelog noise
- Gets ambiguity surfaced with a `primary_guess` instead of hard errors
- Knows which beliefs are still load-bearing and which were superseded

That's the persistent memory layer. The LLM doesn't have to remember;
projmem remembers, and tells the LLM what's still true.
