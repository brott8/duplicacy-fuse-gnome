from __future__ import annotations

import os
import threading
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import client
from .profiles import Profile, load_profiles, profile_mode, save_profiles, validate_profile


class MountWindow(Adw.ApplicationWindow):
    def __init__(self, application: "DuplicacyFuseApp") -> None:
        super().__init__(application=application)
        self.application = application
        self.set_title("Duplicacy FUSE")
        self.set_default_size(980, 720)
        self.connect("close-request", self._quit_on_close)

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
        notice = Gtk.Label(label="Snapshots are read-only, not sandboxed. Review safety details before mounting.")
        notice.set_wrap(True)
        notice.set_xalign(0)
        notice.add_css_class("warning")
        safety = Gtk.Expander(label="Safety details")
        safety_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        safety_detail = Gtk.Label(
            label=(
                "The mounted snapshot is read-only, but this is not a sandbox. Duplicacy may still ensure snapshot metadata "
                "directories on remote storage. Archived symlinks may lead outside the mount. Persistent chunk caching "
                "stores decrypted plaintext and is disabled by default; clearing it cannot guarantee secure deletion."
            )
        )
        safety_detail.set_wrap(True)
        safety_detail.set_xalign(0)
        safety_box.append(safety_detail)
        safety.set_child(safety_box)
        safety_group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        safety_group.append(notice)
        safety_group.append(safety)
        content.append(safety_group)

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

    def _quit_on_close(self, *_args) -> bool:
        self.application.confirm_quit()
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
            revision_summary = (
                f"Compare {len(profile.comparison_revisions)} revisions · {len(profile.selected_paths)} paths"
                if profile.comparison_revisions
                else f"Revision {profile.revision or 'not selected'}"
            )
            detail = (
                f"{profile.repository} · {profile.storage}\n"
                f"Snapshot {profile.snapshot_id or 'not selected'} · {revision_summary}\n"
                f"Mountpoint: {mountpoint}"
            )
            row = Adw.ActionRow(title=profile.name, subtitle=detail)
            row.set_subtitle_lines(3)
            status_badge = Gtk.Label(label=state)
            status_badge.add_css_class("caption")
            if state == "Mounted":
                status_badge.add_css_class("success")
            elif state in {"Starting", "Stopping"}:
                status_badge.add_css_class("accent")
            elif state == "Failed":
                status_badge.add_css_class("error")
            else:
                status_badge.add_css_class("dim-label")
            row.add_suffix(status_badge)
            controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            if active:
                unmount = Gtk.Button(label="Unmount")
                unmount.add_css_class("suggested-action")
                unmount.set_tooltip_text("Unmount this snapshot")
                unmount.set_sensitive(active.get("handle") is not None)
                unmount.connect("clicked", lambda _button, item=profile: self.application.unmount(item.id))
                controls.append(unmount)
            else:
                mount = Gtk.Button(label="Mount")
                mount.add_css_class("suggested-action")
                mount.set_tooltip_text("Mount this snapshot read-only")
                mount.set_sensitive(
                    bool(profile.snapshot_id.strip())
                    and (
                        profile.reference_revision > 0 and bool(profile.selected_paths)
                        if profile.comparison_revisions
                        else profile.revision > 0
                    )
                )
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
            remove.add_css_class("destructive-action")
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
        self.set_default_size(760, 760)
        self.storage_data: list[dict] = []
        self.snapshot_ids: list[str] = []
        self.snapshot_storage = profile.storage if profile else ""
        self.revision_values: list[int] = []
        self.revision_labels: dict[int, str] = {}
        self.revision_choices_loaded = False
        self.revision_storage = profile.storage if profile else ""
        self.revision_snapshot_id = profile.snapshot_id if profile else ""
        self.revision_repository = os.path.realpath(profile.repository) if profile else ""
        self.selected_comparison_revisions = list(profile.comparison_revisions) if profile else []
        self.revision_chooser: RevisionChooser | None = None
        self.pending_revision_storage = ""
        self.pending_revision_snapshot = ""
        self.pending_revision_repository = ""
        self._storage_request_id = 0
        self._snapshot_request_id = 0
        self._revision_request_id = 0
        self.fields: dict[str, Gtk.Entry] = {}
        self.mode_rows: dict[str, list[Gtk.Widget]] = {"single": [], "compare": []}
        self.section_errors: dict[str, Gtk.Label] = {}

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        root.set_margin_top(20)
        root.set_margin_bottom(20)
        root.set_margin_start(24)
        root.set_margin_end(24)
        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.sections = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.sections.set_hexpand(True)
        scroller.set_child(self.sections)
        root.append(scroller)
        self._new_section("Repository and storage")
        self._add_entry("Setup name", "name", profile.name if profile else "")
        repo_row = self._add_entry("Repository directory", "repository", profile.repository if profile else "")
        self._add_browse(repo_row, "repository")
        storage_row = self._add_drop_down("Configured storage", "storage", [])
        load_storage = Gtk.Button(label="Load repository settings")
        load_storage.connect("clicked", self.load_storages)
        self.form.attach(load_storage, 2, storage_row, 1, 1)
        self.storage_spinner = Gtk.Spinner()
        self.storage_spinner.set_visible(False)
        self.form.attach(self.storage_spinner, 3, storage_row, 1, 1)
        self.storage_load_button = load_storage

        self._new_section("Snapshot view")
        mode_model = Gtk.StringList.new(["Single revision", "Compare revisions"])
        self.mode_selector = Gtk.DropDown.new(mode_model, None)
        self.mode_selector.set_hexpand(True)
        self.mode_selector.set_selected(1 if profile and profile_mode(profile) == "compare" else 0)
        self.mode_selector.connect("notify::selected", self.mode_selected)
        self._add_widget("Mount mode", self.mode_selector)

        snapshot_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.snapshot_entry = Gtk.Entry()
        self.snapshot_entry.set_hexpand(True)
        self.snapshot_entry.set_placeholder_text("Enter snapshot ID or load snapshots")
        if profile:
            self.snapshot_entry.set_text(profile.snapshot_id)
        snapshot_box.append(self.snapshot_entry)
        self.snapshot_choices_button = Gtk.MenuButton()
        self.snapshot_choices_button.set_icon_name("pan-down-symbolic")
        self.snapshot_choices_button.set_tooltip_text("Choose from loaded snapshot IDs")
        self.snapshot_choices_button.set_sensitive(False)
        self.snapshot_choices_button.add_css_class("flat")
        self.snapshot_popover = Gtk.Popover()
        snapshot_popover_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        snapshot_popover_box.set_margin_top(8)
        snapshot_popover_box.set_margin_bottom(8)
        snapshot_popover_box.set_margin_start(8)
        snapshot_popover_box.set_margin_end(8)
        self.snapshot_search = Gtk.SearchEntry()
        self.snapshot_search.set_placeholder_text("Search snapshot IDs")
        self.snapshot_search.connect("search-changed", self._render_snapshot_choices)
        snapshot_popover_box.append(self.snapshot_search)
        self.snapshot_choice_list = Gtk.ListBox()
        self.snapshot_choice_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.snapshot_choice_list.add_css_class("boxed-list")
        snapshot_scroll = Gtk.ScrolledWindow()
        snapshot_scroll.set_min_content_height(180)
        snapshot_scroll.set_max_content_height(320)
        snapshot_scroll.set_child(self.snapshot_choice_list)
        snapshot_popover_box.append(snapshot_scroll)
        self.snapshot_popover.set_child(snapshot_popover_box)
        self.snapshot_choices_button.set_popover(self.snapshot_popover)
        snapshot_box.append(self.snapshot_choices_button)
        snapshot_row = self._add_widget("Snapshot ID", snapshot_box)
        snapshots_button = Gtk.Button(label="Load snapshot IDs")
        snapshots_button.set_sensitive(False)
        snapshots_button.connect("clicked", self.load_snapshots)
        self.form.attach(snapshots_button, 2, snapshot_row, 1, 1)
        self.snapshot_spinner = Gtk.Spinner()
        self.snapshot_spinner.set_visible(False)
        self.form.attach(self.snapshot_spinner, 3, snapshot_row, 1, 1)
        self.snapshots_load_button = snapshots_button

        self._new_section("Revision selection")
        self.revision_model = Gtk.StringList.new(["Select a revision"])
        self.revision_selector = Gtk.DropDown.new(self.revision_model, None)
        self.revision_selector.set_hexpand(True)
        revision_row = self._add_widget("Single revision", self.revision_selector)
        self.mode_rows["single"].extend(self._row_widgets(revision_row))
        revisions_button = Gtk.Button(label="Load revisions")
        revisions_button.set_sensitive(False)
        revisions_button.connect("clicked", self.load_revisions)
        self.form.attach(revisions_button, 2, revision_row, 1, 1)
        self.revision_spinner = Gtk.Spinner()
        self.revision_spinner.set_visible(False)
        self.form.attach(self.revision_spinner, 3, revision_row, 1, 1)
        self.revisions_load_button = revisions_button

        self.reference_model = Gtk.StringList.new(["Load revisions first"])
        self.reference_selector = Gtk.DropDown.new(self.reference_model, None)
        self.reference_selector.set_hexpand(True)
        self.reference_selector.set_sensitive(False)
        reference_row = self._add_widget("Reference revision for browsing paths", self.reference_selector)
        self.mode_rows["compare"].extend(self._row_widgets(reference_row))

        comparison_controls = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.comparison_revision_summary = Gtk.Label(label="No comparison revisions selected")
        self.comparison_revision_summary.set_xalign(0)
        self.comparison_revision_summary.set_wrap(True)
        comparison_controls.append(self.comparison_revision_summary)
        self.comparison_revision_button = Gtk.Button(label="Choose revisions")
        self.comparison_revision_button.set_halign(Gtk.Align.START)
        self.comparison_revision_button.set_sensitive(False)
        self.comparison_revision_button.connect("clicked", self.open_revision_chooser)
        comparison_controls.append(self.comparison_revision_button)
        comparison_row = self._add_widget("Comparison revisions", comparison_controls)
        self.mode_rows["compare"].extend(self._row_widgets(comparison_row))

        self.comparison_path_values = list(profile.selected_paths) if profile else []
        self.comparison_path_label = Gtk.Label(label=self._format_comparison_paths())
        self.comparison_path_label.set_xalign(0)
        self.comparison_path_label.set_wrap(True)
        self.comparison_path_label.set_selectable(True)
        browse_tree = Gtk.Button(label="Browse snapshot tree")
        browse_tree.set_sensitive(False)
        browse_tree.connect("clicked", self.open_snapshot_picker)
        self.browse_tree_button = browse_tree
        path_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        path_box.append(self.comparison_path_label)
        path_box.append(browse_tree)
        path_row = self._add_widget("Comparison paths", path_box)
        self.mode_rows["compare"].extend(self._row_widgets(path_row))

        single_path_row = self._add_entry("Repository-relative path (optional)", "selected_path", profile.selected_path if profile else "")
        self.mode_rows["single"].extend(self._row_widgets(single_path_row))

        advanced = Gtk.Expander(label="Advanced mount options")
        advanced.set_hexpand(True)
        advanced_grid = Gtk.Grid(column_spacing=12, row_spacing=12)
        advanced_grid.set_margin_top(8)
        advanced.set_child(advanced_grid)
        self.sections.append(advanced)
        self.form = advanced_grid
        self.row = 0
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

        self.fields["repository"].connect("changed", self.repository_changed)
        self.snapshot_entry.connect("changed", self.snapshot_value_changed)
        self.fields["fuse_binary"].connect(
            "changed", lambda *_args: self.show_error("", "Snapshot view")
        )
        self.repository_source = self._current_repository()
        self.storage_dropdown.set_sensitive(False)
        self.revision_selector.set_sensitive(False)
        if profile:
            self._set_revision(profile.revision)
            self.comparison_path_label.set_text(self._format_comparison_paths())
            self._set_comparison_revision_rows(set(profile.comparison_revisions), profile.comparison_revisions)
            self._load_stored_storage(profile.storage)
        self._update_revision_button()
        self._update_mode_visibility()
        self.present()

    def _new_section(self, title: str) -> None:
        heading = Gtk.Label(label=title)
        heading.set_xalign(0)
        heading.add_css_class("title-3")
        self.sections.append(heading)
        self.form = Gtk.Grid(column_spacing=12, row_spacing=10)
        self.form.set_column_homogeneous(False)
        self.form.set_hexpand(True)
        self.sections.append(self.form)
        error = Gtk.Label()
        error.set_xalign(0)
        error.set_wrap(True)
        error.add_css_class("error")
        error.set_visible(False)
        self.section_errors[title] = error
        self.sections.append(error)
        self.row = 0

    def _row_widgets(self, row: int) -> list[Gtk.Widget]:
        return [widget for column in range(4) if (widget := self.form.get_child_at(column, row)) is not None]

    def mode_selected(self, *_args) -> None:
        self._update_mode_visibility()

    def _update_mode_visibility(self) -> None:
        mode = "compare" if self.mode_selector.get_selected() == 1 else "single"
        for name, widgets in self.mode_rows.items():
            for widget in widgets:
                if widget is not None:
                    widget.set_visible(name == mode)

    def _add_widget(self, label: str, widget: Gtk.Widget) -> int:
        current = self.row
        text = Gtk.Label(label=label)
        text.set_xalign(0)
        text.set_valign(Gtk.Align.START)
        text.set_wrap(True)
        text.set_max_width_chars(28)
        text.set_hexpand(False)
        widget.set_hexpand(True)
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

    def _load_stored_storage(self, name: str) -> None:
        if name:
            self.storage_data = [{"name": name}]
            self.storage_model = Gtk.StringList.new([name])
            self.storage_dropdown.set_model(self.storage_model)
            self.storage_dropdown.set_selected(0)
            self.storage_dropdown.set_sensitive(True)
            self.snapshot_storage = name

    def _set_snapshot_ids(self, snapshot_ids: list[str]) -> None:
        self.snapshot_ids = list(snapshot_ids)
        if self.snapshot_search.get_text():
            self.snapshot_search.set_text("")
        self._render_snapshot_choices()
        self._update_revision_button()

    def _render_snapshot_choices(self, *_args) -> None:
        child = self.snapshot_choice_list.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.snapshot_choice_list.remove(child)
            child = following
        query = self.snapshot_search.get_text().casefold().strip()
        for snapshot_id in self.snapshot_ids:
            if query not in snapshot_id.casefold():
                continue
            button = Gtk.Button()
            button.add_css_class("flat")
            button.set_hexpand(True)
            label = Gtk.Label(label=snapshot_id)
            label.set_xalign(0)
            label.set_wrap(True)
            button.set_child(label)
            button.connect("clicked", lambda *_args, value=snapshot_id: self.choose_snapshot(value))
            self.snapshot_choice_list.append(button)
        self.snapshot_choices_button.set_sensitive(bool(self.snapshot_ids))

    def choose_snapshot(self, snapshot_id: str) -> None:
        self.snapshot_entry.set_text(snapshot_id)
        self.snapshot_popover.popdown()

    def _selected_snapshot_id(self) -> str:
        return self.snapshot_entry.get_text().strip()

    def _update_revision_button(self) -> None:
        selected_storage = self.storage_dropdown.get_selected()
        has_storage = selected_storage < len(self.storage_data)
        self.revisions_load_button.set_sensitive(has_storage and bool(self._selected_snapshot_id()))

    def _clear_revision_choices(self) -> None:
        self._revision_request_id += 1
        self.revisions_load_button.set_sensitive(True)
        self.revision_spinner.stop()
        self.revision_spinner.set_visible(False)
        self.revision_values = []
        self.revision_labels = {}
        self.revision_choices_loaded = False
        self.revision_storage = ""
        self.revision_snapshot_id = ""
        self.revision_repository = ""
        self.revision_model = Gtk.StringList.new(["Load revisions"])
        self.revision_selector.set_model(self.revision_model)
        self.revision_selector.set_sensitive(False)
        self.reference_model = Gtk.StringList.new(["Load revisions first"])
        self.reference_selector.set_model(self.reference_model)
        self.reference_selector.set_sensitive(False)
        self.comparison_revision_button.set_sensitive(False)
        self.browse_tree_button.set_sensitive(False)
        self._update_revision_button()

    def snapshot_value_changed(self, *_args) -> None:
        snapshot_id = self._selected_snapshot_id()
        if snapshot_id:
            self.show_error("", "Snapshot view")
        if snapshot_id != self.revision_snapshot_id:
            self._clear_revision_choices()
            self._clear_comparison_selection()
            self.fields["selected_path"].set_text("")
        self._update_revision_button()

    def storage_selected(self, dropdown: Gtk.DropDown, _property) -> None:
        index = dropdown.get_selected()
        if index < len(self.storage_data):
            storage_name = self.storage_data[index].get("name", "")
            if storage_name != self.snapshot_storage:
                self.snapshot_storage = storage_name
                self.show_error("", "Snapshot view")
                self._snapshot_request_id += 1
                self.snapshots_load_button.set_sensitive(True)
                self.snapshot_spinner.stop()
                self.snapshot_spinner.set_visible(False)
                self._clear_revision_choices()
                self._clear_comparison_selection()
                self.fields["selected_path"].set_text("")
                self._set_snapshot_ids([])
                self.snapshot_entry.set_text("")
                self.snapshots_load_button.set_sensitive(True)
            self._update_revision_button()

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
        request_repository = os.path.realpath(repository) if repository else ""
        binary = self.fields["fuse_binary"].get_text().strip() or "duplicacy-fuse"
        self._storage_request_id += 1
        request_id = self._storage_request_id
        self.storage_load_button.set_sensitive(False)
        self.storage_spinner.set_visible(True)
        self.storage_spinner.start()
        self.show_error("", "Repository and storage")

        def worker():
            try:
                values = client.list_storages(binary, repository)
                GLib.idle_add(self._set_storages, values, "", request_id, request_repository)
            except Exception as error:
                GLib.idle_add(self._set_storages, [], str(error), request_id, request_repository)

        threading.Thread(target=worker, daemon=True).start()

    def _set_storages(self, values: list[dict], error: str, request_id: int, repository: str) -> bool:
        if request_id != self._storage_request_id or repository != self._current_repository():
            return GLib.SOURCE_REMOVE
        self.storage_load_button.set_sensitive(True)
        self.storage_spinner.stop()
        self.storage_spinner.set_visible(False)
        if error:
            self.show_error(error, "Repository and storage")
            return GLib.SOURCE_REMOVE
        self.storage_data = values
        names = [str(item["name"]) for item in values]
        repository = os.path.realpath(self.fields["repository"].get_text().strip())
        same_repository = self.profile and repository == os.path.realpath(self.profile.repository)
        if same_repository and self.profile.storage and self.profile.storage not in names:
            self.storage_data.insert(0, {"name": self.profile.storage})
            names.insert(0, self.profile.storage)
        self.storage_model = Gtk.StringList.new(names or ["No configured storage found"])
        self.storage_dropdown.set_model(self.storage_model)
        self.storage_dropdown.set_sensitive(bool(names))
        self.snapshots_load_button.set_sensitive(bool(names))
        if names:
            selected = self.profile.storage if self.profile else names[0]
            self.storage_dropdown.set_selected(names.index(selected) if selected in names else 0)
        else:
            self.show_error("This repository has no configured Duplicacy storage.", "Repository and storage")
        return GLib.SOURCE_REMOVE

    def load_revisions(self, *_args) -> None:
        selected = self.storage_dropdown.get_selected()
        if selected >= len(self.storage_data):
            self.show_error("Load and select a configured storage first.", "Revision selection")
            return
        snapshot_id = self._selected_snapshot_id()
        if not snapshot_id:
            self.show_error("Enter or choose a snapshot ID first.", "Revision selection")
            return
        storage = self.storage_data[selected]
        profile = self._draft_profile()
        profile.storage = storage["name"]
        profile.snapshot_id = snapshot_id
        self.pending_revision_storage = profile.storage
        self.pending_revision_snapshot = profile.snapshot_id
        self.pending_revision_repository = os.path.realpath(profile.repository)
        self._revision_request_id += 1
        request_id = self._revision_request_id
        request_source = (self.pending_revision_repository, profile.storage, profile.snapshot_id)
        self.revisions_load_button.set_sensitive(False)
        self.revision_spinner.set_visible(True)
        self.revision_spinner.start()
        self.show_error("", "Revision selection")
        self.application.run_revisions(
            profile,
            lambda revisions, error: self.revisions_loaded(revisions, error, request_id, request_source),
        )

    def load_snapshots(self, *_args) -> None:
        selected = self.storage_dropdown.get_selected()
        if selected >= len(self.storage_data):
            self.show_error("Load and select a configured storage first.", "Snapshot view")
            return
        storage = self.storage_data[selected]
        profile = self._draft_profile()
        profile.storage = storage["name"]
        request_repository = os.path.realpath(profile.repository)
        self._snapshot_request_id += 1
        request_id = self._snapshot_request_id
        request_source = (request_repository, profile.storage)
        self.snapshots_load_button.set_sensitive(False)
        self.snapshot_spinner.set_visible(True)
        self.snapshot_spinner.start()
        self.show_error("", "Snapshot view")
        self.application.run_snapshots(
            profile,
            lambda snapshot_ids, error: self.snapshots_loaded(snapshot_ids, error, request_id, request_source),
        )

    def snapshots_loaded(self, snapshot_ids: list[str] | None, error: str, request_id: int, source: tuple[str, str]) -> None:
        if request_id != self._snapshot_request_id or source != self._current_storage_source():
            return
        self.snapshots_load_button.set_sensitive(True)
        self.snapshot_spinner.stop()
        self.snapshot_spinner.set_visible(False)
        if error:
            self.show_error(error, "Snapshot view")
            return
        self.show_error("", "Snapshot view")
        selected = self.storage_dropdown.get_selected()
        available = snapshot_ids or []
        self._set_snapshot_ids(available)
        self.snapshot_storage = self.storage_data[selected]["name"] if selected < len(self.storage_data) else ""
        if not available:
            self.show_error("No snapshot IDs were returned. You can still enter one manually.", "Snapshot view")

    def revisions_loaded(
        self,
        revisions: list[dict] | None,
        error: str,
        request_id: int,
        source: tuple[str, str, str],
    ) -> None:
        if request_id != self._revision_request_id or source != self._current_revision_source():
            return
        self.revisions_load_button.set_sensitive(True)
        self.revision_spinner.stop()
        self.revision_spinner.set_visible(False)
        if error:
            self.show_error(error, "Revision selection")
            return
        self.revision_storage = self.pending_revision_storage
        self.revision_snapshot_id = self.pending_revision_snapshot
        self.revision_repository = self.pending_revision_repository
        self.revision_values = [int(item["revision"]) for item in revisions or []]
        self.revision_labels = {
            int(item["revision"]): (
                f"Revision {item['revision']} · "
                f"{datetime.fromtimestamp(int(item['created_at'])).astimezone().strftime('%Y-%m-%d %H:%M')}"
            )
            for item in revisions or []
        }
        self.revision_choices_loaded = True
        same_profile_source = bool(
            self.profile
            and source
            == (
                os.path.realpath(self.profile.repository),
                self.profile.storage,
                self.profile.snapshot_id,
            )
        )
        labels = [self.revision_labels[int(item["revision"])] for item in revisions or []]
        self.revision_model = Gtk.StringList.new(labels or ["No revisions found"])
        self.revision_selector.set_model(self.revision_model)
        self.revision_selector.set_sensitive(bool(self.revision_values))
        if same_profile_source and self.profile.revision in self.revision_values:
            self.revision_selector.set_selected(self.revision_values.index(self.profile.revision))
        elif self.revision_values:
            self.revision_selector.set_selected(len(self.revision_values) - 1)
        self.reference_model = Gtk.StringList.new(labels or ["No revisions found"])
        self.reference_selector.set_model(self.reference_model)
        self.reference_selector.set_sensitive(bool(self.revision_values))
        reference = self.profile.reference_revision if same_profile_source else 0
        if self.revision_values:
            self.reference_selector.set_selected(
                self.revision_values.index(reference) if reference in self.revision_values else len(self.revision_values) - 1
            )
        self._set_comparison_revision_rows(set(self.selected_comparison_revisions), self.revision_values)
        self.comparison_revision_button.set_sensitive(bool(self.revision_values))
        self.browse_tree_button.set_sensitive(bool(self.revision_values))

    def _set_comparison_revision_rows(self, selected: set[int], revisions: list[int] | None = None) -> None:
        available = self.revision_values if revisions is None else revisions
        self.selected_comparison_revisions = [revision for revision in available if revision in selected]
        count = len(self.selected_comparison_revisions)
        self.comparison_revision_summary.set_text(
            f"{count} comparison revision{'s' if count != 1 else ''} selected"
            if count
            else "No comparison revisions selected"
        )
        self.comparison_revision_button.set_label(
            f"Choose revisions ({count})" if count else "Choose revisions"
        )

    def _selected_comparison_revisions(self) -> list[int]:
        return list(self.selected_comparison_revisions)

    def open_revision_chooser(self, *_args) -> None:
        if not self.revision_values:
            self.show_error("Load revisions before choosing comparison revisions.", "Revision selection")
            return
        if self.revision_chooser is not None:
            self.revision_chooser.present()
            return
        self.revision_chooser = RevisionChooser(
            self,
            self.revision_values,
            self.revision_labels,
            self.selected_comparison_revisions,
            self._set_comparison_revision_rows,
        )

    def revision_chooser_closed(self, chooser: "RevisionChooser") -> None:
        if self.revision_chooser is chooser:
            self.revision_chooser = None

    def _format_comparison_paths(self) -> str:
        return "\n".join(self.comparison_path_values) if self.comparison_path_values else "No paths selected"

    def _clear_comparison_selection(self) -> None:
        self.comparison_path_values.clear()
        self.comparison_path_label.set_text(self._format_comparison_paths())
        self._set_comparison_revision_rows(set())

    def repository_changed(self, entry: Gtk.Entry) -> None:
        value = entry.get_text().strip()
        repository = os.path.realpath(value) if value else ""
        if repository == self.repository_source:
            return
        self.repository_source = repository
        self._storage_request_id += 1
        self._snapshot_request_id += 1
        self.storage_load_button.set_sensitive(True)
        self.storage_spinner.stop()
        self.storage_spinner.set_visible(False)
        self.snapshots_load_button.set_sensitive(False)
        self.snapshot_spinner.stop()
        self.snapshot_spinner.set_visible(False)
        self.storage_data = []
        self.snapshot_storage = ""
        self.storage_model = Gtk.StringList.new(["Load repository settings"])
        self.storage_dropdown.set_model(self.storage_model)
        self.storage_dropdown.set_sensitive(False)
        self._set_snapshot_ids([])
        self.snapshot_entry.set_text("")
        self._clear_revision_choices()
        self._clear_comparison_selection()
        self.fields["selected_path"].set_text("")
        self.show_error("", "Repository and storage")
        self.show_error("", "Snapshot view")

    def _current_repository(self) -> str:
        value = self.fields["repository"].get_text().strip()
        return os.path.realpath(value) if value else ""

    def _current_storage_source(self) -> tuple[str, str]:
        selected = self.storage_dropdown.get_selected()
        storage = self.storage_data[selected]["name"] if selected < len(self.storage_data) else ""
        return self._current_repository(), storage

    def _current_revision_source(self) -> tuple[str, str, str]:
        return (*self._current_storage_source(), self._selected_snapshot_id())

    def open_snapshot_picker(self, *_args) -> None:
        index = self.reference_selector.get_selected()
        if index >= len(self.revision_values):
            self.show_error("Load revisions and select a reference revision first.", "Revision selection")
            return
        SnapshotTreePicker(self, self.revision_values[index])

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
        same_source = bool(
            self.profile
            and os.path.realpath(self.fields["repository"].get_text().strip()) == os.path.realpath(self.profile.repository)
            and storage == self.profile.storage
            and self._selected_snapshot_id() == self.profile.snapshot_id
        )
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
            revision=self.profile.revision if same_source else 0,
            reference_revision=self.profile.reference_revision if same_source else 0,
            comparison_revisions=list(self.profile.comparison_revisions) if same_source else [],
            selected_paths=list(self.comparison_path_values),
        )

    def save_profile(self, *_args) -> None:
        try:
            profile = self._draft_profile()
            if not profile.snapshot_id:
                raise ValueError("Enter or choose a snapshot ID before saving this setup.")
            compare_mode = self.mode_selector.get_selected() == 1
            revision_choices_match = self.revision_choices_loaded and (
                self.revision_selector.get_selected() < len(self.revision_values)
                and self.revision_storage == profile.storage
                and self.revision_snapshot_id == profile.snapshot_id
                and self.revision_repository == os.path.realpath(profile.repository)
            )
            if revision_choices_match:
                revision_index = self.revision_selector.get_selected()
                profile.revision = self.revision_values[revision_index]
                reference_index = self.reference_selector.get_selected()
                if reference_index < len(self.revision_values):
                    profile.reference_revision = self.revision_values[reference_index]
                profile.comparison_revisions = self._selected_comparison_revisions()
            if compare_mode:
                if not revision_choices_match and not (
                    profile.comparison_revisions and profile.reference_revision > 0
                ):
                    raise ValueError("Load revisions for the selected repository, storage, and snapshot first.")
                if not profile.comparison_revisions:
                    raise ValueError("Select at least one comparison revision.")
                profile.revision = 0
                profile.selected_path = ""
            else:
                profile.reference_revision = 0
                profile.comparison_revisions = []
                profile.selected_paths = []
            if self.cache_toggle.get_active():
                profile.cache_bytes = int(self.cache_size.get_value() * 1024**3)
            else:
                profile.cache_bytes = 0
            validate_profile(profile)
            self.application.save_profile(profile)
        except Exception as error:
            self.show_error(str(error))
            return
        self.close()

    def add_comparison_path(self, path: str, selected: bool) -> None:
        if selected and path not in self.comparison_path_values:
            self.comparison_path_values.append(path)
        elif not selected:
            self.comparison_path_values = [value for value in self.comparison_path_values if value != path]
        self.comparison_path_label.set_text(self._format_comparison_paths())

    def show_error(self, message: str, section: str | None = None) -> None:
        if section is not None:
            label = self.section_errors[section]
            label.add_css_class("error")
            label.set_text(message)
            label.set_visible(bool(message))
            return
        self.error_label.set_text(message)
        self.error_label.set_visible(bool(message))


