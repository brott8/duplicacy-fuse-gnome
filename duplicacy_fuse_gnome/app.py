from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from . import client
from .profiles import Profile, load_profiles, save_profiles, validate_profile


class MountWindow(Adw.ApplicationWindow):
    def __init__(self, application: "DuplicacyFuseApp") -> None:
        super().__init__(application=application)
        self.application = application
        self.set_title("Duplicacy FUSE")
        self.set_default_size(980, 720)
        self.connect("close-request", self._hide_on_close)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label="Duplicacy FUSE"))
        add_button = Gtk.Button(label="New Setup")
        add_button.add_css_class("suggested-action")
        add_button.connect("clicked", lambda *_: self.edit_profile(None))
        header.pack_start(add_button)

        menu = Gio.Menu()
        menu.append("Quit and unmount", "app.quit")
        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name("open-menu-symbolic")
        menu_button.set_menu_model(menu)
        header.pack_end(menu_button)
        toolbar.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        content.set_margin_top(20)
        content.set_margin_bottom(20)
        content.set_margin_start(24)
        content.set_margin_end(24)
        notice = Gtk.Label(
            label=(
                "Snapshots are mounted read-only, not sandboxed. Duplicacy may still ensure snapshot metadata directories on remote storage, "
                "and an archived symlink may lead outside the mount. "
                "Persistent chunk caching stores decrypted plaintext and is disabled by default."
            )
        )
        notice.set_wrap(True)
        notice.set_xalign(0)
        notice.add_css_class("card")
        notice.set_margin_top(4)
        notice.set_margin_bottom(4)
        content.append(notice)

        section = Gtk.Label(label="Mount Profiles")
        section.set_xalign(0)
        section.add_css_class("title-2")
        content.append(section)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.profile_list = Gtk.ListBox()
        self.profile_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.profile_list.add_css_class("boxed-list")
        scroller.set_child(self.profile_list)
        content.append(scroller)

        self.empty_label = Gtk.Label(label="Add a repository setup to begin.")
        self.empty_label.add_css_class("dim-label")
        content.append(self.empty_label)
        self.profile_error = Gtk.Label(label=application.profile_load_error)
        self.profile_error.set_wrap(True)
        self.profile_error.add_css_class("error")
        self.profile_error.set_visible(bool(application.profile_load_error))
        content.append(self.profile_error)
        toolbar.set_content(content)
        self.set_content(toolbar)
        self.refresh_profiles()

    def _hide_on_close(self, *_args) -> bool:
        self.set_visible(False)
        return True

    def refresh_profiles(self) -> None:
        child = self.profile_list.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.profile_list.remove(child)
            child = following
        profiles = self.application.profiles
        self.empty_label.set_visible(not profiles)
        for profile in profiles:
            active = self.application.mounts.get(profile.id)
            state = active["state"] if active else "Not mounted"
            mountpoint = (
                str(active["mountpoint"])
                if active and active.get("mountpoint")
                else profile.mountpoint or "Session runtime mountpoint"
            )
            detail = (
                f"{profile.repository}\n"
                f"{profile.storage} · snapshot {profile.snapshot_id or 'not selected'} · {state}\n"
                f"Mountpoint: {mountpoint}"
            )
            row = Adw.ActionRow(title=profile.name, subtitle=detail)
            row.set_subtitle_lines(3)
            controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            if active:
                unmount = Gtk.Button(label="Unmount")
                unmount.set_sensitive(active.get("handle") is not None)
                unmount.connect("clicked", lambda _button, item=profile: self.application.unmount(item.id))
                controls.append(unmount)
            else:
                mount = Gtk.Button(label="Mount")
                mount.add_css_class("suggested-action")
                mount.set_sensitive(profile.revision > 0)
                mount.connect("clicked", lambda _button, item=profile: self.application.mount(item))
                controls.append(mount)
            edit = Gtk.Button.new_from_icon_name("document-edit-symbolic")
            edit.set_tooltip_text("Edit setup")
            edit.connect("clicked", lambda _button, item=profile: self.edit_profile(item))
            controls.append(edit)
            clear = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            clear.set_tooltip_text("Clear cached plaintext")
            clear.set_sensitive(not active)
            clear.connect("clicked", lambda _button, item=profile: self.application.confirm_clear_cache(item))
            controls.append(clear)
            remove = Gtk.Button.new_from_icon_name("edit-delete-symbolic")
            remove.set_tooltip_text("Remove setup")
            remove.set_sensitive(not active)
            remove.connect("clicked", lambda _button, item=profile: self.application.confirm_remove(item))
            controls.append(remove)
            row.add_suffix(controls)
            self.profile_list.append(row)

    def edit_profile(self, profile: Profile | None) -> None:
        ProfileEditor(self, profile)


