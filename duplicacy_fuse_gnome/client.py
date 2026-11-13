from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Callable

from .profiles import Profile


EventCallback = Callable[[dict], None]
ExitCallback = Callable[[int, str, str], None]


def clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key in {"DUPLICACY_PASSWORD", "DUPLICACY_FUSE_BACKGROUND_CHILD", "DUPLICACY_FUSE_READY_FD"} or (
            key.startswith("DUPLICACY_")
            and key.endswith("_PASSWORD")
            and not key.endswith("_SMB_PASSWORD")
        ):
            environment.pop(key, None)
    return environment


def resolve_fuse_binary(value: str) -> str:
    executable = os.path.expanduser(value or "duplicacy-fuse")
    resolved = shutil.which(executable)
    if resolved is None:
        raise FileNotFoundError(f"Could not find {executable}. Install duplicacy-fuse or set its path in the profile.")
    return resolved


def list_storages(binary: str, repository: str) -> list[dict]:
    result = subprocess.run(
        [resolve_fuse_binary(binary), "gui-storages", "--repo", repository],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=clean_environment(),
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Could not load configured Duplicacy storages.")
    value = json.loads(result.stdout)
    if not isinstance(value, list):
        raise RuntimeError("The FUSE helper returned an invalid storage list.")
    return value


def runtime_mountpoint(profile_id: str) -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        raise RuntimeError("GNOME did not provide XDG_RUNTIME_DIR; choose a user-owned mountpoint.")
    runtime_path = Path(runtime)
    info = runtime_path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise RuntimeError("XDG_RUNTIME_DIR is not a private directory owned by the current user.")
    root = runtime_path / "duplicacy-fuse"
    root.mkdir(mode=0o700, exist_ok=True)
    root_info = root.lstat()
    if not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.getuid():
        raise RuntimeError("The runtime mountpoint parent is not a user-owned directory.")
    root.chmod(0o700)
    mountpoint = root / profile_id
    mountpoint.mkdir(mode=0o700, exist_ok=False)
    return mountpoint


def validate_custom_mountpoint(value: str) -> Path:
    mountpoint = Path(os.path.abspath(os.path.expanduser(value)))
    info = mountpoint.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise RuntimeError("Mountpoint must be a real, non-writable directory owned by the current user.")
    if any(mountpoint.iterdir()):
        raise RuntimeError("Mountpoint must be empty.")
    return mountpoint


class HelperProcess:
    def __init__(
        self,
        process: subprocess.Popen,
        control: socket.socket,
        on_event: EventCallback,
        on_exit: ExitCallback,
        capture_stdout: bool,
    ) -> None:
        self.process = process
        self.control = control
        self.on_event = on_event
        self.on_exit = on_exit
        self.capture_stdout = capture_stdout
        self._send_lock = threading.Lock()
        self._reader_thread = threading.Thread(target=self._read_events, daemon=True)
        self._reader_thread.start()
        threading.Thread(target=self._wait, daemon=True).start()

    def _read_events(self) -> None:
        try:
            stream = self.control.makefile("rb")
            try:
                for line in stream:
                    try:
                        message = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if isinstance(message, dict):
                        self.on_event(message)
            finally:
                stream.close()
        except OSError:
            pass

    def _wait(self) -> None:
        if self.capture_stdout:
            stdout, stderr = self.process.communicate()
            output = stdout.decode("utf-8", "replace") if stdout else ""
            detail = stderr.decode("utf-8", "replace") if stderr else ""
        else:
            code = self.process.wait()
            output, detail = "", ""
        if self.capture_stdout:
            code = self.process.returncode
        try:
            self.control.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.control.close()
        self._reader_thread.join()
        self.on_exit(code, output, detail)

    def _send(self, message: dict) -> None:
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        with self._send_lock:
            self.control.sendall(data)

    def send_password(self, password: str) -> None:
        self._send({"type": "password", "value": password})

    def cancel_password(self) -> None:
        self._send({"type": "cancel"})

    def unmount(self) -> None:
        self._send({"type": "unmount"})


def _spawn(
    binary: str,
    arguments: list[str],
    on_event: EventCallback,
    on_exit: ExitCallback,
    *,
    capture_stdout: bool = False,
) -> HelperProcess:
    local, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    command = [resolve_fuse_binary(binary), *arguments, "--gui-control-fd", str(child.fileno())]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            env=clean_environment(),
            pass_fds=(child.fileno(),),
            close_fds=True,
        )
    except Exception:
        local.close()
        child.close()
        raise
    child.close()
    return HelperProcess(process, local, on_event, on_exit, capture_stdout)


