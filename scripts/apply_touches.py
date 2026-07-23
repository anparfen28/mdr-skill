#!/usr/bin/env python3
"""Тест на занятость контакта: кого мы имеем право забрать после ивента.

Модель маркетинг-директора Mindbox (уточнена 20.07.2026). Занятость считается
ПО КОНТАКТУ, а не по сделке и не по компании: открытая сделка на организации и
закреплённый за ней консультант человека НЕ занимают.

🔑 ГЛАВНОЕ: блокирует не факт активности, а ДВУСТОРОННЯЯ ДОГОВОРЁННОСТЬ с
клиентом о контакте. Односторонняя активность консультанта человека не занимает.

    КОНТАКТ ЗАНЯТ, если выполнено хотя бы одно:
      1. ДВУСТОРОННЕЕ общение < 3 мес назад — клиент отвечал;
      2. есть ДОГОВОРЁННОСТЬ о контакте в ближайшие 3 мес — согласованный
         с клиентом следующий шаг («бюджета нет, вернуться в конце квартала
         в цикл бюджетирования»). Блокирует НЕЗАВИСИМО от даты общения.
    Иначе — свободен, забираем.

    ОТБИРАЕМ (НЕ блокирует), даже если активность недавняя:
      • общение «в одни ворота» — консультант писал, клиент не ответил;
      • односторонняя задача «попробовать зайти» / «прозвонить» без
        договорённости: клиент о ней не знает и ничего не обещал.

⚠️ Задача ДАЛЬШЕ 3 месяцев не блокирует: «перезвонить через год» не должно
закрывать контакт навсегда. Окно договорённости совпадает с окном общения.

⚠️ ПРОСРОЧЕННАЯ незакрытая задача (дата в прошлом) — трактуем как НЕ блокирующую,
но помечаем в причине. Это допущение: такая задача обычно значит «собирались, но
не дошли», а не активную работу. Спорно — вынесено Анне на подтверждение.

⚠️ Регистрация на ивент общением НЕ является: она есть у всех регистрантов и
датирована днём ивента, поэтому фаза Touch её отбрасывает ещё до этого скрипта.

РАЗДЕЛЕНИЕ ТРУДА: двусторонность и наличие договорённости — это СМЫСЛ, его
вычитывает из содержания активностей фаза Touch (модель). Здесь, в скрипте, —
только арифметика порогов: «меньше трёх месяцев» на глаз даёт ошибки, а решение
бинарное. Скрипт доверяет разметке агента, но требует её явно (см. --touches).

    python3 scripts/apply_touches.py --rows qualified_260722.json \
        --touches touches_260722.json --today 2026-07-20

Контракт --touches (по каждой персоне):
    person_id      — id
    last_touch     — «YYYY-MM» последнего ДВУСТОРОННЕГО общения, "" если не было
    one_way_touch  — «YYYY-MM» последнего одностороннего касания (для лога, НЕ блокирует)
    planned_task   — «YYYY-MM-DD» ближайшей незакрытой задачи, "" если нет
    task_agreed    — true, если задача = договорённость с клиентом; false, если
                     односторонняя («попробовать зайти»)
    kind / task_owner / evidence — контекст для причины и аудита

Файл строк перезаписывается на месте: проставляются last_touch, planned_task,
task_agreed, touch_checked; занятым work → «нет». Печатает JSON со статистикой.
"""
import argparse, json, re, sys
from datetime import date

THRESHOLD_MONTHS = 3
WRITEABLE = ("да", "уточнить")


def months_between(ym, today):
    """Полных месяцев между «YYYY-MM» и датой прогона. None, если не разобрать."""
    a = re.match(r"^(\d{4})-(\d{2})$", str(ym or "").strip())
    b = re.match(r"^(\d{4})-(\d{2})", str(today or "").strip())
    if not a or not b:
        return None
    return (int(b.group(1)) - int(a.group(1))) * 12 + (int(b.group(2)) - int(a.group(2)))


