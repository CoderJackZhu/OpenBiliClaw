"""Validate a published and rebuilt desktop installer on a disposable Windows runner."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

PUBLISHED = (
    "https://github.com/whiteguo233/OpenBiliClaw/releases/download/"
    "desktop-v0.3.235/OpenBiliClaw-windows-0.3.235-Setup.exe"
)
PUBLISHED_SHA = "f48619ad5e2366a93b3dcdba85e9051bac7f86b56656109890fcc62ebda6d6ee"
UNINSTALL_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    r"\{B4F3D2A1-7C6E-4A8B-9D1F-0E2A6C5B3D14}_is1"
)
SILENT = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]


def sha(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


class Wizard:
    def __init__(self) -> None:
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        hwnd, uint = wintypes.HWND, wintypes.UINT
        wparam, lparam = wintypes.WPARAM, wintypes.LPARAM
        self.callback = ctypes.WINFUNCTYPE(wintypes.BOOL, hwnd, lparam)
        for name, args, result in (
            ("EnumWindows", [self.callback, lparam], wintypes.BOOL),
            ("EnumChildWindows", [hwnd, self.callback, lparam], wintypes.BOOL),
            ("IsWindowVisible", [hwnd], wintypes.BOOL),
            ("IsWindowEnabled", [hwnd], wintypes.BOOL),
            ("GetClassNameW", [hwnd, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            ("GetWindowTextW", [hwnd, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            (
                "SendMessageTimeoutW",
                [hwnd, uint, wparam, lparam, uint, uint, ctypes.POINTER(ctypes.c_size_t)],
                lparam,
            ),
            ("PostMessageW", [hwnd, uint, wparam, lparam], wintypes.BOOL),
        ):
            function = getattr(self.user32, name)
            function.argtypes, function.restype = args, result

    def _message(self, handle: int, message: int, value: int, buffer: ctypes.Array) -> bool:
        result = ctypes.c_size_t()
        return bool(
            self.user32.SendMessageTimeoutW(
                handle, message, value, ctypes.addressof(buffer), 2, 750, ctypes.byref(result)
            )
            and result.value
        )

    def controls(self) -> list[tuple[int, str, str]]:
        found: list[tuple[int, str, str]] = []

        def windows(parent: int | None = None) -> None:
            @self.callback
            def visit(handle: int, _unused: int) -> bool:
                if not self.user32.IsWindowVisible(handle):
                    return True
                kind = ctypes.create_unicode_buffer(256)
                self.user32.GetClassNameW(handle, kind, len(kind))
                label = ctypes.create_unicode_buffer(4096)
                if parent is None:
                    self.user32.GetWindowTextW(handle, label, len(label))
                    if kind.value != "TWizardForm" and label.value not in (
                        "Setup",
                        "Exit Setup",
                        "Error",
                    ):
                        return True
                    found.append((handle, kind.value, label.value))
                    windows(handle)
                else:
                    self._message(handle, 0x000D, len(label), label)
                    found.append((handle, kind.value, label.value))
                return True

            if parent is None:
                self.user32.EnumWindows(visit, 0)
            else:
                self.user32.EnumChildWindows(parent, visit, 0)

        windows()
        return found

    def wait(self, text: str, timeout: int = 45) -> list[tuple[int, str, str]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            controls = self.controls()
            if any(text in label for _, _, label in controls):
                return controls
            time.sleep(0.15)
        raise AssertionError(f"Window did not show {text!r}. Controls: {self.controls()}")

    def set_path(self, path: Path) -> None:
        controls = self.wait("Select Destination Location")
        handle = next(h for h, kind, _ in controls if kind == "TNewPathEdit")
        assert self._message(handle, 0x000C, 0, ctypes.create_unicode_buffer(str(path)))
        assert any(label == str(path) for _, _, label in self.controls())

    def click(self, title: str) -> None:
        for handle, _, label in self.controls():
            if label.replace("&", "").rstrip(" >") == title and self.user32.IsWindowEnabled(handle):
                assert self.user32.PostMessageW(handle, 0x00F5, 0, 0)
                return
        raise AssertionError(f"No enabled {title!r} button: {self.controls()}")

    def finish(self, process: subprocess.Popen) -> None:
        self.click("Next")
        self.wait("Select Additional Tasks")
        self.click("Next")
        self.wait("Ready to Install")
        self.click("Install")
        self.wait("Completing the OpenBiliClaw Setup Wizard", timeout=240)
        self.click("Finish")
        assert process.wait(timeout=60) == 0


def main() -> None:
    import winreg

    new = Path(sys.argv[1]).resolve()
    frozen = Path("dist/OpenBiliClaw/OpenBiliClaw.exe")
    root = Path(os.environ["RUNNER_TEMP"]) / "installer destination acceptance"
    root.mkdir()
    evidence_path = root / "evidence.json"
    old = root / "published.exe"
    profile = root / "isolated profile"
    original = root / "original install"
    relocated = root / "new install 测试"
    client = build_opener(ProxyHandler({}))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = dict(
        os.environ,
        OPENBILICLAW_PROJECT_ROOT=str(profile),
        OPENBILICLAW_HOST="127.0.0.1",
        OPENBILICLAW_PORT=str(port),
    )
    env.pop("OPENBILICLAW_SELFTEST", None)
    evidence = {
        "os": sys.getwindowsversion().build,
        "commit": os.environ.get("GITHUB_SHA"),
        "published_setup_sha256": PUBLISHED_SHA,
        "rebuilt_setup_sha256": sha(new),
        "frozen_exe_sha256": sha(frozen),
        "checks": {},
    }

    def passed(name: str, value: object = True) -> None:
        evidence["checks"][name] = value
        evidence_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print(f"PASS {name}: {value}", flush=True)

    def api(path: str, body: dict | None = None, timeout: int = 25):
        request = Request(
            base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        return client.open(request, timeout=timeout)

    def get_json(path: str, body: dict | None = None, timeout: int = 25):
        with api(path, body, timeout) as response:
            return json.load(response)

    def alive() -> None:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                if get_json("/api/ping", timeout=5):
                    return
            except OSError:
                time.sleep(1)
        raise RuntimeError("Installed application did not start its HTTP server")

    def stop_app() -> None:
        subprocess.run(
            ["taskkill", "/F", "/T", "/IM", "OpenBiliClaw.exe"], capture_output=True, timeout=30
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    time.sleep(0.25)
            except OSError:
                return
        raise RuntimeError("Installed HTTP server did not exit")

    def setup(exe: Path, *args: str) -> subprocess.Popen:
        return subprocess.Popen([str(exe), "/SP-", "/LANG=default", *args], env=env)

    def installed_dir() -> Path:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            return Path(winreg.QueryValueEx(key, "Inno Setup: App Path")[0])

    def uninstall(directory: Path) -> None:
        assert installed_dir() == directory
        uninstaller = next(directory.glob("unins*.exe"))
        subprocess.run([str(uninstaller), *SILENT], env=env, check=True, timeout=150)
        assert not (directory / "OpenBiliClaw.exe").exists()

    with client.open(PUBLISHED, timeout=90) as response, old.open("wb") as file:
        import shutil

        shutil.copyfileobj(response, file)
    assert sha(old) == PUBLISHED_SHA
    wizard = Wizard()
    assert setup(old, *SILENT, f"/DIR={original}").wait(timeout=210) == 0
    assert installed_dir() == original and (original / "OpenBiliClaw.exe").is_file()
    alive()
    stop_app()
    try:
        previous = setup(old)
        controls = wizard.wait("Select Additional Tasks")
        assert not any(kind == "TNewPathEdit" for _, kind, _ in controls)
        wizard.click("Cancel")
        wizard.wait("Exit Setup?")
        wizard.click("Yes")
        assert previous.wait(timeout=30) == 2
        passed("published_interactive_upgrade_skips_destination")

        upgrade = setup(new)
        controls = wizard.wait("Select Destination Location")
        assert any(kind == "TNewPathEdit" and label == str(original) for _, kind, label in controls)
        assert any("Browse" in label.replace("&", "") for _, _, label in controls)
        passed("rebuild_prefills_registered_directory")
        wizard.set_path(relocated)
        wizard.click("Next")
        wizard.wait("Keep this folder to upgrade")
        wizard.click("OK")
        assert not relocated.exists() and installed_dir() == original
        passed("interactive_wrong_path_rejected")
        wizard.set_path(original)
        wizard.finish(upgrade)
        assert sha(original / "OpenBiliClaw.exe") == evidence["frozen_exe_sha256"]
        alive()
        health = get_json("/api/health", timeout=200)
        assert health["status"] == "ok" and health["llm_registered"]
        with api("/setup/") as response:
            assert response.status == 200 and len(response.read()) > 1024
        passed(
            "upgraded_binary_serves_http",
            {
                "status": health["status"],
                "embedding_ready": health.get("embedding_ready"),
            },
        )

        nonce = f"OBCTEST{uuid.uuid4().hex[:8].upper()}"
        session = get_json("/api/chat/sessions", {"title": "Windows installer E2E"})
        turn_id = f"installer-{uuid.uuid4().hex}"
        body = {
            "turn_id": turn_id,
            "session_id": session["session_id"],
            "session": "desktop",
            "streaming": True,
            "message": f"Reply with the exact text {nonce}. Do not use any tools.",
        }
        get_json("/api/chat/turns", body)
        events = {}
        event_name = ""
        reply = ""
        started = time.monotonic()
        with api("/api/chat/agent/stream", body, timeout=210) as stream:
            assert stream.headers.get_content_type() == "text/event-stream"
            for raw in stream:
                line = raw.decode("utf-8").strip()
                if line.startswith("event: "):
                    event_name = line[7:]
                elif line.startswith("data: "):
                    events[event_name] = events.get(event_name, 0) + 1
                    if event_name == "error":
                        raise RuntimeError("Real upstream SSE request returned an error")
                    if event_name == "done":
                        reply = json.loads(line[6:]).get("reply", "")
                        break
        assert nonce in reply and events.get("delta"), events
        turn = get_json(f"/api/chat/turns/{turn_id}")
        assert turn["status"] == "completed" and turn["reply"] == reply
        passed(
            "real_free_model_stream_persisted",
            {
                "seconds": round(time.monotonic() - started, 2),
                "events": events,
            },
        )

        stop_app()
        mismatch = setup(new, *SILENT, f"/DIR={relocated}").wait(timeout=60)
        assert mismatch != 0 and not relocated.exists() and installed_dir() == original
        passed("silent_wrong_path_rejected", mismatch)
        uninstall(original)
        assert (profile / "config.toml").is_file()
        fresh = setup(new)
        wizard.set_path(relocated)
        wizard.finish(fresh)
        assert installed_dir() == relocated
        assert sha(relocated / "OpenBiliClaw.exe") == evidence["frozen_exe_sha256"]
        alive()
        recovered = get_json(f"/api/chat/turns/{turn_id}")
        assert recovered["status"] == "completed" and recovered["reply"] == reply
        passed("reinstalled_in_unicode_path_with_persisted_chat")
        stop_app()
        uninstall(relocated)
    finally:
        stop_app()


if __name__ == "__main__":
    main()
