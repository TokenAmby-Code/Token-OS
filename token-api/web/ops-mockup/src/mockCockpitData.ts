// ─────────────────────────────────────────────────────────────────────────
// Ops Cockpit — static mockup data.
//
// Everything here is FROZEN and deterministic. No Date.now(), no Math.random(),
// no fetch. The shapes intentionally echo the live contracts (TimerHistory,
// OpsState, CockpitLayoutModel) so this study can be transplanted into
// token-api/web/ops later without rethinking the component boundaries.
//
// Iteration 2 scope: timer + dials only. The below-timer surfaces (fleet,
// events, assertions, subsystems, voice) are OUT this round — their *types*
// stay defined for the later rebuild, but the mock data arrays are gone.
// ─────────────────────────────────────────────────────────────────────────

export type MockTimerMode = 'working' | 'multitasking' | 'distracted' | 'break' | 'idle';

export type MockTimerPoint = {
  t: string; // "HH:MM" local
  mode: MockTimerMode;
  breakBalanceMinutes: number; // signed: >0 credit, <0 debt
};

export type MockModeSegment = {
  start: string; // "HH:MM"
  end: string; // "HH:MM"
  mode: MockTimerMode;
};

// ── time helpers (pure) ────────────────────────────────────────────────────
const toMin = (hhmm: string): number => {
  const [h, m] = hhmm.split(':').map(Number);
  return h * 60 + m;
};
const toClock = (min: number): string => {
  const h = Math.floor(min / 60);
  const m = Math.round(min % 60);
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
};

export const DAY_START = '07:20';
export const DAY_END = '16:45';

// Mode bands across the day. These are authoritative (not reconstructed from
// sampled points), mirroring the live rule that segments come from backend
// mode-transition history.
export const timerSegments: MockModeSegment[] = [
  { start: '07:20', end: '08:05', mode: 'idle' },
  { start: '08:05', end: '10:10', mode: 'working' },
  { start: '10:10', end: '10:40', mode: 'multitasking' },
  { start: '10:40', end: '11:10', mode: 'break' },
  { start: '11:10', end: '12:30', mode: 'working' },
  { start: '12:30', end: '13:20', mode: 'distracted' },
  { start: '13:20', end: '14:10', mode: 'break' },
  { start: '14:10', end: '14:35', mode: 'idle' },
  { start: '14:35', end: '16:20', mode: 'working' },
  { start: '16:20', end: '16:45', mode: 'multitasking' },
];

// Signed break-balance keyframes [minute-of-day, balanceMinutes]. The line is
// interpolated between these so the debt trough and two zero-crossings are
// deterministic and obvious.
const balanceKeyframes: Array<[number, number]> = [
  [toMin('07:20'), 0],
  [toMin('08:05'), -2],
  [toMin('10:10'), 46], // strong morning credit
  [toMin('10:40'), 40],
  [toMin('11:10'), 8], // break spent it down
  [toMin('12:30'), 34], // earned back
  [toMin('13:20'), -6], // distraction tips into debt
  [toMin('14:10'), -38], // deep break-debt trough
  [toMin('14:35'), -33],
  [toMin('16:20'), 14], // clawed back through the afternoon
  [toMin('16:45'), 9],
];

const interpBalance = (min: number): number => {
  for (let i = 0; i < balanceKeyframes.length - 1; i++) {
    const [t0, v0] = balanceKeyframes[i];
    const [t1, v1] = balanceKeyframes[i + 1];
    if (min >= t0 && min <= t1) {
      const f = t1 === t0 ? 0 : (min - t0) / (t1 - t0);
      return v0 + (v1 - v0) * f;
    }
  }
  return balanceKeyframes[balanceKeyframes.length - 1][1];
};

const modeAt = (min: number): MockTimerMode => {
  for (const s of timerSegments) {
    if (min >= toMin(s.start) && min < toMin(s.end)) return s.mode;
  }
  return 'idle';
};

// Sample every 5 minutes — frozen at module load, deterministic.
export const timerPoints: MockTimerPoint[] = (() => {
  const out: MockTimerPoint[] = [];
  for (let m = toMin(DAY_START); m <= toMin(DAY_END); m += 5) {
    out.push({ t: toClock(m), mode: modeAt(m), breakBalanceMinutes: Math.round(interpBalance(m) * 10) / 10 });
  }
  return out;
})();

// Current posture readout (right-most point).
export const nowPoint = timerPoints[timerPoints.length - 1];

