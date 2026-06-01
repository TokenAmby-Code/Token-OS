# Discord Voice Draft Routing

Discord voice is a visible manual-submit draft buffer for every voice bot.

- The daemon forwards every completed transcript to `/api/discord/message`; it does not suppress, debounce, pool, or auto-submit short fragments.
- Token API keeps an in-memory draft lock keyed by `(bot_name, author_id)`.
- First non-command utterance starts a draft, resolves the target pane, marks the pane title with a lock prefix, and types the text without Enter.
- Further non-command utterances append to that same locked pane, even if focus/cursor changes elsewhere.
- `ship` or `ship it` sends Enter to the locked pane, restores the title, and clears the lock. These commands may be standalone or suffixes; suffix text is appended before shipping.
- `scratch` or `scratch that` sends Ctrl-C to the locked pane, restores the title, and clears the lock.
- `command` is a neutral leading filler for cold-starting transcription before a command, e.g. `command ship`.
- `mute` asks the Discord daemon to temporarily server-mute the speaking member for 15s when permissions allow; `unmute` clears it early. `retarget` / `clear target` clears the draft lock without sending keys so the next utterance can choose a new pane.
- Draft birth respects the typing guard: if the target pane already has unrelated pending input, no draft starts. Draft continuation bypasses the guard only for the locked voice-owned pane.
- If the locked pane dies, Token API clears the draft, fails closed, and sends Discord voice feedback. The next utterance may start a new draft.
- State is process-local. After a Token API restart, text already visible in tmux remains, but the voice draft lock is gone.

Targeting:

- `imperial_guard`: locks the daemon-supplied `target_tmux_pane` from the first utterance.
- `mechanicus`: resolves the existing synced live Mechanicus instance, then locks its pane.
- `custodes`: resolves the existing live Custodes pane, including the tmux marker fallback, then locks it.


Debug/admin surfaces:

- `GET /api/discord/voice-drafts` lists active in-memory locks and pane liveness.
- `POST /api/discord/voice-drafts/clear` clears locks without restarting Token API; optional JSON filters: `bot_name`, `author_id`.
- Every voice draft decision writes a `discord_voice_draft` event with transcript, target pane, and result for regression debugging.
