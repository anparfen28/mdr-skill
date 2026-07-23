#!/usr/bin/env python3
"""Детерминированная валидация строк квалификации — замена агента-аудитора.

Проверяет те пункты чеклиста SKILL.md, которые не требуют суждения. Раньше их
«проверял» LLM-агент (108K токенов на 336 строк) и всё равно пропускал
Enterprise/Midmarket в столбце «Уровень» — тут это ловится regexp'ом за секунду.

    python3 scripts/validate_rows.py --rows rows.json [--fields references/fields.json] [--json]

rows.json — список объектов квалификации (person_id, level, industry, domain,
company_verdict, person_verdict, work, …). Выход: список проблем.
Код возврата 1, если есть проблемы уровня ERROR — прогон должен падать, а не
«предупреждать»: оба раза баг с уровнем доехал до таблицы именно потому, что
проверка была просьбой в промпте, а не воротами.
"""
import argparse, json, re, sys
from datetime import date


# ⚠️ Дата прогона — ОДНА на весь конвейер. apply_touches.py получает её через --today;
# если валидатор возьмёт свою date.today(), прогон через полночь/границу месяца даст
# ERROR на строке, которую скрипт законно оставил. Задаётся --today, дефолт — сегодня.
TODAY = date.today()


def _months_ago(ym):
    """Сколько полных месяцев назад был «YYYY-MM». None, если не разобрать."""
    m = re.match(r"^(\d{4})-(\d{2})$", str(ym or "").strip())
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    return (TODAY.year - y) * 12 + (TODAY.month - mo)


