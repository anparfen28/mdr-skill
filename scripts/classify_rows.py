#!/usr/bin/env python3
"""classify_rows.py — вердикты ПРАВИЛАМИ там, где Pipedrive уже всё сказал.

Правило Анны 19.07: «большинство лидов уже есть в Pipedrive со всеми полями —
эту информацию просто надо вытаскивать». Значит модель нужна только для остатка.

Прогон 260618 показал, во что обходится обратное: 967 регистрантов ушли в 23
LLM-слайса, каждый заново читал критерии и заново «квалифицировал» компанию,
у которой уровень, индустрия и бизнес-модель уже стояли в Pipedrive.

Скрипт делит строки на две стопки:
  decided   — вердикт выведен ПРАВИЛАМИ из полей Pipedrive (0 токенов)
  needs_llm — реальные пробелы: нет индустрии/уровня, непонятная роль

    python3 scripts/classify_rows.py --enriched enriched.json --deals deals.json \\
        --refs references --out-decided decided.json --out-residue residue.json
"""
import argparse, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validate_rows import load_disqualifiers, _norm, GEO_STOP, GEO_OTHER_TEAM, TLD_GEO  # единый источник правил

DEAD_LEVELS = {"Нецелевой", "Агентство", "Компания ушла из России",
               "Компания прекратила существование"}
REAL_LEVELS = {"Wood", "Bronze", "Silver", "Gold", "Platinum", "Diamond"}

# Роль → слой «персона».
# ⚠️ Спорная роль НЕ уходит к модели: правильный ответ на неё — «уточнить, найти ЛПР»,
# то есть ровно то, что модель и напишет. Прогон 260618: 137 лидов ушли в LLM только
# из-за роли, из них 28 — с должностью «СМО» (это CMO кириллицей, регексп ловил латиницу).
ROLE_DM = re.compile(
    r"директор|руковод|head|chief|основател|founder|owner|владел|"
    r"c[emtofpr]o\b|\b(сmo|смо|сео|сто|сфо|сро|спо|ссо|сbo|сpo)\b|"
    r"маркетолог|маркетинг|marketing|crm|срм|лояльн|коммуникац|ecom|e-com|"
    r"бренд|brand|managing|партнёр|партнер|"
    r"начальник|управляющ|глава|lead\b|владелец|предпринимател|\bип\b|\bceo\b", re.I)
# ⚠️ ГРАНИЦЫ СЛОВ ОБЯЗАТЕЛЬНЫ у «водител» и «интерн» — подстроки-ловушки, найдены на
# живом прогоне 260416 (134 ложных срабатывания из 1106):
#   «рукоВОДИТЕЛь» ловился на «водител» → руководитель отдела маркетинга = «не ЛПР»;
#   «ИНТЕРНет-маркетолог» ловился на «интерн» → маркетолог = «стажёр».
# Это самые целевые роли, и они молча уходили в «уточнить».
ROLE_NOT_DM = re.compile(
    r"ассистент|секретар|помощник|стажёр|стажер|\bинтерн\b|курьер|\bводител|"
    r"разработчик|программист|developer|qa\b|тестировщик|дизайнер|верстальщик|"
    r"бухгалтер|юрист|hr\b|рекрут|студент|аспирант|преподават|учител|инженер", re.I)

# ⚠️ Product Manager — спорный случай, ради которого и нужен «Тир ЛПР» (доп. критериев
# в qualification_criteria.md: «голый PM → Уточнить; тир Менеджмент ближе к Целевой»).
# Поэтому голый product/продукт УБРАН из ROLE_DM: иначе любой PM получал «да» ещё до
# того, как дело доходило до тира, и поле оставалось декоративным.
PM_ROLE = re.compile(r"продукт|product|\bpm\b|\bпм\b", re.I)
PM_CONTEXT = re.compile(
    r"crm|срм|growth|лояльн|loyalty|retention|ретеншн|martech|мартех|"
    r"маркетинг|marketing|коммуникац|digital|диджитал|data|платформ|platform", re.I)

# Мусорные домены — тоже решаются без модели
GARBAGE_DOM = re.compile(
    r"^(company\.com|test\.com|example\.(com|ru)|no|нет|ооо|оао|ип|самозанят\w*|"
    r"mail\.ru|gmail\.com|yandex\.ru|vk\.com|vk\.ru|t\.me|instagram\.com|-|—|n/?a)$|"
    r"^[^.]+$|\s|\.(ru|com|net|org|kz|by)[a-z]{2,}$", re.I)


def blank(v):
    return v is None or str(v).strip() in ("", "None", "—")