class RevisionChooser(Gtk.Window):
    def __init__(
        self,
        editor: ProfileEditor,
        revisions: list[int],
        labels: dict[int, str],
        selected: list[int],
        apply_selection,
    ) -> None:
        super().__init__(transient_for=editor, modal=True, title="Choose comparison revisions")
        self.editor = editor
        self.revisions = revisions
        self.labels = labels
        self.selected = set(selected)
        self.apply_selection_callback = apply_selection
        self.set_default_size(620, 560)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        description = Gtk.Label(label="Select one or more revisions to compare against the reference revision.")
        description.set_xalign(0)
        description.set_wrap(True)
        box.append(description)
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search revisions")
        self.search_entry.connect("search-changed", self.render)
        box.append(self.search_entry)
        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_box.add_css_class("boxed-list")
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_child(self.list_box)
        box.append(scroll)
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.count_label = Gtk.Label()
        self.count_label.set_hexpand(True)
        self.count_label.set_xalign(0)
        footer.append(self.count_label)
        clear = Gtk.Button(label="Clear selection")
        clear.connect("clicked", self.clear_selection)
        footer.append(clear)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        footer.append(cancel)
        done = Gtk.Button(label="Done")
        done.add_css_class("suggested-action")
        done.connect("clicked", self.apply_selection)
        footer.append(done)
        box.append(footer)
        self.set_child(box)
        self.connect("close-request", self._close_request)
        self.render()
        self.present()

    def _close_request(self, *_args) -> bool:
        self.editor.revision_chooser_closed(self)
        return False

    def render(self, *_args) -> None:
        child = self.list_box.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.list_box.remove(child)
            child = following
        query = self.search_entry.get_text().casefold().strip()
        visible = [
            revision
            for revision in self.revisions
            if query in self.labels.get(revision, f"Revision {revision}").casefold()
        ]
        for revision in visible:
            checkbox = Gtk.CheckButton(label=self.labels.get(revision, f"Revision {revision}"))
            checkbox.set_active(revision in self.selected)
            checkbox.connect("toggled", self.revision_toggled, revision)
            self.list_box.append(checkbox)
        if self.revisions and not visible:
            empty = Gtk.Label(label="No revisions match this search.")
            empty.set_margin_top(12)
            empty.set_margin_bottom(12)
            self.list_box.append(empty)
        self._update_count()

    def revision_toggled(self, checkbox: Gtk.CheckButton, revision: int) -> None:
        if checkbox.get_active():
            self.selected.add(revision)
        else:
            self.selected.discard(revision)
        self._update_count()

    def _update_count(self) -> None:
        self.count_label.set_text(f"{len(self.selected)} of {len(self.revisions)} revisions selected")

    def clear_selection(self, *_args) -> None:
        self.selected.clear()
        self.render()

    def apply_selection(self, *_args) -> None:
        self.apply_selection_callback(set(self.selected))
        self.close()


