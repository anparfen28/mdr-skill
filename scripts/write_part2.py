#!/usr/bin/env python3
"""
write_part2.py — writer ЧАСТИ 2: пишет «Бриф» · «Кейсы» · «Заход — Telegram» · «Заход — Email»
в уже существующую вкладку ивента (созданную `build_table.py`).

Зачем отдельный скрипт: часть 2 идёт ПОСЛЕ подтверждения SDR и часто волнами (агенты
отрабатывают лидов пачками). Гонять ради этого `build_table.py` нельзя — он пересоздаёт вкладку.
Этот скрипт точечно дописывает 4 колонки в НУЖНЫЕ строки и ничего больше не трогает.

ВХОДНОЙ КОНТРАКТ — каталог с файлами `res_<ROW>.json`, по одному на персону:
{
  "row": 52,                 # номер строки в Google Sheet (1-based, как в UI)
  "fio": "Феофанова Ирина",  # для сверки, что пишем в ту строку
  "brief": "БРИФ — ...",
  "cases": "КЕЙСЫ ...",
  "tg":    "текст сообщения в Telegram",
  "email": "Тема: ...\n\nтекст письма"
}

Запуск:
  python3 scripts/write_part2.py --dir <каталог с res_*.json> --tab 260722_messengersfoxford          # dry-run
  python3 scripts/write_part2.py --dir <каталог> --tab 260722_messengersfoxford --write

По умолчанию DRY-RUN: показывает, что и куда запишет, и СВЕРЯЕТ ФИО в строке с `fio` из файла.
Расхождение ФИО → строка пропускается (защита от записи не в ту строку после сдвигов).
Уже заполненные ячейки не перезатираются без `--force`.
"""
import warnings, json, sys, os, re, argparse, glob
warnings.filterwarnings("ignore")
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(SKILL_DIR, "config.json")

PART2_COLUMNS = ["Бриф", "Кейсы", "Заход — Telegram", "Заход — Email"]
KEY_MAP = {"Бриф": "brief", "Кейсы": "cases", "Заход — Telegram": "tg", "Заход — Email": "email"}
HEADER_ROW = 11          # строка шапки данных (1-based) — совпадает с build_table/format_table


def a1(col_idx):
    """0-based индекс колонки → буква(ы) A1."""
    s = ""
    n = col_idx + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def load_svc(token_path, cfg=None):
    """OAuth пользователя — основной путь. Сервис-аккаунт — запасной.

    Тот же fallback, что в build_table.py/format_table.py: OAuth-приложение в
    testing-режиме, refresh-токен живёт ~7 дней и потом отдаёт invalid_grant.
    Часть 2 стоит десятки агентов — падать на записи из-за токена нельзя.
    """
    try:
        creds = Credentials.from_authorized_user_file(os.path.expanduser(token_path))
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return build("sheets", "v4", credentials=creds)
    except Exception as e:
        sa = (cfg or {}).get("service_account_path")
        if not sa or not os.path.exists(os.path.expanduser(sa)):
            raise SystemExit(
                f"OAuth-токен не работает ({str(e)[:90]}).\n"
                f"Почини: python3 scripts/setup_oauth.py — или задай service_account_path в config.json.")
        from google.oauth2 import service_account
        print(f"  ⚠️ OAuth мёртв ({str(e)[:60]}) → работаем сервис-аккаунтом")
        sacreds = service_account.Credentials.from_service_account_file(
            os.path.expanduser(sa), scopes=["https://www.googleapis.com/auth/spreadsheets"])
        return build("sheets", "v4", credentials=sacreds)


def norm_name(s):
    """Сравнение ФИО без учёта регистра/порядка слов/лишних пробелов."""
    return " ".join(sorted(re.sub(r"[^\w\s]", " ", str(s or "")).lower().split()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="каталог с res_<ROW>.json")
    ap.add_argument("--tab", required=True, help="имя вкладки ивента")
    ap.add_argument("--write", action="store_true", help="применить (иначе dry-run)")
    ap.add_argument("--force", action="store_true", help="перезаписывать непустые ячейки")
    args = ap.parse_args()

    cfg = json.load(open(CONFIG))
    svc = load_svc(cfg["token_path"], cfg)
    sid = cfg["sheet_id"]

    # шапка → индексы колонок части 2 (ищем ПО ИМЕНИ, не по позиции)
    hdr = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{args.tab}'!A{HEADER_ROW}:ZZ{HEADER_ROW}"
    ).execute().get("values", [[]])[0]
    try:
        idx = {c: hdr.index(c) for c in PART2_COLUMNS}
    except ValueError as e:
        sys.exit(f"❌ в шапке вкладки '{args.tab}' нет колонки части 2: {e}")

    files = sorted(glob.glob(os.path.join(args.dir, "res_*.json")),
                   key=lambda p: int(re.search(r"res_(\d+)", p).group(1)))
    if not files:
        sys.exit(f"❌ в {args.dir} нет файлов res_*.json")

    # текущее содержимое строк — для сверки ФИО и защиты от перезатирания
    lo = min(int(re.search(r"res_(\d+)", f).group(1)) for f in files)
    hi = max(int(re.search(r"res_(\d+)", f).group(1)) for f in files)
    cur = svc.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{args.tab}'!A{lo}:ZZ{hi}"
    ).execute().get("values", [])

    def row_vals(r):
        i = r - lo
        return cur[i] if 0 <= i < len(cur) else []

    updates, skipped, written = [], [], []
    for f in files:
        d = json.load(open(f))
        r = int(d["row"])
        rv = row_vals(r)
        sheet_fio = rv[0] if rv else ""
        if d.get("fio") and norm_name(sheet_fio) != norm_name(d["fio"]):
            skipped.append(f"строка {r}: ФИО не сходится — в таблице «{sheet_fio}», в файле «{d['fio']}»")
            continue
        for col in PART2_COLUMNS:
            val = (d.get(KEY_MAP[col]) or "").strip()
            if not val:
                continue
            ci = idx[col]
            existing = rv[ci] if ci < len(rv) else ""
            if existing.strip() and not args.force:
                skipped.append(f"строка {r} · {col}: уже заполнено ({len(existing)} симв.), пропуск (--force чтобы перезаписать)")
                continue
            updates.append({"range": f"'{args.tab}'!{a1(ci)}{r}", "values": [[val]]})
            written.append(f"строка {r} · {col} · {len(val)} симв.")

    print(f"Файлов: {len(files)} · ячеек к записи: {len(updates)}")
    for w in written:
        print("  +", w)
    for s in skipped:
        print("  ⚠️", s)

    if not args.write:
        print("\nDRY-RUN. Добавь --write, чтобы применить.")
        return
    if not updates:
        print("\nНечего писать.")
        return

    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=sid,
        body={"valueInputOption": "RAW", "data": updates},
    ).execute()
    print(f"\n✅ записано ячеек: {len(updates)}")
    print("Дальше: python3 scripts/format_table.py --tab", args.tab, "--write")


if __name__ == "__main__":
    main()
