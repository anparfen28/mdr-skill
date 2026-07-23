#!/usr/bin/env python3
"""
format_table.py — детерминированный форматтер таблицы MDR-квалификации (ИСТОЧНИК ПРАВДЫ по цветам).

Кто бы ни записал ЗНАЧЕНИЯ (модель / writer / под-агенты), этот скрипт приводит ПРЕЗЕНТАЦИЮ
к финальным правилам Анны (17.07), чтобы цвета/merge/прочерки никогда не «поехали».

ДВЕ независимые цветовые схемы (чтобы продажнику было визуально понятно):
  • Столбец «Статус у нас» (O) — по СТАТУСУ:
        Действующий → серый #e5e5e5 · Новый → зелёный #ccefcc · В воронке → жёлтый #fff7bf · Отток → красный #f9cccc
  • Вердикт-столбцы «Компания целевая / Персона целевая / Работаем» — по СВЕТОФОРУ значения:
        Целевая/Да → зелёный · Уточнить → жёлтый · Нецелевая/Нет → красный

ДЕЙСТВУЮЩИЙ = только столбец O серым; ВСЁ остальное (оценка/работа) = прочерк «—» без заливки:
        Уровень, Индустрия, Бизнес-модель, Посещаемость, Конкурент, Сделка, Компания целевая,
        Персона целевая, Причина, Работаем. По действующему нам не важна никакая инфа.
Идентичность (ФИО/Должность/Тир/Тел/Email/Сайт), Ивент, «Был в Pipedrive», Статус — сохраняем.

Прочее: заголовок A1:C1 merge+overflow+bold; строка «Компаний с >1» A:C merge/overflow;
шапка данных — без заливки/bold; заморозка по шапку; CLIP на данные; basic filter.

Идемпотентно. По умолчанию DRY-RUN; --write применяет.
  python3 format_table.py --tab 260722_messengersfoxford [--write]
  python3 format_table.py --tab auto --write
Конфиг (sheet_id, token_path) — из config.json рядом со скиллом.
"""
import warnings, json, sys, os, argparse
warnings.filterwarnings("ignore")
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(SKILL_DIR, "config.json")

GRAY   = {"red":0.898, "green":0.898, "blue":0.898}  # e5e5e5
GREEN  = {"red":0.8,   "green":0.937, "blue":0.8}    # ccefcc
YELLOW = {"red":1.0,   "green":0.969, "blue":0.749}  # fff7bf
RED    = {"red":0.976, "green":0.8,   "blue":0.8}    # f9cccc
WHITE  = {"red":1.0,   "green":1.0,   "blue":1.0}

FIO_COL     = "ФИО"
STATUS_COL  = "Статус у нас"
VERDICT_COLS = ("Компания целевая", "Персона целевая", "Работаем")
# столбцы, которые для ДЕЙСТВУЮЩЕГО забиваем прочерком «—» без заливки
DASH_COLS_ACTIVE = ("Уровень клиента", "Индустрия", "Бизнес-модель", "Посещаемость",
                    "Конкурент", "Сделка на компании", "Компания целевая",
                    "Персона целевая", "Причина", "Работаем")
SITE_COL = "Основной сайт"
REASON_COL = "Причина"
import re
# мусорные/несуществующие домены → сразу Нецелевая (тесты/боты/не бизнес)
JUNK_DOMAINS = {"company.com","test.com","abc.com","yandex.rucompany","net.sayta.com","123.ru",
    "aaaaaaa.ru","hjhvjhmfghh.ru","none.ogr","zaya.com","no","ооо","самозанятость","адп","mindbox"}
def clean_domain(d):
    if not d: return d
    x = d.strip().lower()
    x = re.sub(r"^https?://","",x); x = re.sub(r"^www\.","",x)
    x = x.split("/")[0].split("#")[0].split("?")[0].split(",")[0].strip().rstrip(".").replace(" ","")
    return x
def is_junk(dom):
    return bool(dom) and dom in JUNK_DOMAINS

def status_fill(status):
    if status.startswith("Действующий"): return GRAY
    if status.startswith("Новый"):       return GREEN
    if status.startswith("В воронке"):   return YELLOW
    if status.startswith("Отток"):       return RED
    return WHITE

def verdict_fill(t):
    if t in ("Целевая", "Да"): return GREEN
    if t == "Уточнить": return YELLOW
    if t in ("Нецелевая", "Нет"): return RED
    if t == "—": return WHITE  # прочерк (напр. персона у нецелевой компании) — без заливки
    return None  # неизвестный текст (напр. недочищенный «В воронке…») — заливку не трогаем