class SnapshotTreePicker(Gtk.Window):
    def __init__(self, editor: ProfileEditor, revision: int) -> None:
        super().__init__(transient_for=editor, modal=True, title=f"Choose paths from revision {revision}")
        self.editor = editor
        self.revision = revision
        self.current_path = ""
        self.path_stack: list[str] = []
        self.draft_paths = list(editor.comparison_path_values)
        self.entries: list[dict] = []
        self.password_cache: str | None = None
        self.password_dialog: Gtk.Window | None = None
        self.closed = False
        self.request_id = 0
        self.set_default_size(620, 540)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        navigation = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.up_button = Gtk.Button.new_from_icon_name("go-up-symbolic")
        self.up_button.set_tooltip_text("Go to parent folder")
        self.up_button.connect("clicked", self.go_up)
        navigation.append(self.up_button)
        self.breadcrumbs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.breadcrumbs.set_hexpand(True)
        breadcrumb_scroll = Gtk.ScrolledWindow()
        breadcrumb_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        breadcrumb_scroll.set_hexpand(True)
        breadcrumb_scroll.set_child(self.breadcrumbs)
        navigation.append(breadcrumb_scroll)
        box.append(navigation)
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search this folder")
        self.search_entry.connect("search-changed", self._filter_entries)
        box.append(self.search_entry)
        self.state_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.loading_spinner = Gtk.Spinner()
        self.loading_spinner.set_visible(False)
        self.state_row.append(self.loading_spinner)
        self.state_label = Gtk.Label()
        self.state_label.set_xalign(0)
        self.state_label.set_wrap(True)
        self.state_label.set_hexpand(True)
        self.state_row.append(self.state_label)
        box.append(self.state_row)
        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_box.add_css_class("boxed-list")
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_child(self.list_box)
        box.append(scroll)
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.add_css_class("toolbar")
        self.count_label = Gtk.Label()
        self.count_label.set_hexpand(True)
        self.count_label.set_xalign(0)
        footer.append(self.count_label)
        clear = Gtk.Button(label="Clear selection")
        clear.connect("clicked", self.clear_selection)
        footer.append(clear)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        footer.append(cancel)
        done = Gtk.Button(label="Done")
        done.add_css_class("suggested-action")
        done.connect("clicked", self.apply_selection)
        footer.append(done)
        box.append(footer)
        self.set_child(box)
        self.connect("close-request", self._clear_password_cache)
        self._update_count()
        self.present()
        self.load_directory("")

    def _clear_password_cache(self, *_args) -> bool:
        self.closed = True
        self.password_cache = None
        if self.password_dialog is not None:
            self.password_dialog.close()
            self.password_dialog = None
        return False

    def remember_password(self, password: str) -> None:
        if not self.closed:
            self.password_cache = password

    def password_dialog_opened(self, dialog: Gtk.Window) -> None:
        if self.closed:
            dialog.close()
        else:
            self.password_dialog = dialog

    def go_up(self, *_args) -> None:
        if self.path_stack:
            self.load_directory(self.path_stack.pop())

    def open_directory(self, path: str) -> None:
        self.load_directory(path)

    def _update_breadcrumbs(self) -> None:
        child = self.breadcrumbs.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.breadcrumbs.remove(child)
            child = following
        root = Gtk.Button(label="/")
        root.add_css_class("flat")
        root.connect("clicked", lambda *_: self.load_directory(""))
        self.breadcrumbs.append(root)
        path = ""
        for component in filter(None, self.current_path.split("/")):
            path = f"{path}/{component}" if path else component
            button = Gtk.Button(label=component)
            button.add_css_class("flat")
            button.connect("clicked", lambda _button, value=path: self.load_directory(value))
            self.breadcrumbs.append(button)

    def _clear_rows(self) -> None:
        child = self.list_box.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.list_box.remove(child)
            child = following

    def _render_entries(self) -> None:
        self._clear_rows()
        query = self.search_entry.get_text().casefold().strip()
        visible = [entry for entry in self.entries if query in entry["name"].casefold()]
        for entry in visible:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.set_margin_top(4)
            row.set_margin_bottom(4)
            checkbox = Gtk.CheckButton()
            path = entry["path"]
            is_directory = entry["type"] == "directory"
            path_key = self._selection_key(path)
            is_selected = any(self._selection_key(value) == path_key for value in self.draft_paths)
            checkbox.set_active(is_selected)
            partially_selected = is_directory and not is_selected and self._has_selected_descendant(path)
            checkbox.set_inconsistent(partially_selected)
            checkbox.set_tooltip_text(
                f"Partially selected: paths inside {path} are selected"
                if partially_selected
                else f"Select {path}"
            )
            checkbox.connect(
                "toggled",
                lambda button, value=path, directory=is_directory: self._set_selected(
                    value, button.get_active(), directory
                ),
            )
            row.append(checkbox)
            if entry["type"] == "directory":
                open_button = Gtk.Button(label=entry["name"])
                open_button.set_hexpand(True)
                open_button.set_halign(Gtk.Align.START)
                open_button.add_css_class("flat")
                open_button.set_tooltip_text(f"Open folder {entry['name']}")
                open_button.connect("clicked", lambda _button, value=entry["path"]: self.open_directory(value))
                row.append(open_button)
            else:
                name = Gtk.Label(label=entry["name"])
                name.set_hexpand(True)
                name.set_xalign(0)
                name.set_max_width_chars(36)
                row.append(name)
            details = Gtk.Label(label=f"{entry['type']} · {entry['size']} bytes")
            details.add_css_class("dim-label")
            details.set_xalign(1)
            details.set_wrap(True)
            row.append(details)
            self.list_box.append(row)
        if self.entries and not visible:
            self.state_label.set_text("No names match this search.")
            self.state_label.remove_css_class("error")
        elif not self.entries:
            self.state_label.set_text("This directory is empty.")
            self.state_label.remove_css_class("error")
        else:
            self.state_label.set_text("")
            self.state_label.remove_css_class("error")

    def _filter_entries(self, *_args) -> None:
        if self.state_label.get_text() != "Loading...":
            self._render_entries()

    def _has_selected_descendant(self, path: str) -> bool:
        path_key = self._selection_key(path)
        prefix = f"{path_key}/"
        return any(
            self._selection_key(value).startswith(prefix)
            for value in self.draft_paths
            if self._selection_key(value) != path_key
        )

    @staticmethod
    def _selection_key(path: str) -> str:
        return path.removeprefix("./").rstrip("/")

    def _set_selected(self, path: str, selected: bool, is_directory: bool = False) -> None:
        path_key = self._selection_key(path)
        prefix = f"{path_key}/"
        if selected:
            self.draft_paths = [
                value
                for value in self.draft_paths
                if self._selection_key(value) != path_key
                and not (is_directory and self._selection_key(value).startswith(prefix))
            ]
            if not any(self._selection_key(value) == path_key for value in self.draft_paths):
                self.draft_paths.append(path)
        else:
            self.draft_paths = [
                value for value in self.draft_paths if self._selection_key(value) != path_key
            ]
        self._update_count()
        self._render_entries()

    def _update_count(self) -> None:
        count = len(self.draft_paths)
        self.count_label.set_text(f"{count} path{'s' if count != 1 else ''} selected")

    def clear_selection(self, *_args) -> None:
        self.draft_paths.clear()
        self._update_count()
        self._render_entries()

    def apply_selection(self, *_args) -> None:
        self.editor.comparison_path_values = list(self.draft_paths)
        self.editor.comparison_path_label.set_text(self.editor._format_comparison_paths())
        self.close()

    def load_directory(self, path: str) -> None:
        self.request_id += 1
        request_id = self.request_id
        self.current_path = path
        if self.search_entry.get_text():
            self.search_entry.set_text("")
        components = [part for part in path.split("/") if part]
        self.path_stack = ["/".join(components[:index]) for index in range(len(components))]
        self._update_breadcrumbs()
        self.up_button.set_sensitive(bool(self.path_stack))
        self.entries = []
        self._clear_rows()
        self.state_label.set_text("Loading...")
        self.state_label.remove_css_class("error")
        self.loading_spinner.set_visible(True)
        self.loading_spinner.start()
        profile = self.editor._draft_profile()
        profile.revision = self.revision
        self.editor.application.run_browse(
            profile,
            self.revision,
            path,
            lambda entries, error: self.directory_loaded(entries, error, request_id, path),
            self.password_cache,
            self.remember_password,
            lambda: not self.closed,
            self.password_dialog_opened,
        )

    def directory_loaded(self, entries: list[dict] | None, error: str, request_id: int, path: str) -> None:
        if self.closed or request_id != self.request_id or path != self.current_path:
            return
        if error:
            self.loading_spinner.stop()
            self.loading_spinner.set_visible(False)
            self.entries = []
            self.state_label.set_text(error)
            self.state_label.add_css_class("error")
            self._clear_rows()
            return
        self.loading_spinner.stop()
        self.loading_spinner.set_visible(False)
        self.entries = entries or []
        self._render_entries()


