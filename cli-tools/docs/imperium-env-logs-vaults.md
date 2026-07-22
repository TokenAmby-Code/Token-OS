# Imperium ENV and Logs Vaults

Imperium has two local Obsidian vaults that are one storage unit:

| Vault | Role |
| --- | --- |
| `Imperium-ENV` | Curated personal and manufactured information. This is the default vault. |
| `Imperium-Logs` | Session documents, transcripts, machine logs, and other potentially unbounded generated output. |

They must always be siblings under the same local parent directory. Token-OS
exports that pair as `IMPERIUM_VAULTS_ROOT`, `IMPERIUM_VAULT`, and
`IMPERIUM_LOGS_VAULT`. Neither vault may resolve through a NAS path or silently
fall back to one.

On k12-personal, the headless Obsidian GUI runs in the `obsidian-imperium` LXC.
It mounts both host vaults under `/home/ubuntu/vaults/`. Its registry correctly
uses those *container* paths; do not replace them with host paths
(`/home/tokenamby/vaults/...`).

## Obsidian CLI

Ordinary commands default to `Imperium-ENV` unless `vault=<name>` is supplied.
Session-document commands default to `Imperium-Logs`:

```bash
obsidian session-docs fetch my-session-doc
```

`fetch` reads the canonical Logs session document and writes a disposable copy
under `Imperium-ENV/Embassy/`, preserving the source's relative session path.
It never writes to the canonical document. The cache copy is tagged with:

- `embassy: true`
- `embassy_source_vault: Imperium-Logs`
- `embassy_source_path`
- `embassy_fetched_at`
- `embassy_expires_at` (one hour after fetch)

A repeated fetch refreshes only that Embassy copy. Expiry is advisory in this
MVP: no background janitor and no write-through synchronization exists. Treat
Embassy files as temporary and potentially stale.

## Peripheral Sync

Logs is currently about 10,700 Markdown files and 50 MB. Disk space is not the
constraint; initial indexing, Sync metadata churn, and UI noise are. Keep Logs
off peripheral devices by default. Register it only where direct browsing of
canonical session history is useful.

## Deferred Embassy Work

Embassy write-through, conflict handling, automatic expiry deletion, and
cross-vault navigation are intentionally deferred. They require an explicit
sync/ownership design and must not be inferred from the fetch cache.