def decide(e, deal, competitors, partners):
    """Вернуть (row, None). Правила закрывают ВСЕ строки — остатка для модели нет."""
    # ⚠️ Домен берём ТОЛЬКО из доменных полей. Раньше сюда подставлялось НАЗВАНИЕ
    # компании, и «ООО Ромашка» (карточка заполнена, Целевой=TRUE, но пустой сайт)
    # улетала в GARBAGE_DOM как «мусорный домен» — валидный целевой лид молча
    # вычёркивался, и в таблице это выглядело легитимно.
    dom, dom_raw = _clean_domain(e.get("domain") or e.get("site") or "")
    level = str(e.get("level") or "").strip()
    industry = str(e.get("industry") or "").strip()
    target = str(e.get("target") or "").strip().upper()
    role = str(e.get("role") or "").strip()
    tier = str(e.get("tier") or "").strip()

    src = {"domain": f"очищен из «{dom_raw}»"} if dom_raw else {}
    r = {
        "person_id": e.get("person_id"), "org_id": e.get("org_id"), "domain": dom,
        "company": e.get("company") or dom, "industry": industry, "level": level,
        "bizmodel": e.get("bizmodel") or "", "competitor": e.get("competitor") or "",
        "status": e.get("status") or "", "geo": "", "tier": tier, "in_pipedrive": "да" if e.get("org_classified") else "нет",
        "deal_cell": (deal or {}).get("cell") or "Нет",
        "in_funnel": _in_funnel(deal),
        "on_deal": _on_deal(deal, e.get("person_id")),
        "won_month": (deal or {}).get("won_month") or "",
        # Порог 3 мес по последнему касанию считается ПОЗЖЕ, фазой Touch и только по
        # шорт-листу (см. pipedrive_fields.md): один getActivities на каждого из ~1000
        # регистрантов — это повтор токенового провала 17.07. Здесь только слоты.
        "last_touch": "",
        "touch_checked": False,
    }

    # ── Жёсткие дисквалификаторы: решаются без модели ────────────────────────
    core = _norm(re.sub(r"\.(ru|com|net|org|kz|by|am|io|pro|su)$", "", dom))
    name = _norm(e.get("company"))
    if (core and core in competitors) or (name and name in competitors):
        return _seal(r, "🔴", "—", "нет", "конкурент, не общаемся",
                     {**src, "level": "pipedrive", "verdict": "конкурент (competitors.md)"}), None
    if (core and core in partners) or (name and name in partners):
        return _seal(r, "🔴", "—", "нет", "партнёр Mindbox, не общаемся",
                     {**src, "level": "pipedrive", "verdict": "партнёр (partners.md)"}), None
    # ── Wood: не работаем ВООБЩЕ (правило Анны 21.07.2026) ──────────────────
    # Заходы по Wood не пишем — этот сегмент обрабатывает отдельный автоматический
    # агент. Wood ≠ «нецелевой»: компания может быть целевой, просто не наша работа.
    # Поэтому 🟡, а не 🔴, и персону не оцениваем.
    if level == "Wood":
        return _seal(r, "🟡", "—", "нет",
                     "Wood — заходы не пишем, обрабатывается отдельным автоагентом",
                     {**src, "level": "pipedrive", "verdict": "Wood — не наш сегмент"}), None

    if level in DEAD_LEVELS:
        return _seal(r, "🔴", "—", "нет", f"уровень «{level}» в Pipedrive",
                     {**src, "level": "pipedrive", "verdict": f"уровень {level}"}), None
    if target == "FALSE":
        return _seal(r, "🔴", "—", "нет", "«Целевой» = FALSE в Pipedrive",
                     {**src, "level": "pipedrive", "verdict": "Целевой=FALSE"}), None

    # Гео по TLD — дешёвая и однозначная проверка
    geo = next((v for t, v in TLD_GEO.items() if dom.endswith(t)), "")
    r["geo"] = geo or "РФ"
    if geo and any(s in geo for s in GEO_STOP):
        return _seal(r, "🔴", "—", "нет", "гео Украина — временно не работаем",
                     {**src, "level": "pipedrive", "verdict": "гео Украина"}), None

    if dom and GARBAGE_DOM.match(dom):
        return _seal(r, "🔴", "—", "нет", f"мусорный домен «{dom}» — не бизнес",
                     {**src, "level": "pipedrive", "verdict": "мусорный домен"}), None

    # ── Флаг «карточка автосоздана у крупной компании» ──────────────────────
    # Форма с лендинга заводит НОВУЮ орг под доменом (название = домен), даже если
    # настоящая карточка уже есть — Pipedrive пишет в заметку регистрации
    # «!!! Конфликт привязки !!!». Статус клиента в такой карточке пуст, и крупный
    # действующий клиент выглядит новым лидом (проверено 21.07: t-j.ru = Тинькофф
    # Журнал, x5paket.ru — оба действующие; 55 таких строк из 281 «Да»).
    # НЕ блокируем (иначе теряем реально новых), но предупреждаем в причине.
    big = level in {"Gold", "Platinum", "Diamond"}
    autocard = dom and str(e.get("company") or "").strip().lower() == dom
    no_signal = not str(e.get("status") or "").strip() and not (deal or {}).get("cell", "Нет").startswith("Открыта")
    warn_autocard = " · ⚠️ карточка автосоздана (имя=домен), статус пуст — проверь, не действующий ли" \
        if (big and autocard and no_signal) else ""

    # ── Платящий клиент: won с реальной продажей (value>0 / pipe 53 / стадия «Оплата»).
    # Строже статуса «Действующий» и живёт отдельно: в карточке орга статус может быть
    # не проставлен, а деньги уже платят. Такому не пишем НИКОГДА — это класс ошибки T2
    # (заход действующему клиенту в обход владельца сделки).
    if (deal or {}).get("paying"):
        # ⚠️ Ставим НАШ статус «Действующий» (в карточке орга он может быть не проставлен).
        # Это не косметика: по нему конвертер и форматтер сами дают серую заливку и
        # прочерки во всех оценочных ячейках. Без этого платящий клиент приезжает к MDR
        # зелёным «Новым» с пустым вердиктом — ровно та путаница, от которой прочерки и
        # придуманы. Вердикт-иконку НЕ выдумываем: у действующих все три ячейки = «—».
        r["status"] = "Действующий"
        # Поля тоже гасим — по действующему мы не показываем НИЧЕГО (канон таблицы:
        # серый статус + прочерки во всех оценочных ячейках). Иначе валидатор
        # справедливо ругается «действующий, но level заполнено», а MDR видит
        # полкарточки данных по клиенту, с которым и так не работаем.
        for _f in ("level", "industry", "bizmodel", "competitor", "traffic"):
            r[_f] = "—"
        return _seal(r, "—", "—", "нет", "платящий клиент (won с реальной продажей) — не пишем",
                     {**src, "verdict": "won с продажей = действующий, статус проставлен нами"}), None

    # ── БАТЧ 2: есть открытая сделка ────────────────────────────────────────
    # Компания целевая ПО ОПРЕДЕЛЕНИЮ (сделки заводят только с целевыми) —
    # индустрию, уровень и трафик заново НЕ требуем, к модели не отправляем.
    # Вердикт «пишем/нет» здесь НЕ выносим: решит тест на занятость (фаза Touch).
    # ⚠️ Раньше тут стояло безусловное «нет, уже в воронке» — это и было главной
    # ошибкой модели: сделка на компании человека не занимает.
    if r["in_funnel"]:
        # Принцип: что Pipedrive уже признал — заново не пересуживаем.
        # Компанию признали, заведя сделку. Персону — ПРИВЯЗАВ её к сделке.
        if r["on_deal"]:
            return _seal(r, "🟢", "🟢", "да", "привязан к открытой сделке — должность целевая по умолчанию" + warn_autocard,
                         {**src, "level": "pipedrive (сделка)", "industry": "не требовалась",
                          "traffic": "не требовался",
                          "verdict": "батч 2 · лид привязан к сделке (должность не пересуживаем)"}), None
        # Не привязан: зарегистрироваться на ивент мог кто угодно из компании,
        # включая разработчика → должность проверяем как обычно.
        pv, work, why = _role_verdict(role, tier)
        return _seal(r, "🟢", pv, work, f"{why} · сделка на компании, но на другой персоне" + warn_autocard,
                     {**src, "level": "pipedrive (сделка)", "industry": "не требовалась",
                      "traffic": "не требовался", "verdict": f"батч 2 · не в сделке · {why}"}), None

    # ── Персона по роли: три исхода, все детерминированные ───────────────────
    pv, work, why = _role_verdict(role, tier)

    # ── БАТЧ 3: новый контакт ───────────────────────────────────────────────
    # ⚠️ С 21.07.2026 к модели НЕ уходит ничего: вся классификация уже проставлена
    # в Pipedrive (команда дозаполняет карточки отдельным ежесуточным промптом).
    # Пустая индустрия/уровень больше НЕ повод идти на сайт или в SimilarWeb —
    # поле остаётся пустым, а вердикт по компании становится «уточнить».
    if blank(industry) or blank(level) or level not in REAL_LEVELS:
        cv = "🟢" if target == "TRUE" else "🟡"
        pv2, work2, why2 = _role_verdict(role, tier)
        gap = " · ".join(x for x in (
            "нет индустрии в Pipedrive" if blank(industry) else "",
            "нет уровня в Pipedrive" if blank(level) or level not in REAL_LEVELS else "") if x)
        if cv == "🟡":
            pv2, work2, why2 = pv2, "уточнить", f"{gap} — карточка не дозаполнена"
        return _seal(r, cv, pv2, work2, f"{why2}" + (f" · {gap}" if cv == "🟢" else ""),
                     {**src, "level": "pipedrive (пусто)" if blank(level) else "pipedrive",
                      "industry": "pipedrive (пусто)" if blank(industry) else "pipedrive",
                      "traffic": "не собираем", "verdict": why2}), None

    # «ASK EVERY TIME» в поле «Целевой» = Pipedrive прямо просит спрашивать каждый раз.
    # Ответить «целевая» и отправить лида в работу — значит проигнорировать эту просьбу.
    if target == "ASK EVERY TIME":
        cv, work = "🟡", "уточнить"
        why = "«Целевой» = ASK EVERY TIME в Pipedrive — уточнить на звонке"
    else:
        cv = "🟢" if target == "TRUE" or level in REAL_LEVELS else "🟡"
    return _seal(r, cv, pv, work, why + warn_autocard,
                 {**src, "level": "pipedrive", "industry": "pipedrive",
                  "traffic": "не требовался", "verdict": why}), None


