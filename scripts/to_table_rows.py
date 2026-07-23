#!/usr/bin/env python3
"""to_table_rows.py — мост между прогоном и writer'ом таблицы.

`workflow.js` отдаёт данные в своём виде (person_id, level, company_verdict "🟢", work "да"),
а `build_table.py` ждёт КАНОН: 23 русские колонки и значения «Целевая» / «Уточнить» / «Да».
Раньше этот перевод делался руками в сессии — каждый раз заново и каждый раз чуть иначе.

⚠️ ГЛАВНОЕ: прогон квалифицирует только КАНДИДАТОВ. Действующие клиенты и заведомо
нецелевые отсекаются раньше и строк квалификации не получают. Но в таблице обязаны быть
ВСЕ регистранты — иначе сводка (Всего / Действующий / В воронке / Новый) соврёт.
Поэтому база строк — enriched (все), а вердикты накладываются поверх.

    python3 scripts/to_table_rows.py --event 260618_on_midyearreview --dir <scratchpad> --out rows.json
"""
import argparse, json, os, re, sys

COLUMNS = [
    "ФИО", "Должность", "Тир ЛПР", "Телефон", "Email", "Основной сайт",
    "Уровень клиента", "Индустрия", "Бизнес-модель", "Посещаемость", "Конкурент",
    "Сделка на компании", "Ивент", "Уже был в Pipedrive", "Статус у нас",
    "Компания целевая", "Персона целевая", "Причина", "Работаем",
    "Бриф", "Кейсы", "Заход — Telegram", "Заход — Email",
]
DASH = "—"
VERDICT = {"🟢": "Целевая", "🟡": "Уточнить", "🔴": "Нецелевая", DASH: DASH, "": ""}
WORK = {"да": "Да", "уточнить": "Уточнить", "нет": "Нет", "": ""}
DEAD_LEVELS = {"Нецелевой", "Агентство", "Компания ушла из России"}


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default if default is not None else []


def as_rows(data):
    """Файлы бывают списком или словарём {person_id: row} — принимаем оба."""
    if isinstance(data, dict):
        return list(data.values())
    return data or []


def clean_domain(d):
    d = str(d or "").strip().lower()
    d = re.sub(r"^https?:(//)?", "", d)     # встречается и «https:yandex.ru» без слэшей
    d = re.sub(r"^www\.", "", d)
    return d.split("/")[0].split("#")[0].split("?")[0].split(",")[0].strip().rstrip(".").replace(" ", "")


def verdict(v):
    v = str(v or "").strip()
    for emoji, word in VERDICT.items():
        if emoji and emoji in v:
            return word
    return v if v in ("Целевая", "Уточнить", "Нецелевая", DASH) else ""


def fmt_traffic(v):
    """Месячные визиты → компактно (352.1K / 1.2M). Строку-как-есть не портим."""
    if v in (None, "", DASH):
        return ""
    s = str(v).strip()
    if re.match(r"^[\d.,]+\s*[KМMКk]?$", s) and not s.replace(".", "").replace(",", "").isdigit():
        return s                                    # уже «352.1K»
    try:
        n = float(str(s).replace(" ", "").replace(",", "."))
    except ValueError:
        return s
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(int(n))


