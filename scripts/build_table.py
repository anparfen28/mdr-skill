#!/usr/bin/env python3
"""
build_table.py — СОЗДАЁТ вкладку ивента и пишет в неё канон (заголовок + сводка + шапка + данные).

Это «writer». Пара к `format_table.py`:
  build_table.py  → создаёт вкладку и пишет ЗНАЧЕНИЯ + структуру (merge/заморозка/фильтр/ширины)
  format_table.py → накатывает ЦВЕТА и гигиену (2 схемы, прочерки, чистка доменов, мусор→нецелевая)

Порядок на новом ивенте:
  1) python3 scripts/build_table.py --rows rows.json --tab 260722_messengersfoxford --write
  2) python3 scripts/format_table.py --tab 260722_messengersfoxford --write

ВХОДНОЙ КОНТРАКТ (rows.json):
{
  "event_code": "260722_on_messengersfoxford",   # полный код (с _on_), идёт в заголовок
  "tab": "260722_messengersfoxford",             # необязательно; по умолчанию = event_code без "_on_"
  "rows": [
    {"ФИО": "Иван Иванов", "Должность": "CRM-маркетолог", ..., "Статус у нас": "Новый",
     "Компания целевая": "Целевая", "Персона целевая": "Целевая", "Работаем": "Да",
     "_org_id": 12345}                            # служебное поле (не колонка) — для счёта КОМПАНИЙ
  ]
}
Любая из 23 колонок может отсутствовать — будет пустой. `_org_id` необязателен: если его нет,
компании считаются по нормализованному домену из «Основной сайт».

Сводка (Всего / Действующий / В воронке / Отток / Новый / Работаем (Да) + «Компаний с >1 регистрантом»)
считается ИЗ ДАННЫХ — руками не заполнять.

По умолчанию DRY-RUN (показывает, что создаст). `--write` применяет. Вкладка уже есть → ошибка,
если не передан `--force` (канон: 1 ивент = 1 своя вкладка, чужую не трогаем).
"""
import warnings, json, sys, os, re, argparse
warnings.filterwarnings("ignore")
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(SKILL_DIR, "config.json")

# КАНОН: 23 колонки в фиксированном порядке (19 квалификации + 4 части 2)
COLUMNS = [
    "ФИО", "Должность", "Тир ЛПР", "Телефон", "Email", "Основной сайт",
    "Уровень клиента", "Индустрия", "Бизнес-модель", "Посещаемость", "Конкурент",
    "Сделка на компании", "Ивент", "Уже был в Pipedrive", "Статус у нас",
    "Компания целевая", "Персона целевая", "Причина", "Работаем",
    "Бриф", "Кейсы", "Заход — Telegram", "Заход — Email",
]
STATUS_ORDER = ["Действующий", "В воронке", "Отток (win-back)", "Новый"]
HEADER_ROW = 11          # строка шапки данных (1-based) — совпадает с format_table
DATA_START = HEADER_ROW + 1
PART2_START_IDX = COLUMNS.index("Бриф")   # колонки части 2 делаем шире

def clean_domain(d):
    if not d: return ""
    x = str(d).strip().lower()
    x = re.sub(r"^https?://", "", x); x = re.sub(r"^www\.", "", x)
    return x.split("/")[0].split("#")[0].split("?")[0].split(",")[0].strip().rstrip(".").replace(" ", "")

def org_key(row):
    """Ключ компании для подсчёта: _org_id, иначе нормализованный домен."""
    if row.get("_org_id"): return f"id:{row['_org_id']}"
    d = clean_domain(row.get("Основной сайт"))
    return f"dom:{d}" if d else None