class ProfileEditor(Gtk.Window):
    def __init__(self, parent: MountWindow, profile: Profile | None) -> None:
        super().__init__(transient_for=parent, modal=True)
        self.parent = parent
        self.application = parent.application
        self.profile = profile
        self.set_title("Edit Mount Setup" if profile else "New Mount Setup")
        self.set_default_size(700, 700)
        self.storage_data: list[dict] = []
        self.snapshot_ids: list[str] = []
        self.snapshot_storage = profile.storage if profile else ""
        self.revision_values: list[int] = []
        self.revision_storage = profile.storage if profile else ""
        self.revision_snapshot_id = profile.snapshot_id if profile else ""
        self.revision_repository = os.path.realpath(profile.repository) if profile else ""
        self.pending_revision_storage = ""
        self.pending_revision_snapshot = ""
        self.pending_revision_repository = ""
        self.fields: dict[str, Gtk.Entry] = {}

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        root.set_margin_top(20)
        root.set_margin_bottom(20)
        root.set_margin_start(24)
        root.set_margin_end(24)
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        form = Gtk.Grid(column_spacing=12, row_spacing=12)
        form.set_column_homogeneous(False)
        form.set_hexpand(True)
        scroller.set_child(form)
        root.append(scroller)
        self.form = form
        self.row = 0

        self._add_entry("Setup name", "name", profile.name if profile else "")
        repo_row = self._add_entry("Repository directory", "repository", profile.repository if profile else "")
        self._add_browse(repo_row, "repository")
        self._add_drop_down("Configured storage", "storage", [])
        load_storage = Gtk.Button(label="Load repository settings")
        load_storage.connect("clicked", self.load_storages)
        form.attach(load_storage, 1, self.row, 1, 1)
        self.row += 1
        self.snapshot_model = Gtk.StringList.new(["Load snapshot IDs"])
        self.snapshot_selector = Gtk.DropDown.new(self.snapshot_model, None)
        self.snapshot_selector.set_hexpand(True)
        self.snapshot_selector.set_enable_search(True)
        self.snapshot_selector.connect("notify::selected", self.snapshot_selected)
        snapshot_row = self._add_widget("Snapshot ID", self.snapshot_selector)
        snapshots_button = Gtk.Button(label="Load snapshot IDs")
        snapshots_button.connect("clicked", self.load_snapshots)
        form.attach(snapshots_button, 1, snapshot_row + 1, 1, 1)
        self.row += 1
        self.revision_model = Gtk.StringList.new(["Select a revision"])
        self.revision_selector = Gtk.DropDown.new(self.revision_model, None)
        self.revision_selector.set_hexpand(True)
        revision_row = self._add_widget("Revision", self.revision_selector)
        revisions_button = Gtk.Button(label="Load revisions")
        revisions_button.connect("clicked", self.load_revisions)
        self.form.attach(revisions_button, 1, revision_row + 1, 1, 1)
        self.row += 1
        self._add_entry("Repository-relative path (optional)", "selected_path", profile.selected_path if profile else "")
        mount_row = self._add_entry("Mountpoint (optional)", "mountpoint", profile.mountpoint if profile else "")
        self._add_browse(mount_row, "mountpoint")
        cache_row = self._add_entry("Chunk cache directory (optional)", "cache_dir", profile.cache_dir if profile else "")
        self._add_browse(cache_row, "cache_dir")
        self._add_entry("duplicacy-fuse executable", "fuse_binary", profile.fuse_binary if profile else "duplicacy-fuse")

        self.cache_toggle = Gtk.CheckButton(label="Persistently cache decrypted chunks (plaintext)")
        self.cache_toggle.set_active(bool(profile and profile.cache_bytes))
        self.form.attach(self.cache_toggle, 1, self.row, 1, 1)
        self.row += 1
        cache_gib = profile.cache_bytes / (1024**3) if profile and profile.cache_bytes else 10
        self.cache_size = Gtk.SpinButton.new_with_range(1, 1024, 1)
        self.cache_size.set_value(cache_gib)
        self._add_widget("Maximum cache (GiB)", self.cache_size)

        self.error_label = Gtk.Label()
        self.error_label.set_wrap(True)
        self.error_label.set_xalign(0)
        self.error_label.add_css_class("error")
        self.error_label.set_visible(False)
        root.append(self.error_label)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        save = Gtk.Button(label="Save Setup")
        save.add_css_class("suggested-action")
        save.connect("clicked", self.save_profile)
        actions.append(cancel)
        actions.append(save)
        root.append(actions)
        self.set_child(root)

        if profile:
            self._set_revision(profile.revision)
            self._load_stored_storage(profile.storage, profile.snapshot_id)
        self.present()

    def _add_widget(self, label: str, widget: Gtk.Widget) -> int:
        current = self.row
        text = Gtk.Label(label=label)
        text.set_xalign(0)
        text.set_valign(Gtk.Align.CENTER)
        self.form.attach(text, 0, current, 1, 1)
        self.form.attach(widget, 1, current, 1, 1)
        self.row += 1
        return current

    def _add_entry(self, label: str, key: str, value: str) -> int:
        entry = Gtk.Entry()
        entry.set_text(value)
        entry.set_hexpand(True)
        self.fields[key] = entry
        return self._add_widget(label, entry)

    def _add_drop_down(self, label: str, key: str, values: list[str]) -> int:
        model = Gtk.StringList.new(values or ["Load repository settings"])
        dropdown = Gtk.DropDown.new(model, None)
        dropdown.set_hexpand(True)
        dropdown.set_enable_search(True)
        dropdown.connect("notify::selected", self.storage_selected)
        self.storage_dropdown = dropdown
        self.storage_model = model
        return self._add_widget(label, dropdown)

    def _add_browse(self, row: int, key: str) -> None:
        button = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Choose directory")
        button.connect("clicked", lambda *_: self.choose_directory(key))
        self.form.attach(button, 2, row, 1, 1)

    def _load_stored_storage(self, name: str, snapshot_id: str) -> None:
        if name:
            self.storage_data = [{"name": name, "snapshot_id": snapshot_id}]
            self.storage_model = Gtk.StringList.new([name])
            self.storage_dropdown.set_model(self.storage_model)
            self.storage_dropdown.set_selected(0)

    def _set_snapshot_ids(self, snapshot_ids: list[str], selected: str = "") -> None:
        self.snapshot_ids = snapshot_ids
        labels = snapshot_ids or ["No snapshot IDs found"]
        self.snapshot_model = Gtk.StringList.new(labels)
        self.snapshot_selector.set_model(self.snapshot_model)
        if snapshot_ids:
            index = snapshot_ids.index(selected) if selected in snapshot_ids else 0
            self.snapshot_selector.set_selected(index)

    def _selected_snapshot_id(self) -> str:
        index = self.snapshot_selector.get_selected()
        return self.snapshot_ids[index] if index < len(self.snapshot_ids) else ""

    def _clear_revision_choices(self) -> None:
        self.revision_values = []
        self.revision_storage = ""
        self.revision_snapshot_id = ""
        self.revision_repository = ""
        self.revision_model = Gtk.StringList.new(["Load revisions"])
        self.revision_selector.set_model(self.revision_model)

    def snapshot_selected(self, dropdown: Gtk.DropDown, _property) -> None:
        index = dropdown.get_selected()
        snapshot_id = self.snapshot_ids[index] if index < len(self.snapshot_ids) else ""
        if snapshot_id and snapshot_id != self.revision_snapshot_id:
            self._clear_revision_choices()

    def storage_selected(self, dropdown: Gtk.DropDown, _property) -> None:
        index = dropdown.get_selected()
        if index < len(self.storage_data):
            storage_name = self.storage_data[index].get("name", "")
            configured_snapshot = self.storage_data[index].get("snapshot_id", "")
            if storage_name != self.snapshot_storage:
                self.snapshot_storage = storage_name
                self._clear_revision_choices()
                self._set_snapshot_ids([configured_snapshot] if configured_snapshot else [], configured_snapshot)
            elif not self.snapshot_ids and configured_snapshot:
                self._set_snapshot_ids([configured_snapshot], configured_snapshot)

    def choose_directory(self, key: str) -> None:
        chooser = Gtk.FileChooserNative.new(
            "Choose directory",
            self,
            Gtk.FileChooserAction.SELECT_FOLDER,
            "_Select",
            "_Cancel",
        )

        def response(dialog, response_id):
            if response_id == Gtk.ResponseType.ACCEPT:
                selected = dialog.get_file()
                if selected and selected.get_path():
                    self.fields[key].set_text(selected.get_path())
            dialog.destroy()

        chooser.connect("response", response)
        chooser.show()

    def load_storages(self, *_args) -> None:
        repository = self.fields["repository"].get_text().strip()
        binary = self.fields["fuse_binary"].get_text().strip() or "duplicacy-fuse"

        def worker():
            try:
                values = client.list_storages(binary, repository)
                GLib.idle_add(self._set_storages, values, "")
            except Exception as error:
                GLib.idle_add(self._set_storages, [], str(error))

        threading.Thread(target=worker, daemon=True).start()

    def _set_storages(self, values: list[dict], error: str) -> bool:
        if error:
            self.show_error(error)
            return GLib.SOURCE_REMOVE
        self.storage_data = values
        names = [str(item["name"]) for item in values]
        repository = os.path.realpath(self.fields["repository"].get_text().strip())
        same_repository = self.profile and repository == os.path.realpath(self.profile.repository)
        if same_repository and self.profile.storage and self.profile.storage not in names:
            self.storage_data.insert(0, {"name": self.profile.storage, "snapshot_id": self.profile.snapshot_id})
            names.insert(0, self.profile.storage)
        self.storage_model = Gtk.StringList.new(names or ["No configured storage found"])
        self.storage_dropdown.set_model(self.storage_model)
        if names:
            selected = self.profile.storage if self.profile else names[0]
            self.storage_dropdown.set_selected(names.index(selected) if selected in names else 0)
        else:
            self.show_error("This repository has no configured Duplicacy storage.")
        return GLib.SOURCE_REMOVE

    def load_revisions(self, *_args) -> None:
        selected = self.storage_dropdown.get_selected()
        if selected >= len(self.storage_data):
            self.show_error("Load and select a configured storage first.")
            return
        snapshot_id = self._selected_snapshot_id()
        if not snapshot_id:
            self.show_error("Load snapshot IDs and select a snapshot first.")
            return
        storage = self.storage_data[selected]
        profile = self._draft_profile()
        profile.storage = storage["name"]
        profile.snapshot_id = snapshot_id
        self.pending_revision_storage = profile.storage
        self.pending_revision_snapshot = profile.snapshot_id
        self.pending_revision_repository = os.path.realpath(profile.repository)
        self.show_error("")
        self.application.run_revisions(profile, self.revisions_loaded)

    def load_snapshots(self, *_args) -> None:
        selected = self.storage_dropdown.get_selected()
        if selected >= len(self.storage_data):
            self.show_error("Load and select a configured storage first.")
            return
        storage = self.storage_data[selected]
        profile = self._draft_profile()
        profile.storage = storage["name"]
        self.show_error("")
        self.application.run_snapshots(profile, self.snapshots_loaded)

    def snapshots_loaded(self, snapshot_ids: list[str] | None, error: str) -> None:
        if error:
            self.show_error(error)
            return
        selected = self.storage_dropdown.get_selected()
        configured_snapshot = self.storage_data[selected].get("snapshot_id", "") if selected < len(self.storage_data) else ""
        previous_snapshot = self._selected_snapshot_id()
        available = snapshot_ids or []
        preferred = previous_snapshot if previous_snapshot in available else configured_snapshot
        self._set_snapshot_ids(available, preferred)
        self.snapshot_storage = self.storage_data[selected]["name"] if selected < len(self.storage_data) else ""
        if not available:
            self.show_error("No snapshot IDs were found in this storage.")
        elif configured_snapshot and configured_snapshot not in available:
            self.show_error("The configured snapshot ID was not found in this storage. Select one of the listed IDs.")

    def revisions_loaded(self, revisions: list[dict] | None, error: str) -> None:
        if error:
            self.show_error(error)
            return
        self.revision_storage = self.pending_revision_storage
        self.revision_snapshot_id = self.pending_revision_snapshot
        self.revision_repository = self.pending_revision_repository
        self.revision_values = [int(item["revision"]) for item in revisions or []]
        labels = [
            f"Revision {item['revision']} · {datetime.fromtimestamp(int(item['created_at'])).astimezone().strftime('%Y-%m-%d %H:%M')}"
            for item in revisions or []
        ]
        self.revision_model = Gtk.StringList.new(labels or ["No revisions found"])
        self.revision_selector.set_model(self.revision_model)
        if self.profile and self.profile.revision in self.revision_values:
            self.revision_selector.set_selected(self.revision_values.index(self.profile.revision))
        elif self.revision_values:
            self.revision_selector.set_selected(len(self.revision_values) - 1)

    def _set_revision(self, revision: int) -> None:
        if revision > 0:
            self.revision_values = [revision]
            self.revision_storage = self.profile.storage
            self.revision_snapshot_id = self.profile.snapshot_id
            self.revision_repository = os.path.realpath(self.profile.repository)
            self.revision_model = Gtk.StringList.new([f"Revision {revision} (saved)"])
            self.revision_selector.set_model(self.revision_model)
            self.revision_selector.set_selected(0)

    def _draft_profile(self) -> Profile:
        selected = self.storage_dropdown.get_selected()
        storage = self.storage_data[selected]["name"] if selected < len(self.storage_data) else ""
        return Profile(
            name=self.fields["name"].get_text(),
            repository=self.fields["repository"].get_text(),
            storage=storage,
            snapshot_id=self._selected_snapshot_id(),
            selected_path=self.fields["selected_path"].get_text(),
            mountpoint=self.fields["mountpoint"].get_text(),
            cache_bytes=0,
            cache_dir=self.fields["cache_dir"].get_text(),
            fuse_binary=self.fields["fuse_binary"].get_text(),
            id=self.profile.id if self.profile else "",
        )

    def save_profile(self, *_args) -> None:
        try:
            profile = self._draft_profile()
            revision_index = self.revision_selector.get_selected()
            if (
                revision_index < len(self.revision_values)
                and self.revision_storage == profile.storage
                and self.revision_snapshot_id == profile.snapshot_id
                and self.revision_repository == os.path.realpath(profile.repository)
            ):
                profile.revision = self.revision_values[revision_index]
            if self.cache_toggle.get_active():
                profile.cache_bytes = int(self.cache_size.get_value() * 1024**3)
            validate_profile(profile)
            self.application.save_profile(profile)
        except Exception as error:
            self.show_error(str(error))
            return
        self.close()

    def show_error(self, message: str) -> None:
        self.error_label.set_text(message)
        self.error_label.set_visible(bool(message))


