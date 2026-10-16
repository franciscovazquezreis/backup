"""Progress and result views shared by the backup and the restore pages."""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from ..core import fmt, paths  # noqa: E402
from ..core.engine import PHASE_COPY, PHASE_FINISH, PHASE_PREPARE, PHASE_SCAN, Issue, Progress  # noqa: E402
from . import common  # noqa: E402


class ProgressView(Gtk.Box):
    def __init__(self, verb: str = "A copiar", hint: str = "Podes continuar a usar o computador. "
                 "Não desligues o disco até ao fim.", on_cancel: Optional[Callable[[], None]] = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.verb = verb
        self.set_valign(Gtk.Align.CENTER)
        self.set_margin_top(24)
        self.set_margin_bottom(24)

        self.spinner = Gtk.Spinner()
        self.spinner.set_size_request(32, 32)
        self.pack_start(self.spinner, False, False, 0)

        self.title = common.label(verb + "…", ("sg-hero-title",), xalign=0.5,
                                  justify=Gtk.Justification.CENTER)
        self.pack_start(self.title, False, False, 0)
        self.hint = common.label(hint, ("sg-dim",), wrap=True, xalign=0.5,
                                 justify=Gtk.Justification.CENTER, max_chars=60)
        self.pack_start(self.hint, False, False, 4)

        self.bar = Gtk.ProgressBar()
        common.add_classes(self.bar, "sg-progress")
        self.bar.set_margin_top(12)
        self.pack_start(self.bar, False, False, 0)

        self.amounts = common.label("", (), xalign=0.5, justify=Gtk.Justification.CENTER)
        self.pack_start(self.amounts, False, False, 0)
        self.timing = common.label("", ("sg-dim",), xalign=0.5, justify=Gtk.Justification.CENTER)
        self.pack_start(self.timing, False, False, 0)
        self.current = common.label("", ("sg-dim", "sg-small"), xalign=0.5,
                                    ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.pack_start(self.current, False, False, 0)

        self.cancel_button = Gtk.Button.new_with_mnemonic("_Cancelar")
        common.add_classes(self.cancel_button, "sg-pill")
        self.cancel_button.set_halign(Gtk.Align.CENTER)
        self.cancel_button.set_margin_top(16)
        if on_cancel is not None:
            self.cancel_button.connect("clicked", lambda *_: on_cancel())
        self.pack_start(self.cancel_button, False, False, 0)
        self._pulse_id = 0

    def reset(self) -> None:
        self.title.set_text(self.verb + "…")
        self.bar.set_fraction(0.0)
        self.amounts.set_text("")
        self.timing.set_text("")
        self.current.set_text("")
        self.cancel_button.set_sensitive(True)
        self.spinner.start()
        self._start_pulse()

    def stop(self) -> None:
        self.spinner.stop()
        self._stop_pulse()

    def _start_pulse(self) -> None:
        if not self._pulse_id:
            self._pulse_id = GLib.timeout_add(120, self._pulse)

    def _stop_pulse(self) -> None:
        if self._pulse_id:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = 0

    def _pulse(self) -> bool:
        self.bar.pulse()
        return True

    def set_phase(self, phase: str, text: str) -> None:
        self.title.set_text(text)
        if phase in (PHASE_PREPARE, PHASE_SCAN, PHASE_FINISH):
            self._start_pulse()
            self.timing.set_text("")
            if phase == PHASE_FINISH:
                self.amounts.set_text("")
                self.current.set_text("")
                self.cancel_button.set_sensitive(False)
        else:
            self._stop_pulse()

    def update(self, p: Progress) -> None:
        if p.phase == PHASE_SCAN:
            self.amounts.set_text("{} {}".format(fmt.files(p.scanned),
                                                 fmt.agree(p.scanned, "verificado", "verificados")))
            self.current.set_text(paths.safe_text(paths.display_path(p.current)) if p.current else "")
            return
        if p.phase != PHASE_COPY:
            return
        self._stop_pulse()
        self.bar.set_fraction(p.fraction)
        self.title.set_text("{}… {} %".format(self.verb, int(p.fraction * 100)))
        parts = []
        if p.bytes_total:
            parts.append("{} de {}".format(fmt.size(min(p.bytes_done, p.bytes_total)),
                                           fmt.size(p.bytes_total)))
        if p.files_total:
            parts.append("{} de {}".format(fmt.number(min(p.files_done, p.files_total)),
                                           fmt.files(p.files_total)))
        self.amounts.set_text(" · ".join(parts))
        timing = []
        if p.eta is not None:
            timing.append(fmt.remaining(p.eta))
        if p.speed:
            timing.append(fmt.speed(p.speed))
        self.timing.set_text(" · ".join(timing))
        self.current.set_text(paths.safe_text(paths.display_path(p.current)) if p.current else "")


STATUS_STYLE = {
    "ok": ("emblem-ok-symbolic", "sg-success"),
    "nothing": ("emblem-ok-symbolic", "sg-success"),
    "warnings": ("dialog-warning-symbolic", "sg-warning"),
    "cancelled": ("process-stop-symbolic", "sg-dim"),
    "failed": ("dialog-error-symbolic", "sg-error"),
}


class ResultView(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.set_valign(Gtk.Align.CENTER)
        self.set_margin_top(24)
        self.set_margin_bottom(24)
        self.icon_holder = Gtk.Box()
        self.icon_holder.set_halign(Gtk.Align.CENTER)
        self.pack_start(self.icon_holder, False, False, 0)
        self.title = common.label("", ("sg-hero-title",), xalign=0.5, wrap=True,
                                  justify=Gtk.Justification.CENTER)
        self.pack_start(self.title, False, False, 0)
        self.message = common.label("", (), xalign=0.5, wrap=True,
                                    justify=Gtk.Justification.CENTER, max_chars=60)
        self.message.set_selectable(True)
        self.pack_start(self.message, False, False, 0)
        self.notes = common.label("", ("sg-dim",), xalign=0.5, wrap=True,
                                  justify=Gtk.Justification.CENTER, max_chars=64)
        self.pack_start(self.notes, False, False, 0)

        self.expander = Gtk.Expander()
        self.expander.set_margin_top(8)
        self.issue_list = common.boxed_list()
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(80)
        scroller.set_max_content_height(240)
        scroller.set_propagate_natural_height(True)
        scroller.add(self.issue_list)
        scroller.set_margin_top(8)
        self.expander.add(scroller)
        self.expander.set_no_show_all(True)
        self._issue_scroller = scroller
        self.pack_start(self.expander, False, False, 0)

        self.buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.buttons.set_halign(Gtk.Align.CENTER)
        self.buttons.set_margin_top(16)
        self.pack_start(self.buttons, False, False, 0)

    def show_result(self, status: str, title: str, message: str, notes: Sequence[str],
                    issues: Sequence[Issue], buttons: List[Gtk.Widget]) -> None:
        common.clear(self.icon_holder)
        icon_name, css = STATUS_STYLE.get(status, STATUS_STYLE["ok"])
        image = common.icon(icon_name, 64, (css, "sg-status-icon"))
        self.icon_holder.pack_start(image, False, False, 0)
        self.title.set_text(title)
        self.message.set_text(message)
        self.notes.set_text("\n".join(notes))
        self.notes.set_no_show_all(not notes)
        self.notes.set_visible(bool(notes))
        common.clear(self.issue_list)
        if issues:
            errors = sum(1 for i in issues if i.level == "error")
            warnings = len(issues) - errors
            parts = []
            if errors:
                parts.append(fmt.plural(errors, "erro", "erros"))
            if warnings:
                parts.append(fmt.plural(warnings, "aviso", "avisos"))
            self.expander.set_label("Ver " + " e ".join(parts))
            for issue in list(issues)[:300]:
                icon_name = "dialog-error-symbolic" if issue.level == "error" else "dialog-warning-symbolic"
                lrow = common.row(paths.safe_text(issue.path), issue.message, icon_name)
                lrow.title_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
                lrow.title_label.set_tooltip_text(paths.safe_text(issue.path))
                self.issue_list.add(lrow)
            if len(issues) > 300:
                self.issue_list.add(common.row("… e mais {}".format(fmt.number(len(issues) - 300)),
                                               "A lista completa está no registo."))
            self.expander.set_expanded(False)
            self._issue_scroller.show_all()
            self.expander.show()
        else:
            self.expander.hide()
        common.clear(self.buttons)
        for button in buttons:
            self.buttons.pack_start(button, False, False, 0)
        self.buttons.show_all()
        self.icon_holder.show_all()
