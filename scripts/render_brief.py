#!/usr/bin/env python3
"""Печатает бриф по лиду из JSON в канонический вид.

Зачем скрипт, а не проза в reference-файле: формат брифа жил описанием
в `references/outreach_brief.md` и ничем не удерживался — каждый прогон
агент собирал бриф по-своему, а у вердикта («можно ли писать») формата
не было вообще. Здесь вид форсится кодом, как таблицу ивента форсит
build_table.py. Агент отдаёт ДАННЫЕ, скрипт отвечает за ВИД.

Использование:
    python3 scripts/render_brief.py --in brief.json
    cat brief.json | python3 scripts/render_brief.py

Схема входного JSON — в докстринге render() ниже и в
references/outreach_brief.md. Любой блок можно не передавать: он
напечатается с прочерком, но НЕ исчезнет — иначе два брифа нельзя
сравнить глазами.
"""

import argparse
import json
import sys
import textwrap

WIDTH = 78          # общая ширина строки
LABEL_W = 14        # ширина колонки меток, включая значок и пробелы
IND = " " * LABEL_W

BLOCKS = [
    ("contact", "👤 КОНТАКТ"),
    ("verdict", "✅ ВЕРДИКТ"),
    ("history", "🕓 ИСТОРИЯ"),
    ("pain", "💢 БОЛЬ"),
    ("card", "🏢 КАРТОЧКА"),
    ("deals", "💼 СДЕЛКИ"),
    ("activity", "🎟 АКТИВНОСТЬ"),
    ("technical", "⚠️ СЛУЖЕБНОЕ"),
]

# Значки типов касания в блоке ИСТОРИЯ. Разделение «наше сообщение» и
# «ответ клиента» обязательно: по нему MDR отличает «мы общались» от
# «мы писали в пустоту», а это меняет первую фразу захода.
ICONS = {
    "meeting": "📅", "message": "💬", "reply": "🗣", "silence": "🔇",
    "task": "⏳", "burned": "🔥", "won": "✔", "lost": "✖", "open": "🔵",
}


def dwidth(s):
    """Ширина строки на экране: эмодзи рисуются в две колонки, а len() их
    считает за один символ — из-за этого колонка меток съезжала."""
    w = 0
    for ch in s:
        o = ord(ch)
        w += 2 if (0x1F300 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF
                   or o in (0x2705, 0x26D4, 0x2714, 0x2716)) else 1
    return w


def pad(label):
    return label + " " * max(1, LABEL_W - dwidth(label))


def emit(out, label, lines):
    """Печатает блок: метка в левой колонке, содержимое справа с переносом."""
    lines = [l for l in lines if l not in (None, "")]
    if not lines:
        lines = ["—"]
    first = True
    for line in lines:
        # строки, начинающиеся с пробелов, — уже выровненное продолжение
        keep = line.startswith("  ")
        body = line if keep else line
        wrapped = textwrap.wrap(
            body, width=WIDTH - LABEL_W,
            subsequent_indent="   " if keep else "",
            drop_whitespace=not keep,
        ) or [""]
        for w in wrapped:
            prefix = pad(label) if first else IND
            out.append(f"{prefix}{w}".rstrip())
            first = False
    out.append("")


def j(*parts, sep=" · "):
    """Склеивает непустые куски через разделитель."""
    return sep.join(str(p) for p in parts if p not in (None, "", []))


def plural(n):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return "регистрация"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "регистрации"
    return "регистраций"


def num(v):
    return f"{v:,}".replace(",", " ") if isinstance(v, (int, float)) else v


def money(v):
    if not isinstance(v, (int, float)):
        return v
    if v >= 1_000_000_000:
        return f"{v / 1_000_000_000:.2f}".rstrip("0").rstrip(".") + " млрд ₽"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}".rstrip("0").rstrip(".") + " млн ₽"
    return f"{num(int(v))} ₽"