def start_mount(
    profile: Profile,
    on_event: EventCallback,
    on_exit: ExitCallback,
) -> tuple[HelperProcess, Path, bool]:
    if profile.comparison_revisions and (profile.reference_revision < 1 or not profile.selected_paths):
        raise ValueError("comparison mounts require a reference revision and at least one selected path")
    snapshot_id = profile.snapshot_id.strip()
    if not snapshot_id:
        raise ValueError("an explicit snapshot ID is required before mounting")
    created = not bool(profile.mountpoint)
    mountpoint = runtime_mountpoint(profile.id) if created else validate_custom_mountpoint(profile.mountpoint)
    binary = resolve_fuse_binary(profile.fuse_binary)
    arguments = [
        "mount",
        "--repo",
        profile.repository,
        "--storage",
        profile.storage,
        "--snapshot",
        snapshot_id,
    ]
    if profile.comparison_revisions:
        arguments.append("--compare")
        for revision in profile.comparison_revisions:
            arguments.extend(("--revision", str(revision)))
        for selected_path in profile.selected_paths:
            arguments.extend(("--path", selected_path))
    else:
        arguments.extend(("--revision", str(profile.revision)))
    arguments.extend(("--mountpoint", str(mountpoint), "--cache-size", str(profile.cache_bytes)))
    if not profile.comparison_revisions and profile.selected_path:
        arguments.extend(("--path", profile.selected_path))
    if profile.cache_dir:
        arguments.extend(("--cache-dir", profile.cache_dir))
    try:
        process = _spawn(binary, arguments, on_event, on_exit)
    except Exception:
        if created:
            try:
                mountpoint.rmdir()
            except OSError:
                pass
        raise
    return process, mountpoint, created


def validate_directory_entries(value: object) -> list[dict]:
    if not isinstance(value, list):
        raise RuntimeError("The FUSE helper returned an invalid directory listing.")
    for entry in value:
        if not isinstance(entry, Mapping):
            raise RuntimeError("The FUSE helper returned an invalid directory entry.")
        if (
            not isinstance(entry.get("name"), str)
            or not entry["name"]
            or not isinstance(entry.get("path"), str)
            or not entry["path"]
            or PurePosixPath(entry["path"]).is_absolute()
            or ".." in PurePosixPath(entry["path"]).parts
            or not isinstance(entry.get("type"), str)
            or entry["type"] not in {"directory", "file", "symlink"}
            or not isinstance(entry.get("size"), int)
            or isinstance(entry["size"], bool)
            or entry["size"] < 0
        ):
            raise RuntimeError("The FUSE helper returned an invalid directory entry.")
    return [dict(entry) for entry in value]


def browse_snapshot(
    profile: Profile,
    revision: int,
    path: str,
    on_event: EventCallback,
    on_exit: ExitCallback,
) -> HelperProcess:
    arguments = [
        "gui-browse",
        "--repo",
        profile.repository,
        "--storage",
        profile.storage,
        "--snapshot",
        profile.snapshot_id,
        "--revision",
        str(revision),
        "--path",
        path,
    ]
    return _spawn(profile.fuse_binary, arguments, on_event, on_exit)


def list_revisions(
    profile: Profile,
    on_event: EventCallback,
    on_exit: ExitCallback,
) -> HelperProcess:
    arguments = [
        "gui-revisions",
        "--repo",
        profile.repository,
        "--storage",
        profile.storage,
    ]
    if profile.snapshot_id:
        arguments.extend(("--snapshot", profile.snapshot_id))
    return _spawn(profile.fuse_binary, arguments, on_event, on_exit, capture_stdout=True)


def list_snapshots(
    profile: Profile,
    on_event: EventCallback,
    on_exit: ExitCallback,
) -> HelperProcess:
    arguments = [
        "gui-snapshots",
        "--repo",
        profile.repository,
        "--storage",
        profile.storage,
    ]
    return _spawn(profile.fuse_binary, arguments, on_event, on_exit, capture_stdout=True)


def clear_cache(binary: str, cache_dir: str = "") -> None:
    arguments = [resolve_fuse_binary(binary), "clear-cache"]
    if cache_dir:
        arguments.extend(("--cache-dir", cache_dir))
    result = subprocess.run(arguments, check=False, capture_output=True, text=True, timeout=60, env=clean_environment())
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Could not clear the chunk cache.")