def _clean_domain(raw):
    """Грязное поле «Основной сайт» → один нормальный домен.

    В Pipedrive домен часто записан списком: «alfabank.ru, Alfabank.ru»,
    «.mindbox.ru, mindbox.ru, mindbox.ru», «megafon.ru, https://megafon.ru/».
    Раньше такие строки уезжали в GARBAGE_DOM («пробел = не домен») и крупные
    компании молча получали 🔴 «мусорный домен». Проверено на прогоне 260416: 23 шт.

    Возвращает (чистый_домен, исходная_строка_если_чистили).
    """
    s0 = str(raw or "").strip().lower()
    cands = []
    for part in re.split(r"[,;\s]+", s0):
        part = re.sub(r"^https?:(//)?|^www\.", "", part).split("/")[0].strip(". ")
        if part and "." in part and part not in cands:
            cands.append(part)
    if not cands:                       # ни одного домено-подобного куска — реально мусор
        return re.sub(r"^https?:(//)?|^www\.", "", s0).split("/")[0], ""
    return cands[0], (s0 if cands[0] != s0 else "")


def _on_deal(deal, person_id):
    """Привязан ли ЭТОТ человек к открытой сделке организации.

    Если да — его должность целевая по умолчанию: раз его привязали к сделке,
    релевантным его уже признали, и пересуживать по тайтлу мы не вправе
    (в сделке вполне может сидеть «Директор по IT», ведущий внедрение).
    Если нет — на ивент мог зарегистрироваться кто угодно из компании, включая
    разработчика, поэтому должность проверяем обычным порядком.
    """
    if not deal or not person_id:
        return False
    ids = deal.get("person_ids") or ([deal["person_id"]] if deal.get("person_id") else [])
    return any(str(p) == str(person_id) for p in ids)


