# Running on Melissa's Mac (Clawdaddy)

These rules add to CLAUDE.md and win where they differ.

- You run on Melissa's own Mac, not a server. Your folder is `~/lobster-workspace`. Everything you create or change goes there.
- A guard checks every tool call. Inside `~/lobster-workspace` it lets Read, Write, Edit, Glob and Grep through, plus your Lobster inbox tools. Outside it, the guard refuses.
- Prefer, in this order: Lobster's MCP tools; Read, Write, Edit, Glob and Grep inside `~/lobster-workspace`. Use Bash only when nothing else can do the job.
- **Every Bash call needs Melissa's approval first**, even inside your folder. Call `ask_melissa` before the Bash call, not after a refusal.
- Bash commands must be literal: no quotes, no globs (`*`, `?`, `[`, `{`), no `$`, no backticks, no backslashes, no `cd`, no `..`, no `ln`. Use absolute paths. One plain command per call.
- Some places are never allowed, with or without approval (for example keys, keychains, Lobster's config and the message inbox). If the guard says "never allowed", tell Melissa you cannot do it and stop.

## When the guard refuses

1. Call `ask_melissa` with:
   - `action`: one plain-language line saying what you want to do.
   - `why`: one plain-language line saying why.
   - `tool_name`: the exact tool you will retry, e.g. `Bash` or `Read`.
   - `tool_input`: the exact input you will retry with. It must match exactly, or the approval will not apply.
2. If she approves, retry the exact same call once. The approval is used up by that one call.
3. If she says no or does not answer, do not do it. Tell her what you could not do.
4. Never try another route around a refusal: no different tool, no different path spelling, no copying the file somewhere else, no asking a subagent to do it.

## Approval button messages

Messages whose text starts with `[Button pressed: approve:` or `[Button pressed: deny:` are Melissa's answers to `ask_melissa`. They belong to `ask_melissa`, which collects them itself. Never reply to them and never call `mark_processed` on them. Leave them alone and move on to the next message.
