// ─────────────────────────────────────────────────────────────────────────
// Compass object spec — the star-reduction algebra, baked into the type system.
//
// Pure and deterministic (mirrors cockpitData.ts: no Date.now(), no I/O).
// The dial's eight rim stars are NOT authored directly — an operator authors a
// list of red/blue stars (a `CompassStar[]`), and `resolveCompass` reduces it to
// a canonical `ResolvedCompass` that the render consumes. The reduction rules:
//
//   1. Same-point merge — red + blue at one direction → one PURPLE point.
//   2. Two-ordinal collapse — two ordinals flanking a common cardinal collapse
//      INTO that cardinal (NE+SE→E, NW+NE→N, SE+SW→S, SW+NW→W). Opposite
//      ordinals share no cardinal → no collapse.
//   3. Rules stack — NE blue + SE blue + E red → E purple (collapse feeds blue
//      into E, then E's red merges → purple).
//   4. Contested ordinal hydrates BOTH — an ordinal shared by two complete pairs
//      feeds both cardinals rather than being consumed: NW,NE,SE → N,E.
//
// Colour algebra is the foundation: red = bit 0, blue = bit 1, purple = both.
// Every merge (same-point, collapse, stacking) is a single bitwise OR — which is
// what makes the reduction order-independent and conflict-free: each cardinal
// activates iff both its flanking ordinals are present, no tie-break ever needed.
// ─────────────────────────────────────────────────────────────────────────

// ── Directions ──────────────────────────────────────────────────────────────
export const CARDINALS = ['N', 'E', 'S', 'W'] as const;
export const ORDINALS = ['NE', 'SE', 'SW', 'NW'] as const;
export type Cardinal = (typeof CARDINALS)[number];
export type Ordinal = (typeof ORDINALS)[number];
export type Direction = Cardinal | Ordinal;

// Clockwise from up (North = 0). The render reads star positions from this.
export const DIR_DEGREES: Record<Direction, number> = {
  N: 0,
  NE: 45,
  E: 90,
  SE: 135,
  S: 180,
  SW: 225,
  W: 270,
  NW: 315,
};

// The two ordinals flanking each cardinal — the collapse (rules 2–4) reads this.
export const CARDINAL_FLANKS: Record<Cardinal, [Ordinal, Ordinal]> = {
  N: ['NW', 'NE'],
  E: ['NE', 'SE'],
  S: ['SE', 'SW'],
  W: ['SW', 'NW'],
};

// ── Colour algebra ───────────────────────────────────────────────────────────
// Authored stars can never *be* purple (invariant #1: purple is derived-only).
export type PrimitiveColor = 'red' | 'blue';
// Rendered stars can be purple (the derived merge result).
export type StarColor = 'red' | 'blue' | 'purple';

type ColorMask = 1 | 2 | 3; // 1 = red, 2 = blue, 3 = purple

function toMask(color: PrimitiveColor): 1 | 2 {
  return color === 'red' ? 1 : 2;
}

function colorFromMask(mask: ColorMask): StarColor {
  return mask === 1 ? 'red' : mask === 2 ? 'blue' : 'purple';
}

// ── Authored spec ────────────────────────────────────────────────────────────
// A list; multiple entries per direction are allowed (they merge). Purple is
// unauthorable — it only ever arises from a merge.
export interface CompassStar {
  dir: Direction;
  color: PrimitiveColor;
}

// ── Canonical output ─────────────────────────────────────────────────────────
// A branded resolved form. The brand is a `unique symbol` no external code can
// forge, so the ONLY way to obtain a `ResolvedCompass` is `resolveCompass`. The
// render prop accepts nothing else — handing it a raw `CompassStar[]` is a type
// error, which bakes in "render only ever consumes the reduced form".
declare const resolvedBrand: unique symbol;
export type ResolvedStar = { dir: Direction; color: StarColor; deg: number };
export type ResolvedCompass = ReadonlyArray<ResolvedStar> & {
  readonly [resolvedBrand]: true;
};

// ── The reducer ──────────────────────────────────────────────────────────────
export function resolveCompass(stars: readonly CompassStar[]): ResolvedCompass {
  // Step 1 — same-point merge: OR every authored star into its direction's mask.
  const mask: Record<Direction, number> = {
    N: 0, E: 0, S: 0, W: 0, NE: 0, SE: 0, SW: 0, NW: 0,
  };
  for (const star of stars) {
    mask[star.dir] |= toMask(star.color);
  }

  // Step 2 — two-ordinal collapse (rules 2–4). A cardinal hydrates iff BOTH its
  // flanking ordinals are present; the ordinals are marked as participated but
  // NOT consumed, so a contested ordinal can hydrate a second cardinal too.
  const hydrated: Record<Cardinal, number> = { N: 0, E: 0, S: 0, W: 0 };
  const participated: Record<Ordinal, boolean> = {
    NE: false, SE: false, SW: false, NW: false,
  };
  for (const cardinal of CARDINALS) {
    const [o1, o2] = CARDINAL_FLANKS[cardinal];
    if (mask[o1] && mask[o2]) {
      hydrated[cardinal] |= mask[o1] | mask[o2];
      participated[o1] = true;
      participated[o2] = true;
    }
  }

  // Step 3 — emit the canonical set. Each cardinal with any mask (authored or
  // hydrated); each ordinal that carries a mask and did NOT participate in a
  // collapse. Guarantees: no emitted cardinal still has both flanks present.
  const out: ResolvedStar[] = [];
  for (const cardinal of CARDINALS) {
    const m = mask[cardinal] | hydrated[cardinal];
    if (m) out.push({ dir: cardinal, color: colorFromMask(m as ColorMask), deg: DIR_DEGREES[cardinal] });
  }
  for (const ordinal of ORDINALS) {
    if (mask[ordinal] && !participated[ordinal]) {
      out.push({ dir: ordinal, color: colorFromMask(mask[ordinal] as ColorMask), deg: DIR_DEGREES[ordinal] });
    }
  }
  return out as unknown as ResolvedCompass;
}