class DuplicacyFuseApp(Adw.Application):
    def __init__(self) -> None:
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
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
        valid_revision = (
            profile.reference_revision > 0 and bool(profile.selected_paths)
            if profile.comparison_revisions
            else profile.revision > 0
        )
        if not profile.snapshot_id.strip() or not valid_revision or not Path(profile.repository).is_dir():
            self.message("Cannot mount", "Enter a snapshot ID and select a valid repository and revision first.")
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

    def run_browse(
        self,
        profile: Profile,
        revision: int,
        path: str,
        callback,
        cached_password=None,
        remember_password=None,
        is_active=None,
        password_dialog_opened=None,
    ) -> None:
        command = lambda item, event, exited: client.browse_snapshot(item, revision, path, event, exited)
        self.run_listing(
            profile,
            "directory-ready",
            "entries",
            command,
            callback,
            cached_password,
            remember_password,
            is_active,
            password_dialog_opened,
        )

    def run_listing(
        self,
        profile: Profile,
        ready_event: str,
        result_key: str,
        command,
        callback,
        cached_password=None,
        remember_password=None,
        is_active=None,
        password_dialog_opened=None,
    ) -> None:
        holder: dict = {
            "handle": None,
            "error": "",
            "results": None,
            "cached_password": cached_password,
            "cached_password_used": False,
            "entered_password": None,
        }

        def answer_password(handle, event_message: dict) -> None:
            if is_active and not is_active():
                try:
                    handle.cancel_password()
                except OSError:
                    pass
                return
            cached = holder.get("cached_password")
            if cached is not None and not holder["cached_password_used"]:
                holder["cached_password_used"] = True
                try:
                    handle.send_password(cached)
                    return
                except OSError as error:
                    holder["error"] = str(error)
            record = {"profile": profile, "handle": handle}
            if remember_password:
                record["on_password"] = lambda value: holder.__setitem__("entered_password", value)
            if is_active:
                record["is_active"] = is_active
            if password_dialog_opened:
                record["on_dialog"] = password_dialog_opened
            GLib.idle_add(self.password_dialog, record, event_message)

        def event(event_message: dict) -> None:
            if event_message.get("type") == "password-required":
                if holder.get("handle") is None:
                    holder["pending_password"] = event_message
                else:
                    answer_password(holder["handle"], event_message)
            elif event_message.get("type") == "failed":
                holder["error"] = event_message.get("message", "Snapshot discovery failed.")
            elif event_message.get("type") == ready_event:
                try:
                    result = event_message.get(result_key)
                    holder["results"] = client.validate_directory_entries(result) if ready_event == "directory-ready" else result
                except (RuntimeError, TypeError) as error:
                    holder["error"] = str(error)

        def exited(code: int, _output: str, detail: str) -> None:
            results = holder.get("results")
            if code == 0 and isinstance(results, list) and not holder.get("error"):
                error = ""
                if holder.get("entered_password") is not None and remember_password:
                    password = holder.pop("entered_password")
                    GLib.idle_add(lambda: (remember_password(password), GLib.SOURCE_REMOVE)[1])
            else:
                results = None
                error = holder.get("error") or detail.strip() or "The FUSE helper exited without listing results."
            GLib.idle_add(callback, results, error)

        def worker() -> None:
            try:
                handle = command(profile, event, exited)
                holder["handle"] = handle
                pending = holder.pop("pending_password", None)
                if pending:
                    answer_password(handle, pending)
            except Exception as error:
                GLib.idle_add(callback, None, str(error))

        threading.Thread(target=worker, daemon=True).start()

    def password_dialog(self, record: dict, event: dict) -> bool:
        handle = record.get("handle")
        if handle is None:
            return GLib.SOURCE_REMOVE
        is_active = record.get("is_active")
        if is_active and not is_active():
            try:
                handle.cancel_password()
            except OSError:
                pass
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
            if is_active and not is_active():
                password = False
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
                    remember = record.get("on_password")
                    if remember:
                        remember(value)
                else:
                    handle.cancel_password()
            except OSError:
                pass
            value = ""

        def key_pressed(_controller, keyval: int, _keycode: int, _state) -> bool:
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                finish(True)
                return True
            return False

        cancel.connect("clicked", lambda *_: finish(False))
        submit.connect("clicked", lambda *_: finish(True))
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", key_pressed)
        entry.add_controller(key_controller)
        dialog.connect("close-request", close_request)
        on_dialog = record.get("on_dialog")
        if on_dialog:
            on_dialog(dialog)
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