def render(d):
    """Собирает бриф. Ключи верхнего уровня — как в BLOCKS.

    contact  : name, title, company, domain, business, email, phone,
               telegram, tier
    verdict  : decision ("ПИШЕМ"/"НЕ ПИШЕМ"), reason, rule, owned_by,
               warnings[], hook, collapse ("active"|"wood"|"nontarget")
    history  : events[{date, icon, who, text}], summary, burned[]
    pain     : explicit, signals[], stack[]
    card     : domain, level, segment, industry, model, traffic, revenue,
               points, client_status, target, competitors[], warnings[]
    deals    : total, open, rows[{date,status,amount,owner,pipeline,note}],
               warnings[]
    activity : count_24m, events[{date,name,source}], content[]
    technical: org_id, person_id, duplicates[], notes[]
    """
    out = []
    v = d.get("verdict") or {}
    collapse = v.get("collapse")           # схлопывание тяжёлых блоков
    short = {"active": "клиент действующий", "wood": "уровень Wood",
             "nontarget": "компания нецелевая"}.get(collapse)

    for key, label in BLOCKS:
        b = d.get(key) or {}
        lines = []

        if key == "contact":
            lines = [
                j((b.get("name") or "").upper(), b.get("title")),
                j(b.get("company") and f"{b['company']} ({b.get('domain','')})".replace(" ()", ""),
                  b.get("business")),
                j(b.get("email"), b.get("phone"),
                  b.get("telegram") or "Telegram нет"),
                b.get("tier") and f"тир ЛПР: {b['tier']}",
            ]
            label = "👤 КОНТАКТ"

        elif key == "verdict":
            label = "⛔ ВЕРДИКТ" if str(v.get("decision", "")).startswith("НЕ") else "✅ ВЕРДИКТ"
            lines = [j(v.get("decision"), v.get("reason"))]
            lines.append(v.get("rule"))
            if v.get("owned_by"):
                lines.append(f"ведут {v['owned_by']}")
            for w in v.get("warnings") or []:
                lines.append(f"⚠️ {w}")
            if v.get("hook"):
                lines.append(f"зацепка: {v['hook']}")

        elif key == "history":
            if short:
                lines = [f"не разбираем — {short}"]
            else:
                for e in b.get("events") or []:
                    ic = ICONS.get(e.get("icon", ""), e.get("icon", ""))
                    lines.append(j(e.get("date"), ic, e.get("who"), e.get("text")))
                if b.get("summary"):
                    lines.append(f"итог: {b['summary']}")
                for x in b.get("burned") or []:
                    lines.append(f"{ICONS['burned']} сожжено: {x}")

        elif key == "pain":
            if short:
                lines = [f"не собираем — {short}"]
            else:
                lines = [b.get("explicit") or "явной нет — в карточке цитат нет"]
                for s in b.get("signals") or []:
                    lines.append(f"сигналы: {s}")
                if b.get("stack"):
                    lines.append("🧰 стек: " + " · ".join(b["stack"]))

        elif key == "card":
            lines = [
                j(b.get("domain"), b.get("level"), b.get("segment")),
                j(b.get("industry"), b.get("model")),
                j(b.get("traffic") and f"{num(b['traffic'])} визитов/мес",
                  b.get("revenue") and money(b["revenue"]),
                  b.get("points") and f"{b['points']} точек"),
                j(f"статус: {b.get('client_status') or 'не заполнен'}",
                  f"целевой: {b.get('target') or 'не заполнен'}"),
                b.get("competitors") and "🧰 конкурент: " + " · ".join(b["competitors"]),
            ]
            for w in b.get("warnings") or []:
                lines.append(f"⚠️ {w}")

        elif key == "deals":
            if short == "клиент действующий":
                lines = [f"{b.get('total', '—')} на орге — не разбираем, {short}"]
            else:
                head = j(f"{b.get('total', 0)} всего",
                         b.get("open") and f"из них {b['open']} открыто")
                lines = [head]
                for r in b.get("rows") or []:
                    ic = ICONS.get(r.get("status", ""), "")
                    lines.append(j(r.get("date"), ic, money(r["amount"]) if r.get("amount") is not None else None,
                                   r.get("owner"), r.get("pipeline") and f"воронка {r['pipeline']}",
                                   r.get("note")))
                for w in b.get("warnings") or []:
                    lines.append(f"⚠️ {w}")

        elif key == "activity":
            if short == "клиент действующий":
                lines = [f"не разбираем — {short}"]
            else:
                n = b.get("count_24m")
                lines = [n is not None and f"{n} {plural(n)} за 24 мес"]
                for e in b.get("events") or []:
                    lines.append(j(e.get("date"), e.get("name") and f"«{e['name']}»",
                                   e.get("source")))
                if b.get("content"):
                    lines.append("контент: " + " · ".join(b["content"]))
                else:
                    lines.append("контента нет")

        elif key == "technical":
            lines = [j(b.get("org_id") and f"org {b['org_id']}",
                       b.get("person_id") and f"person {b['person_id']}")]
            for x in b.get("duplicates") or []:
                lines.append(f"⚠️ дубль: {x}")
            for n in b.get("notes") or []:
                lines.append(f"⚠️ {n}")

        emit(out, label, lines)

    # разделитель перед служебным блоком
    sep_at = next(i for i, l in enumerate(out) if l.startswith("⚠️ СЛУЖЕБНОЕ"))
    out.insert(sep_at, "─" * WIDTH)
    return "\n".join(out).rstrip() + "\n"


def main():
    p = argparse.ArgumentParser(description="Печать брифа по лиду в каноническом виде")
    p.add_argument("--in", dest="src", help="JSON-файл; без него читаем stdin")
    a = p.parse_args()
    raw = open(a.src, encoding="utf-8").read() if a.src else sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Не разобрал JSON: {e}", file=sys.stderr)
        return 2
    sys.stdout.write(render(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