def _task_within_3m(due):
    """Задача «YYYY-MM-DD» в ближайших 3 месяцах? Просроченная и далёкая — нет."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", str(due or "").strip())
    if not m:
        return False
    d, t = date(*map(int, m.groups())), TODAY
    if d < t:
        return False                      # просрочена — «собирались, но не дошли»
    months = (d.year - t.year) * 12 + (d.month - t.month)
    return months < 3 or (months == 3 and d.day <= t.day)

LEVELS = {"Wood", "Bronze", "Silver", "Gold", "Platinum", "Diamond",
          "Агентство", "Нецелевой", "Компания ушла из России",
          # найдено на живом прогоне 260416 — реальная опция поля, дисквалификатор
          "Компания прекратила существование"}
SEGMENTS = {"Enterprise", "Midmarket", "Small business"}

INDUSTRIES = {
    "DIY, ремонт", "Автоиндустрия, транспорт, логистика", "АЗС", "Благотворительность",
    "Детские товары, материнство", "Зоотовары", "Книги, канцтовары, подарки",
    "Косметика, гигиена, интим", "Маркетинг", "Мебель, товары для дома, бытовые услуги",
    "Медицина и мед. товары", "Металлургия", "Недвижимость", "Общее образование",
    "Общественное питание", "Одежда и обувь", "Продукты питания, табак",
    "Сельское хозяйство", "Смешанные товары и услуги", "Спорт и фитнес",
    "Телекоммуникации, IT", "Техника и электроника", "Туризм, развлечения, кино, радио",
    "Ювелирные товары", "Финансы, управление, страхование, юриспруденция",
}

WORK = {"да", "уточнить", "нет"}
RED, GREEN, YELLOW = "🔴", "🟢", "🟡"
DASH = "—"

# ── Гео (критерии, стр. 13/29/37) ────────────────────────────────────────────
GEO_STOP = {"украина", "ukraine", "ua"}                      # временно не работаем
GEO_OTHER_TEAM = {"ес", "eu", "сша", "usa", "us", "европа"}  # обслуживает другая команда
GEO_OK = {"рф", "россия", "russia", "ru", "казахстан", "kz", "армения", "am", "беларусь", "by"}
TLD_GEO = {".ua": "украина", ".kz": "казахстан", ".by": "беларусь", ".am": "армения"}

# ── Мусорные / несуществующие домены → компания обязана быть нецелевой ───────
GARBAGE_EXACT = re.compile(
    r"^(company\.com|test\.com|example\.(com|ru)|no|нет|ооо|оао|ип|самозанят\w*|"
    r"mail\.ru|gmail\.com|yandex\.ru|vk\.com|vk\.ru|t\.me|instagram\.com|-|—|n/?a)$",
    re.I,
)
# домен без точки («ооо ромашка»), с пробелом, или со слипшимся TLD («yandex.rucompany»)
NO_TLD = re.compile(r"^[^.]+$")
HAS_SPACE = re.compile(r"\s")
GLUED_TLD = re.compile(r"\.(ru|com|net|org|kz|by)[a-z]{2,}$", re.I)
# ⚠️ 23.07: GLUED_TLD ловит ".com"/".net"/".org" как ПРЕФИКС и внутри настоящих
# многобуквенных gTLD ("company" = "com"+"pany", "network" = "net"+"work") —
# ложный ERROR на живом домене aag.company. Реальные gTLD с таким префиксом
# исключаем явно, а не ослабляем сам regex (иначе пропустим настоящие опечатки).
REAL_LONG_TLD = re.compile(
    r"\.(company|community|network|contact|contractors|construction|consulting|"
    r"computer|condos|contractors|corsica|comcast)$", re.I)
DIRTY_DOMAIN = re.compile(r"(^https?://)|(^www\.)|(/)|(#)|([A-ZА-Я])")


def _norm(s):
    """Нормализация для матча названий: только буквы/цифры, нижний регистр."""
    return re.sub(r"[^a-z0-9а-я]", "", str(s or "").lower())


def load_disqualifiers(ref_dir):
    """Списки конкурентов и партнёров — ПАРСЯТСЯ ИЗ .md, а не дублируются здесь.

    Иначе списки разъедутся: люди правят competitors.md / partners.md, а копия
    в скрипте останется старой. Единственный источник — те же файлы, что читает MDR.
    """
    # generic-токены: попадают из скобок-пояснений («SAP (CRM/Emarsys-линейка)») и
    # дают ложные 🔴 у обычных компаний. В списки не берём.
    GENERIC = {"crm", "erp", "cdp", "esp", "cms", "sms", "email", "retail", "cloud",
               "marketing", "сайт", "сервис", "агентство", "платформа"}
    comp, part = set(), set()
    try:  # competitors.md: строки вида «- **edna** (edna.ru) — CPaaS…»
        txt = open(f"{ref_dir}/competitors.md", encoding="utf-8").read()
        for m in re.finditer(r"^-\s+\*\*(.+?)\*\*(.*)$", txt, re.M):
            name = _norm(m.group(1).split("/")[0])
            if len(name) >= 3 and name not in GENERIC:
                comp.add(name)
            for alias in re.findall(r"\(([^)]+)\)", m.group(2)):
                for a in alias.split(","):
                    al = _norm(a.split("/")[0])
                    if len(al) >= 4 and al not in GENERIC:  # алиасы строже: они шумнее
                        comp.add(al)
    except OSError:
        pass
    try:  # partners.md: абзац из имён через « · »
        txt = open(f"{ref_dir}/partners.md", encoding="utf-8").read()
        block = re.search(r"##\s*Список партнёров.*?\n(.*?)(?:\n>|\Z)", txt, re.S)
        if block:
            for name in block.group(1).split("·"):
                name = re.sub(r"\*\*|\(.*?\)", "", name).strip()
                for variant in name.split("/"):
                    v = _norm(variant)
                    if len(v) >= 3 and v not in GENERIC:
                        part.add(v)
    except OSError:
        pass
    return comp, part


def blank(v):
    return v is None or str(v).strip() in ("", "None")


def check(rows, competitors=frozenset(), partners=frozenset()):
    problems = []

    def bad(level, pid, issue):
        problems.append({"level": level, "person_id": pid, "issue": issue})

    for r in rows:
        pid = r.get("person_id")
        lvl = (r.get("level") or "").strip()
        ind = (r.get("industry") or "").strip()
        dom = (r.get("domain") or r.get("Основной сайт") or "").strip()
        cv = str(r.get("company_verdict") or "")
        pv = str(r.get("person_verdict") or "")
        work = str(r.get("work") or "").strip()
        status = (r.get("status") or "").strip()

        # 1. Уровень ≠ Сегмент — регрессировало дважды, поэтому ERROR
        if lvl in SEGMENTS:
            bad("ERROR", pid, f"в «Уровень клиента» записан СЕГМЕНТ «{lvl}» — читается не то поле "
                              f"(уровень = org.level 11e6a039, сегмент = org.segment c332e6c0)")
        elif lvl and lvl != DASH and lvl not in LEVELS:
            # «—» = канонический маркер «неприменимо» (действующий/платящий клиент),
            # а не значение уровня. Проверка стоит ВЫШЕ отсечения действующих,
            # поэтому исключение нужно здесь, иначе прочерк роняет прогон ERROR-ом.
            bad("ERROR", pid, f"«Уровень клиента» = «{lvl}» вне фикс-списка {sorted(LEVELS)}")

        # 1б. Wood — не работаем вообще (отдельный автоагент). Если вердикт не «нет»,
        # значит ветка Wood в classify_rows не отработала — это ERROR, а не мелочь:
        # заход по Wood дублирует работу автоагента.
        if lvl == "Wood" and work and work != "нет":
            bad("ERROR", pid, f"уровень Wood — заходы не пишем (отдельный автоагент), "
                              f"но «Работаем» = «{work}»")

        # 2. Действующий — по нему не смотрим ничего, всё «—»
        if status == "Действующий":
            for col in ("level", "industry", "traffic", "competitor"):
                if not blank(r.get(col)) and str(r.get(col)).strip() != DASH:
                    bad("WARN", pid, f"действующий клиент, но «{col}» заполнено «{r.get(col)}» — "
                                     f"должен быть «—» (по действующим не тратим токены)")
            if work and work != "нет":
                bad("ERROR", pid, f"действующий клиент, но «Работаем» = «{work}» — должно быть «нет»")
            continue

        # 3. Индустрия — ⚠️ с 21.07.2026 НЕ наша ответственность.
        # Классификация проставляется в Pipedrive (команда дозаполняет карточки
        # ежесуточным промптом); мы её только ВЫТАСКИВАЕМ и на сайт за ней не ходим.
        # Пустое поле = карточка не дозаполнена → это сигнал команде, а не наш баг.
        # Поэтому пусто → WARN, а не ERROR: прогон из-за чужого пробела не роняем.
        src_ind = str((r.get("sources") or {}).get("industry") or "")
        src_lvl = str((r.get("sources") or {}).get("level") or "")
        # Батч 2 помечает поля «не требовалась» — там пустота ОЖИДАЕМА, это не пробел
        # в карточке. Без этого условия каждая строка батча 2 даёт по 2 WARN, и на
        # тысяче регистрантов настоящие предупреждения тонут в сотнях ложных.
        if blank(ind) and "не требовал" not in src_ind:
            bad("WARN", pid, "пустая «Индустрия» в Pipedrive — карточка не дозаполнена "
                             "(на сайт за ней НЕ идём, это задача ежесуточного промпта)")
        elif ind not in INDUSTRIES:
            # ⚠️ WARN, не ERROR: значение приходит ИЗ Pipedrive, мы его не производим.
            # Ронять прогон из-за опции, которой нет в нашей копии списка, — значит
            # блокировать работу из-за расхождения справочников. Список ниже мог
            # устареть: сверить с реальными опциями поля «Индустрия» в Pipedrive.
            bad("WARN", pid, f"«Индустрия» = «{ind}» вне нашей копии фикс-списка — "
                             f"либо опечатка в Pipedrive, либо наш список устарел")

        # 4. Уровень — то же самое: пусто в Pipedrive → WARN, дефолт Wood НЕ подставляем
        if blank(lvl) and "не требовал" not in src_lvl and "сделка" not in src_lvl:
            bad("WARN", pid, "пустой «Уровень клиента» в Pipedrive — карточка не дозаполнена")

        # 5. Гигиена домена + мусорные/несуществующие
        if dom:
            if DIRTY_DOMAIN.search(dom):
                bad("WARN", pid, f"грязный домен «{dom}» — нужен чистый нижний регистр без http/www//#")
            why = None
            if GARBAGE_EXACT.match(dom):
                why = "не домен компании (заглушка/соцсеть/публичная почта)"
            elif NO_TLD.match(dom):
                why = "домен без TLD"
            elif HAS_SPACE.search(dom):
                why = "пробел в домене — это не домен"
            elif GLUED_TLD.search(dom) and not REAL_LONG_TLD.search(dom):
                why = "слипшийся TLD (тип «yandex.rucompany») — опечатка при вводе"
            # ⚠️ Смысл проверки — не пустить мусорный домен В РАБОТУ. Если лид и так
            # отсечён («Работаем» = нет), вердикт-иконка роли не играет: правило Wood
            # срабатывает РАНЬШЕ проверки домена и ставит 🟡, а не 🔴 — оба дают «не пишем».
            # Без этого условия коллизия двух корректных правил давала ложный ERROR
            # и роняла прогон на ровном месте (найдено на прогоне 260416: домен «nosite», Wood).
            if why and RED not in cv and work != "нет":
                bad("ERROR", pid, f"мусорный домен «{dom}» ({why}), но компания не 🔴")

        # 5б. Конкурент / партнёр Mindbox = сама компания → жёсткий дисквалификатор
        # Матчим детерминированно по спискам, а не «модель вспомнит».
        comp_name = _norm(r.get("company") or r.get("business_type") or "")
        dom_core = _norm(re.sub(r"\.(ru|com|net|org|kz|by|am|io|pro|su)$", "", dom))
        for key, pool, what in ((comp_name, competitors, "конкурент"), (dom_core, competitors, "конкурент"),
                                (comp_name, partners, "партнёр Mindbox"), (dom_core, partners, "партнёр Mindbox")):
            if key and len(key) >= 3 and key in pool and RED not in cv:
                bad("ERROR", pid, f"компания «{r.get('company') or dom}» есть в списке «{what}» "
                                  f"(references/{'competitors' if what == 'конкурент' else 'partners'}.md), "
                                  f"но вердикт «{cv or 'пусто'}» — должно быть 🔴, не общаемся")
                break

        # 5в. Гео — стоп-страны и чужая зона ответственности
        geo = str(r.get("geo") or "").strip().lower()
        geo_tld = next((v for t, v in TLD_GEO.items() if dom.endswith(t)), None)
        if geo_tld and geo and _norm(geo_tld) not in _norm(geo):
            bad("WARN", pid, f"домен «{dom}» указывает на «{geo_tld}», а в «Гео» стоит «{geo}» — сверить")
        eff_geo = geo or (geo_tld or "")
        if eff_geo:
            if any(s in eff_geo for s in GEO_STOP) and work != "нет":
                bad("ERROR", pid, f"гео «{eff_geo}» — Украина, временно не работаем; «Работаем» должно быть «нет»")
            elif any(s == eff_geo or f" {s}" in f" {eff_geo}" for s in GEO_OTHER_TEAM) and work != "нет":
                bad("WARN", pid, f"гео «{eff_geo}» — ЕС/США обслуживает отдельная команда, не наш лид")

        # 5г. «В воронке» — обязана быть настоящая открытая сделка со стадией и владельцем.
        # Автосайнап (pipe 46) сделкой-в-воронке НЕ считается: если его засчитать,
        # живой лид помечается чужим и MDR его пропускает, хотя им никто не занимается.
        cell = str(r.get("deal_cell") or "").strip()
        in_funnel = bool(r.get("in_funnel"))
        if in_funnel:
            if not cell or cell in ("Нет", DASH):
                bad("ERROR", pid, "in_funnel=true, но «Сделка на компании» пустая — "
                                  "признак засчитанного автосайнапа, а не реальной воронки")
            elif "owner" not in cell.lower() or "·" not in cell:
                bad("WARN", pid, f"«В воронке», но в ячейке «{cell}» нет стадии/владельца — "
                                 f"нужен формат «Открыта · <стадия> · owner <Имя> · <N> касаний»")
            # ⚠️ Проверки «в воронке → Работаем должно быть нет» здесь БОЛЬШЕ НЕТ.
            # Открытая сделка на компании контакт не занимает (модель 20.07):
            # батч 2 — целевые по умолчанию, вердикт им выносит тест занятости ниже.

        # 5д. Занятость КОНТАКТА — порог 3 мес (правило маркетинг-директора 20.07,
        # заменило won-порог 6 мес). Блокирует не факт активности, а ДВУСТОРОННЯЯ
        # ДОГОВОРЁННОСТЬ: last_touch = только двустороннее общение (клиент отвечал),
        # planned_task блокирует лишь при task_agreed=true. Регистрация на ивент
        # общением не является (иначе «свежими» окажутся все регистранты).
        last_touch = str(r.get("last_touch") or "").strip()
        planned = str(r.get("planned_task") or "").strip()
        agreed = bool(r.get("task_agreed"))
        if last_touch and not re.match(r"^\d{4}-\d{2}$", last_touch):
            bad("WARN", pid, f"last_touch «{last_touch}» не в формате YYYY-MM")
        elif last_touch and _months_ago(last_touch) is not None and _months_ago(last_touch) < 3:
            if work != "нет":
                bad("ERROR", pid, f"двустороннее общение ({last_touch}, меньше 3 мес назад) — "
                                  f"контакт занят; «Работаем» должно быть «нет», а стоит «{work}»")
        # Договорённость блокирует НЕЗАВИСИМО от даты общения. Односторонняя задача — НЕТ.
        if planned and agreed and work != "нет" and _task_within_3m(planned):
            bad("ERROR", pid, f"есть договорённость о контакте ({planned}) — занят; "
                              f"«Работаем» должно быть «нет», а стоит «{work}»")
        # Строка, которой собираются писать, обязана быть проверена на занятость.
        # Без этой проверки правило снова останется «описанным в доке» — ровно так
        # 17.07 won-правило не применилось ни разу на 14 организациях.
        if work in ("да", "уточнить") and not r.get("touch_checked"):
            bad("ERROR", pid, f"«Работаем» = «{work}», но занятость контакта не проверена "
                              f"(touch_checked=false) — прогони фазу Touch по шорт-листу")
        # Задача есть, но её природа не размечена → фаза Touch не вынесла суждение.
        # Без task_agreed правило «односторонняя не блокирует» молча не применяется.
        if planned and "task_agreed" not in r:
            bad("WARN", pid, f"задача {planned} без task_agreed — не размечено, договорённость "
                             f"это с клиентом или одностороннее напоминание консультанта")

        won_month = str(r.get("won_month") or "").strip()
        if won_month and not re.match(r"^\d{4}-\d{2}$", won_month):
            bad("WARN", pid, f"won_month «{won_month}» не в формате YYYY-MM")

        # 5е. ⚠️ Проверка «блок SimilarWeb ≠ нет данных» УДАЛЕНА 21.07.2026 вместе
        # с фазой Traffic: посещаемость берётся из поля Pipedrive as-is, в SimilarWeb
        # не ходим, дефолт Wood не подставляем — блокироваться нечему.


        # 5ж. Лог решений — по каждой строке должно быть видно, откуда взялся вердикт
        src = r.get("sources") or {}
        if not src.get("verdict"):
            bad("WARN", pid, "пустой sources.verdict — не видно, что решило вердикт "
                             "(нечем ответить MDR на «это неправильно»)")
        if lvl and not src.get("level"):
            bad("WARN", pid, f"уровень «{lvl}» без sources.level — неясно, из Pipedrive он или посчитан")

        # 6. Согласованность вердиктов
        if work and work not in WORK:
            bad("ERROR", pid, f"«Работаем» = «{work}» вне {sorted(WORK)}")
        if RED in cv:
            if work != "нет":
                bad("ERROR", pid, f"компания 🔴, но «Работаем» = «{work}» — должно быть «нет»")
            if pv and pv.strip() != DASH:
                bad("WARN", pid, f"компания 🔴 → «Персона целевая» должна быть «—», а не «{pv}» "
                                 f"(иначе путает продажника)")
        # «нет» у целевой компании законно, если контакт занят (двустороннее общение
        # или договорённость) — это не рассогласование вердиктов, а правило занятости.
        fresh_touch = _months_ago(r.get("last_touch")) is not None \
            and _months_ago(r.get("last_touch")) < 3
        busy = fresh_touch or (bool(r.get("task_agreed"))
                               and _task_within_3m(r.get("planned_task")))
        if GREEN in cv and GREEN in pv and work and work != "да" and not busy:
            bad("WARN", pid, f"компания 🟢 + персона 🟢, но «Работаем» = «{work}»")
        if not cv:
            bad("ERROR", pid, "нет вердикта по компании")

    # 7. Один вердикт на компанию. Слой «компания» общий для всех её персон:
    # батч режется по границам компаний именно ради этого. Расхождение = баг нарезки
    # (на прогоне 260722 так разошёлся hh.ru: 🟢 у одного человека, 🟡 у другого).
    COMPANY_LEVEL = ("company_verdict", "level", "industry", "bizmodel", "geo")
    by_org = {}
    for r in rows:
        oid = r.get("org_id")
        if oid and (r.get("status") or "").strip() != "Действующий":
            by_org.setdefault(oid, []).append(r)
    for oid, group in by_org.items():
        if len(group) < 2:
            continue
        for col in COMPANY_LEVEL:
            vals = {str(g.get(col) or "").strip() for g in group}
            vals.discard("")
            if len(vals) > 1:
                name = group[0].get("company") or group[0].get("domain") or f"org {oid}"
                bad("ERROR", group[0].get("person_id"),
                    f"компания «{name}» (org {oid}, {len(group)} регистрантов): поле «{col}» расходится "
                    f"между персонами — {sorted(vals)}. Слой «компания» обязан быть одинаковым у всех её людей")

    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", required=True)
    ap.add_argument("--fields", help="references/fields.json — сверка, что ID полей не разъехались")
    ap.add_argument("--refs", help="каталог references/ — списки конкурентов и партнёров")
    ap.add_argument("--today", help="YYYY-MM-DD — та же дата, что у apply_touches.py")
    ap.add_argument("--json", action="store_true", help="выводить машинно (для workflow)")
    a = ap.parse_args()
    if a.today:
        import re as _re
        m = _re.match(r"^(\d{4})-(\d{2})-(\d{2})$", a.today.strip())
        if not m:
            print(f"--today должен быть YYYY-MM-DD, получено «{a.today}»", file=sys.stderr)
            return 2
        globals()["TODAY"] = date(*map(int, m.groups()))


    ref_dir = a.refs or (a.fields and a.fields.rsplit("/", 1)[0]) or "references"
    competitors, partners = load_disqualifiers(ref_dir)

    data = json.load(open(a.rows, encoding="utf-8"))
    rows = data if isinstance(data, list) else data.get("rows", list(data.values()))
    problems = check(rows, competitors, partners)

    if a.fields:  # страховка от подмены ключей
        f = json.load(open(a.fields, encoding="utf-8"))
        if f["org"]["level"]["key"][:8] != "11e6a039" or f["org"]["traffic"]["key"][:8] != "5955678b":
            problems.append({"level": "ERROR", "person_id": None,
                             "issue": "fields.json: ID уровня/посещаемости не совпадают с проверенными "
                                      "(level=11e6a039…, traffic=5955678b…)"})

    errors = [p for p in problems if p["level"] == "ERROR"]
    if a.json:
        print(json.dumps({"clean": not errors, "problems": problems}, ensure_ascii=False))
    else:
        print(f"списки: конкурентов {len(competitors)} · партнёров {len(partners)}")
        print(f"строк: {len(rows)} · проблем: {len(problems)} (ERROR {len(errors)})")
        for p in problems[:60]:
            print(f"  [{p['level']}] {p['person_id']}: {p['issue']}")
        if len(problems) > 60:
            print(f"  … ещё {len(problems)-60}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
