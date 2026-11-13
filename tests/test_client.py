import os
import json
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from duplicacy_fuse_gnome.client import (
    browse_snapshot,
    clean_environment,
    runtime_mountpoint,
    start_mount,
    validate_directory_entries,
)
from duplicacy_fuse_gnome.profiles import Profile


class ClientSecurityTests(unittest.TestCase):
    def test_helper_environment_drops_encryption_passwords_but_keeps_smb_credentials(self):
        values = {
            "DUPLICACY_PASSWORD": "backup-password",
            "DUPLICACY_NAS_PASSWORD": "storage-password",
            "DUPLICACY_NAS_SMB_PASSWORD": "smb-password",
            "DUPLICACY_FUSE_READY_FD": "4",
            "PATH": os.environ.get("PATH", ""),
        }
        with patch.dict(os.environ, values, clear=False):
            environment = clean_environment()
        self.assertNotIn("DUPLICACY_PASSWORD", environment)
        self.assertNotIn("DUPLICACY_NAS_PASSWORD", environment)
        self.assertNotIn("DUPLICACY_FUSE_READY_FD", environment)
        self.assertEqual(environment["DUPLICACY_NAS_SMB_PASSWORD"], "smb-password")

    def test_runtime_mountpoints_are_private_and_profile_scoped(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "runtime"
            runtime.mkdir(mode=0o700)
            runtime.chmod(0o700)
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
                mountpoint = runtime_mountpoint("a3b2c1d0-1234-5678-9012-123456789abc")
            self.assertEqual(mountpoint.parent.name, "duplicacy-fuse")
            self.assertEqual(stat.S_IMODE(mountpoint.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(mountpoint.stat().st_mode), 0o700)

    def test_mount_helper_gets_password_only_over_inherited_socket(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            repository = root / "repo"
            repository.mkdir()
            helper = root / "fake-fuse"
            helper.write_text(
                "#!" + os.sys.executable + "\n"
                "import json, os, socket, sys\n"
                "assert '--password' not in sys.argv\n"
                "assert 'DUPLICACY_PASSWORD' not in os.environ\n"
                "assert 'DUPLICACY_NAS_PASSWORD' not in os.environ\n"
                "fd = int(sys.argv[sys.argv.index('--gui-control-fd') + 1])\n"
                "sock = socket.socket(fileno=fd)\n"
                "sock.sendall(b'{\\\"type\\\":\\\"password-required\\\",\\\"attempt\\\":1}\\n')\n"
                "reader = sock.makefile('rb')\n"
                "response = json.loads(reader.readline())\n"
                "if response.get('type') != 'password' or response.get('value') != 'entered-once': sys.exit(7)\n"
                "sock.sendall(b'{\\\"type\\\":\\\"mounted\\\"}\\n')\n"
                "sock.close()\n"
            )
            helper.chmod(0o700)
            events = []
            exited = threading.Event()
            result = []
            profile = Profile(
                name="Test",
                repository=str(repository),
                storage="nas",
                snapshot_id="test-snapshot",
                revision=1,
                fuse_binary=str(helper),
            )

            with patch.dict(
                os.environ,
                {
                    "XDG_RUNTIME_DIR": str(runtime),
                    "DUPLICACY_PASSWORD": "inherited-secret",
                    "DUPLICACY_NAS_PASSWORD": "stored-secret",
                },
            ):
                handle, mountpoint, created = start_mount(
                    profile,
                    lambda event: events.append(event),
                    lambda code, _output, _detail: (result.append(code), exited.set()),
                )
                deadline = threading.Event()
                for _ in range(200):
                    if events:
                        break
                    deadline.wait(0.01)
                self.assertTrue(events, "helper did not request the password")
                self.assertEqual(events[0]["type"], "password-required")
                handle.send_password("entered-once")
                self.assertTrue(exited.wait(5), "helper did not exit")
            self.assertEqual(result, [0])
            self.assertIn({"type": "mounted"}, events)
            self.assertTrue(created)
            mountpoint.rmdir()

    def test_legacy_mount_keeps_scalar_arguments(self):
        profile = Profile(
            name="Legacy",
            repository="/repo",
            storage="nas",
            snapshot_id="snapshot",
            revision=9,
            selected_path="legacy/path",
        )
        with patch("duplicacy_fuse_gnome.client.runtime_mountpoint", return_value=Path("/mount")), patch(
            "duplicacy_fuse_gnome.client.resolve_fuse_binary", return_value="/helper"
        ), patch("duplicacy_fuse_gnome.client._spawn", return_value="handle") as spawn:
            start_mount(profile, lambda _event: None, lambda *_args: None)
        arguments = spawn.call_args.args[1]
        self.assertEqual(arguments[arguments.index("--revision") + 1], "9")
        self.assertEqual(arguments[arguments.index("--path") + 1], "legacy/path")
        self.assertEqual(arguments.count("--revision"), 1)

    def test_mount_requires_an_explicit_snapshot_id(self):
        profile = Profile(name="No snapshot", repository="/repo", storage="nas", revision=1)
        with patch("duplicacy_fuse_gnome.client.runtime_mountpoint") as create_mountpoint:
            with self.assertRaisesRegex(ValueError, "explicit snapshot ID"):
                start_mount(profile, lambda _event: None, lambda *_args: None)
        create_mountpoint.assert_not_called()

    def test_comparison_mount_repeats_revisions_and_paths_independently(self):
        profile = Profile(
            name="Compare",
            repository="/repo",
            storage="nas",
            snapshot_id="snapshot",
            revision=9,
            reference_revision=2,
            comparison_revisions=[3, 5],
            selected_paths=["./folder/a.txt", "space name/file"],
        )
        with patch("duplicacy_fuse_gnome.client.runtime_mountpoint", return_value=Path("/mount")), patch(
            "duplicacy_fuse_gnome.client.resolve_fuse_binary", return_value="/helper"
        ), patch("duplicacy_fuse_gnome.client._spawn", return_value="handle") as spawn:
            start_mount(profile, lambda _event: None, lambda *_args: None)
        arguments = spawn.call_args.args[1]
        values = [arguments[index + 1] for index, value in enumerate(arguments[:-1]) if value == "--revision"]
        paths = [arguments[index + 1] for index, value in enumerate(arguments[:-1]) if value == "--path"]
        self.assertIn("--compare", arguments)
        self.assertEqual(values, ["3", "5"])
        self.assertEqual(paths, ["./folder/a.txt", "space name/file"])
        self.assertIn("--mountpoint", arguments)
        self.assertIn("--cache-size", arguments)

    def test_comparison_mount_requires_reference_and_selected_paths(self):
        for profile in (
            Profile(name="Compare", repository="/repo", storage="nas", comparison_revisions=[3], selected_paths=["a"]),
            Profile(
                name="Compare",
                repository="/repo",
                storage="nas",
                reference_revision=2,
                comparison_revisions=[3],
            ),
        ):
            with self.subTest(profile=profile), self.assertRaisesRegex(ValueError, "comparison mounts require"):
                start_mount(profile, lambda _event: None, lambda *_args: None)

    def test_browse_operation_uses_fixed_helper_contract(self):
        profile = Profile(
            name="Browse", repository="/repo", storage="nas", snapshot_id="snap", fuse_binary="/helper"
        )
        with patch("duplicacy_fuse_gnome.client._spawn", return_value="handle") as spawn:
            result = browse_snapshot(profile, 6, "dir/sub dir", lambda _event: None, lambda *_args: None)
        self.assertEqual(result, "handle")
        self.assertEqual(
            spawn.call_args.args[1],
            [
                "gui-browse",
                "--repo",
                "/repo",
                "--storage",
                "nas",
                "--snapshot",
                "snap",
                "--revision",
                "6",
                "--path",
                "dir/sub dir",
            ],
        )

    def test_browse_directory_entries_are_validated_without_path_rewriting(self):
        entries = [
            {"name": "name with spaces", "path": "./folder/name with spaces", "type": "file", "size": 4},
            {"name": "link", "path": "link", "type": "symlink", "size": 1},
        ]
        self.assertEqual(validate_directory_entries(entries), entries)
        for invalid in (
            {"name": "x", "path": "x", "type": "other", "size": 1},
            {"name": "x", "path": "x", "type": "directory", "size": True},
            {"name": "x", "path": "x", "type": "file"},
        ):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(RuntimeError, "invalid directory entry"):
                validate_directory_entries([invalid])
        with self.assertRaisesRegex(RuntimeError, "invalid directory listing"):
            validate_directory_entries({"entries": []})


if __name__ == "__main__":
    unittest.main()
