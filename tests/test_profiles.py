import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from duplicacy_fuse_gnome.profiles import Profile, load_profiles, save_profiles, validate_profile


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_round_trip_uses_private_atomic_file_and_no_password(self):
        path = self.root / "config" / "profiles.json"
        profile = Profile(name="Home", repository=str(self.repo), storage="nas")
        save_profiles([profile], path)

        loaded = load_profiles(path)
        self.assertEqual(loaded, [profile])
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertNotIn("password", path.read_text().casefold())
        self.assertEqual(profile.cache_bytes, 0)

    def test_rejects_password_fields_in_existing_profile_file(self):
        path = self.root / "config" / "profiles.json"
        path.parent.mkdir(mode=0o700)
        path.write_text(json.dumps([{"name": "x", "password": "must-not-load"}]))
        with self.assertRaisesRegex(ValueError, "passwords and credentials"):
            load_profiles(path)

    def test_rejects_nested_secret_fields(self):
        path = self.root / "config" / "profiles.json"
        path.parent.mkdir(mode=0o700)
        path.write_text(json.dumps([{"name": "x", "metadata": {"password": "must-not-load"}}]))
        with self.assertRaisesRegex(ValueError, "passwords and credentials"):
            load_profiles(path)

    def test_rejects_cache_inside_repository_and_parent_path(self):
        with self.assertRaisesRegex(ValueError, "cache must be outside"):
            validate_profile(Profile("Home", str(self.repo), "nas", cache_dir=str(self.repo / "cache")))
        with self.assertRaisesRegex(ValueError, "selected path"):
            validate_profile(Profile("Home", str(self.repo), "nas", selected_path="../outside"))

    def test_rejects_profile_symlink(self):
        path = self.root / "config" / "profiles.json"
        path.parent.mkdir(mode=0o700)
        other = self.root / "other.json"
        other.write_text("[]")
        path.symlink_to(other)
        with self.assertRaisesRegex(ValueError, "regular file"):
            load_profiles(path)


if __name__ == "__main__":
    unittest.main()
