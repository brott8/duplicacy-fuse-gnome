from __future__ import annotations

import json
import os
import stat
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class Profile:
    name: str
    repository: str
    storage: str
    snapshot_id: str = ""
    revision: int = 0
    selected_path: str = ""
    mountpoint: str = ""
    cache_bytes: int = 0
    cache_dir: str = ""
    fuse_binary: str = "duplicacy-fuse"
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = str(uuid.uuid4())


def config_directory() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "duplicacy-fuse-gnome"


def profiles_path() -> Path:
    return config_directory() / "profiles.json"


def _secure_config_directory(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("profile configuration path must be a real directory")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError("profile configuration directory must be owned by the current user")
    directory.chmod(0o700)


def validate_profile(profile: Profile) -> None:
    profile.name = profile.name.strip()
    profile.repository = os.path.abspath(os.path.expanduser(profile.repository.strip())) if profile.repository.strip() else ""
    profile.storage = profile.storage.strip()
    profile.snapshot_id = profile.snapshot_id.strip()
    profile.selected_path = profile.selected_path.strip()
    profile.mountpoint = os.path.abspath(os.path.expanduser(profile.mountpoint.strip())) if profile.mountpoint.strip() else ""
    profile.cache_dir = os.path.abspath(os.path.expanduser(profile.cache_dir.strip())) if profile.cache_dir.strip() else ""
    profile.fuse_binary = profile.fuse_binary.strip() or "duplicacy-fuse"

    try:
        if str(uuid.UUID(profile.id)) != profile.id:
            raise ValueError
    except (ValueError, AttributeError):
        raise ValueError("profile identifier is invalid") from None
    if not profile.name or len(profile.name) > 80:
        raise ValueError("profile name must contain 1 to 80 characters")
    if not profile.repository:
        raise ValueError("repository directory is required")
    if not profile.storage:
        raise ValueError("a configured storage must be selected")
    if not isinstance(profile.revision, int) or profile.revision < 0:
        raise ValueError("revision must not be negative")
    if not isinstance(profile.cache_bytes, int) or profile.cache_bytes < 0:
        raise ValueError("cache size must not be negative")
    if os.path.isabs(profile.selected_path):
        raise ValueError("selected path must be relative to the repository")
    components = Path(profile.selected_path).parts
    if ".." in components:
        raise ValueError("selected path must remain inside the repository")
    if profile.cache_dir:
        repository = os.path.realpath(profile.repository)
        cache = os.path.realpath(profile.cache_dir)
        if os.path.commonpath((repository, cache)) == repository:
            raise ValueError("chunk cache must be outside the repository")


def _from_dict(value: dict[str, Any]) -> Profile:
    if _contains_secret_field(value):
        raise ValueError("passwords and credentials are not allowed in saved profiles")
    allowed = set(Profile.__dataclass_fields__)
    return Profile(**{key: value[key] for key in value if key in allowed})


def _contains_secret_field(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = key.casefold()
            if "password" in normalized or normalized in {"secret", "credential"}:
                return True
            if _contains_secret_field(child):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_field(item) for item in value)
    return False


def load_profiles(path: Path | None = None) -> list[Profile]:
    target = path or profiles_path()
    _secure_config_directory(target.parent)
    try:
        info = target.lstat()
    except FileNotFoundError:
        return []
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("profile file must be a regular file, not a symlink")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError("profile file must be owned by the current user")
    target.chmod(0o600)
    raw = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("profile file must contain a JSON array")
    profiles = [_from_dict(item) for item in raw if isinstance(item, dict)]
    if len(profiles) != len(raw):
        raise ValueError("profile file contains an invalid entry")
    for profile in profiles:
        validate_profile(profile)
    return profiles


def save_profiles(profiles: list[Profile], path: Path | None = None) -> None:
    target = path or profiles_path()
    _secure_config_directory(target.parent)
    for profile in profiles:
        validate_profile(profile)
    data = json.dumps([asdict(profile) for profile in profiles], indent=2, sort_keys=True) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=".profiles-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