def eq(a, b):
    if a is None: a = WHITE
    if b is None: return False
    return all(abs(a.get(k,0) - b.get(k,0)) < 0.02 for k in ("red","green","blue"))

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

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tab", required=True, help="имя вкладки или 'auto'")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    cfg = json.load(open(CONFIG))
    sheet_id = cfg["sheet_id"]
    svc = load_svc(cfg["token_path"], cfg)

    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheets = meta["sheets"]
    tab = sheets[-1]["properties"]["title"] if args.tab == "auto" else args.tab
    props = next((s["properties"] for s in sheets if s["properties"]["title"] == tab), None)
    if not props:
        sys.exit(f"Вкладка '{tab}' не найдена: {[s['properties']['title'] for s in sheets]}")
    gid = props["sheetId"]; ncols = props["gridProperties"]["columnCount"]
    last_col = chr(ord("A") + min(ncols, 26) - 1)

    grid = svc.spreadsheets().get(
        spreadsheetId=sheet_id, ranges=[f"{tab}!A1:{last_col}"], includeGridData=True,
        fields="sheets(merges,data(rowData(values(formattedValue,userEnteredFormat/backgroundColor))))",
    ).execute()["sheets"][0]
    rows = grid["data"][0].get("rowData", [])
    merges = grid.get("merges", [])

    def fv(r, ci):
        v = r.get("values", []); return ((v[ci].get("formattedValue") if ci < len(v) else "") or "").strip()
    def bgv(r, ci):
        v = r.get("values", []); return (v[ci].get("userEnteredFormat", {}) if ci < len(v) else {}).get("backgroundColor")

    header_i = next((i for i, r in enumerate(rows) if fv(r, 0) == FIO_COL), None)
    if header_i is None: sys.exit("Не нашёл шапку данных («ФИО»).")
    note_i = next((i for i, r in enumerate(rows[:header_i]) if fv(r, 0).startswith("Компаний с >1")), None)
    header = rows[header_i]
    col = {fv(header, ci): ci for ci in range(ncols) if fv(header, ci)}
    for need in (STATUS_COL, *VERDICT_COLS):
        if need not in col: sys.exit(f"Нет колонки «{need}». Есть: {list(col)}")

    reqs = []
    def has_merge(r0, c1):
        return any(m["startRowIndex"]==r0 and m["endRowIndex"]==r0+1 and m["startColumnIndex"]==0 and m["endColumnIndex"]==c1 for m in merges)
    def set_bg(ri, ci, color):
        reqs.append({"repeatCell":{"range":{"sheetId":gid,"startRowIndex":ri,"endRowIndex":ri+1,"startColumnIndex":ci,"endColumnIndex":ci+1},
            "cell":{"userEnteredFormat":{"backgroundColor":color}},"fields":"userEnteredFormat.backgroundColor"}})
    def set_val(ri, ci, s):
        reqs.append({"updateCells":{"range":{"sheetId":gid,"startRowIndex":ri,"endRowIndex":ri+1,"startColumnIndex":ci,"endColumnIndex":ci+1},
            "rows":[{"values":[{"userEnteredValue":{"stringValue":s}}]}],"fields":"userEnteredValue"}})

    # Заголовок + строка-заметка
    if not has_merge(0, 3):
        reqs.append({"mergeCells":{"range":{"sheetId":gid,"startRowIndex":0,"endRowIndex":1,"startColumnIndex":0,"endColumnIndex":3},"mergeType":"MERGE_ALL"}})
    reqs.append({"repeatCell":{"range":{"sheetId":gid,"startRowIndex":0,"endRowIndex":1,"startColumnIndex":0,"endColumnIndex":3},
        "cell":{"userEnteredFormat":{"wrapStrategy":"OVERFLOW_CELL","textFormat":{"bold":True}}},"fields":"userEnteredFormat.wrapStrategy,userEnteredFormat.textFormat.bold"}})
    if note_i is not None:
        if not has_merge(note_i, 3):
            reqs.append({"mergeCells":{"range":{"sheetId":gid,"startRowIndex":note_i,"endRowIndex":note_i+1,"startColumnIndex":0,"endColumnIndex":3},"mergeType":"MERGE_ALL"}})
        reqs.append({"repeatCell":{"range":{"sheetId":gid,"startRowIndex":note_i,"endRowIndex":note_i+1,"startColumnIndex":0,"endColumnIndex":3},
            "cell":{"userEnteredFormat":{"wrapStrategy":"OVERFLOW_CELL"}},"fields":"userEnteredFormat.wrapStrategy"}})

    # Шапка данных: снять заливку с вердикт-ячеек
    for cname in VERDICT_COLS:
        if not eq(bgv(header, col[cname]), WHITE):
            set_bg(header_i, col[cname], WHITE)

    stats = {"O":0, "verdict":0, "dash":0}
    for i in range(header_i+1, len(rows)):
        r = rows[i]
        status = fv(r, col[STATUS_COL])
        if not status and not fv(r, 0): continue
        # 0) чистим домен во всех строках (гигиена: единый вид, без http/www/#)
        dom = ""
        if SITE_COL in col:
            cur_site = fv(r, col[SITE_COL]); dom = clean_domain(cur_site)
            if dom and dom != cur_site: set_val(i, col[SITE_COL], dom)
        # 1) столбец O — по статусу
        want = status_fill(status)
        if not eq(bgv(r, col[STATUS_COL]), want): set_bg(i, col[STATUS_COL], want); stats["O"] += 1
        if status.startswith("Действующий"):
            # 2а) действующий → прочерки без заливки во всех оценочных столбцах
            for cname in DASH_COLS_ACTIVE:
                if cname not in col: continue
                ci = col[cname]
                if fv(r, ci) != "—": set_val(i, ci, "—"); stats["dash"] += 1
                if not eq(bgv(r, ci), WHITE): set_bg(i, ci, WHITE)
        else:
            # 2б) новый/воронке/отток → вердикт-столбцы по светофору.
            # Мусорный/несуществующий домен → Нецелевая (тест/бот, не бизнес).
            junk = is_junk(dom)
            if junk and fv(r, col["Компания целевая"]) != "Нецелевая":
                set_val(i, col["Компания целевая"], "Нецелевая")
                if REASON_COL in col and "мусор" not in fv(r, col[REASON_COL]).lower():
                    set_val(i, col[REASON_COL], f"{dom} — мусорный/несуществующий домен (тест/бот), не общаемся")
            # Компания Нецелевая (мусор/агентство/партнёр/конкурент/незаконное) → персону не оцениваем:
            #   «Персона целевая» = «—», «Работаем» = Нет (не путаем продажника).
            disq = junk or fv(r, col["Компания целевая"]) == "Нецелевая"
            for cname in VERDICT_COLS:
                ci = col[cname]; cur = fv(r, ci)
                if cname == "Компания целевая" and junk: want = "Нецелевая"
                elif disq and cname == "Персона целевая": want = "—"
                elif disq and cname == "Работаем": want = "Нет"
                else: want = cur
                if want != cur: set_val(i, ci, want)
                f = verdict_fill(want)
                if f is not None and not eq(bgv(r, ci), f): set_bg(i, ci, f); stats["verdict"] += 1

    # Заморозка / CLIP / фильтр
    # заморозка: строки по шапку данных включительно + столбцы A–F (идентичность: ФИО..Основной сайт)
    reqs.append({"updateSheetProperties":{"properties":{"sheetId":gid,"gridProperties":{"frozenRowCount":header_i+1,"frozenColumnCount":6}},"fields":"gridProperties.frozenRowCount,gridProperties.frozenColumnCount"}})
    reqs.append({"repeatCell":{"range":{"sheetId":gid,"startRowIndex":header_i+1,"endRowIndex":len(rows),"startColumnIndex":0,"endColumnIndex":ncols},
        "cell":{"userEnteredFormat":{"wrapStrategy":"CLIP"}},"fields":"userEnteredFormat.wrapStrategy"}})
    # текст во ВСЕХ ячейках — по верхнему краю (длинные Бриф/Заходы иначе висят по центру/низу)
    reqs.append({"repeatCell":{"range":{"sheetId":gid,"startRowIndex":0,"endRowIndex":props["gridProperties"]["rowCount"],
        "startColumnIndex":0,"endColumnIndex":ncols},
        "cell":{"userEnteredFormat":{"verticalAlignment":"TOP"}},"fields":"userEnteredFormat.verticalAlignment"}})
    reqs.append({"setBasicFilter":{"filter":{"range":{"sheetId":gid,"startRowIndex":header_i,"endRowIndex":len(rows),"startColumnIndex":0,"endColumnIndex":ncols}}}})

    print(f"Вкладка {tab} | шапка стр.{header_i+1} | O-заливок={stats['O']} вердикт-заливок={stats['verdict']} прочерков(действ.)={stats['dash']} | запросов={len(reqs)}")
    if args.write:
        svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": reqs}).execute()
        print("ПРИМЕНЕНО.")
    else:
        print("DRY-RUN. Добавь --write.")

if __name__ == "__main__":
    main()
