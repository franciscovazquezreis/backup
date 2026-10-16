"""Widgets and helpers shared by the pages."""

from __future__ import annotations

import os
from typing import Callable, List, Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from .. import APP_ID  # noqa: E402
from ..core import system  # noqa: E402

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def data_path(*parts: str) -> str:
    return os.path.join(DATA_DIR, *parts)


def load_css() -> None:
    provider = Gtk.CssProvider()
    try:
        provider.load_from_path(data_path("style.css"))
    except GLib.Error:
        return
    screen = Gdk.Screen.get_default()
    if screen is not None:
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def app_icon_name() -> str:
    theme = Gtk.IconTheme.get_default()
    if theme is not None and theme.has_icon(APP_ID):
        return APP_ID
    return ""


def app_icon_image(pixel_size: int) -> Gtk.Image:
    name = app_icon_name()
    if name:
        image = Gtk.Image.new_from_icon_name(name, Gtk.IconSize.DIALOG)
        image.set_pixel_size(pixel_size)
        return image
    from gi.repository import GdkPixbuf

    path = data_path(APP_ID + ".svg")
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(path, pixel_size, pixel_size)
        return Gtk.Image.new_from_pixbuf(pixbuf)
    except GLib.Error:
        return icon("drive-harddisk-symbolic", pixel_size)


def icon(name: str, pixel_size: int = 16, classes: Sequence[str] = ()) -> Gtk.Image:
    gicon = Gio.ThemedIcon.new_with_default_fallbacks(name)
    image = Gtk.Image.new_from_gicon(gicon, Gtk.IconSize.BUTTON)
    image.set_pixel_size(pixel_size)
    for cls in classes:
        image.get_style_context().add_class(cls)
    return image


def label(text: str = "", classes: Sequence[str] = (), *, wrap: bool = False,
          xalign: float = 0.0, ellipsize: Optional[Pango.EllipsizeMode] = None,
          markup: bool = False, selectable: bool = False, max_chars: int = -1,
          justify: Gtk.Justification = Gtk.Justification.LEFT) -> Gtk.Label:
    widget = Gtk.Label()
    if markup:
        widget.set_markup(text)
    else:
        widget.set_text(text)
    widget.set_xalign(xalign)
    widget.set_justify(justify)
    if wrap:
        widget.set_line_wrap(True)
        widget.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if ellipsize is not None:
        widget.set_ellipsize(ellipsize)
    if max_chars > 0:
        widget.set_max_width_chars(max_chars)
    if selectable:
        widget.set_selectable(True)
    for cls in classes:
        widget.get_style_context().add_class(cls)
    return widget


def add_classes(widget: Gtk.Widget, *classes: str) -> Gtk.Widget:
    ctx = widget.get_style_context()
    for cls in classes:
        ctx.add_class(cls)
    return widget


def vbox(spacing: int = 6, *children: Gtk.Widget) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
    for child in children:
        box.pack_start(child, False, False, 0)
    return box


def hbox(spacing: int = 6, *children: Gtk.Widget) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing)
    for child in children:
        box.pack_start(child, False, False, 0)
    return box


def card(child: Gtk.Widget) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    add_classes(box, "sg-card")
    box.pack_start(child, True, True, 0)
    return box


def boxed_list() -> Gtk.ListBox:
    listbox = Gtk.ListBox()
    listbox.set_selection_mode(Gtk.SelectionMode.NONE)
    add_classes(listbox, "sg-boxed-list")
    return listbox


def row(title: str, subtitle: Optional[str] = None, icon_name: Optional[str] = None,
        suffix: Optional[Gtk.Widget] = None, activatable: bool = False,
        title_classes: Sequence[str] = (), extra: Optional[Gtk.Widget] = None) -> Gtk.ListBoxRow:
    lrow = Gtk.ListBoxRow()
    lrow.set_activatable(activatable)
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    if icon_name:
        image = icon(icon_name, 20)
        image.set_valign(Gtk.Align.CENTER)
        box.pack_start(image, False, False, 0)
    texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    texts.set_valign(Gtk.Align.CENTER)
    title_line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    title_label = label(title, title_classes, ellipsize=Pango.EllipsizeMode.END)
    title_line.pack_start(title_label, False, False, 0)
    if extra is not None:
        extra.set_valign(Gtk.Align.CENTER)
        title_line.pack_start(extra, False, False, 0)
    texts.pack_start(title_line, False, False, 0)
    if subtitle:
        sub = label(subtitle, ("sg-dim", "sg-small"), wrap=True, max_chars=60)
        texts.pack_start(sub, False, False, 0)
        lrow.subtitle_label = sub  # type: ignore[attr-defined]
    box.pack_start(texts, True, True, 0)
    if suffix is not None:
        suffix.set_valign(Gtk.Align.CENTER)
        box.pack_end(suffix, False, False, 0)
    lrow.add(box)
    lrow.title_label = title_label  # type: ignore[attr-defined]
    return lrow


def badge(text: str) -> Gtk.Label:
    return label(text, ("sg-badge",))


