# src/minecraft/deploy_pack/prompt_ui.py

"""Textual audit UI for mod side assignments (Project_Specs.md §6.1).

Loads ``config.d/side_overrides.toml`` and every ``*.pw.toml`` from
``<modpack_dir>/.index/``, and lets the operator toggle each mod
between server / client / both / skipped / no-entry.

On save, the ``[deployment_tool_review]`` section of
``side_overrides.toml`` is written by ``overrides.save_side_overrides``,
which is a line-based splice (§6.1 step 5) that preserves every other
byte of the file.

Import behaviour
----------------

The ``textual`` package is imported lazily. Importing this module never
fails when Textual is missing - it sets ``HAS_TEXTUAL = False`` and
leaves ``AuditApp`` as None. The caller (``main.py``) checks the flag
before invoking :func:`run_audit`.

Testability
-----------

:class:`AuditRow`, :func:`build_audit_rows`, :func:`compute_review_entries`,
and the toggle state machine are plain Python and do not require
Textual. The ``AuditApp`` class is a thin driver over that model.

Toggle semantics (§6.1)
-----------------------

The four toggles are S/C/N/D:

    S       server
    C       client
    S+C     both
    N       skipped
    D       remove any existing review entry for this mod

State transitions:

    * S and C toggle independently; either or both may be on.
    * S or C turning on clears N and D.
    * N or D turning on clears the others (they are exclusive).
    * All-off and D-on both produce "no review entry" on save; D is
      a UI affordance, not a distinct persistence state.

Scope
-----

The audit shows only mods that appear in the Prism index - i.e. jars
with a ``.pw.toml``. Jars without one are reported as unmarked by the
deploy pipeline (§6.3), but are not shown here. The review section
mechanism keys off a filename; a review entry for a jar with no
``.pw.toml`` would not be picked up by the marking logic, so
including such jars would be misleading. Flagged as a limitation.

Logging
-------

Module logger is ``minecraft.deploy_pack.prompt_ui``. :class:`AuditRow`
and its methods are pure and emit nothing. The ``AuditApp`` widget
emits nothing on the hot path (``action_toggle``, ``_refresh_row``,
``_selected_row`` fire once per keystroke or repaint); only the two
terminal transitions - :meth:`AuditApp.action_save` and
:meth:`AuditApp.action_discard` - log at DEBUG. :func:`build_audit_rows`
logs at INFO with the row count and the distribution of existing
override sections; :func:`compute_review_entries` logs at DEBUG with
the number of entries that will be written. :func:`run_audit` is the
operator-facing surface: INFO on entry with row count, INFO on discard,
INFO on save with entry count, ERROR before the stderr ``print`` on
save failure, and ERROR on the defensive ``HAS_TEXTUAL`` check. The
``HAS_TEXTUAL`` guard is defense-in-depth - ``main.py`` already
checks the flag before dispatching - but logging it keeps direct
library use diagnosable.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

from . import deps
from .config_model import DeploymentConfig
from .overrides import SideOverrides, load_side_overrides, save_side_overrides

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal
    from textual.widgets import DataTable, Footer, Header, Static

    HAS_TEXTUAL = True
except ImportError:
    HAS_TEXTUAL = False
    App = None
    ComposeResult = None
    Binding = None
    Horizontal = None
    DataTable = None
    Footer = None
    Header = None
    Static = None

from .logging_setup import get_logger

_log = get_logger(__name__)

__all__ = ["HAS_TEXTUAL", "AuditRow", "build_audit_rows", "compute_review_entries", "run_audit"]
if HAS_TEXTUAL:
    __all__.append("AuditApp")
VALID_REVIEW_VALUES = ("server", "client", "both", "skipped")


@dataclass
class AuditRow:
    """One mod's audit state.

    Pure data plus pure methods. No logging: :meth:`toggle` runs once
    per keystroke and :meth:`from_entry` runs once per Prism entry, so
    per-call logging would drown the sink for no diagnostic gain.
    """

    filename: str
    declared_side: str
    declared_source: str
    override_value: str | None
    override_section: str | None
    toggle_s: bool = False
    toggle_c: bool = False
    toggle_n: bool = False
    toggle_d: bool = False

    def toggle(self, key: str) -> None:
        """Apply a toggle key. ``key`` is one of s/c/n/d."""
        key = key.lower()
        if key == "s":
            self.toggle_s = not self.toggle_s
            self.toggle_n = False
            self.toggle_d = False
        elif key == "c":
            self.toggle_c = not self.toggle_c
            self.toggle_n = False
            self.toggle_d = False
        elif key == "n":
            if self.toggle_n:
                self.toggle_n = False
            else:
                self.toggle_n = True
                self.toggle_s = False
                self.toggle_c = False
                self.toggle_d = False
        elif key == "d":
            if self.toggle_d:
                self.toggle_d = False
            else:
                self.toggle_d = True
                self.toggle_s = False
                self.toggle_c = False
                self.toggle_n = False
        else:
            raise ValueError(f"unknown toggle key: {key!r}")

    @property
    def review_value(self) -> str | None:
        """The review-section value implied by the current toggles.

        ``None`` means "no entry" - either D-on or all-off. Either
        outcome produces the same dict after save.
        """
        if self.toggle_d:
            return None
        if self.toggle_s and self.toggle_c:
            return "both"
        if self.toggle_s:
            return "server"
        if self.toggle_c:
            return "client"
        if self.toggle_n:
            return "skipped"
        return None

    def format_declared(self) -> str:
        """Formats the declared value for display."""
        if self.declared_source:
            return f"{self.declared_side} (from {self.declared_source})"
        return self.declared_side

    def format_override(self) -> str:
        """Formats the override value for display."""
        if self.override_value is None or self.override_section is None:
            return "-"
        return f"{self.override_value} ({self.override_section})"

    def format_toggle(self, key: str) -> str:
        """Formats a toggle key as a checked or unchecked indicator."""
        on = {"s": self.toggle_s, "c": self.toggle_c, "n": self.toggle_n, "d": self.toggle_d}.get(key.lower(), False)
        return "[X]" if on else "[ ]"

    @classmethod
    def from_entry(cls, entry: dict, overrides: SideOverrides) -> AuditRow | None:
        """Build a row from a Prism entry plus the loaded overrides.

        Returns None if the entry has no filename.
        """
        filename = entry.get("file")
        if not filename:
            return None
        declared_raw = entry.get("side_raw")
        declared = declared_raw if isinstance(declared_raw, str) else str(entry.get("side", "both"))
        row = cls(filename=str(filename), declared_side=declared, declared_source=str(entry.get("index_file", "")), override_value=None, override_section=None)
        mid = str(entry.get("id", ""))
        if mid and mid in overrides.by_id:
            row.override_value = overrides.by_id[mid]
            row.override_section = "by_id"
        elif filename in overrides.by_filename:
            row.override_value = overrides.by_filename[filename]
            row.override_section = "by_filename"
        elif filename in overrides.deployment_tool_review:
            row.override_value = overrides.deployment_tool_review[filename]
            row.override_section = "deployment_tool_review"
        review_val = overrides.deployment_tool_review.get(str(filename))
        if review_val == "server":
            row.toggle_s = True
        elif review_val == "client":
            row.toggle_c = True
        elif review_val == "both":
            row.toggle_s = True
            row.toggle_c = True
        elif review_val == "skipped":
            row.toggle_n = True
        return row


def build_audit_rows(config: DeploymentConfig, logger: Any = None) -> list[AuditRow]:
    """Load every indexed mod and its current override state.

    Logs at INFO with the final row count and the number of rows that
    carried a pre-existing override from each section.
    """
    if logger is None:
        logger = _log
    index_dir = config.modpack_dir / ".index"
    logger.debug(f"build_audit_rows: loading index from {index_dir}")
    entries = deps.load_prism_index(index_dir)
    overrides_path = config.config_dir / "side_overrides.toml"
    overrides = load_side_overrides(overrides_path, logger)
    rows: list[AuditRow] = []
    for entry in entries:
        row = AuditRow.from_entry(entry, overrides)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda r: r.filename.lower())
    from_by_id = sum(1 for r in rows if r.override_section == "by_id")
    from_by_filename = sum(1 for r in rows if r.override_section == "by_filename")
    from_review = sum(1 for r in rows if r.override_section == "deployment_tool_review")
    logger.info(
        f"build_audit_rows: {len(rows)} row(s) from {len(entries)} index entr(ies); "
        f"overrides by_id={from_by_id} by_filename={from_by_filename} review={from_review}"
    )
    return rows


def compute_review_entries(rows: list[AuditRow], logger: Any = None) -> dict[str, str]:
    """Build the [deployment_tool_review] dict from the row toggles.

    Rows whose :attr:`AuditRow.review_value` is None are omitted, so
    saving after toggling D (or clearing all toggles) removes the entry.
    """
    if logger is None:
        logger = _log
    out: dict[str, str] = {}
    skipped = 0
    for row in rows:
        value = row.review_value
        if value is None:
            skipped += 1
            continue
        out[row.filename] = value
    logger.debug(f"compute_review_entries: {len(rows)} row(s) -> {len(out)} entr(ies) ({skipped} without a review value)")
    return out


if HAS_TEXTUAL:
    _BINDINGS = [
        Binding("s", "toggle('s')", "Server"),
        Binding("c", "toggle('c')", "Client"),
        Binding("n", "toggle('n')", "Skip"),
        Binding("d", "toggle('d')", "Delete"),
        Binding("ctrl+s", "save", "Save & Exit"),
        Binding("escape", "discard", "Discard"),
        Binding("ctrl+c", "discard", "Quit", priority=True),
    ]

    class AuditApp(App):
        """Textual app for auditing mod side assignments (§6.1).

        Widget methods stay silent on the hot path (per-keystroke toggles,
        per-repaint row updates, per-action selection lookups). Only the
        two terminal transitions - :meth:`action_save` and
        :meth:`action_discard` - emit a DEBUG line so the closing intent
        is visible in the log.
        """

        CSS = "\n        Screen {\n            layers: base overlay;\n        }\n        #status {\n            dock: bottom;\n            height: 1;\n            background: $boost;\n            color: $text;\n            padding: 0 1;\n        }\n        "
        BINDINGS = _BINDINGS

        def __init__(self, rows: list[AuditRow]) -> None:
            super().__init__()
            self.rows = rows
            self._row_by_filename: dict[str, AuditRow] = {r.filename: r for r in rows}
            self._saved = False

        def compose(self) -> ComposeResult:
            """Builds the screen layout with a header, data table, status bar, and footer.

            Yields:
                The widgets composing the screen.
            """
            yield Header(show_clock=False)
            yield DataTable(zebra_stripes=True, cursor_type="row")
            yield Static("S server * C client * N skip * D remove * Ctrl+S save * Esc discard", id="status")
            yield Footer()

        def on_mount(self) -> None:
            """Handles the mount event."""
            table = self.query_one(DataTable)
            table.add_columns("Filename", "Declared", "Override", "S", "C", "N", "D")
            for row in self.rows:
                self._add_row(table, row)
            self.title = "deploy_pack - audit mods"
            self.sub_title = f"{len(self.rows)} entries"
            _log.debug(f"AuditApp: mounted with {len(self.rows)} row(s)")

        def _add_row(self, table: Any, row: AuditRow) -> None:
            table.add_row(
                row.filename,
                row.format_declared(),
                row.format_override(),
                row.format_toggle("s"),
                row.format_toggle("c"),
                row.format_toggle("n"),
                row.format_toggle("d"),
                key=row.filename,
            )

        def _refresh_row(self, row: AuditRow) -> None:
            table = self.query_one(DataTable)
            try:
                table.update_cell(row.filename, "S", row.format_toggle("s"))
                table.update_cell(row.filename, "C", row.format_toggle("c"))
                table.update_cell(row.filename, "N", row.format_toggle("n"))
                table.update_cell(row.filename, "D", row.format_toggle("d"))
            except Exception:
                pass

        def _selected_row(self) -> AuditRow | None:
            table = self.query_one(DataTable)
            if table.cursor_row is None:
                return None
            try:
                coord = table.cursor_coordinate
                key = table.coordinate_to_cell_key(coord).row_key.value
            except Exception:
                return None
            if key is None:
                return None
            return self._row_by_filename.get(str(key))

        def action_toggle(self, key: str) -> None:
            """Handles the toggle action."""
            row = self._selected_row()
            if row is None:
                return
            row.toggle(key)
            self._refresh_row(row)

        def action_save(self) -> None:
            """Handles the save action."""
            self._saved = True
            _log.debug("AuditApp: save requested; exiting with saved=True")
            self.exit(True)

        def action_discard(self) -> None:
            """Handles the discard action."""
            self._saved = False
            _log.debug("AuditApp: discard requested; exiting with saved=False")
            self.exit(False)

    def _app_factory(rows: list[AuditRow]) -> AuditApp:
        return AuditApp(rows)


def run_audit(config: DeploymentConfig, logger: Any = None) -> int:
    """Run the audit UI. Returns the process exit code.

    0 on save, discard, or Ctrl+C. 1 only on an unrecoverable error
    (missing Textual, config parse failure, or save write failure).

    The save path is ``overrides.save_side_overrides`` - the same
    line-based splice used by the CLI. A crash mid-save leaves the
    original file intact (§6.1, §4.10).
    """
    if logger is None:
        logger = _log
    if not HAS_TEXTUAL:
        msg = "--audit-mods requires the 'textual' package. Install it with: pip install textual"
        logger.error(f"run_audit: {msg}")
        print(f"error: {msg}", file=sys.stderr)
        return 1
    logger.info(f"run_audit: entering; config_dir={config.config_dir} modpack_dir={config.modpack_dir}")
    rows = build_audit_rows(config, logger)
    app = _app_factory(rows)
    saved = app.run()
    if not saved:
        logger.info(f"run_audit: changes discarded ({len(rows)} row(s) not persisted)")
        return 0
    entries = compute_review_entries(rows, logger)
    overrides_path = config.config_dir / "side_overrides.toml"
    try:
        save_side_overrides(overrides_path, entries, logger=logger)
    except Exception as exc:
        logger.error(f"run_audit: failed to save {overrides_path}: {exc}")
        print(f"error saving {overrides_path}: {exc}", file=sys.stderr)
        return 1
    logger.info(f"run_audit: wrote {len(entries)} review entr(ies) to {overrides_path}")
    return 0