// ── state dials (the floating radial cluster) ──────────────────────────────
// In iteration 2 the dials ARE the stack test: the demo slider drives how many
// of these render, fanned radially from the top-right corner. `noteworthy`
// still tags the ones the live layout would surface first, so the slider fills
// the noteworthy dials before the nominal tail.
export type DialTone = 'good' | 'warn' | 'bad' | 'neutral' | 'idle';

// A dial's click contract lives in its TYPE. Omit `action` and the dial does
// the default thing — opens the dials drawer. Provide an override and the
// generic <Dial> component runs that dial's own on-click feature instead. New
// override kinds are added here (and to <Dial>'s switch) as features land.
export type DialAction =
  | { kind: 'toggle-timer' } // timer dial → pause/resume the running timer
  | { kind: 'dismiss-phone' } // phone dial → dismiss the phone-distraction alert
  | { kind: 'ack-enforce' }; // enforce dial → acknowledge the pending enforcement

export type MockDial = {
  id: string;
  label: string;
  glyph: string;
  value: string;
  tone: DialTone;
  noteworthy: boolean;
  subtitle: string; // "what is this dial?" subheader — hover tip + drawer line
  tag?: string; // optional mono id chip shown before the label in the hover tip
  //               (the TTS stack uses it for the sender's tmuxctl id, e.g. "2:S")
  action?: DialAction; // omit → default click opens the dials drawer
};

// Ordered noteworthy-first, so growing the count reveals the important gauges
// before the nominal subsystem tail.
export const dials: MockDial[] = [
  { id: 'timer', label: 'Timer', glyph: '❚❚', value: 'BREAK', tone: 'warn', noteworthy: true,
    subtitle: 'Focus timer state — currently on a counted break.', action: { kind: 'toggle-timer' } },
  { id: 'debt', label: 'Balance', glyph: '▼', value: '+9m', tone: 'good', noteworthy: true,
    subtitle: 'Running break-balance — minutes of credit vs. debt.' },
  { id: 'phone', label: 'Phone', glyph: '✕', value: 'YouTube', tone: 'bad', noteworthy: true,
    subtitle: 'Phone foreground app — a distraction is on screen.', action: { kind: 'dismiss-phone' } },
  { id: 'desktop', label: 'Desktop', glyph: '▣', value: 'at desk', tone: 'neutral', noteworthy: true,
    subtitle: 'Desktop presence — inferred from keyboard & focus.' },
  { id: 'enforce', label: 'Enforce', glyph: '!', value: '1 pending', tone: 'bad', noteworthy: true,
    subtitle: 'Enforcement queue — a shock/TTS is armed to fire.', action: { kind: 'ack-enforce' } },
  { id: 'gt', label: 'Gold. Throne', glyph: '♛', value: '2 armed', tone: 'warn', noteworthy: true,
    subtitle: 'Golden Throne rubrics currently armed on live threads.' },
  // nominal / suppressed subsystems — the tail of the stack
  { id: 'cron', label: 'Cron', glyph: '◷', value: 'nominal', tone: 'good', noteworthy: false,
    subtitle: 'Scheduled cron routines — all firing on time.' },
  { id: 'tts', label: 'TTS', glyph: '♪', value: 'nominal', tone: 'good', noteworthy: false,
    subtitle: 'Text-to-speech voice queue — draining normally.' },
  { id: 'mac', label: 'Mac', glyph: '⌘', value: 'up', tone: 'good', noteworthy: false,
    subtitle: 'Mac node — Token-API and daemons reachable.' },
  { id: 'wsl', label: 'WSL', glyph: '⊞', value: 'up', tone: 'good', noteworthy: false,
    subtitle: 'WSL satellite — reachable over the mesh.' },
  { id: 'net', label: 'Mesh', glyph: '⇄', value: 'up', tone: 'good', noteworthy: false,
    subtitle: 'Tailscale mesh — all nodes online.' },
];

// Demo slider bounds. 9 = a full double radial (5 outer + 4 nested inner); past
// that the overflow trails straight down the right edge. The tail is UNBOUNDED
// in geometry (each extra dial just stacks another row lower) — this max only
// sets how far the demo slider lets you push it, so raise it freely to watch
// the stack keep growing. Default lands on the full double radial.
export const MAX_DIAL_COUNT = 20;
export const initialDialCount = 9;

// ── TTS queue (the left-side stack) ─────────────────────────────────────────
// Modelled as a QUEUE, not a flat status list, so the left cluster isn't hard-
// locked to a static display: a live queue can later reorder / grow / drain it
// without changing this contract. `posInQueue` is the order key (0 = head =
// currently speaking); `status` drives the dial's tone + glyph in the render.
// Kept static + deterministic this round like the rest of the mock.
export type MockTtsStatus = 'speaking' | 'queued' | 'done';

