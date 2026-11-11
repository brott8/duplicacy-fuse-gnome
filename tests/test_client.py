import os
import json
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from duplicacy_fuse_gnome.client import clean_environment, runtime_mountpoint, start_mount
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


if __name__ == "__main__":
    unittest.main()