def section(title: str, description: Optional[str] = None) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    box.pack_start(label(title, ("sg-section-title",)), False, False, 0)
    if description:
        box.pack_start(label(description, ("sg-dim", "sg-small"), wrap=True, max_chars=70),
                       False, False, 0)
    return box


class Clamp(Gtk.Bin):
    """Limits the width of its child and centres it (like AdwClamp)."""

    def __init__(self, child: Gtk.Widget, max_width: int = 640):
        super().__init__()
        self.max_width = max_width
        self.add(child)

    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_get_preferred_width(self):
        child = self.get_child()
        if child is None or not child.get_visible():
            return 0, 0
        minimum, natural = child.get_preferred_width()
        return minimum, max(minimum, min(self.max_width, max(natural, self.max_width)))

    def do_get_preferred_height_for_width(self, width):
        child = self.get_child()
        if child is None or not child.get_visible():
            return 0, 0
        return child.get_preferred_height_for_width(min(width, self.max_width))

    def do_get_preferred_height(self):
        child = self.get_child()
        if child is None or not child.get_visible():
            return 0, 0
        return child.get_preferred_height_for_width(self.max_width)

    def do_size_allocate(self, allocation):
        self.set_allocation(allocation)
        child = self.get_child()
        if child is None or not child.get_visible():
            return
        width = min(allocation.width, self.max_width)
        rect = Gdk.Rectangle()
        rect.x = allocation.x + (allocation.width - width) // 2
        rect.y = allocation.y
        rect.width = width
        rect.height = allocation.height
        child.size_allocate(rect)


def scrolled_page(content: Gtk.Widget, max_width: int = 640) -> Gtk.ScrolledWindow:
    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    add_classes(content, "sg-page")
    scroller.add(Clamp(content, max_width))
    return scroller


# ------------------------------------------------------------------ dialogs
def message(parent: Optional[Gtk.Window], title: str, text: str = "",
            kind: Gtk.MessageType = Gtk.MessageType.INFO) -> None:
    dialog = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=kind,
                               buttons=Gtk.ButtonsType.CLOSE, text=title)
    if text:
        dialog.format_secondary_text(text)
    dialog.run()
    dialog.destroy()


def choose(parent: Optional[Gtk.Window], title: str, text: str,
           options: Sequence[Tuple[str, str]], details: Sequence[str] = (),
           destructive: Optional[str] = None, suggested: Optional[str] = None,
           kind: Gtk.MessageType = Gtk.MessageType.QUESTION) -> str:
    """Ask a question; the first option is the safe answer (also used on Esc)."""
    dialog = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=kind,
                               buttons=Gtk.ButtonsType.NONE, text=title)
    body = text
    if details:
        body += "\n\n" + "\n".join("• " + d for d in details)
    dialog.format_secondary_text(body)
    for index, (answer, button_label) in enumerate(options):
        button = dialog.add_button(button_label, index)
        if answer == destructive:
            add_classes(button, "destructive-action")
        elif answer == suggested or (suggested is None and destructive is None
                                     and index == len(options) - 1 and len(options) > 1):
            add_classes(button, "suggested-action")
    dialog.set_default_response(0)
    response = dialog.run()
    dialog.destroy()
    if isinstance(response, int) and 0 <= response < len(options):
        return options[response][0]
    return options[0][0]


def confirm(parent: Optional[Gtk.Window], title: str, text: str, ok_label: str,
            cancel_label: str = "Cancelar", destructive: bool = False) -> bool:
    answer = choose(parent, title, text, [("no", cancel_label), ("yes", ok_label)],
                    destructive="yes" if destructive else None,
                    suggested=None if destructive else "yes")
    return answer == "yes"


def choose_folder(parent: Optional[Gtk.Window], title: str, accept: str = "Escolher",
                  start: Optional[str] = None, files: bool = False) -> Optional[str]:
    action = Gtk.FileChooserAction.OPEN if files else Gtk.FileChooserAction.SELECT_FOLDER
    chooser = Gtk.FileChooserNative.new(title, parent, action, accept, "Cancelar")
    chooser.set_modal(True)
    if start and os.path.isdir(start):
        chooser.set_current_folder(start)
    try:
        chooser.set_show_hidden(True)
    except (AttributeError, TypeError):
        pass
    result = None
    if chooser.run() == Gtk.ResponseType.ACCEPT:
        result = chooser.get_filename()
    chooser.destroy()
    return result


def open_path(path: str) -> None:
    try:
        Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(path, None), None)
    except GLib.Error:
        system.open_in_file_manager(path)


def clear(container: Gtk.Container) -> None:
    for child in container.get_children():
        container.remove(child)
        child.destroy()


def idle(func: Callable, *args) -> None:
    def _call():
        func(*args)
        return False

    GLib.idle_add(_call)


def join_names(names: List[str], limit: int = 3) -> str:
    if not names:
        return ""
    if len(names) <= limit:
        if len(names) == 1:
            return names[0]
        return ", ".join(names[:-1]) + " e " + names[-1]
    return ", ".join(names[:limit]) + " e mais {}".format(len(names) - limit)
