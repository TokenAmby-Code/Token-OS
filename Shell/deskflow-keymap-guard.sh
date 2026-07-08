#!/usr/bin/env bash
set -euo pipefail

# Deskflow/macOS keymap guard.
# The US layout can be misapplied to Deskflow's virtual key events after macOS restart,
# producing a JIS-like symbol map (`'` -> `:`). Australian is US-ANSI compatible for
# normal typing but avoids the Apple US+JIS symbol remap path seen with Deskflow.
TARGET_SOURCE="${DESKFLOW_KEYMAP_SOURCE:-com.apple.keylayout.Australian}"
CONF="${HOME}/Library/Deskflow/Deskflow.conf"
HELPER_DIR="${HOME}/Library/Application Support/Imperium"
HELPER="${HELPER_DIR}/deskflow-select-input-source"
SRC="${HELPER_DIR}/deskflow-select-input-source.swift"
LOCKDIR="${HELPER_DIR}/deskflow-keymap-guard.lock"

mkdir -p "$HELPER_DIR"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  echo "deskflow-keymap-guard: another guard run is active; skipping" >&2
  exit 0
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT

tmp_src="$(mktemp "${HELPER_DIR}/deskflow-select-input-source.swift.XXXXXX")"
cat > "$tmp_src" <<'SWIFT'
import Foundation
import Carbon

let target = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "com.apple.keylayout.Australian"

func cfstr(_ src: TISInputSource, _ key: CFString) -> String {
    guard let p = TISGetInputSourceProperty(src, key) else { return "" }
    return unsafeBitCast(p, to: CFString.self) as String
}
func cfbool(_ src: TISInputSource, _ key: CFString) -> Bool {
    guard let p = TISGetInputSourceProperty(src, key) else { return false }
    return unsafeBitCast(p, to: CFBoolean.self) == kCFBooleanTrue
}

let props = [kTISPropertyInputSourceType as String: kTISTypeKeyboardLayout] as CFDictionary
guard let list = TISCreateInputSourceList(props, true)?.takeRetainedValue() as? [TISInputSource] else {
    fputs("could not list input sources\n", stderr)
    exit(2)
}

for src in list {
    let id = cfstr(src, kTISPropertyInputSourceID)
    if id == target {
        let enableStatus = TISEnableInputSource(src)
        let selectStatus = TISSelectInputSource(src)
        if selectStatus != noErr {
            fputs("failed selecting \(target): enable=\(enableStatus) select=\(selectStatus)\n", stderr)
            exit(1)
        }
        print("selected \(target)")
        exit(0)
    }
}

fputs("input source not found: \(target)\n", stderr)
exit(3)
SWIFT

if [[ ! -f "$SRC" ]] || ! cmp -s "$tmp_src" "$SRC"; then
  mv "$tmp_src" "$SRC"
else
  rm -f "$tmp_src"
fi

if [[ ! -x "$HELPER" || "$SRC" -nt "$HELPER" ]]; then
  tmp_helper="$(mktemp "${HELPER_DIR}/deskflow-select-input-source.XXXXXX")"
  swiftc "$SRC" -o "$tmp_helper"
  chmod 755 "$tmp_helper"
  mv "$tmp_helper" "$HELPER"
fi

# Keep Deskflow client config deterministic. This does not wipe settings.
if [[ -f "$CONF" ]]; then
  python3 - <<'PY'
from pathlib import Path
p = Path.home() / "Library/Deskflow/Deskflow.conf"
text = p.read_text()
if "[client]" not in text:
    text = "[client]\n" + text
lines = text.splitlines()
out=[]
in_client=False
seen={"languageSync":False,"invertXScroll":False,"invertYScroll":False}

def flush_client():
    if not seen["languageSync"]: out.append("languageSync=false")
    if not seen["invertXScroll"]: out.append("invertXScroll=false")
    if not seen["invertYScroll"]: out.append("invertYScroll=false")

for line in lines:
    stripped=line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        if in_client:
            flush_client()
        in_client = stripped.lower() == "[client]"
        seen={"languageSync":False,"invertXScroll":False,"invertYScroll":False}
        out.append(line)
        continue
    if in_client:
        if stripped.startswith("languageSync="):
            out.append("languageSync=false"); seen["languageSync"]=True; continue
        if stripped.startswith("invertXScroll="):
            out.append("invertXScroll=false"); seen["invertXScroll"]=True; continue
        if stripped.startswith("invertYScroll="):
            out.append("invertYScroll=false"); seen["invertYScroll"]=True; continue
    out.append(line)
if in_client:
    flush_client()
p.write_text("\n".join(out) + "\n")
PY
fi

"$HELPER" "$TARGET_SOURCE"