def _role_verdict(role, tier=""):
    """Целевая ли должность: тайтл + поле «Тир ЛПР» из Pipedrive.

    Тир — уровень должности, проставленный командой: «Менеджмент (Маркдир, Дир по
    ecom, Коммерческий)» vs «Линейный сотрудник». Он разрешает спорные тайтлы,
    которые регекспом не берутся: голый «Product Manager» у менеджмента — целевой,
    у линейного — уточнить. Собирается батчами getPersons(ids) по 100 — дёшево.
    """
    # ⚠️ Значений тира ТРИ, не два (проверено на живом прогоне 260416):
    # «Топ (Гендир, Владелец)» · «Менеджмент (Маркдир, Дир по ecom, Коммерческий)» ·
    # «Линейный сотрудник (Маркетолог, CRM-менеджер, Перфоманс, Категорийный)».
    # Топ — сильнейший сигнал ЛПР; забыть его = потерять гендиров и владельцев.
    tl = tier.lower()
    mgmt = ("менеджмент" in tl) or ("топ" in tl)
    line = "линейн" in tl
    if blank(role):
        # Тайтла нет, но тир есть — решаем по тиру, это ровно его назначение.
        if mgmt:
            return "🟢", "да", "должность не указана, но тир ЛПР = Менеджмент"
        return "🟡", "уточнить", "должность не указана — уточнить ЛПР"
    if ROLE_NOT_DM.search(role):
        # 🟡, не 🔴: компанию не теряем, ищем другого человека (SKILL.md «итог 🟡 найти ЛПР»).
        # 🔴 в этой колонке читается как «персона нецелевая» и путает продажника.
        return "🟡", "уточнить", "роль не ЛПР — найти ЛПР в компании"
    # Product Manager: с контекстом (CRM/growth/лояльность/маркетинг…) — целевой сразу;
    # голый — решает тир, а без тира честно «уточнить», а не «да».
    if PM_ROLE.search(role) and not PM_CONTEXT.search(role):
        if mgmt:
            return "🟢", "да", f"«{role[:26]}» + тир ЛПР = Менеджмент"
        if line:
            return "🟡", "уточнить", f"«{role[:26]}», тир = Линейный — нужен выход на ЛПР"
        return "🟡", "уточнить", f"«{role[:26]}» без контекста CRM/маркетинга — уточнить"

    if ROLE_DM.search(role):
        return "🟢", "да", "целевая компания + релевантная роль"
    # Тайтл неоднозначный — тир решает. Менеджмент → берём, линейный → уточнить.
    if mgmt:
        return "🟢", "да", f"роль «{role[:26]}» + тир ЛПР = Менеджмент"
    if line:
        return "🟡", "уточнить", f"роль «{role[:26]}», тир = Линейный сотрудник — искать ЛПР"
    return "🟡", "уточнить", f"роль «{role[:30]}» неоднозначна — уточнить на звонке"


