# Proposal: scoped enforcement pause (design only)

Provide a named runtime pause for one enforcement pipeline (for example
`phone_distraction`) without entering Quiet Hours. The pause must suppress physical
enforcement only; TTS, Custodes/Administratum records, and autonomous communications
continue operating.

## Shape

A privileged runtime mutation creates a record with `pipeline`, `reason`, `actor`,
`created_at`, `expires_at` (mandatory, bounded), and a generated audit ID. Every
suppressed action logs the audit ID and returns an explicit `pipeline_paused` reason.
A read endpoint exposes active pauses and expiry. Expiry is automatic; early removal
is privileged and separately audited. Model the authorization/audit mechanics on the
existing runtime-write-protect break-glass path, rather than adding an environment
flag or a Quiet Hours exception.

## Open questions for Emperor ratification

- Which exact pipeline identifiers are allowed, and may one pause include all physical
  modalities (Pavlok plus redirect) while retaining state detection?
- Who can create, renew, and clear a pause; what maximum TTL is acceptable?
- Does an active pause require a periodic operator reminder or only audit/event log?
- Should a pause prevent newly queued enforcement from firing after expiry, or must
  queued work be discarded with an explicit audit record?
