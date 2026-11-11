# Duplicacy FUSE for GNOME

This is a separate GNOME desktop companion for the standalone `duplicacy-fuse` CLI. It keeps GTK/libadwaita dependencies and desktop packaging out of the FUSE project. The companion requires a CLI build that provides `gui-storages`, `gui-snapshots`, `gui-revisions`, and the `mount --gui-control-fd` helper interface.

## Requirements

- Linux with GNOME, GTK 4, libadwaita, and Python 3.11 or newer
- PyGObject GTK 4/libadwaita bindings from the operating system packages
- `duplicacy-fuse` built with FUSE 2 support and available on `PATH` (or configured in a profile)
- User access to `/dev/fuse` and `fusermount`; no root service is used

On Debian/Ubuntu, install the system bindings and FUSE tools with:

```sh
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 fuse
```

Install the companion in a virtual environment that can import the system PyGObject bindings, or install it with the system Python package manager:

```sh
python3 -m pip install .
```

Install `data/com.duplicacy.Fuse.desktop` under `~/.local/share/applications/` to expose the app in the GNOME app grid.

## Security and lifecycle

Profiles live in `$XDG_CONFIG_HOME/duplicacy-fuse-gnome/profiles.json` with owner-only permissions. Profiles contain no passwords. Encrypted storage passwords are requested for every mount and sent over a private inherited Unix socket to the CLI helper, never in command-line arguments, environment variables, logs, notifications, or profile data. Duplicacy encryption password environment variables are removed from helper environments; separate SMB credentials remain available to the storage backend.

Closing the window hides it while active mounts remain supervised in the user session. Selecting Quit asks the helper to unmount all active mounts. There is no login auto-mount. Runtime mountpoints are created below `$XDG_RUNTIME_DIR/duplicacy-fuse/`; custom mountpoints must be empty private directories owned by the user.

Persistent decrypted chunk caching is disabled by default. Enabling it is an explicit opt-in; cached data is plaintext, and clearing a cache is not a guarantee of secure deletion. Read-only mounts are not a sandbox: archived symlinks may resolve outside the mount.

## Development

Run the profile/security tests with:

```sh
python3 -m unittest discover -s tests -v
```

The GTK application can be launched from the project root with:

```sh
python3 -m duplicacy_fuse_gnome.app
```
