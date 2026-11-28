"""Small register and file helpers, from jipfit_lifecycle@d86dcdf:verify_lifecycle.py."""
import csv
import re
from datetime import date


def csv_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def csv_output(path, rows, fields):
    with path.open("x", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def text(row, key):
    return str(row.get(key) or "").strip()


def name_key(name):
    return re.sub(r"[^가-힣a-z0-9]", "", name.lower()).removesuffix("아파트")


def ymd(value):
    try:
        if re.fullmatch(r"\d{8}", value):
            return date(int(value[:4]), int(value[4:6]), int(value[6:])).isoformat()
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return ""


def residential(row):
    return (text(row, "mainAtchGbCd") == "0"
            and any(word in text(row, "mainPurpsCdNm") + text(row, "etcPurps")
                    for word in ("아파트", "공동주택")))
