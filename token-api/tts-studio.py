#!/usr/bin/env python3
"""TTS Studio - Voice testing and selection TUI."""

import os
import subprocess
import sys
from dataclasses import dataclass, field
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.live import Live
from rich import box
import time

console = Console()

# Voice definitions: (name, region, gender)
VOICES = [
    ("Microsoft David", "US", "M"),
    ("Microsoft Zira", "US", "F"),
    ("Microsoft Mark", "US", "M"),
    ("Microsoft George", "UK", "M"),
    ("Microsoft Susan", "UK", "F"),
    ("Microsoft Catherine", "AU", "F"),
    ("Microsoft James", "AU", "M"),
    ("Microsoft Sean", "IE", "M"),
    ("Microsoft Hazel", "IE", "F"),  # Reassigned from UK for array balance
    ("Microsoft Heera", "IN", "F"),
    ("Microsoft Ravi", "IN", "M"),
    ("Microsoft Linda", "CA", "F"),
]

@dataclass
class VoiceConfig:
    name: str
    region: str
    gender: str
    rate: int = 2
    selected: bool = False


def speak_tts(message: str, voice: str, rate: int) -> bool:
    """Speak text using Windows SAPI."""
    escaped = message.replace("\\", "\\\\").replace("'", "''").replace("$", "\\$").replace("`", "\\`")
    ps_script = f"""
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SelectVoice('{voice}')
$synth.Rate = [int]{rate}
$synth.Speak('{escaped}')
"""
    try:
        process = subprocess.Popen(
            ["powershell.exe"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        process.communicate(input=ps_script.encode(), timeout=30)
        return process.returncode == 0
    except Exception as e:
        console.print(f"[red]TTS Error: {e}[/red]")
        return False


def create_voice_table(voices: list[VoiceConfig], selected_idx: int, editing_rate: int | None = None) -> Table:
    """Create the voice selection table."""
    table = Table(box=box.ROUNDED, expand=True, title="Available Voices")

    table.add_column("#", style="dim", width=3, justify="right")
    table.add_column("", width=3, justify="center")  # Checkbox
    table.add_column("Voice", min_width=20)
    table.add_column("Region", width=6, justify="center")
    table.add_column("Gender", width=6, justify="center")
    table.add_column("Rate", width=8, justify="center")
    table.add_column("", width=6, justify="center")  # Test hint

    for i, v in enumerate(voices):
        is_selected = i == selected_idx
        checkbox = "[green]☑[/green]" if v.selected else "[dim]☐[/dim]"

        # Rate display
        if editing_rate is not None and is_selected:
            rate_str = f"[yellow]>{editing_rate}<[/yellow]"
        else:
            rate_str = f"[cyan]{v.rate:+d}[/cyan]" if v.rate != 0 else "[dim]0[/dim]"

        # Row styling
        style = "reverse" if is_selected else ""

        # Gender icon
        gender_icon = "[blue]♂[/blue]" if v.gender == "M" else "[magenta]♀[/magenta]"

        table.add_row(
            str(i + 1),
            checkbox,
            v.name.replace("Microsoft ", ""),
            v.region,
            gender_icon,
            rate_str,
            "[dim]Enter[/dim]" if is_selected else "",
            style=style
        )

    return table


def create_help_panel() -> Panel:
    """Create the help panel."""
    help_text = """[bold]Navigation[/bold]
  ↑/↓ or j/k    Move selection
  Space         Toggle voice selection
  Enter         Test selected voice

[bold]Rate Adjustment[/bold]
  ←/→ or h/l    Adjust rate (-10 to +10)
  r             Edit rate directly (then Enter)
  0             Reset rate to 0

[bold]Testing[/bold]
  t             Test current voice
  T             Test ALL selected voices

[bold]Actions[/bold]
  s             Save selection to main.py
  q             Quit"""
    return Panel(help_text, title="Controls", border_style="blue")


def create_test_text_panel(text: str, editing: bool = False) -> Panel:
    """Create the test text input panel."""
    if editing:
        content = f"[yellow]> {text}_[/yellow]"
    else:
        content = f"[white]{text}[/white]\n[dim]Press 'e' to edit test text[/dim]"
    return Panel(content, title="Test Text", border_style="cyan")


def create_status_panel(voices: list[VoiceConfig], message: str = "") -> Panel:
    """Create status bar."""
    selected_count = sum(1 for v in voices if v.selected)
    selected_names = [v.name.replace("Microsoft ", "") for v in voices if v.selected]

    if message:
        content = f"[yellow]{message}[/yellow]"
    else:
        content = f"Selected: [green]{selected_count}[/green]/12  "
        if selected_names:
            content += f"[dim]({', '.join(selected_names)})[/dim]"

    return Panel(content, border_style="dim")


# Map WSL SAPI voices to closest macOS `say` voice for fallback
MAC_VOICE_PAIRS = {
    "Microsoft David": "Daniel",
    "Microsoft Zira": "Karen",
    "Microsoft Mark": "Daniel",
    "Microsoft George": "Daniel",
    "Microsoft Susan": "Karen",
    "Microsoft Catherine": "Karen",
    "Microsoft James": "Daniel",
    "Microsoft Sean": "Moira",
    "Microsoft Hazel": "Moira",
    "Microsoft Heera": "Rishi",
    "Microsoft Ravi": "Rishi",
    "Microsoft Linda": "Karen",
}


def generate_profile_code(voices: list[VoiceConfig]) -> str:
    """Generate the PROFILES code for main.py (unified WSL + Mac format)."""
    selected = [v for v in voices if v.selected]
    if not selected:
        return "# No voices selected"

    sounds = ["chimes.wav", "notify.wav", "ding.wav", "tada.wav", "chord.wav", "recycle.wav"]
    colors = ["#0099ff", "#00cc66", "#ff9900", "#cc66ff", "#ff6666", "#66cccc", "#ffcc00", "#cc99ff"]

    lines = ["# Profile pool for voice/sound assignment"]
    lines.append("# WSL voices via Windows SAPI, Mac voices via macOS `say` (fallback)")
    lines.append("PROFILES = [")

    for i, v in enumerate(selected):
        sound = sounds[i % len(sounds)]
        color = colors[i % len(colors)]
        mac_voice = MAC_VOICE_PAIRS.get(v.name, "Daniel")
        lines.append(
            f'    {{"name": "profile_{i+1}", "wsl_voice": "{v.name}", "wsl_rate": {v.rate}, '
            f'"mac_voice": "{mac_voice}", "notification_sound": "{sound}", "color": "{color}"}},'
        )

    lines.append("]")
    return "\n".join(lines)


def read_char() -> str:
    """Read a single character from stdin."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        # Handle escape sequences
        if ch == '\x1b':
            ch2 = sys.stdin.read(1)
            if ch2 == '[':
                ch3 = sys.stdin.read(1)
                if ch3 == 'A': return 'UP'
                if ch3 == 'B': return 'DOWN'
                if ch3 == 'C': return 'RIGHT'
                if ch3 == 'D': return 'LEFT'
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def read_line(prompt: str = "") -> str:
    """Read a line of input."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        line = ""
        while True:
            ch = sys.stdin.read(1)
            if ch in ('\r', '\n'):
                return line
            elif ch == '\x7f':  # Backspace
                if line:
                    line = line[:-1]
                    sys.stdout.write('\b \b')
                    sys.stdout.flush()
            elif ch == '\x1b':  # Escape - cancel
                return None
            elif ch >= ' ':
                line += ch
                sys.stdout.write(ch)
                sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def main():
    # Initialize voices
    voices = [VoiceConfig(name=v[0], region=v[1], gender=v[2]) for v in VOICES]
    selected_idx = 0
    test_text = "Hello, I am testing this voice for the Token API notification system."
    status_message = ""
    editing_text = False
    editing_rate = None

    console.clear()

    while True:
        # Build layout
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="status", size=3)
        )

        layout["main"].split_row(
            Layout(name="voices", ratio=2),
            Layout(name="sidebar", ratio=1)
        )

        layout["sidebar"].split_column(
            Layout(name="help"),
            Layout(name="test_text", size=6)
        )

        # Render panels
        layout["header"].update(Panel(
            Text.from_markup("[bold cyan]TTS Studio[/bold cyan] - Voice Testing & Selection"),
            border_style="cyan"
        ))
        layout["voices"].update(Panel(
            create_voice_table(voices, selected_idx, editing_rate),
            border_style="green"
        ))
        layout["help"].update(create_help_panel())
        layout["test_text"].update(create_test_text_panel(test_text, editing_text))
        layout["status"].update(create_status_panel(voices, status_message))

        # Display
        console.clear()
        console.print(layout)

        status_message = ""

        # Handle input
        if editing_text:
            console.print("[yellow]Enter test text (Esc to cancel):[/yellow] ", end="")
            new_text = read_line()
            if new_text is not None and new_text.strip():
                test_text = new_text
            editing_text = False
            continue

        if editing_rate is not None:
            console.print(f"[yellow]Enter rate (-10 to 10):[/yellow] ", end="")
            rate_str = read_line()
            if rate_str is not None:
                try:
                    new_rate = int(rate_str)
                    voices[selected_idx].rate = max(-10, min(10, new_rate))
                except ValueError:
                    status_message = "Invalid rate"
            editing_rate = None
            continue

        key = read_char()

        if key in ('q', '\x03'):  # q or Ctrl+C
            break

        elif key in ('UP', 'k'):
            selected_idx = (selected_idx - 1) % len(voices)

        elif key in ('DOWN', 'j'):
            selected_idx = (selected_idx + 1) % len(voices)

        elif key == ' ':  # Toggle selection
            voices[selected_idx].selected = not voices[selected_idx].selected

        elif key in ('LEFT', 'h'):
            voices[selected_idx].rate = max(-10, voices[selected_idx].rate - 1)

        elif key in ('RIGHT', 'l'):
            voices[selected_idx].rate = min(10, voices[selected_idx].rate + 1)

        elif key == '0':
            voices[selected_idx].rate = 0

        elif key == 'r':
            editing_rate = voices[selected_idx].rate

        elif key == 'e':
            editing_text = True

        elif key in ('t', '\r', '\n'):  # Test current voice
            v = voices[selected_idx]
            status_message = f"Testing {v.name.replace('Microsoft ', '')} at rate {v.rate}..."
            console.clear()
            console.print(layout)
            speak_tts(test_text, v.name, v.rate)
            status_message = f"Played: {v.name.replace('Microsoft ', '')}"

        elif key == 'T':  # Test all selected
            selected = [v for v in voices if v.selected]
            if not selected:
                status_message = "No voices selected"
            else:
                for i, v in enumerate(selected):
                    status_message = f"Testing {i+1}/{len(selected)}: {v.name.replace('Microsoft ', '')}..."
                    # Quick refresh
                    console.clear()
                    layout["status"].update(create_status_panel(voices, status_message))
                    console.print(layout)
                    speak_tts(test_text, v.name, v.rate)
                    time.sleep(0.5)
                status_message = f"Tested {len(selected)} voices"

        elif key == 's':  # Save to main.py
            selected = [v for v in voices if v.selected]
            if not selected:
                status_message = "No voices selected to save"
            else:
                code = generate_profile_code(voices)
                console.clear()
                console.print(Panel(code, title="Generated PROFILES", border_style="yellow"))
                console.print("\n[yellow]Save this to main.py? (y/n)[/yellow] ", end="")
                confirm = read_char()
                if confirm.lower() == 'y':
                    # Actually update main.py
                    try:
                        _token_os = os.environ.get("TOKEN_OS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                        main_py_path = os.path.join(_token_os, "token-api", "main.py")

                        with open(main_py_path, "r") as f:
                            content = f.read()

                        # Find and replace PROFILES block
                        import re
                        pattern = r'# Profile pool for voice/sound assignment\n.*?PROFILES = \[.*?\]'
                        new_content = re.sub(pattern, code, content, flags=re.DOTALL)

                        with open(main_py_path, "w") as f:
                            f.write(new_content)

                        status_message = f"Saved {len(selected)} voices to main.py!"
                    except Exception as e:
                        status_message = f"Error saving: {e}"
                else:
                    status_message = "Save cancelled"

    console.clear()
    console.print("[dim]TTS Studio closed[/dim]")


if __name__ == "__main__":
    main()
