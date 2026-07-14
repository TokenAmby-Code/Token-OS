// The canonical persistent tmux estate for k12-personal (rung 2).
//
// This is the DECLARATION the boot-time constructor stands (see
// Daemon.constructEstate in core.ts). It is derived from the mac estate's
// current shape — `tmuxctld/lib/tmuxctl/builder.py:build_workspace` — NOT
// invented here. Each seat traces to its builder.py origin so a reviewer can
// audit the mirror. Canonical ids only (colons and all); the tmux membrane in
// tmux.ts sanitizes them into session names.
//
// Grouping is deliberate: the perpetual singleton windows (council, mechanicus,
// reservists = 9 seats) are separated from the two workspace grids (palace,
// somnium = 9 seats). If the Emperor rules the grids out, a reviewer trims to
// the perpetual-only tail. Full mirror is the faithful default per "derive from
// the mac estate's current shape — do not invent seat semantics."

export const K12_ESTATE: readonly string[] = [
  // ── Workspace grids (build_workspace stack panes) ──────────────────────────
  // palace: the primary 4-pane orchestration stack (W/N/S/E).
  'palace:W',
  'palace:N',
  'palace:S',
  'palace:E',
  // somnium: the 5-pane stack (W/N/S + NE/SE split column).
  'somnium:W',
  'somnium:N',
  'somnium:S',
  'somnium:NE',
  'somnium:SE',

  // ── Perpetual singleton windows (build_workspace fixed persona seats) ──────
  // council: the five ruling-body singletons.
  'council:custodes',
  'council:pax',
  'council:malcador',
  'council:true-terminal',
  'council:administratum',
  // mechanicus: the forge singletons.
  'mechanicus:fabricator-general',
  'mechanicus:orchestrator',
  // reservists: the standby launcher seats.
  'reservists:civic',
  'reservists:token-os',
];