def load_svc(token_path, cfg=None):
    """OAuth пользователя — основной путь. Сервис-аккаунт — запасной.

    ⚠️ OAuth-приложение в testing-режиме: refresh-токен живёт ~7 дней и потом
    отдаёт invalid_grant. Прогон на 1100 регистрантов при этом уже отработал, и
    падать на последнем шаге из-за протухшего токена — терять всю работу.
    Поэтому: токена нет / протух → берём service_account_path из config.json,
    если он там задан И у аккаунта уже есть доступ к таблице. Просить кого-то
    расшаривать файл на сервис-аккаунт НЕ надо: нет доступа — чиним OAuth.
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

def compute_summary(rows):
    """Считает сводку из данных: по статусам (компаний/людей) + Работаем(Да) + >1 регистранта."""
    ppl, comp = {}, {}
    all_orgs, work_orgs, work_ppl = set(), set(), 0
    per_org_people = {}
    for r in rows:
        st = (r.get("Статус у нас") or "").strip()
        k = org_key(r)
        ppl[st] = ppl.get(st, 0) + 1
        comp.setdefault(st, set())
        if k:
            comp[st].add(k); all_orgs.add(k)
            per_org_people[k] = per_org_people.get(k, 0) + 1
        if (r.get("Работаем") or "").strip() == "Да":
            work_ppl += 1
            if k: work_orgs.add(k)
    lines = [["Статус у нас", "Компаний", "Людей"],
             ["Всего", str(len(all_orgs)), str(len(rows))]]
    for st in STATUS_ORDER:
        lines.append([st, str(len(comp.get(st, set()))), str(ppl.get(st, 0))])
    lines.append(["Работаем (Да)", str(len(work_orgs)), str(work_ppl)])
    multi = sum(1 for k, n in per_org_people.items() if n > 1)
    note = f"Компаний с >1 регистрантом: {multi}  (делить между MDR по компаниям)"
    return lines, note

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", required=True, help="путь к rows.json (см. контракт в докстринге)")
    ap.add_argument("--tab", help="имя вкладки; по умолчанию из json или event_code без _on_")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--force", action="store_true", help="писать, даже если вкладка уже существует")
    args = ap.parse_args()

    payload = json.load(open(args.rows, encoding="utf-8"))
    rows = payload.get("rows") or []
    if not rows: sys.exit("В rows.json нет строк (ключ 'rows').")
    event_code = payload.get("event_code") or ""
    tab = args.tab or payload.get("tab") or event_code.replace("_on_", "_")
    if not tab: sys.exit("Не задано имя вкладки (--tab / tab / event_code).")

    cfg = json.load(open(CONFIG, encoding="utf-8"))
    svc = load_svc(cfg["token_path"], cfg); sheet_id = cfg["sheet_id"]

    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = {s["properties"]["title"]: s["properties"] for s in meta["sheets"]}
    summary, note = compute_summary(rows)
    ncols, nrows = len(COLUMNS), DATA_START + len(rows) + 5

    print(f"Вкладка: {tab} | ивент: {event_code or '—'} | строк данных: {len(rows)} | колонок: {ncols}")
    print("Сводка:"); [print("   ", " · ".join(l)) for l in summary]; print("   ", note)
    if tab in existing and not args.force:
        print(f"⚠️ Вкладка '{tab}' уже существует. Канон: 1 ивент = своя вкладка. Передай --force, если правда надо переписать.")
        if not args.write: print("DRY-RUN."); return
        sys.exit(1)
    if not args.write:
        print("DRY-RUN. Добавь --write, чтобы создать вкладку и записать."); return

    # 1) создать вкладку
    if tab not in existing:
        resp = svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": [
            {"addSheet": {"properties": {"title": tab, "gridProperties": {"rowCount": nrows, "columnCount": ncols}}}}]}).execute()
        gid = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
    else:
        gid = existing[tab]["sheetId"]

    # 2) значения: заголовок, сводка, заметка, шапка, данные
    data = [
        {"range": f"{tab}!A1", "values": [[f"СВОДКА · Ивент {event_code or tab}"]]},
        {"range": f"{tab}!A3:C{3+len(summary)-1}", "values": summary},
        {"range": f"{tab}!A{3+len(summary)}", "values": [[note]]},
        {"range": f"{tab}!A{HEADER_ROW}", "values": [COLUMNS]},
    ]
    body_rows = [[str(r.get(c, "") or "") for c in COLUMNS] for r in rows]
    data.append({"range": f"{tab}!A{DATA_START}", "values": body_rows})
    svc.spreadsheets().values().batchUpdate(spreadsheetId=sheet_id,
        body={"valueInputOption": "RAW", "data": data}).execute()

    # 3) структура: merge заголовка и заметки, жирные шапки, заморозка (строки + A–F), фильтр, CLIP, ширины
    note_row0 = 3 + len(summary) - 1           # 0-based индекс строки заметки
    last_row = DATA_START + len(rows) - 1
    reqs = [
        {"mergeCells": {"range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": 1,
            "startColumnIndex": 0, "endColumnIndex": 3}, "mergeType": "MERGE_ALL"}},
        {"repeatCell": {"range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": 1,
            "startColumnIndex": 0, "endColumnIndex": 3},
            "cell": {"userEnteredFormat": {"wrapStrategy": "OVERFLOW_CELL", "textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.wrapStrategy,userEnteredFormat.textFormat.bold"}},
        {"mergeCells": {"range": {"sheetId": gid, "startRowIndex": note_row0, "endRowIndex": note_row0+1,
            "startColumnIndex": 0, "endColumnIndex": 3}, "mergeType": "MERGE_ALL"}},
        {"repeatCell": {"range": {"sheetId": gid, "startRowIndex": note_row0, "endRowIndex": note_row0+1,
            "startColumnIndex": 0, "endColumnIndex": 3},
            "cell": {"userEnteredFormat": {"wrapStrategy": "OVERFLOW_CELL"}}, "fields": "userEnteredFormat.wrapStrategy"}},
        # жирные: строка-шапка сводки и шапка данных
        {"repeatCell": {"range": {"sheetId": gid, "startRowIndex": 2, "endRowIndex": 3,
            "startColumnIndex": 0, "endColumnIndex": 3},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}}, "fields": "userEnteredFormat.textFormat.bold"}},
        {"repeatCell": {"range": {"sheetId": gid, "startRowIndex": HEADER_ROW-1, "endRowIndex": HEADER_ROW,
            "startColumnIndex": 0, "endColumnIndex": ncols},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}, "wrapStrategy": "CLIP"}},
            "fields": "userEnteredFormat.textFormat.bold,userEnteredFormat.wrapStrategy"}},
        # заморозка: строки по шапку + столбцы A–F (идентичность)
        {"updateSheetProperties": {"properties": {"sheetId": gid,
            "gridProperties": {"frozenRowCount": HEADER_ROW, "frozenColumnCount": 6}},
            "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"}},
        # данные: CLIP
        {"repeatCell": {"range": {"sheetId": gid, "startRowIndex": DATA_START-1, "endRowIndex": last_row,
            "startColumnIndex": 0, "endColumnIndex": ncols},
            "cell": {"userEnteredFormat": {"wrapStrategy": "CLIP"}}, "fields": "userEnteredFormat.wrapStrategy"}},
        # фильтр на шапке данных
        {"setBasicFilter": {"filter": {"range": {"sheetId": gid, "startRowIndex": HEADER_ROW-1,
            "endRowIndex": last_row, "startColumnIndex": 0, "endColumnIndex": ncols}}}},
        # ширины: колонки части 2 (Бриф/Кейсы/Заходы) — шире
        {"updateDimensionProperties": {"range": {"sheetId": gid, "dimension": "COLUMNS",
            "startIndex": PART2_START_IDX, "endIndex": ncols}, "properties": {"pixelSize": 320}, "fields": "pixelSize"}},
        # текст во ВСЕХ ячейках — по верхнему краю (иначе длинные Бриф/Заходы висят по низу)
        {"repeatCell": {"range": {"sheetId": gid, "startRowIndex": 0, "endRowIndex": nrows,
            "startColumnIndex": 0, "endColumnIndex": ncols},
            "cell": {"userEnteredFormat": {"verticalAlignment": "TOP"}}, "fields": "userEnteredFormat.verticalAlignment"}},
    ]
    svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": reqs}).execute()
    print(f"ГОТОВО: вкладка '{tab}' создана и заполнена ({len(rows)} строк).")
    print(f"Теперь накати цвета:  python3 scripts/format_table.py --tab {tab} --write")

if __name__ == "__main__":
    main()
