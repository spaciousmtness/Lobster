# Running on Melissa's Mac (Clawdaddy)

These rules add to CLAUDE.md and win where they differ.

- You run on Melissa's own Mac, not a server. Your folder is `~/lobster-workspace`. Everything you create or change goes there.
- A guard checks every tool call. Inside `~/lobster-workspace` it lets Read, Write, Edit, Glob and Grep through, plus your Lobster inbox tools. You may also read (not change) `~/lobster`, except its `config/` and `.git/` folders. Everywhere else, the guard refuses.
- Prefer, in this order: Lobster's MCP tools; Read, Write, Edit, Glob and Grep inside `~/lobster-workspace`. Use Bash only when nothing else can do the job.
- **Every Bash call needs Melissa's approval first**, even inside your folder. Call `ask_melissa` before the Bash call, not after a refusal.
- Bash commands must be literal: no quotes, no globs (`*`, `?`, `[`, `{`), no `$`, no backticks, no backslashes, no `cd`, no `..`, no `ln`. Use absolute paths. One plain command per call.
- If the guard says `never allowed: command must be literal`, the command itself broke the rules above. Rewrite it literally (absolute paths, no quotes, globs or `$`), then get approval for the rewritten command through `ask_melissa` and run exactly that.
- Outside your folder you can only ever write inside `~/Documents`, `~/Desktop` or `~/Downloads`, and only with Melissa's approval.
- Any other `never allowed` means the place is off limits with or without approval (for example keys, keychains, Lobster's code, config and settings, the message inbox, outbox and sent folders, and `~/lobster-workspace/.claude`). Tell Melissa you cannot do it and stop. `ask_melissa` answers `never allowed` itself for these, without texting her.

## Starting up

Claude Code loads no CLAUDE.md or SessionStart hooks here, so load Lobster's instructions yourself:

1. Read `~/lobster/CLAUDE.md`, then `~/lobster/.claude/sys.dispatcher.bootup.md`, and follow its Startup Behavior with these changes:
   - Skip step 0b (ToolSearch pre-load). Tools are not deferred here, and ToolSearch is refused.
   - `ADMIN_CHAT_ID` is written in `~/lobster-workspace/CLAUDE.md`. There is no SessionStart hook, so there is no `hook_input` session id: in step 1, call `session_start` without `claude_session_id`. There is no startup-cause banner either: in step 2d, treat it as `startup-cause: restart`.
   - Skip step 2c (the `context-handoff.json` check and clear); it needs a non-literal Bash command.
   - Skip step 2e (the `~/messages/bootup-prompt.md` reflection sidecar); deleting it needs Bash outside your folder.
2. Lobster's user config lives in `~/lobster-workspace/user-config`. Wherever Lobster's instructions say `~/lobster-user-config`, use `~/lobster-workspace/user-config` instead.

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

Messages whose text starts with `[Button pressed: approve:` or `[Button pressed: deny:` are Melissa's answers to `ask_melissa`. They belong to `ask_melissa`, which collects them itself (and clears out stale ones). Never reply to them and never call `mark_processed` on them. Leave them alone and move on to the next message. Never send buttons whose data starts with `approve:` or `deny:` yourself.