def task_state(due, today):
    """Состояние запланированной задачи относительно окна 3 мес.

    Возвращает: "blocking" (в ближайшие 3 мес — контакт занят) ·
    "overdue" (дата в прошлом — не блокирует, но помечаем) ·
    "far" (дальше 3 мес — не блокирует) · "" (задачи нет / не разобрать дату).
    """
    d = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(due or "").strip())
    t = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(today or "").strip())
    if not d or not t:
        return ""
    due_d = date(*map(int, d.groups()))
    today_d = date(*map(int, t.groups()))
    if due_d < today_d:
        return "overdue"
    # Граница окна: тот же день через THRESHOLD_MONTHS месяцев.
    m = today_d.month - 1 + THRESHOLD_MONTHS
    edge = date(today_d.year + m // 12, m % 12 + 1,
                min(today_d.day, [31, 29 if (today_d.year + m // 12) % 4 == 0 else 28, 31, 30, 31,
                                  30, 31, 31, 30, 31, 30, 31][m % 12]))
    return "blocking" if due_d <= edge else "far"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", required=True, help="qualified_<event>.json — перезаписывается на месте")
    ap.add_argument("--touches", required=True, help="[{person_id, last_touch, kind}]")
    ap.add_argument("--today", required=True, help="YYYY-MM-DD, дата прогона (date +%%F)")
    a = ap.parse_args()

    if not re.match(r"^\d{4}-\d{2}-\d{2}$", a.today.strip()):
        print(f"--today должен быть YYYY-MM-DD, получено «{a.today}»", file=sys.stderr)
        return 2

    rows = json.load(open(a.rows, encoding="utf-8"))
    touches = {str(t.get("person_id")): t for t in json.load(open(a.touches, encoding="utf-8"))}

    checked = fresh = tasked = open_now = missing = 0
    freed_one_way = freed_unilateral = 0
    for r in rows:
        if str(r.get("work") or "") not in WRITEABLE:
            continue                      # кому и так не пишем — занятость не меняет вердикт
        t = touches.get(str(r.get("person_id")))
        if not t:
            missing += 1                  # валидатор поймает как ERROR: touch_checked=false
            continue
        checked += 1
        last = str(t.get("last_touch") or "").strip()          # ДВУСТОРОННЕЕ общение
        one_way = str(t.get("one_way_touch") or "").strip()    # одни ворота — не блокирует
        task = str(t.get("planned_task") or "").strip()
        agreed = bool(t.get("task_agreed"))
        r["last_touch"] = last
        r["one_way_touch"] = one_way
        r["planned_task"] = task
        r["task_agreed"] = agreed
        r["touch_checked"] = True

        m = months_between(last, a.today)
        # Пустой last_touch = двустороннего общения не было. Это НОРМА, а не «нет данных»:
        # человек мог получать письма и ни разу не ответить — такой контакт свободен.
        is_fresh = m is not None and m < THRESHOLD_MONTHS
        # Задача блокирует, ТОЛЬКО если она — договорённость с клиентом.
        # Односторонняя («попробовать зайти») не занимает: клиент о ней не знает.
        state = task_state(task, a.today)
        blocks = state == "blocking" and agreed

        if not (is_fresh or blocks):
            open_now += 1
            notes = []
            if state == "blocking" and not agreed:
                freed_unilateral += 1
                notes.append(f"задача {task} односторонняя (без договорённости) — не блокирует")
            elif state in ("overdue", "far"):
                notes.append(f"задача {task} ({'просрочена' if state == 'overdue' else 'дальше 3 мес'}) — не блокирует")
            if one_way and not last:
                freed_one_way += 1
                notes.append(f"общение в одни ворота ({one_way}), ответа не было — не блокирует")
            if notes:
                r["reason"] = " · ".join([r.get("reason", "").strip()] + notes).strip(" ·")
            continue

        if blocks:
            tasked += 1
            owner = str(t.get("task_owner") or "").strip()
            why = f"договорённость о контакте на {task}" + (f" ({owner})" if owner else "")
            verdict = "занят: договорённость о контакте в ближайшие 3 мес"
        else:
            fresh += 1
            kind = str(t.get("kind") or "").strip()
            why = f"двустороннее общение ({last}{', ' + kind if kind else ''})"
            verdict = f"занят: двустороннее общение <{THRESHOLD_MONTHS} мес"
        r["work"] = "нет"
        r["reason"] = f"{why} — не пишем"
        src = dict(r.get("sources") or {})
        src["verdict"] = verdict
        r["sources"] = src

    json.dump(rows, open(a.rows, "w", encoding="utf-8"), ensure_ascii=False)
    print(json.dumps({"checked": checked, "fresh": fresh, "tasked": tasked,
                      "open_now": open_now, "missing": missing,
                      "freed_one_way": freed_one_way,
                      "freed_unilateral_task": freed_unilateral}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
