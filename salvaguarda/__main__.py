"""Entry point: ``python3 -m salvaguarda`` (or the ``salvaguarda`` command)."""

import argparse
import sys

from . import APP_ID, APP_NAME, VERSION


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="salvaguarda",
        description="Cópias de segurança simples para um disco externo.",
    )
    parser.add_argument("--copiar", action="store_true",
                        help="faz a cópia sem abrir a janela (útil em atalhos de teclado)")
    parser.add_argument("--versao", action="version", version="{} {}".format(APP_NAME, VERSION))
    args, gtk_args = parser.parse_known_args(argv)

    if args.copiar:
        from .cli import run_backup

        return run_backup()

    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import GLib

        # Before GTK starts (importing it opens the display): the window class must
        # match the .desktop file so the panel shows the right icon.
        GLib.set_prgname(APP_ID)
        GLib.set_application_name(APP_NAME)
        sys.argv = [sys.argv[0], "--class=" + APP_ID] + gtk_args
        from gi.repository import Gtk  # noqa: F401
    except (ImportError, ValueError):
        sys.stderr.write(
            "A Salvaguarda precisa do GTK 3 para Python. Instala os pacotes "
            "python3-gi e gir1.2-gtk-3.0 (por exemplo: sudo apt install python3-gi "
            "gir1.2-gtk-3.0).\n")
        return 1
    from .ui.app import run

    return run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
