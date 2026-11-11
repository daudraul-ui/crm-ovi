"""Federal five-day calendar; verified against the published 2026/2027 rules.

Sources: https://www.cbr.ru/other/holidays/
2026: https://static.government.ru/media/files/4jeB8hNKm69ggOa9yDiYOli6YoAyM21i.pdf
2027: https://government.ru/docs/59913/ (17.09.2026, No. 1187)
No extrapolation to unapproved future years or regional holidays.
"""

from datetime import date

from django.core.exceptions import ValidationError


def russian_calendar(year):
    if year not in (2026, 2027):
        raise ValidationError(
            "Проверенный календарь РФ доступен только на 2026 и 2027 годы."
        )
    holidays = {date(year, 1, day) for day in range(1, 9)}
    holidays.update(
        date(year, month, day)
        for month, day in [(2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4)]
    )
    extra = {
        2026: [(1, 9), (3, 9), (5, 11), (12, 31)],
        2027: [(2, 22), (5, 3), (5, 10), (6, 14), (11, 5), (12, 31)],
    }
    holidays.update(date(year, month, day) for month, day in extra[year])
    workdays = [date(2027, 2, 20)] if year == 2027 else []
    return {
        "start": date(year, 1, 1),
        "end": date(year, 12, 31),
        "weekdays": ["0", "1", "2", "3", "4"],
        "holidays": "\n".join(d.isoformat() for d in sorted(holidays)),
        "extra_workdays": "\n".join(d.isoformat() for d in workdays),
    }
