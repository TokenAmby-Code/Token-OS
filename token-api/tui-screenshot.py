#!/Users/tokenclaw/Token-OS/token-api/.venv/bin/python
"""
Render specific TUI panels to SVG/text for debugging.

Usage:
    python tui-screenshot.py timer-stats [--width 100] [--height 20]
    python tui-screenshot.py monitor
    python tui-screenshot.py dashboard
    python tui-screenshot.py info

Outputs:
    /tmp/tui-screenshot.svg  — Rich SVG export
    /tmp/tui-screenshot.txt  — Plain text export
"""

import argparse
import importlib.util
import os
import sys
from pathlib import Path

# Import the TUI module (hyphenated filename requires importlib)
TUI_PATH = Path(__file__).parent / "token-api-tui.py"

def load_tui():
    spec = importlib.util.spec_from_file_location("tui", str(TUI_PATH))
    tui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tui)
    return tui

def main():
    parser = argparse.ArgumentParser(description="Render TUI panels to SVG/text")
    parser.add_argument(
        "panel",
        choices=["timer-stats", "monitor", "dashboard", "info"],
        help="Which panel to render",
    )
    parser.add_argument("--width", type=int, default=None, help="Terminal width to simulate (default: current terminal width)")
    parser.add_argument("--height", type=int, default=20, help="max_lines for panel (default: 20)")
    parser.add_argument("--svg", default="/tmp/tui-screenshot.svg", help="SVG output path")
    parser.add_argument("--txt", default="/tmp/tui-screenshot.txt", help="Text output path")
    args = parser.parse_args()

    width = args.width or os.get_terminal_size(fallback=(120, 40)).columns

    # Load TUI module
    tui = load_tui()

    # Override the TUI module's console so panel width calculations are correct
    tui.console = __import__("rich.console", fromlist=["Console"]).Console(width=width)

    # Map panel names to page numbers and render functions
    panel_map = {
        "timer-stats": 4,
        "monitor": 3,
        "info": 0,
        "dashboard": None,
    }
    tui.panel_page = panel_map[args.panel] if panel_map[args.panel] is not None else 0

    # Create a recording console
    from rich.console import Console

    console = Console(width=width, record=True, force_terminal=True)

    # Render the requested panel
    try:
        if args.panel == "timer-stats":
            renderable = tui.create_timer_stats_panel(max_lines=args.height)
        elif args.panel == "monitor":
            renderable = tui.create_monitor_panel(max_lines=args.height)
        elif args.panel == "info":
            renderable = tui.create_info_panel(max_lines=args.height)
        elif args.panel == "dashboard":
            renderable = tui.generate_dashboard([], 0)
        else:
            print(f"Unknown panel: {args.panel}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"Error rendering panel '{args.panel}': {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)

    console.print(renderable)

    # Export plain text first (export_svg clears the buffer)
    text_output = console.export_text(clear=False)
    Path(args.txt).write_text(text_output)
    print(f"TXT: {args.txt}")

    # Export SVG
    svg_output = console.export_svg(title=f"TUI: {args.panel} ({args.layout}, {width}w)")
    Path(args.svg).write_text(svg_output)
    print(f"SVG: {args.svg}")


if __name__ == "__main__":
    main()
