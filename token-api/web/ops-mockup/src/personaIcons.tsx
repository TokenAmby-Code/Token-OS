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

// persona / chapter key → icon file basename. Keys are lower-kebab so they line
// up with vault slugs (chapter locks, civic persona slugs) and DB persona names.
// `satisfies` keeps the literal keys for PersonaKey while type-checking the map.
export const PERSONA_ICON = {
  // ── overseers + named personas ──
  custodes: 'custodian-helmet',
  administratum: 'scroll-quill',
  'fabricator-general': 'gears',
  mechanicus: 'cog',
  malcador: 'wizard-staff',
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
  deathwatch: 'skull-crossed-bones',
  'legion-of-the-damned': 'burning-skull',
  'soul-drinkers': 'jeweled-chalice',
  // ── successor / additional loyalist chapters ──
  'iron-hands': 'gauntlet',
  'black-templars': 'gothic-cross',
  'crimson-fists': 'armor-punch',
  'grey-knights': 'pointy-sword',
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