def build(event, d):
    enriched = as_rows(load(f"{d}/enriched_{event}.json"))
    qualified = {str(r.get("person_id")): r for r in as_rows(load(f"{d}/qualified_{event}.json"))}
    # ⚠️ traffic_<event>.json больше не существует: фаза Traffic (SimilarWeb) удалена
    # 21.07.2026. Посещаемость берём ТОЛЬКО из поля Pipedrive as-is.
    if not enriched:
        sys.exit(f"нет enriched_{event}.json в {d} — нечего конвертировать")

    rows, stats = [], {"active": 0, "dead": 0, "qualified": 0, "missing": 0}
    for e in enriched:
        pid = str(e.get("person_id"))
        q = qualified.get(pid, {})
        # ⚠️ НАШ вердикт по статусу имеет приоритет над полем Pipedrive: платящему
        # клиенту (won с реальной продажей) классификатор сам ставит «Действующий»,
        # даже если в карточке орга статус не проставлен. Читать только e[] значит
        # показать его зелёным «Новым» — ровно та путаница, от которой прочерки.
        status_pd = str(q.get("status") or e.get("status") or "").strip()
        level = str(q.get("level") or e.get("level") or "").strip()
        domain = clean_domain(q.get("domain") or e.get("domain") or e.get("site") or e.get("company"))

        # Статус у нас — та самая колонка O, по которой красится строка
        if status_pd.startswith("Действующий"):
            status = "Действующий"
        elif status_pd.startswith("Отток"):
            status = "Отток (win-back)"
        elif q.get("in_funnel"):
            status = "В воронке"
        else:
            status = "Новый"

        r = dict.fromkeys(COLUMNS, "")
        r.update({
            "ФИО": e.get("name") or "",
            "Должность": e.get("role") or "",
            "Тир ЛПР": e.get("tier") or "",
            "Телефон": e.get("phone") or "",
            "Email": e.get("email") or "",
            "Основной сайт": domain,
            "Ивент": event,
            "Уже был в Pipedrive": q.get("in_pipedrive") or ("да" if e.get("org_classified") else ""),
            "Статус у нас": status,
        })

        if status == "Действующий":
            # Канон: по действующему не показываем НИЧЕГО — только статус. Токены на него не тратили.
            for c in ("Уровень клиента", "Индустрия", "Бизнес-модель", "Посещаемость", "Конкурент",
                      "Сделка на компании", "Компания целевая", "Персона целевая", "Работаем"):
                r[c] = DASH
            r["Причина"] = "действующий клиент"
            stats["active"] += 1
        else:
            r["Уровень клиента"] = level
            r["Индустрия"] = q.get("industry") or e.get("industry") or ""
            r["Бизнес-модель"] = q.get("bizmodel") or e.get("bizmodel") or ""
            r["Конкурент"] = q.get("competitor") or e.get("competitor") or ""
            r["Сделка на компании"] = q.get("deal_cell") or ("Нет" if q else "")
            # Пусто в Pipedrive → пусто в таблице. В SimilarWeb НЕ идём.
            r["Посещаемость"] = fmt_traffic(e.get("traffic") or q.get("traffic"))

            if q:
                cv, pv = verdict(q.get("company_verdict")), verdict(q.get("person_verdict"))
                r["Компания целевая"] = cv
                # Компания нецелевая → персону не оцениваем (иначе путает продажника)
                r["Персона целевая"] = DASH if cv == "Нецелевая" else pv
                r["Причина"] = q.get("reason") or ""
                r["Работаем"] = WORK.get(str(q.get("work") or "").strip().lower(), "")
                if cv == "Нецелевая":
                    r["Работаем"] = "Нет"
                stats["qualified"] += 1
            elif level in DEAD_LEVELS:
                # Отсечён до квалификации — уровень в Pipedrive уже говорит «не наш»
                r["Компания целевая"] = "Нецелевая"
                r["Персона целевая"] = DASH
                r["Работаем"] = "Нет"
                r["Причина"] = f"уровень «{level}» в Pipedrive"
                stats["dead"] += 1
            else:
                r["Причина"] = "не дошёл до квалификации — проверить"
                stats["missing"] += 1

        r["_org_id"] = e.get("org_id")
        rows.append(r)

    return rows, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", required=True, help="полный код с _on_")
    ap.add_argument("--dir", required=True, help="каталог с файлами прогона")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    rows, stats = build(a.event, a.dir.rstrip("/"))
    tab = a.event.replace("_on_", "_")          # канон именования вкладки
    payload = {"event_code": a.event, "tab": tab, "rows": rows}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    print(f"строк: {len(rows)} → {a.out}")
    print(f"  вкладка: {tab}")
    print(f"  действующие (всё в прочерках): {stats['active']}")
    print(f"  отсечены до квалификации:      {stats['dead']}")
    print(f"  с вердиктом квалификации:      {stats['qualified']}")
    if stats["missing"]:
        print(f"  ⚠️ БЕЗ вердикта (потерялись между фазами): {stats['missing']} — проверить прогон")
    return 1 if stats["missing"] else 0


if __name__ == "__main__":
    sys.exit(main())
