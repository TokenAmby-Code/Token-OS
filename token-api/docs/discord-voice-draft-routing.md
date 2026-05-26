# Discord Voice Draft Routing

Discord voice is a visible manual-submit draft buffer for every voice bot.

- The daemon forwards every completed transcript to `/api/discord/message`; it does not suppress, debounce, pool, or auto-submit short fragments.
- Token API keeps an in-memory draft lock keyed by `(bot_name, author_id)`.
- First non-command utterance starts a draft, resolves the target pane, marks the pane title with a lock prefix, and types the text without Enter.
- Further non-command utterances append to that same locked pane, even if focus/cursor changes elsewhere.
- `ship` or `ship it` sends Enter to the locked pane, restores the title, and clears the lock.
- `scratch` or `scratch that` sends Ctrl-C to the locked pane, restores the title, and clears the lock.
- Draft birth respects the typing guard: if the target pane already has unrelated pending input, no draft starts. Draft continuation bypasses the guard only for the locked voice-owned pane.
- If the locked pane dies, Token API clears the draft, fails closed, and sends Discord voice feedback. The next utterance may start a new draft.
- State is process-local. After a Token API restart, text already visible in tmux remains, but the voice draft lock is gone.

Targeting:

- `imperial_guard`: locks the daemon-supplied `target_tmux_pane` from the first utterance.
- `mechanicus`: resolves the existing synced live Mechanicus instance, then locks its pane.
- `custodes`: resolves the existing live Custodes pane, including the tmux marker fallback, then locks it.
