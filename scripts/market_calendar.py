#!/usr/bin/env python3
"""US equity market session status for today (HST calendar date).

Prints exactly one line:
  open
  closed:weekend
  closed:holiday:<name>

NYSE full-closure holidays, computed by rule (no API):
New Year's Day, MLK Day, Washington's Birthday, Good Friday, Memorial Day,
Juneteenth, Independence Day, Labor Day, Thanksgiving, Christmas.
Sat holidays observe Friday; Sun holidays observe Monday.

Note: 6am HST = noon ET same calendar date, so today's HST date is the
correct US session date for the morning brief.
"""
import datetime, zoneinfo


def easter(year):
    """Anonymous Gregorian computus → Easter Sunday."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return datetime.date(year, month, day + 1)


def nth_weekday(year, month, weekday, n):
    """n-th <weekday> (0=Mon) of month; n=-1 for last."""
    if n > 0:
        d = datetime.date(year, month, 1)
        offset = (weekday - d.weekday()) % 7 + (n - 1) * 7
        return d + datetime.timedelta(days=offset)
    d = (datetime.date(year, month + 1, 1) if month < 12
         else datetime.date(year + 1, 1, 1)) - datetime.timedelta(days=1)
    return d - datetime.timedelta(days=(d.weekday() - weekday) % 7)


def observed(d):
    """NYSE observation shift: Sat → Fri, Sun → Mon."""
    if d.weekday() == 5:
        return d - datetime.timedelta(days=1)
    if d.weekday() == 6:
        return d + datetime.timedelta(days=1)
    return d


def nyse_holidays(year):
    return {
        observed(datetime.date(year, 1, 1)):   "New Year's Day",
        nth_weekday(year, 1, 0, 3):            "MLK Day",
        nth_weekday(year, 2, 0, 3):            "Washington's Birthday",
        easter(year) - datetime.timedelta(2):  "Good Friday",
        nth_weekday(year, 5, 0, -1):           "Memorial Day",
        observed(datetime.date(year, 6, 19)):  "Juneteenth",
        observed(datetime.date(year, 7, 4)):   "Independence Day",
        nth_weekday(year, 9, 0, 1):            "Labor Day",
        nth_weekday(year, 11, 3, 4):           "Thanksgiving",
        observed(datetime.date(year, 12, 25)): "Christmas",
    }


def main():
    today = datetime.datetime.now(zoneinfo.ZoneInfo('Pacific/Honolulu')).date()
    if today.weekday() >= 5:
        print("closed:weekend")
        return
    name = nyse_holidays(today.year).get(today)
    print(f"closed:holiday:{name}" if name else "open")


if __name__ == "__main__":
    main()