class DuplicacyFuseApp(Adw.Application):
    def __init__(self) -> None:
        settings = Gtk.Settings.get_default()
        prefers_dark = bool(settings.get_property("gtk-application-prefer-dark-theme")) if settings else False
        if settings and prefers_dark:
            settings.set_property("gtk-application-prefer-dark-theme", False)
        scheme = Adw.ColorScheme.PREFER_DARK if prefers_dark else Adw.ColorScheme.DEFAULT
        Adw.StyleManager.get_default().set_color_scheme(scheme)
        super().__init__(application_id="com.duplicacy.Fuse", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        try:
            self.profiles = load_profiles()
            self.profile_load_error = ""
        except Exception as error:
            self.profiles = []
            self.profile_load_error = f"Could not load saved profiles: {error}"
        self.mounts: dict[str, dict] = {}
        self.window: MountWindow | None = None
        self._quit_requested = False
        self.hold()
        self._add_action("quit", self.confirm_quit)
        self.connect("activate", self.activate)
        self.connect("shutdown", self.shutdown)

    def _add_action(self, name: str, callback) -> None:
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", callback)
        self.add_action(action)

    def activate(self, *_args) -> None:
        if self.window is None:
            self.window = MountWindow(self)
        self.window.present()

    def save_profile(self, profile: Profile) -> None:
        if self.profile_load_error:
            self.message("Profiles were not changed", self.profile_load_error)
            return
        profiles = [item for item in self.profiles if item.id != profile.id]
        profiles.append(profile)
        save_profiles(profiles)
        self.profiles = profiles
        if self.window:
            self.window.refresh_profiles()

    def remove_profile(self, profile: Profile) -> None:
        self.profiles = [item for item in self.profiles if item.id != profile.id]
        save_profiles(self.profiles)
        if self.window:
            self.window.refresh_profiles()

    def mount(self, profile: Profile) -> None:
        if profile.revision < 1 or not Path(profile.repository).is_dir():
            self.message("Cannot mount", "Select a valid repository and revision in the setup first.")
            return
        record = {"profile": profile, "state": "Starting", "handle": None, "mountpoint": None, "created": False, "read_error": False, "failed": False}
        self.mounts[profile.id] = record
        if self.window:
            self.window.refresh_profiles()

        def on_event(event: dict) -> None:
            GLib.idle_add(self._mount_event, profile.id, event)

        def on_exit(code: int, _output: str, detail: str) -> None:
            GLib.idle_add(self._mount_exit, profile.id, code, detail)

        def worker() -> None:
            try:
                handle, mountpoint, created = client.start_mount(profile, on_event, on_exit)
                GLib.idle_add(self._set_mount_handle, profile.id, handle, mountpoint, created)
            except Exception as error:
                GLib.idle_add(self._mount_error, profile.id, str(error))

        threading.Thread(target=worker, daemon=True).start()

    def _set_mount_handle(self, profile_id: str, handle, mountpoint: Path, created: bool) -> bool:
        record = self.mounts.get(profile_id)
        if record is None:
            try:
                handle.unmount()
            except OSError:
                pass
            return GLib.SOURCE_REMOVE
        record.update(handle=handle, mountpoint=mountpoint, created=created)
        pending = record.pop("pending_password", None)
        if pending:
            self.password_dialog(record, pending)
        if self.window:
            self.window.refresh_profiles()
        return GLib.SOURCE_REMOVE

    def _mount_event(self, profile_id: str, event: dict) -> bool:
        record = self.mounts.get(profile_id)
        if record is None:
            return GLib.SOURCE_REMOVE
        kind = event.get("type")
        if kind == "password-required":
            if record.get("handle") is None:
                record["pending_password"] = event
            else:
                self.password_dialog(record, event)
        elif kind == "mounted":
            record["state"] = "Mounted"
            self.notify("Snapshot mounted", "A read-only Duplicacy snapshot is mounted.")
            if self.window:
                self.window.refresh_profiles()
        elif kind == "read-error" and not record["read_error"]:
            record["read_error"] = True
            self.notify("Snapshot read failed", "A cached or remote file could not be read. Open the app for status.")
        elif kind == "unmount-failed":
            record["state"] = "Mounted"
            self.message("Unmount failed", event.get("message", "The FUSE helper could not unmount this path."))
            if self.window:
                self.window.refresh_profiles()
        elif kind == "failed":
            record["failed"] = True
            record["state"] = "Failed"
            record["detail"] = event.get("message", "Mount failed")
            self.notify("Mount failed", "Open Duplicacy FUSE to inspect the mount error.")
            self.message("Mount failed", record["detail"])
            if self.window:
                self.window.refresh_profiles()
        elif kind == "unmounted":
            self.notify("Snapshot unmounted", "The Duplicacy FUSE mount has stopped.")
        elif kind == "revisions-ready":
            pass
        return GLib.SOURCE_REMOVE

    def _mount_exit(self, profile_id: str, code: int, detail: str) -> bool:
        record = self.mounts.pop(profile_id, None)
        if record:
            if code and not record["failed"]:
                record["detail"] = detail.strip() or "Mount helper exited unexpectedly."
                self.notify("Mount stopped", "A Duplicacy FUSE mount stopped unexpectedly.")
                self.message("Mount stopped", record["detail"])
            self._remove_runtime_mountpoint(record)
        if self.window:
            self.window.refresh_profiles()
        if self._quit_requested and not self.mounts:
            self.quit()
        return GLib.SOURCE_REMOVE

    def _mount_error(self, profile_id: str, message: str) -> bool:
        self.mounts.pop(profile_id, None)
        self.message("Cannot start mount", message)
        if self.window:
            self.window.refresh_profiles()
        return GLib.SOURCE_REMOVE

    def _remove_runtime_mountpoint(self, record: dict) -> None:
        if record.get("created") and record.get("mountpoint"):
            try:
                record["mountpoint"].rmdir()
            except OSError:
                pass

    def unmount(self, profile_id: str) -> None:
        record = self.mounts.get(profile_id)
        if record and record.get("handle"):
            try:
                record["handle"].unmount()
                record["state"] = "Stopping"
                if self.window:
                    self.window.refresh_profiles()
            except OSError as error:
                self.message("Unmount failed", str(error))

    def run_revisions(self, profile: Profile, callback) -> None:
        self.run_listing(profile, "revisions-ready", "revisions", client.list_revisions, callback)

    def run_snapshots(self, profile: Profile, callback) -> None:
        self.run_listing(profile, "snapshots-ready", "snapshot_ids", client.list_snapshots, callback)

    def run_listing(self, profile: Profile, ready_event: str, result_key: str, command, callback) -> None:
        def event(event_message: dict) -> None:
            if event_message.get("type") == "password-required":
                if holder.get("handle") is None:
                    holder["pending_password"] = event_message
                else:
                    GLib.idle_add(
                        self.password_dialog,
                        {"profile": profile, "handle": holder["handle"]},
                        event_message,
                    )
            elif event_message.get("type") == "failed":
                holder["error"] = event_message.get("message", "Snapshot discovery failed.")
            elif event_message.get("type") == ready_event:
                holder["results"] = event_message.get(result_key)

        def exited(code: int, _output: str, detail: str) -> None:
            results = holder.get("results")
            if code == 0 and isinstance(results, list):
                error = ""
            else:
                results = None
                error = holder.get("error") or detail.strip() or "The FUSE helper exited without listing results."
            GLib.idle_add(callback, results, error)

        holder: dict = {"handle": None, "error": "", "results": None}

        def worker() -> None:
            try:
                handle = command(profile, event, exited)
                holder["handle"] = handle
                pending = holder.pop("pending_password", None)
                if pending:
                    GLib.idle_add(
                        self.password_dialog,
                        {"profile": profile, "handle": handle},
                        pending,
                    )
            except Exception as error:
                GLib.idle_add(callback, None, str(error))

        threading.Thread(target=worker, daemon=True).start()

    def password_dialog(self, record: dict, event: dict) -> bool:
        handle = record.get("handle")
        if handle is None:
            return GLib.SOURCE_REMOVE
        profile = record["profile"]
        dialog = Gtk.Window(transient_for=self.window, modal=True, title="Unlock encrypted storage")
        dialog.set_default_size(440, 260)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(20)
        box.set_margin_end(20)
        label = Gtk.Label(label=f"Enter the password for {profile.storage}. It is required for this operation and will not be saved.")
        label.set_wrap(True)
        label.set_xalign(0)
        box.append(label)
        retry = event.get("message")
        if retry:
            warning = Gtk.Label(label=retry)
            warning.set_wrap(True)
            warning.set_xalign(0)
            warning.add_css_class("error")
            box.append(warning)
        entry = Gtk.PasswordEntry()
        entry.set_show_peek_icon(True)
        box.append(entry)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        submit = Gtk.Button(label="Continue")
        submit.add_css_class("suggested-action")
        actions.append(cancel)
        actions.append(submit)
        box.append(actions)
        dialog.set_child(box)

        completed = False

        def close_request(*_args) -> bool:
            if not completed:
                entry.set_text("")
                try:
                    handle.cancel_password()
                except OSError:
                    pass
            return False

        def finish(password: bool) -> None:
            nonlocal completed
            if completed:
                return
            completed = True
            value = entry.get_text()
            if password and not value:
                completed = False
                label.set_text("Enter the storage password or cancel this operation.")
                return
            if password and len(value.encode("utf-8")) > 4096:
                completed = False
                label.set_text("Storage passwords are limited to 4096 bytes.")
                return
            entry.set_text("")
            dialog.close()
            try:
                if password:
                    handle.send_password(value)
                else:
                    handle.cancel_password()
            except OSError:
                pass
            value = ""

        cancel.connect("clicked", lambda *_: finish(False))
        submit.connect("clicked", lambda *_: finish(True))
        dialog.connect("close-request", close_request)
        dialog.present()
        entry.grab_focus()
        return GLib.SOURCE_REMOVE

    def confirm_remove(self, profile: Profile) -> None:
        self.confirm(
            "Remove setup?",
            f"Remove the saved setup '{profile.name}'? This does not delete backups or cached data.",
            lambda: self.remove_profile(profile),
        )

    def confirm_clear_cache(self, profile: Profile) -> None:
        self.confirm(
            "Clear plaintext cache?",
            "Cached chunks are decrypted plaintext. Clear only this marked cache directory? This cannot securely erase SSD snapshots or backups.",
            lambda: self._clear_cache(profile),
        )

    def _clear_cache(self, profile: Profile) -> None:
        def worker():
            try:
                client.clear_cache(profile.fuse_binary, profile.cache_dir)
                GLib.idle_add(self.notify, "Cache cleared", "The selected Duplicacy FUSE cache was cleared.")
            except Exception as error:
                GLib.idle_add(self.message, "Cache clear failed", str(error))

        threading.Thread(target=worker, daemon=True).start()

    def confirm_quit(self, *_args) -> None:
        if not self.mounts:
            self.quit()
            return
        self.confirm(
            "Unmount and quit?",
            "All mounts started by this app will be unmounted before it exits.",
            self._quit_with_unmount,
        )

    def _quit_with_unmount(self) -> None:
        self._quit_requested = True
        for profile_id in tuple(self.mounts):
            self.unmount(profile_id)
        if not self.mounts:
            self.quit()

    def shutdown(self, *_args) -> None:
        for record in self.mounts.values():
            handle = record.get("handle")
            if handle:
                try:
                    handle.unmount()
                except OSError:
                    pass

    def confirm(self, title: str, body: str, callback) -> None:
        dialog = Gtk.Window(transient_for=self.window, modal=True, title=title)
        dialog.set_default_size(440, 220)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(20)
        box.set_margin_end(20)
        label = Gtk.Label(label=body)
        label.set_wrap(True)
        label.set_xalign(0)
        box.append(label)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        accept = Gtk.Button(label=title.rstrip("?"))
        accept.add_css_class("destructive-action")
        actions.append(cancel)
        actions.append(accept)
        box.append(actions)
        dialog.set_child(box)
        cancel.connect("clicked", lambda *_: dialog.close())

        def accepted(*_args) -> None:
            dialog.close()
            callback()

        accept.connect("clicked", accepted)
        dialog.present()

    def message(self, title: str, body: str) -> None:
        dialog = Gtk.Window(transient_for=self.window, modal=True, title=title)
        dialog.set_default_size(460, 220)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(20)
        box.set_margin_bottom(20)
        box.set_margin_start(20)
        box.set_margin_end(20)
        label = Gtk.Label(label=body)
        label.set_wrap(True)
        label.set_xalign(0)
        box.append(label)
        close = Gtk.Button(label="Close")
        close.set_halign(Gtk.Align.END)
        close.connect("clicked", lambda *_: dialog.close())
        box.append(close)
        dialog.set_child(box)
        dialog.present()

    def notify(self, title: str, body: str) -> None:
        notification = Gio.Notification.new(title)
        notification.set_body(body)
        self.send_notification(f"duplicacy-fuse-{GLib.get_monotonic_time()}", notification)


def main() -> int:
    application = DuplicacyFuseApp()
    return application.run([])


if __name__ == "__main__":
    raise SystemExit(main())
