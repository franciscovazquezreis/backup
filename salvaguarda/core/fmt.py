"""Number, size, duration and date formatting in European Portuguese."""

from __future__ import annotations

import datetime as _dt
from typing import Optional

NBSP = " "

_MONTHS = (
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)


def number(value: int) -> str:
    """12345 -> '12 345' (non-breaking space as thousands separator)."""
    text = "{:,}".format(int(value))
    return text.replace(",", NBSP)


def decimal(value: float, places: int = 1) -> str:
    text = "{:.{}f}".format(value, places)
    return text.replace(".", ",")


def size(num_bytes: Optional[int]) -> str:
    """Decimal units, like the file managers in GNOME and Cinnamon."""
    if num_bytes is None:
        return "?"
    value = float(num_bytes)
    if value < 1000:
        n = int(value)
        return "{}{}byte{}".format(n, NBSP, "" if n == 1 else "s")
    for unit in ("kB", "MB", "GB", "TB", "PB"):
        value /= 1000.0
        if value < 1000 or unit == "PB":
            places = 0 if value >= 100 else 1
            return "{}{}{}".format(decimal(value, places), NBSP, unit)
    return str(num_bytes)


def plural(count: int, singular: str, plural_form: str) -> str:
    return "{} {}".format(number(count), singular if count == 1 else plural_form)


def files(count: int) -> str:
    return plural(count, "ficheiro", "ficheiros")


def agree(count: int, singular: str, plural_form: str) -> str:
    """Only the word, agreeing with ``count`` ('copiado' / 'copiados')."""
    return singular if count == 1 else plural_form


def duration(seconds: float) -> str:
    seconds = int(round(max(0.0, seconds)))
    if seconds < 60:
        return "{} s".format(seconds)
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return "{} min {} s".format(minutes, sec) if sec else "{} min".format(minutes)
    hours, minutes = divmod(minutes, 60)
    return "{} h {} min".format(hours, minutes) if minutes else "{} h".format(hours)


def remaining(seconds: Optional[float]) -> str:
    """Human estimate of time left, deliberately coarse."""
    if seconds is None:
        return ""
    if seconds < 10:
        return "quase a terminar"
    if seconds < 60:
        return "menos de um minuto"
    minutes = int(round(seconds / 60.0))
    if minutes < 60:
        return "cerca de {}".format(plural(minutes, "minuto", "minutos"))
    hours, minutes = divmod(minutes, 60)
    if minutes == 0:
        return "cerca de {}".format(plural(hours, "hora", "horas"))
    return "cerca de {} e {}".format(
        plural(hours, "hora", "horas"), plural(minutes, "minuto", "minutos")
    )


def speed(bytes_per_second: Optional[float]) -> str:
    if not bytes_per_second:
        return ""
    return "{}/s".format(size(int(bytes_per_second)))


def date_long(moment: _dt.datetime) -> str:
    """'16 de outubro de 2026'"""
    return "{} de {} de {}".format(moment.day, _MONTHS[moment.month - 1], moment.year)


def datetime_short(moment: _dt.datetime) -> str:
    """'16/10/2026, 15:30'"""
    return moment.strftime("%d/%m/%Y, %H:%M")


def time_short(moment: _dt.datetime) -> str:
    return moment.strftime("%H:%M")


def relative(moment: Optional[_dt.datetime], now: Optional[_dt.datetime] = None) -> str:
    """'hoje às 15:30', 'ontem às 09:12', 'há 3 dias', '2 de março de 2026'."""
    if moment is None:
        return "nunca"
    now = now or _dt.datetime.now()
    delta = now - moment
    if delta.total_seconds() < 0:
        return datetime_short(moment)
    if delta.total_seconds() < 60:
        return "agora mesmo"
    if delta.total_seconds() < 3600:
        minutes = int(delta.total_seconds() // 60)
        return "há {}".format(plural(minutes, "minuto", "minutos"))
    days = (now.date() - moment.date()).days
    if days == 0:
        return "hoje às {}".format(time_short(moment))
    if days == 1:
        return "ontem às {}".format(time_short(moment))
    if days < 31:
        return "há {} dias".format(days)
    return date_long(moment)


def days_since(moment: Optional[_dt.datetime], now: Optional[_dt.datetime] = None) -> Optional[int]:
    if moment is None:
        return None
    now = now or _dt.datetime.now()
    return (now.date() - moment.date()).days


def parse_iso(text: Optional[str]) -> Optional[_dt.datetime]:
    if not text:
        return None
    try:
        return _dt.datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def iso(moment: _dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def stamp(moment: _dt.datetime) -> str:
    """Folder-safe timestamp: 2026-10-16_15-30-12"""
    return moment.strftime("%Y-%m-%d_%H-%M-%S")


def parse_stamp(text: str) -> Optional[_dt.datetime]:
    try:
        return _dt.datetime.strptime(text[:19], "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None
