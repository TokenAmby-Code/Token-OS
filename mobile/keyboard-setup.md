

# Samsung Keyboard Setup — Power User Configuration

**Phone:** Samsung S24, One UI 8
**Prerequisites:** Keys Cafe installed from Good Lock / Galaxy Store (v1.8+)
**Dictation:** Wispr Flow (replaces Samsung voice input)
**Date:** 2026-02-26

---

## Step 1: Samsung Keyboard Settings

**Path:** Settings → General Management → Samsung Keyboard Settings

### Input Intelligence
- [ ] **Predictive text** → ON (keeps suggestion strip)
- [ ] **Suggest text corrections** → ON (underline only, non-invasive
- [ ] **Auto-replace** → OFF (mangles IPs, commands, technical terms)
- [ ] **Auto-capitalize** → OFF (breaks commands, paths)
- [ ] **Auto-spacing after punctuation** → OFF (breaks `192.168.1.1`, file paths, URLs)

### Swipe, Touch, and Feedback
- [ ] **Swipe to type** → OFF (fights technical input like IPs and paths)
- [ ] **Hold spacebar** → Cursor control (NOT voice input — Wispr handles dictation)
- [ ] **Backspace speed** → Fast

### Verify
Type in any text field:
```
192.168.1.1
/home/user/.ssh/config
curl -X POST http://localhost:7777/health
```
- No auto-spaces after dots
- No auto-capitalization
- No autocorrect mangling

---

## Step 2: Samsung Toolbar (7 Slots)

**Path:** Open keyboard → tap `...` (three dots) on toolbar → drag to reorder

### Target Layout (left to right)
| Slot | Item |
|------|------|
| 1 | Clipboard |
| 2 | Extract Text |
| 3 | Search |
| 4 | Writing Assist |
| 5 | One-Handed Mode |
| 6 | Keyboard Size |
| 7 | Settings |

### Remove These
- Emoji (will be on 3-finger swipe up gesture)
- Stickers, GIFs
- Voice Input (Wispr replaces this)
- Translate
- Samsung Pass
- Grammarly
- Handwriting

### Verify
- [ ] Tap Clipboard icon → clipboard history appears
- [ ] Tap Extract Text → camera/gallery OCR opens
- [ ] Tap Writing Assist → AI rewrite panel appears

---

## Step 3: Keys Cafe — Dev Bar (Extra Row)

**Path:** Good Lock → Keys Cafe → Edit Keyboard → Custom Row

### 8-Key Dev Bar

Add one row ABOVE the QWERTY row:

| Position | Key | Long-press | Notes |
|----------|-----|-----------|-------|
| 1 | `TAB` | — | Form navigation, indentation. No tab key otherwise. |
| 2 | `/` | `\` | URLs, paths. Backslash for escapes. |
| 3 | `-` | `_` | Flags, filenames. Underscore for snake_case. |
| 4 | `{` | `[` | JSON, code blocks. |
| 5 | `}` | `]` | Closing pair. |
| 6 | `\|` | `~` | Pipe for commands. Tilde for home dir. |
| 7 | `:` | `;` | Ports, time, YAML, Python. |
| 8 | `#` | `$` | Markdown headers, comments. Dollar for vars. |

### Sizing
- All 8 keys equal width
- Slightly smaller height than QWERTY keys (compact, visually distinct)

### Theming the Dev Bar
If per-row coloring is supported:
- Dev bar background: `#2A4A4A` (muted teal accent)

### Verify
Open Discord or a browser, type:
```
/home/user/.ssh/config
{"key": "value"}
http://localhost:7777
# heading
$HOME | grep -v test
```
All characters should be reachable from the dev bar without hitting ?123.

---

## Step 4: Keys Cafe — Gestures (8 Slots)

**Path:** Good Lock → Keys Cafe → Gesture Settings

### Two-Finger Gestures (frequent editing)

| Direction | Action | Mnemonic |
|-----------|--------|----------|
| UP | Undo | "Take it back" |
| DOWN | Redo | "Put it back" |
| LEFT | Copy | Copy ← grab |
| RIGHT | Paste | Paste → place |

### Three-Finger Gestures (mode switches)

| Direction | Action | Mnemonic |
|-----------|--------|----------|
| UP | Emoji panel | Replaces removed toolbar emoji button |
| DOWN | Writing Assist | Quick AI rewrite |
| LEFT | Voice Input | Backup when Wispr bubble is offscreen |
| RIGHT | Clipboard | Quick clipboard history |

### Verify
- [ ] Open any text field, type some text
- [ ] 2-finger swipe UP → text undoes
- [ ] 2-finger swipe DOWN → text redoes
- [ ] Select text → 2-finger swipe LEFT → copies
- [ ] 2-finger swipe RIGHT → pastes
- [ ] 3-finger swipe UP → emoji panel opens

---

## Step 5: Keys Cafe — Key Sizing

**Path:** Good Lock → Keys Cafe → Edit Keyboard → Key Size/Layout

| Key | Change |
|-----|--------|
| Backspace | ~20% wider (stock is too narrow, mis-hits on P/L) |
| Spacebar | ~10-15% narrower (stock is enormous) |
| Comma/Period | Wider (absorbs space from narrowed spacebar) |
| Enter | Keep default |
| Shift | Keep default |

### Verify
- [ ] Backspace is easier to hit without catching P or L
- [ ] Comma and period feel more reachable
- [ ] Spacebar still comfortable for thumb typing

---

## Step 6: Keys Cafe — Mode Cycling

**Path:** Good Lock → Keys Cafe → Mode Settings

- [ ] Keep: Standard + Math
- [ ] Remove: Chemistry (not useful for dev work, just one more mode to cycle past)

---

## Step 7: Keys Cafe — Theme

**Path:** Good Lock → Keys Cafe → Theme

| Element | Value |
|---------|-------|
| Background | `#1A1A2E` (deep navy, not pure black) |
| Key color | `#2D2D44` (subtle contrast from bg) |
| Key text | `#E0E0E0` (light gray, high contrast) |
| Accent color | `#4EC9B0` (muted teal — shift indicator, active states) |
| Key shape | Slightly rounded rectangles |
| Key borders | OFF (borderless, cleaner look) |
| Night mode | Auto (matches system dark mode) |

### Verify
- [ ] Keyboard is dark with teal accents
- [ ] Keys are borderless but visually distinct
- [ ] Text is readable in all lighting

---

## Final Layout Reference

```
Samsung Toolbar: [Clipboard] [Extract Text] [Search] [Writing Assist] [1-Hand] [Size] [Settings]
Suggestion Strip: [word1] [word2] [word3]
+-------+-------+-------+-------+-------+-------+-------+-------+
| TAB   |  /\   |  -_   |  {[   |  }]   |  |~   |  :;   |  #$   |  <- Dev Bar
+-------+-------+-------+-------+-------+-------+-------+-------+
|  Q 1  |  W 2  |  E 3  |  R 4  |  T 5  |  Y 6  |  U 7  |  I 8  |  O 9  |  P 0  |
+-------+-------+-------+-------+-------+-------+-------+-------+-------+-------+
  |  A   |  S   |  D   |  F   |  G   |  H   |  J   |  K   |  L   |
  +------+------+------+------+------+------+------+------+------+
    | SHIFT |  Z  |  X  |  C  |  V  |  B  |  N  |  M  | <-- (wide) |
    +-------+-----+-----+-----+-----+-----+-----+-----+----------+
      | ?123  | ,  |         SPACE (hold=cursor)          | .  |  ->  |
      +-------+----+-------------------------------------+----+----+

Gestures: 2F-UP=Undo  2F-DOWN=Redo  2F-LEFT=Copy  2F-RIGHT=Paste
          3F-UP=Emoji 3F-DOWN=Writing Assist  3F-LEFT=Voice  3F-RIGHT=Clipboard
```

---

## Termux Toolbar (NO CHANGES)

The Termux extra-keys toolbar is independent and only renders inside Termux:
```
Top:  ESC  SHIFT  ALT  CTRL  HOME  UP   END
Bot:  |    ~      /    -     LEFT  DOWN RIGHT
```
With `claude-enter` and `exit-enter` on long-press of UP and DOWN.
This stays as-is — the Keys Cafe dev bar covers general-app gaps, not terminal gaps.

---

## Day-After Review Checklist

After using for a full day:

- [ ] Any dev bar key going unused? → swap for `=`, `@`, `<`, or `>`
- [ ] Gesture muscle memory building? → give it 3 days
- [ ] Backspace width comfortable? → fine-tune in Keys Cafe
- [ ] Spacebar cursor control working well? → should feel natural for text editing
- [ ] Missing autocorrect for prose? → Wispr + suggestion strip should cover it
