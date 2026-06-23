from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _script_copy(tmp_path: Path, name: str) -> Path:
    # Place copied wrappers under tmp/bin so their ../lib/nas-path.sh lookup is satisfied
    # and their sibling tmuxctl lookup can be faked without touching the repo script.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir(exist_ok=True)
    (lib_dir / "nas-path.sh").write_text(
        'export TOKEN_API_URL="${TOKEN_API_URL:-http://127.0.0.1:9}"\n', encoding="utf-8"
    )
    copied = bin_dir / name
    copied.write_text((ROOT / "bin" / name).read_text(encoding="utf-8"), encoding="utf-8")
    copied.chmod(0o755)
    return copied


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


class _Requests:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict]] = []


def _http_server() -> tuple[ThreadingHTTPServer, _Requests]:
    seen = _Requests()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("content-length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            seen.items.append(("POST", self.path, payload))
            body = b'{"success":true,"count":1}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, seen


def _fake_tmux(fakebin: Path, *, pane: str = "%closepane", instance: str = "iid-close") -> None:
    _write_executable(
        fakebin / "tmux",
        f"""#!/usr/bin/env bash
case "$1" in
  display-message)
    if [[ "$*" == *"#{{pane_id}}"* ]]; then printf '{pane}\n'; fi
    exit 0 ;;
  show-options)
    if [[ "$*" == *"@INSTANCE_ID"* ]]; then printf '{instance}\n'; fi
    if [[ "$*" == *"@PANE_ID"* ]]; then printf 'legion:worker\n'; fi
    exit 0 ;;
  *) exit 0 ;;
esac
""",
    )


def _fake_fzf(fakebin: Path, lifecycle: str, mode: str) -> None:
    _write_executable(
        fakebin / "fzf",
        f"""#!/usr/bin/env bash
args="$*"
cat >/dev/null
if [[ "$args" == *"closeout>"* ]]; then
  printf '{lifecycle}\tselected\n'
elif [[ "$args" == *"final-message>"* ]]; then
  printf '{mode}\tselected\n'
fi
""",
    )


def test_tmux_instance_exit_ignores_post_commit_sigint(tmp_path: Path) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    _fake_tmux(fakebin)
    _write_executable(
        fakebin / "tmuxctl",
        """#!/usr/bin/env bash
kill -INT "$PPID"
printf 'tmuxctl close ok\n'
exit 0
""",
    )
    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env.get('PATH', '')}"

    result = subprocess.run(
        [
            str(_script_copy(tmp_path, "tmux-instance-exit")),
            "--pane",
            "%closepane",
            "--lifecycle",
            "retire",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "CLOSE_CONTRACT_OK lifecycle=retire instance=iid-close pane=%closepane" in result.stdout


def test_tmux_mark_for_close_now_emits_victory_and_posts_body(tmp_path: Path) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    _fake_tmux(fakebin)
    _fake_fzf(fakebin, "retire", "now")
    server, seen = _http_server()
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fakebin}:{env.get('PATH', '')}",
            "TOKEN_API_URL": f"http://127.0.0.1:{server.server_port}",
        }
    )
    try:
        result = subprocess.run(
            [str(_script_copy(tmp_path, "tmux-mark-for-close")), "--pane", "%closepane"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        server.shutdown()

    assert result.returncode == 0, result.stderr
    assert (
        "MARK_FOR_CLOSE_OK mode=now lifecycle=retire instance=iid-close pane=%closepane"
        in result.stdout
    )
    assert seen.items == [
        (
            "POST",
            "/api/instances/iid-close/mark-for-close",
            {"mode": "now", "lifecycle": "retire", "pane": "%closepane"},
        )
    ]


def test_tmux_mark_for_close_after_stop_sends_message_and_emits_victory(tmp_path: Path) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    sent = tmp_path / "sent.txt"
    _fake_tmux(fakebin)
    _fake_fzf(fakebin, "archive-session-doc", "message")
    _write_executable(fakebin / "tmuxctl", f"#!/usr/bin/env bash\ncat > {sent}\n")
    server, seen = _http_server()
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fakebin}:{env.get('PATH', '')}",
            "TOKEN_API_URL": f"http://127.0.0.1:{server.server_port}",
        }
    )
    try:
        result = subprocess.run(
            [str(_script_copy(tmp_path, "tmux-mark-for-close")), "--pane", "%closepane"],
            input="final validation message\n",
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        server.shutdown()

    assert result.returncode == 0, result.stderr
    assert (
        "MARK_FOR_CLOSE_OK mode=after-stop lifecycle=archive-session-doc instance=iid-close pane=%closepane"
        in result.stdout
    )
    assert sent.read_text(encoding="utf-8") == "final validation message"
    assert seen.items == [
        (
            "POST",
            "/api/instances/iid-close/mark-for-close",
            {"mode": "after-stop", "lifecycle": "archive-session-doc", "pane": "%closepane"},
        )
    ]


def test_prefix_q_popup_command_routes_close_pane_env_to_mark_for_close(tmp_path: Path) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    log = tmp_path / "args.txt"
    _write_executable(
        fakebin / "tmux-mark-for-close", f"#!/usr/bin/env bash\nprintf '%s\n' \"$*\" > {log}\n"
    )
    env = os.environ.copy()
    env.update({"PATH": f"{fakebin}:{env.get('PATH', '')}", "CLOSE_PANE": "%captured"})

    subprocess.run(["bash", "-lc", 'tmux-mark-for-close --pane "$CLOSE_PANE"'], env=env, check=True)

    assert log.read_text(encoding="utf-8") == "--pane %captured\n"
