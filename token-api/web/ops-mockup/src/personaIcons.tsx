// ─────────────────────────────────────────────────────────────────────────
// personaIcons — the standalone persona → icon registry.
//
// Maps each Imperium persona / Astartes chapter to a game-icons.net glyph
// (CC BY 3.0 — see src/icons/CREDITS.md). Every icon SVG is reduced to a single
// path with fill="currentColor", so it inherits the consuming element's `color`
// (a TTS dial's --tone today; any future surface's colour tomorrow).
//
// Built to be imported by MORE than the TTS stack: the forthcoming instance
// table maps a row's persona → this same registry. So the resolver is generic
// (takes any persona string, case-insensitive, chapter- or role-keyed) and the
// icon set covers the whole roster — every named persona, rank base, civic
// persona, and all 13 Astartes chapter locks — not just the senders on screen.
// ─────────────────────────────────────────────────────────────────────────
import type { ReactNode } from 'react';
// Full-colour brand assets for the image personas (see PERSONA_IMAGE below).
// Vite resolves each import to a served URL string.
import paxAvatar from './assets/pax-bot-pfp.png';
import ciMonogram from './assets/ci-logo.svg';
import malcadorPortrait from './assets/malcador-photoroom.png';

// The raw single-path SVG source for every icon, keyed by module path. ONE Vite
// glob keeps src/icons/*.svg as the single source of truth — those files are
// both the shipped assets (with CC-BY attribution) and exactly what this module
// renders inline, so there is no path-data duplicated into this file to drift.
const rawByFile = import.meta.glob('./icons/*.svg', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

function rawIcon(name: string): string | undefined {
  return rawByFile[`./icons/${name}.svg`];
}

// ── Image personas ──────────────────────────────────────────────────────────
// A few personas are represented by a FULL-COLOUR brand asset rather than a
// single-path currentColor glyph — the Pax agent's avatar, the Civic
// Initiatives "ci" monogram, and Malcador's portrait. These can't ride the
// currentColor tint (they carry their own colours), so they resolve through a
// SEPARATE registry and callers render them as an <image>/<img> rather than
// inline SVG path markup. The asset URLs are imported at the top of this module
// (Vite resolves each to a URL). Image personas still keep a glyph entry in
// PERSONA_ICON below (their currentColor fallback for non-image surfaces such as
// the TTS rings and worker chips); the image only takes over on the lemon.
//
// persona key → image asset URL. Keys are lower-kebab like PERSONA_ICON.
export const PERSONA_IMAGE = {
  pax: paxAvatar,
  ci: ciMonogram,
  malcador: malcadorPortrait,
} satisfies Record<string, string>;

// Resolve a persona to a full-colour image asset URL, or undefined if the
// persona is a glyph persona (→ use personaIcon/personaIconInner instead).
export function personaImage(persona: string): string | undefined {
  return (PERSONA_IMAGE as Record<string, string>)[normalize(persona)];
}

// persona / chapter key → icon file basename. Keys are lower-kebab so they line
// up with vault slugs (chapter locks, civic persona slugs) and DB persona names.
// `satisfies` keeps the literal keys for PersonaKey while type-checking the map.
export const PERSONA_ICON = {
  // ── overseers + named personas ──
  custodes: 'custodian-helmet',
  administratum: 'scroll-quill',
  'fabricator-general': 'gears',
  mechanicus: 'cog',
  malcador: 'malcador-sigil',
  inquisitor: 'magnifying-glass',
  vulkan: 'anvil-impact',
  guilliman: 'open-book',
  sanguinius: 'angel-wings',
  perturabo: 'locked-fortress',
  dorn: 'mailed-fist',
  corax: 'raven',
  alpharius: 'hydra',
  // ── rank bases ──
  astartes: 'spartan-helmet',
  aspirant: 'graduate-cap',
  'black-shields': 'checked-shield',
  // ── civic personas (Pax-ENV) ──
  pax: 'peace-dove',
  orchestrator: 'mesh-network',
  'agentic-worker': 'gear-hammer',
  // ── Astartes chapter locks — chapter children resolve to the generic Astartes
  //    behaviourally, but each carries its own heraldry here ──
  ultramarines: 'omega',
  'blood-angels': 'droplets',
  'dark-angels': 'broadsword',
  'space-wolves': 'wolf-head',
  'imperial-fists': 'fist',
  salamanders: 'salamander',
  'raven-guard': 'crow-dive',
  'white-scars': 'lightning-arc',
  'emperors-children': 'sonic-screech',
  'alpha-legion': 'hydra-shot',
  // ── traitor legions (Horus Heresy — First Founding) ──
  'iron-warriors': 'hazard-sign',
  'night-lords': 'bat',
  'world-eaters': 'battle-axe',
  'death-guard': 'poison-cloud',
  'thousand-sons': 'sun',
  'sons-of-horus': 'eye-of-horus',
  'word-bearers': 'book-cover',
  deathwatch: 'skull-crossed-bones',
  'legion-of-the-damned': 'burning-skull',
  'soul-drinkers': 'jeweled-chalice',
  // ── successor / additional loyalist chapters ──
  'iron-hands': 'gauntlet',
  'black-templars': 'gothic-cross',
  'crimson-fists': 'armor-punch',
  'grey-knights': 'winged-sword',
  'iron-snakes': 'snake',
  'flesh-tearers': 'bloody-sword',
  'blood-ravens': 'book-aura',
  'howling-griffons': 'griffin-symbol',
  'silver-skulls': 'crowned-skull',
  carcharodons: 'shark-jaws',
  'red-scorpions': 'scorpion',
  mortifactors: 'grim-reaper',
  'scythes-of-the-emperor': 'scythe',
  novamarines: 'beveled-star',
  minotaurs: 'minotaur',
  exorcists: 'daemon-skull',
  'doom-eagles': 'eagle-emblem',
  'celestial-lions': 'lion',
  'death-spectres': 'ghost',
  'storm-wardens': 'crossed-swords',
  'sons-of-medusa': 'medusa-head',
  lamenters: 'tear-tracks',
  'hawk-lords': 'hawk-emblem',
  'black-consuls': 'eagle-head',
} satisfies Record<string, string>;

// Ordered Astartes chapter/legion heraldry keys — the faction glyphs (excludes
// overseers, civic personas, rank bases). Every key from the chapter/legion
// blocks of PERSONA_ICON above (ultramarines → black-consuls). Hand-listed
// (stable, reviewable) rather than a runtime slice of the map. Consumed by the
// mock worker + TTS rosters so the full heraldry set gets exercised on screen.
export const FACTION_PERSONAS = [
  // First Founding loyalists
  'ultramarines', 'blood-angels', 'dark-angels', 'space-wolves', 'imperial-fists',
  'salamanders', 'raven-guard', 'white-scars', 'emperors-children', 'alpha-legion',
  // traitor legions
  'iron-warriors', 'night-lords', 'world-eaters', 'death-guard', 'thousand-sons',
  'sons-of-horus', 'word-bearers', 'deathwatch', 'legion-of-the-damned', 'soul-drinkers',
  // successor / additional loyalist chapters
  'iron-hands', 'black-templars', 'crimson-fists', 'grey-knights', 'iron-snakes',
  'flesh-tearers', 'blood-ravens', 'howling-griffons', 'silver-skulls', 'carcharodons',
  'red-scorpions', 'mortifactors', 'scythes-of-the-emperor', 'novamarines', 'minotaurs',
  'exorcists', 'doom-eagles', 'celestial-lions', 'death-spectres', 'storm-wardens',
  'sons-of-medusa', 'lamenters', 'hawk-lords', 'black-consuls',
] as const;

// The union of every persona key the registry knows — lets callers (e.g. the
// instance table) reference personas with compile-time checking if they want.
export type PersonaKey = keyof typeof PERSONA_ICON;

// Normalise a caller's persona string to a registry key: lower-cased, whitespace
// and underscores → hyphens. So "Blood Angels", "blood_angels" and
// "blood-angels" all resolve to the same icon.
function normalize(persona: string): string {
  return persona.trim().toLowerCase().replace(/[\s_]+/g, '-');
}

// Resolve a persona to its icon file basename, or undefined if unknown.
export function personaIconName(persona: string): string | undefined {
  return (PERSONA_ICON as Record<string, string>)[normalize(persona)];
}

// Resolve a persona to a ready-to-render icon node. Unknown personas fall back
// to the generic Astartes helmet, so a surface always renders SOMETHING rather
// than a hole (a chapter child not yet mapped still reads as a marine). The
// inline SVG (fill="currentColor") inherits the host element's `color`.
export function personaIcon(persona: string): ReactNode {
  const raw = rawIcon(personaIconName(persona) ?? 'spartan-helmet');
  if (!raw) return null;
  return <span className="persona-icon" aria-hidden dangerouslySetInnerHTML={{ __html: raw }} />;
}

// Resolve a persona to the icon's INNER SVG markup — the `<path fill="currentColor">`
// with the outer `<svg …>` wrapper stripped — so callers can embed it NATIVELY inside
// an existing SVG (e.g. a `<g dangerouslySetInnerHTML>` on the arc layer) rather than
// nesting an <svg>/<foreignObject>. Returns null for a genuinely missing asset; falls
// back to the generic Astartes helmet for an unmapped persona (same policy as
// personaIcon), so a surface tints SOMETHING rather than rendering a hole.
export function personaIconInner(persona: string): string | null {
  const raw = rawIcon(personaIconName(persona) ?? 'spartan-helmet');
  if (!raw) return null;
  return raw.replace(/^<svg[^>]*>/, '').replace(/<\/svg>\s*$/, '');
}