def _in_funnel(deal):
    """Есть ли у организации открытая сделка → лид идёт в БАТЧ 2.

    ⚠️ Это НЕ «занят». Занятость считается по контакту (свежее касание или
    запланированная задача) и применяется скриптом `apply_touches.py` после
    фазы Touch. Открытая сделка на компании человека не занимает — она лишь
    говорит, что компания целевая, и позволяет пропустить её квалификацию.

    Раньше здесь же проверялись шаги/задачи сделки и её привязка к персоне —
    это была попытка выразить занятость через сделку. Модель уточнена 20.07:
    занятость живёт на контакте, поэтому та логика убрана.
    """
    return bool(deal and deal.get("in_funnel"))


def _seal(r, cv, pv, work, reason, sources):
    r.update({"company_verdict": cv, "person_verdict": pv, "work": work,
              "reason": reason, "sources": sources})
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--enriched", required=True)
    ap.add_argument("--deals", help="JSON: [{org_id, person_id, cell, in_funnel, won_month}]")
    ap.add_argument("--refs", default="references")
    ap.add_argument("--out-decided", required=True)
    ap.add_argument("--out-residue", required=True)
    a = ap.parse_args()

    competitors, partners = load_disqualifiers(a.refs)
    enriched = json.load(open(a.enriched, encoding="utf-8"))
    if isinstance(enriched, dict):
        enriched = list(enriched.values())
    deals = {}
    if a.deals and os.path.exists(a.deals):
        for d in json.load(open(a.deals, encoding="utf-8")):
            deals[d.get("org_id")] = d

    decided, residue, why = [], [], {}
    for e in enriched:
        if str(e.get("status") or "").startswith("Действующий"):
            continue                                   # действующих не трогаем вообще
        row, need = decide(e, deals.get(e.get("org_id")), competitors, partners)
        if row:
            decided.append(row)
        else:
            residue.append(e)
            why[need] = why.get(need, 0) + 1

    json.dump(decided, open(a.out_decided, "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(residue, open(a.out_residue, "w", encoding="utf-8"), ensure_ascii=False)

    total = len(decided) + len(residue)
    share = len(decided) / total * 100 if total else 0
    print(f"правилами решено: {len(decided)} из {total} ({share:.0f}%) — модель на них НЕ тратится")
    print(f"остаток для модели: {len(residue)}")
    for k, v in sorted(why.items(), key=lambda x: -x[1]):
        print(f"   {k}: {v}")
    batch2 = sum(1 for r in decided if r.get("in_funnel"))
    todo = sum(1 for r in decided if r.get("work") in ("да", "уточнить"))
    print(f"батч 2 (открытая сделка, целевые по умолчанию): {batch2}")
    print(f"на тест занятости (двустороннее общение <3 мес ИЛИ договорённость) уйдёт: "
          f"{todo} строк + то, что вернёт модель по остатку")
    return 0


if __name__ == "__main__":
    sys.exit(main())