export type MockTtsItem = {
  id: string;
  text: string; // the utterance — surfaced in the hover tip + drawer
  route: string; // sender / delivery route (e.g. "phone · Custodes")
  senderTmuxId: string; // sender's canonical tmuxctl id — "{page}:{id}", e.g. "2:S"
  senderName: string; // sender's instance-name (the live session's descriptive name)
  persona: string; // sender's persona key → its icon (see src/personaIcons.tsx).
  //                   Lower-kebab, matching the registry keys (vault/DB slugs).
  status: MockTtsStatus;
  posInQueue: number; // 0-based order key; head (0) is the one speaking
  durationMs?: number; // speak length (stand-in for real TTS utterance duration);
  //                      omit → the stack's SPEAK_MS default. Data-driven so the
  //                      5 s stand-in isn't a component magic number.
};

// Head-first: index 0 is on the wire, the rest wait their turn, tail already
// delivered. Order-driven so a later pass can animate reorders/drains in place.
// Each sender maps to a DISTINCT persona (→ distinct icon; see personaIcons):
// Custodes (the shield), Dorn (enforcement/security fist), Corax (the watcher),
// Vulkan (the timer core/infra), Sanguinius (the morning herald).
export const ttsQueue: MockTtsItem[] = [
  { id: 'tts-0', text: 'Break debt at thirty-eight minutes — return to work.', route: 'phone · Custodes', senderTmuxId: '1:C', senderName: 'custodes-vigil', persona: 'custodes', status: 'speaking', posInQueue: 0 },
  { id: 'tts-1', text: 'Enforcement armed on the live thread.', route: 'mac · Enforce', senderTmuxId: '2:E', senderName: 'enforce-warden', persona: 'dorn', status: 'queued', posInQueue: 1 },
  { id: 'tts-2', text: 'Phone distraction cleared — YouTube dismissed.', route: 'phone · Phone', senderTmuxId: '3:P', senderName: 'phone-watch', persona: 'corax', status: 'queued', posInQueue: 2 },
  { id: 'tts-3', text: 'Focus streak restored. Good.', route: 'phone · Timer', senderTmuxId: '2:T', senderName: 'timer-core', persona: 'vulkan', status: 'queued', posInQueue: 3 },
  { id: 'tts-4', text: 'Morning voiceline delivered.', route: 'phone · Morning', senderTmuxId: '1:M', senderName: 'morning-herald', persona: 'sanguinius', status: 'done', posInQueue: 4 },
];

// The queue-languishing threshold — a GENERIC concept that lives in the data
// layer alongside the other enforcement events (once more than this many
// utterances back up, the queue is "languishing"). The left stack borrows it as
// a convenient VISUAL marker: the TTS dial size/gap are tuned so about this many
// dials fit above the connecting arc's left-edge contact, so dials that spill
// BELOW the arc read as the languishing overflow. The coupling is one-way and
// cosmetic — the arc itself stays FROZEN and is NOT derived from this value (not
// a hot code path); we just tune the packing to land near it.
export const ttsLanguishThreshold = 8;

// Demo slider bounds for the left stack, mirroring the state-dial density knob.
// Past the static queue length the stack cycles it (same as the status fan), so
// the study can watch the queue grow past the threshold and languish below the
// arc. Default sits a couple past the threshold so the marker reads at a glance.
export const MAX_TTS_DEPTH = 16;
export const initialTtsDepth = 10;

// ─────────────────────────────────────────────────────────────────────────
// Below-timer surfaces — OUT this round. Types kept for the later rebuild; the
// mock data arrays were removed intentionally (see header). Do not re-add data
// here without a plan that puts the fleet/evidence surfaces back on screen.
// ─────────────────────────────────────────────────────────────────────────
export type FleetStatus = 'processing' | 'idle' | 'stale' | 'waiting' | 'blocked';

export type MockFleetRow = {
  id: string;
  persona: string;
  rank: string;
  device: 'mac' | 'wsl' | 'phone';
  engine: 'claude' | 'codex';
  status: FleetStatus;
  ageLabel: string;
  zealotry: number;
  sessionDoc: string | null;
  note: string;
  talking?: boolean;
};

export type MockAssertion = {
  claim: string;
  value: string;
  tone: DialTone;
  confidence: 'high' | 'medium' | 'low';
  evidence: string;
};

export type MockEvent = { t: string; lane: string; label: string; tone: DialTone };

export type MockSubsystem = { label: string; value: string; detail: string; tone: DialTone };
