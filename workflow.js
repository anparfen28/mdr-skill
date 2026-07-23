export const meta = {
  name: 'mdr',
  description: 'Отбор лидов с ивента: всё вытаскивается из Pipedrive готовым (ничего не квалифицируем заново) → правила отсекают действующих и нецелевых без токенов → тест занятости контакта по шорт-листу → таблица с вердиктом «работаем/нет».',
  whenToUse: 'Отбор регистрантов ивента Mindbox. Запуск: Workflow({scriptPath: "~/.claude/skills/mdr/workflow.js", args: {event_code, out_dir}}) — этого достаточно, регистранты собираются сами.',
  phases: [
    { title: 'Collect',  detail: 'все регистранты ивента из Pipedrive по коду ивента' },
    { title: 'Enrich',   detail: 'один bulk-проход: ВСЕ поля as-is, ничего не досчитываем' },
    { title: 'Deals',    detail: 'сделки ПАЧКАМИ по пайплайнам + связка локально' },
    { title: 'Classify', detail: 'вердикты правилами из полей Pipedrive (0 токенов)' },
    { title: 'Touch',    detail: 'занят ли контакт: двустороннее общение + договорённости (3 мес) — по шорт-листу' },
    { title: 'Validate', detail: 'детерминированные проверки чеклиста (скрипт)' },
    { title: 'Table',    detail: 'вкладка ивента по канону: 23 колонки, сводка, цвета' },
  ],
}

// ─────────────────────────────────────────────────────────────────────────────
// ПРИНЦИП: правила живут в файлах скилла, а не в этих промптах.
// ID полей — ТОЛЬКО из references/fields.json (агент читает файл сам).
// Дважды был баг «в Уровень попал Сегмент» именно потому, что ID переписывали по памяти.
// ─────────────────────────────────────────────────────────────────────────────

// ⚠️ Путь к скиллу — литерал, а НЕ process.env: в песочнице workflow нет Node API.
// Переопределяется через args.skill_dir (нужно коллегам с другим домашним каталогом).
const SKILL  = (typeof args === 'object' && args && args.skill_dir) || '/Users/anna/.claude/skills/mdr'
const FIELDS = `${SKILL}/references/fields.json`
const PDFLD  = `${SKILL}/references/pipedrive_fields.md`
const EDGE   = `${SKILL}/references/edge_cases.md`

// args могут прийти объектом ИЛИ JSON-строкой (зависит от того, как их передали в Workflow).
// Не разбирать строку = падение на первой строке с невнятным «event_code обязателен».
const A = typeof args === 'string' ? JSON.parse(args) : (args || {})
const EVENT = A.event_code || (() => { throw new Error(
  `args.event_code обязателен (напр. "260722_on_messengersfoxford"). Получено args: ${JSON.stringify(args)}`) })()
const OUT   = A.out_dir    || (() => { throw new Error('args.out_dir обязателен') })()
// rows_path не обязателен: если не дан — регистранты собираются фазой Collect.
// allow_slow_fallback=true разрешает медленный перебор активностей, когда фильтра нет.
const ROWS  = A.rows_path || `${OUT}/rows_${EVENT}.json`

const ENR = `${OUT}/enriched_${EVENT}.json`
const PD  = 'mcp__555487e0-0932-4938-8d02-6124bc9b2521'

const S_ENRICH = { type:'object', properties:{ path:{type:'string'}, total:{type:'number'}, orgs:{type:'number'},
  filled:{type:'object', properties:{ level:{type:'number'}, traffic:{type:'number'}, industry:{type:'number'}, tier:{type:'number'} } },
  conflicts:{type:'number'} }, required:['path','total','orgs'] }
const S_DEALS = { type:'object', properties:{ today:{type:'string'}, calls:{type:'number'}, deals:{type:'array', items:{ type:'object', properties:{
  org_id:{type:'number'}, cell:{type:'string'}, in_funnel:{type:'boolean'},
  // person_ids — КТО привязан к открытым сделкам орга. Решает, проверять ли должность:
  // привязан → целевая по умолчанию; не привязан → проверяем.
  // Шаги/задачи по сделке НЕ собираем: занятость живёт на контакте.
  person_ids:{type:'array', items:{type:'number'}}, person_id:{type:'number'},
  won_month:{type:'string'}, paying:{type:'boolean'}, won_value:{type:'number'} }, required:['org_id','cell','in_funnel'] } } }, required:['deals'] }
// S_ROW / S_ROWS / S_TRAF удалены вместе с фазами Qualify и Traffic (21.07.2026):
// строки больше не производит модель — их целиком закрывает classify_rows.py.
const S_VALID = { type:'object', properties:{ clean:{type:'boolean'}, problems:{type:'array', items:{type:'object', properties:{
  // level ОБЯЗАТЕЛЕН: гейт перед Table фильтрует по нему. Без него в схеме агент
  // легально вернёт problems без level, гейт увидит 0 ERROR и пропустит брак.
  person_id:{type:'number'}, issue:{type:'string'}, level:{type:'string'} }, required:['issue','level']}},
  by_work:{ type:'object', properties:{ да:{type:'number'}, уточнить:{type:'number'}, нет:{type:'number'} } } },
  required:['clean','problems'] }

// ── Фаза 0. Collect — собрать регистрантов ивента САМОСТОЯТЕЛЬНО ─────────────
// Никаких заранее заведённых руками фильтров: на входе только код ивента.
// Механика (разведка 19.07): регистрация = активность type=registratsiya_na_sobitie,
// owner_id = системный юзер форм сайта, в note лежит анкета с eventId/сайтом/должностью/базой.
// Серверно по type фильтровать нельзя (нет параметра), поэтому сужаем owner_id + окном дат
// и режем тип клиентски. Окно берём из самого кода ивента: YYMMDD.
const DAY = 86400000
const iso = ms => new Date(ms).toISOString().slice(0, 19) + 'Z'
const SCAN_DAYS = A.scan_days || 60            // воронка регистраций до ивента

// ⚠️ Код ивента НЕ всегда YYMMDD. Реальный пример (19.07): «210526_on_retentionweb» —
// это 21.05.26 (DDMMYY), май 2026, а не 26 мая 2021. Разбор «в лоб» отправил бы сбор
// сканировать 2021 год и вернул бы ноль регистрантов, ничего не заподозрив.
// Поэтому: строим ВСЕ правдоподобные прочтения и даём пробе выбрать по фактическим данным.
const dm = /^(\d{2})(\d{2})(\d{2})_/.exec(EVENT)
if (!dm && !A.event_date) throw new Error(`Из кода "${EVENT}" не вывести дату. Передай args.event_date="YYYY-MM-DD".`)
const cands = []
const push = (label, y, m, d) => {
  if (m < 1 || m > 12 || d < 1 || d > 31) return
  const ms = Date.UTC(y, m - 1, d)
  if (!Number.isNaN(ms) && !cands.some(c => c.ms === ms)) cands.push({ label, ms })
}
if (A.event_date) push('args', ...A.event_date.split('-').map(Number))
if (dm) {
  const [, a, b, c] = dm.map(Number)
  push('YYMMDD', 2000 + a, b, c)     // 260618 → 18.06.2026
  push('DDMMYY', 2000 + c, b, a)     // 210526 → 21.05.2026
}
if (!cands.length) throw new Error(`Код "${EVENT}" не даёт ни одной валидной даты`)
let EV_MS = cands[0].ms                        // уточняется пробой по фактическим данным

const S_PROBE = { type:'object', properties:{ owner_id:{type:'number'}, today:{type:'string'},
  found:{type:'number'}, event_name:{type:'string'}, event_date:{type:'string'},
  tried:{type:'string'} }, required:['today','found'] }
const S_WIN = { type:'object', properties:{ path:{type:'string'}, found:{type:'number'},
  scanned:{type:'number'}, calls:{type:'number'}, incomplete:{type:'boolean'}, uncovered:{type:'string'} },
  required:['found','scanned','incomplete'] }

phase('Collect')
let collectedTotal = 0
if (!A.rows_path) {
  // Проба: подтвердить, что ивент существует, и определить owner_id регистраций.
  const probe = await agent(
    `READ-ONLY. Проба перед сбором регистрантов ивента ${EVENT}. Твоя главная задача — ОПРЕДЕЛИТЬ ДАТУ ИВЕНТА по данным.\n` +
    `Прочитай ${FIELDS} → activity.registration_type и activity.registration_owner_id (дефолт, может смениться).\n` +
    `Загрузи ToolSearch "select:${PD}__getActivities".\n` +
    `\n⚠️ Код ивента читается неоднозначно. Кандидаты (в порядке правдоподобия):\n` +
    cands.map(c => `   • ${c.label}: ${iso(c.ms).slice(0,10)}`).join('\n') + `\n` +
    `Проверь их ПО ОЧЕРЕДИ, по одному вызову на кандидата (узкое окно вокруг даты):\n` +
    `getActivities(limit:500, sort_by:"add_time", sort_direction:"desc", updated_since:"<дата-4дня>", updated_until:"<дата+2дня>")\n` +
    `Ответ уйдёт в файл — парси python-ом, в контекст не тяни. Максимум ${cands.length + 2} вызовов.\n` +
    `Ищи активности с type == activity.registration_type, у которых в subject или в note (eventId) есть "${EVENT}".\n` +
    `Победитель — кандидат, у которого находок БОЛЬШЕ. Ни у кого нет находок → верни found=0 и tried со списком проверенного.\n` +
    `\n⚠️ Регистрации на ивент — это активности типа registratsiya_na_sobitie, и код ивента лежит у них в subject ` +
    `(«Регистрация на событие {код}») и в note (eventId). Это проверено на 260618: так собрано 1100 регистрантов. ` +
    `Если в окне кандидата пусто — это значит «дата не та», а НЕ «кодов в активностях не бывает».\n` +
    `\nВерни: event_date (YYYY-MM-DD победившего кандидата), found (находок у победителя), tried (что проверил и сколько нашёл), ` +
    `owner_id (самый частый среди находок; нет находок → дефолт из fields.json), ` +
    `event_name (человекочитаемое из note eventName), today (Bash: date +%F — НЕ выдумывать).`,
    { schema: S_PROBE, label:'probe', phase:'Collect', model:'claude-sonnet-5' }
  )
  const OWNER = probe.owner_id || 24712797
  if (probe.event_date && !Number.isNaN(Date.parse(`${probe.event_date}T00:00:00Z`)))
    EV_MS = Date.parse(`${probe.event_date}T00:00:00Z`)
  const evMs = EV_MS
  log(`Дата ивента: ${iso(evMs).slice(0,10)}${probe.tried ? ` · проверено: ${probe.tried}` : ''}`)
  log(`Проба: ${probe.found} регистраций в контрольном окне · owner_id ${OWNER}` +
      `${probe.event_name ? ` · «${probe.event_name}»` : ''}`)
  if (!probe.found) log(`⚠️ В контрольном окне регистраций не найдено — возможно, код ивента неверен или воронка шла в другие даты. Сбор всё равно пойдёт по полному окну.`)

  // Окна: вся воронка до ивента + хвост до сегодня (updated_time мог уехать вперёд
  // у записей, которые кто-то трогал после ивента — иначе они выпадут из выборки).
  const wins = []
  const STEP = 6 * DAY
  for (let t = evMs - SCAN_DAYS * DAY; t < evMs + 2 * DAY; t += STEP)
    wins.push({ from: t, to: Math.min(t + STEP, evMs + 2 * DAY), idx: wins.length })
  const todayMs = Date.parse(`${probe.today}T23:59:59Z`)
  // Хвост нужен только как страховка (активности почти не правят после создания:
  // 69% не трогают вовсе, ни одной правки позже 30 дней). Поэтому ограничиваем 45 днями,
  // иначе на старом ивенте хвост растянулся бы на годы и сжёг вызовы впустую.
  const tailTo = Math.min(todayMs || 0, evMs + 45 * DAY)
  if (tailTo > evMs + 2 * DAY) wins.push({ from: evMs + 2 * DAY, to: tailTo, idx: wins.length, tail: true })
  log(`Сбор: ${wins.length} окон по ~6 дней, ${iso(evMs - SCAN_DAYS*DAY).slice(0,10)} → ${probe.today} (параллельно)`)

  const wres = await parallel(wins.map(w => () => agent(
    `READ-ONLY. Собери регистрации на ивент ${EVENT} в окне ${iso(w.from)} … ${iso(w.to)}${w.tail ? ' (хвост после ивента)' : ''}.\n` +
    `Загрузи ToolSearch "select:${PD}__getActivities". Прочитай ${FIELDS} → activity.* (тип, формат note).\n` +
    `1. Листай с курсором, ПОКА окно не кончится:\n` +
    `   getActivities(owner_id:${OWNER}, limit:500, sort_by:"add_time", sort_direction:"asc", updated_since:"${iso(w.from)}", updated_until:"${iso(w.to)}")\n` +
    `   дальше — тот же вызов с cursor из additional_data.next_cursor. Ответы уходят в файлы — парси python-ом, в контекст НЕ тяни.\n` +
    `   Лимит 15 вызовов. Упёрся, а курсор ещё есть → incomplete=true и uncovered="<с какой даты не покрыто>". НЕ молчи об этом.\n` +
    `2. Клиентски оставь активности с type == "registratsiya_na_sobitie" И относящиеся к ${EVENT} ` +
    `(eventId в note или хвост subject). Регистрации на другие ивенты и на курсы — мимо.\n` +
    `3. Из каждой распарсь анкету в note (html.unescape, split('<br>'), split(':', 1) — значения содержат двоеточия, ` +
    `неизвестные ключи игнорируй) и собери:\n` +
    `   person_id, org_id, name (Имя), role (Должность), email (Email, вынь из <a href="mailto:...">), ` +
    `   phone (Телефон), site (siteUrl), base_size (Размер базы — часто пусто, это норм), add_time,\n` +
    `   **event_id — код ивента из note (eventId) КАК ЕСТЬ**. Это аудит-след: без него нельзя ` +
    `проверить, что собран именно нужный ивент, а в одном окне дат их бывает несколько. ` +
    `Не подставляй "${EVENT}" вслепую — пиши то, что реально стоит в записи.\n` +
    `   ⚠️ Пустое поле — это пустая строка, НЕ выдумывай значения.\n` +
    `4. Запиши массив в ${OUT}/collect_${EVENT}_${w.idx}.json. Верни path, found, scanned (сколько активностей просмотрено), ` +
    `calls, incomplete, uncovered.`,
    { schema: S_WIN, label:`collect:${w.idx}`, phase:'Collect', model:'claude-sonnet-5' }
  )))

  const ok = wres.filter(Boolean)
  const scanned = ok.reduce((n, r) => n + (r.scanned || 0), 0)
  const rawFound = ok.reduce((n, r) => n + (r.found || 0), 0)
  const bad = ok.filter(r => r.incomplete)
  const died = wres.length - ok.length
  log(`Просмотрено активностей: ${scanned} · найдено регистраций: ${rawFound} (до дедупа)`)
  if (died) log(`⚠️ Окон упало: ${died} — часть периода не покрыта, перезапусти прогон для добора`)
  if (bad.length) {
    log(`⚠️ НЕПОЛНОЕ ПОКРЫТИЕ в ${bad.length} окнах — список НЕ полный:`)
    for (const b of bad) log(`   не покрыто: ${b.uncovered || 'неизвестный интервал'}`)
    log(`   Увеличь лимит вызовов или сузь шаг окна (args.scan_days), затем перезапусти.`)
  }

  const merged = await agent(
    `Слей собранные регистрации в один список.\n` +
    `1. python3: прочитай все ${OUT}/collect_${EVENT}_*.json, слей, ДЕДУПНИ по person_id ` +
    `(при дублях бери запись с непустыми полями), отсортируй по person_id.\n` +
    `2. Запиши в ${ROWS} массив объектов: person_id, name, role, email, phone, org_id, site, base_size, event_id.\n` +
    `2б. ПРОВЕРЬ ЧИСТОТУ ВЫБОРКИ: посчитай, сколько записей с каким event_id. Все должны быть "${EVENT}". ` +
    `Есть чужие — НЕ выбрасывай молча: убери их из ${ROWS} и верни в foreign их коды с количеством.\n` +
    `   Поле company/домен НЕ заполняй — его подставит фаза Enrich из Pipedrive.\n` +
    `3. Верни total (после дедупа), dupes (сколько склеено), with_site, with_base (у скольких непустые site / base_size).`,
    { schema: { type:'object', properties:{ total:{type:'number'}, dupes:{type:'number'},
        with_site:{type:'number'}, with_base:{type:'number'},
        foreign:{type:'array', items:{type:'object', properties:{ event_id:{type:'string'}, n:{type:'number'} }, required:['event_id','n']}} },
        required:['total'] },
      label:'merge', phase:'Collect', effort:'low', model:'claude-sonnet-5' }
  )
  collectedTotal = merged.total
  log(`Регистрантов после дедупа: ${merged.total} (склеено дублей ${merged.dupes ?? 0}) · ` +
      `с сайтом ${merged.with_site ?? '?'} · с размером базы ${merged.with_base ?? '?'}`)
  if (!merged.total) return { event: EVENT, stopped: 'регистрантов не найдено', scanned, rows: [] }
} else {
  log(`Регистранты взяты из готового файла: ${ROWS}`)
}

// ── Фаза 1. Enrich — ОДИН проход, все поля сразу (включая Посещаемость) ──────
phase('Enrich')
const enr = await agent(
  `READ-ONLY, в Pipedrive НИЧЕГО не писать. Обогати регистрантов ивента ${EVENT} классификацией организаций ОДНИМ bulk-проходом.\n` +
  `1. Прочитай ${FIELDS} — это ЕДИНСТВЕННЫЙ источник ID полей. Возьми оттуда строку org.enrich_custom_fields и карту org.*.key. НЕ подставляй ID по памяти.\n` +
  `2. Прочитай ${ROWS} (регистранты: person_id, name, role, email, phone, org_id, site).\n` +
  `\n⚠️⚠️ **ШАГ 3 — САМЫЙ ВАЖНЫЙ. org_id ИЗ РЕГИСТРАЦИИ ВРАТЬ.** Форма с лендинга СОЗДАЁТ новую\n` +
  `организацию под доменом и вешает активность регистрации на НЕЁ, а сам человек в Pipedrive\n` +
  `остаётся привязан к НАСТОЯЩЕЙ карточке компании. Проверено 21.07 на живом ивенте: у 22%\n` +
  `проверенных орг разошлась. Пример: Ольга Балашкина — активность указывала на «t-j.ru» (org 727277,\n` +
  `статус пуст), а персона привязана к org 451 «journal.tinkoff.ru» со статусом **Действующий**.\n` +
  `Возьмёшь орг из регистрации — напишешь заход действующему клиенту в обход владельца сделки.\n` +
  `\n3. **Персоны батчами (ids до 100 за вызов) — ЗАБЕРИ org_id И tier:**\n` +
  `   Загрузи ToolSearch "select:${PD}__getPersons".\n` +
  `   getPersons(ids:"<csv до 100 id>", custom_fields:"<person.tier.key из fields.json>", include_option_labels:true)\n` +
  `   → по каждой персоне возьми **org_id** (это ИСТИНА об организации) и **tier** («Топ (Гендир, Владелец)» /\n` +
  `   «Менеджмент (Маркдир, Дир по ecom, Коммерческий)» / «Линейный сотрудник»). Пусто → пустая строка.\n` +
  `   ⚠️ org_id персоны ПЕРЕЗАПИСЫВАЕТ org_id из регистрации. Если они разошлись — положи в строку\n` +
  `   org_id_from_form = <старый> и пометь autocard_conflict = true (в таблицу пойдёт предупреждение).\n` +
  `\n4. **Организации — по ИСПРАВЛЕННЫМ org_id.** Загрузи ToolSearch "select:${PD}__getOrganizations".\n` +
  `   Батчами по 100: getOrganizations(ids:"<csv>", include_option_labels:true, custom_fields:"<org.enrich_custom_fields из fields.json>")\n` +
  `   Ответ большой — уйдёт в файл, парси python-ом, в контекст не тяни.\n` +
  `4б. Вытащи label'ы: level, segment, traffic, industry, bizmodel, target, competitor, status, domain, points, revenue, base_size.\n` +
  `   ⚠️ level = org.level.key (Wood…Diamond). segment = org.segment.key — РАЗНЫЕ поля, не смешивать.\n` +
  `5. Смёржи поля в каждую строку регистранта, запиши массив в ${ENR}.\n` +
  `   🔒 **ИМЕНА КЛЮЧЕЙ СТРОГО ТАКИЕ** (следующая фаза читает их скриптом; свои варианты вроде org_level ломают всё):\n` +
  `   person_id · name · role · tier · email · phone · org_id · org_id_from_form · autocard_conflict ·\n` +
  `   site · base_size · company · domain ·\n` +
  `   level · segment · traffic · industry · bizmodel · target · competitor · status · points · revenue · org_classified\n` +
  `   company = название организации из Pipedrive (нет — site из анкеты). domain = «Основной сайт» орг (нет — site).\n` +
  `   org_classified = true, если у орг заполнен level ИЛИ industry. Пустое поле — пустая строка, не пропуск ключа.\n` +
  `Верни path, total (строк), orgs (уник. орг), filled{level,traffic,industry,tier} и **conflicts** — ` +
  `у скольких org_id персоны разошёлся с org_id из регистрации.`,
  { schema: S_ENRICH, label:'enrich', phase:'Enrich', model:'claude-sonnet-5' }
)
log(`Обогащено ${enr.total} строк / ${enr.orgs} орг · уровень ${enr.filled?.level ?? '?'} · посещаемость ${enr.filled?.traffic ?? '?'} · тир ЛПР ${enr.filled?.tier ?? '?'}`)
if (enr.conflicts) log(`⚠️ У ${enr.conflicts} персон организация из регистрации НЕ совпала с реальной — взята реальная (форма создаёт орг-пустышку)`)

// ── Фаза 1б. Дубли орг — ищем ПО НАЗВАНИЮ, а не по домену ───────────────────
// Регистрация с лендинга заводит новую ПУСТУЮ орг под вариантом домена
// (t2.ru вместо tele2.ru, cu.ru вместо centraluniversity.ru). Персона цепляется
// к пустышке, и лид ложно выглядит новым. Прогон 18.07: 4 ошибки из 28, включая
// действующего клиента T2, которому чуть не ушёл заход.
const dupes = await agent(
  `READ-ONLY. Найди организации-ДУБЛИ среди обогащённых данных ивента ${EVENT}.\n` +
  `Прочитай ${ENR} и раздел про дубли в ${EDGE}.\n` +
  `1. Отбери ПОДОЗРИТЕЛЬНЫЕ орг: карточка пустая (нет level, нет industry, нет status, нет competitor) — ` +
  `такие создаются автоматически при регистрации с лендинга.\n` +
  `1б. ⚠️ ЛИМИТ: не больше 40 поисков за фазу. Пустых карточек бывают сотни, и поиск по каждой ` +
  `сжигает прогон (260618: 71 вызов). Отсортируй подозрительных по «похоже на крупную компанию» ` +
  `(короткий домен, узнаваемый бренд, несколько регистрантов с одной орг) и возьми топ-40 — ` +
  `цена ошибки есть только там, где заход реально уйдёт. Остальных не трогай.\n` +
  `2. Загрузи ToolSearch "select:${PD}__searchOrganization". Для каждой отобранной орг ищи дубль ` +
  `**ПО НАЗВАНИЮ КОМПАНИИ, НЕ по домену** (домен у дубля другой — в этом вся суть): searchOrganization(term:"<название>").\n` +
  `   Учитывай варианты: сокращения (t2 ↔ tele2), транслит (Фоксфорд ↔ foxford), юрлицо vs бренд, ` +
  `разный TLD, кириллица/латиница.\n` +
  `3. Дубль засчитывается, только если найденная орг ЗАПОЛНЕНА (есть level/status/industry) и это ` +
  `очевидно та же компания. Сомнение → НЕ склеивать, пометить "проверить руками".\n` +
  `4. Перезапиши ${ENR}: у строк с найденным дублем подставь классификацию из ЗАПОЛНЕННОЙ орг ` +
  `(level, industry, bizmodel, target, competitor, status, traffic) и проставь dup_of=<id заполненной орг>.\n` +
  `Верни merged (склеено строк), suspects (сколько проверено), manual[] (что требует рук: company + почему).`,
  { schema: { type:'object', properties:{ merged:{type:'number'}, suspects:{type:'number'},
      manual:{type:'array', items:{type:'object', properties:{ company:{type:'string'}, why:{type:'string'} }, required:['company','why']}} },
    required:['merged','suspects'] }, label:'dupes', phase:'Enrich' }
)
log(`Дубли орг: проверено ${dupes.suspects} пустых карточек · склеено ${dupes.merged}` +
    `${(dupes.manual || []).length ? ` · руками проверить ${dupes.manual.length}` : ''}`)
if ((dupes.manual || []).length) for (const m of dupes.manual) log(`   ⚠️ ${m.company}: ${m.why}`)

// ── Фаза 2. Раскладка по корзинам — ПЛАЙН JS, 0 токенов ─────────────────────
// Правило Анны: по действующим не смотрим НИЧЕГО. Не сделки, не трафик, не сайт.
const DEAD_LEVELS = ['Нецелевой', 'Агентство', 'Компания ушла из России']
const part = await agent(
  `Только чтение файла и python, никаких внешних вызовов. Прочитай ${ENR} и разложи строки по корзинам, НЕ квалифицируя:\n` +
  `- active: status == "Действующий" → по ним не делаем НИЧЕГО дальше (ни сделок, ни трафика, ни сайта).\n` +
  `- dead: level ∈ ${JSON.stringify(DEAD_LEVELS)} → компания заведомо нецелевая, дальше не обрабатываем.\n` +
  `- candidates: всё остальное.\n` +
  `⚠️ **Кандидатов ОТСОРТИРУЙ ПО org_id**, чтобы все персоны одной компании лежали подряд — ` +
  `батч потом режется по границам компаний, а не по номерам строк.\n` +
  `Запиши три файла ${OUT}/active_${EVENT}.json, ${OUT}/dead_${EVENT}.json, ${OUT}/cand_${EVENT}.json ` +
  `(в cand — полные объекты строк, отсортированные по org_id; в active/dead достаточно person_id, org_id, company).\n` +
  `Верни: total, active, dead, candidates, cand_orgs и groups — массив [{org_id, n}] в ТОМ ЖЕ порядке, ` +
  `в каком компании лежат в cand-файле (n = сколько персон у этой компании).`,
  { schema: { type:'object', properties:{ total:{type:'number'}, active:{type:'number'}, dead:{type:'number'},
      candidates:{type:'number'}, cand_orgs:{type:'number'},
      groups:{type:'array', items:{type:'object', properties:{ org_id:{type:'number'}, n:{type:'number'} }, required:['org_id','n']}} },
    required:['total','active','dead','candidates','cand_orgs','groups'] },
    label:'partition', phase:'Enrich', effort:'low', model:'claude-sonnet-5' }
)
log(`Корзины: действующие ${part.active} (пропускаем целиком) · заведомо нецелевые ${part.dead} · кандидаты ${part.candidates} (${part.cand_orgs} орг)`)

// ── Фаза 3. Сделки — только по кандидатам ───────────────────────────────────
phase('Deals')
// 🚨 ПЕРЕПИСАНО 19.07 после разбора прогона 260618.
// Было: цикл по каждой организации, 3 вызова на орг → 1498 getDeals + 143 getStage
// на ~900 орг = 71% всех токенов прогона (≈71M) и упёрлись в лимит сессии.
// Стало: сделки выкачиваются ПАЧКАМИ по пайплайнам (getDeals принимает pipeline_id
// и постраничный курсор БЕЗ org_id), стадии — одним getStages, связка с организациями
// делается локально в памяти. ~15 вызовов вместо 1498.
const dealsOne = await agent(
  `READ-ONLY, в Pipedrive ничего не писать. Собери статус сделок ПАЧКАМИ, НЕ по одной организации.\n` +
  `Прочитай ${FIELDS} (deal.compact_probe, pipelines.working, pipelines.noise) и разделы «Открытая сделка орга» + «Won-сделка» в ${PDFLD}.\n` +
  `Прочитай ${OUT}/cand_${EVENT}.json → множество нужных org_id (только кандидаты; действующих там нет).\n` +
  `Загрузи ToolSearch "select:${PD}__getDeals,${PD}__getStages,${PD}__getNotes".\n` +
  `\n⚠️ ЗАПРЕЩЕНО вызывать getDeals(org_id=…) в цикле по организациям. Именно это сожгло прошлый прогон.\n` +
  `\n1. **Стадии — ОДИН вызов:** getStages(limit:500) → словарь stage_id → {name, pipeline_id, order_nr}. Парси python-ом.\n` +
  `2. **Открытые сделки — пачками по пайплайнам** (pipelines.working, обычно 7/49/53):\n` +
  `   getDeals(pipeline_id:<p>, status:"open", limit:500, custom_fields:"<deal.compact_probe>", include_fields:"activities_count")\n` +
  `   дальше тот же вызов с cursor из additional_data.next_cursor, пока курсор не кончится. ⚠️ custom_fields обязателен, иначе блоб 60+ полей.\n` +
  `   Шумовые пайплайны (pipelines.noise, 46 «Регистрация на видео-курс» = автосайнап) НЕ запрашиваем вовсе.\n` +
  `3. **Won — одной пачкой с ограничением по дате:** getDeals(status:"won", updated_since:"${iso(EV_MS - 210*DAY)}", limit:500, custom_fields:"<probe>") + курсор.\n` +
  `   ⚠️ Won больше НЕ решает «пишем/не пишем» (правило 20.07 — решает последнее касание персоны). ` +
  `Тянем его только как КОНТЕКСТ для колонки «Сделка на компании», поэтому и окна 7 мес хватает с запасом.\n` +
  `4. **Связка локально:** оставь только сделки, чей org_id есть в списке кандидатов. Всё остальное отбрось.\n` +
  `5. Имена владельцев — ТОЛЬКО для отобранных сделок: собери уникальные owner_id (их десятки, не сотни) и по каждому ` +
  `getNotes(user_id:<id>, limit:1) → data[0].user.name. Кэшируй id→имя. Больше 40 владельцев — возьми топ-40 по числу сделок, ` +
  `остальным «owner #<id>». Имена НЕ выдумывать.\n` +
  `6. Текущая дата — Bash: date +%F (не выдумывай).\n` +
  `\nСобери по каждой нужной org_id:\n` +
  `- Есть открытая сделка в рабочем пайплайне → in_funnel=true, cell="Открыта · <стадия> · owner <Имя> · <N> касаний" ` +
  `(несколько открытых → самая продвинутая по order_nr; добавь «+N ещё открытых»).\n` +
  `- ⚠️⚠️ **АВТОСАЙНАП В PIPE 7 — НЕ СДЕЛКА.** В pipe 7 падают ВСЕ новые лиды с формы «Демо». ` +
  `Если единственная открытая сделка орга сидит в pipe 7 на стартовой стадии («Входящие», ` +
  `«Отквалифицирован») — это сырой инбаунд, а НЕ работа консультанта: ставь in_funnel=false, ` +
  `cell="Нет (автосайнап pipe 7)". Продвинулась дальше («Начали общение», «Целевой контакт найден» и позже) ` +
  `→ это настоящая воронка, in_funnel=true. Не отсечёшь — любой, кто когда-то оставил заявку с формы, ` +
  `уедет в батч 2 и получит «должность целевая по умолчанию» без проверки: разработчику уйдёт заход как ЛПР.\n` +
  `- ⚠️ **person_ids — ОБЯЗАТЕЛЬНО: id ВСЕХ людей, привязанных к открытым сделкам этой орг** ` +
  `(пустой массив, если сделки ни к кому не привязаны). Это НЕ для красоты: от привязки зависит, ` +
  `проверять ли должность. Привязан к сделке → должность целевая по умолчанию (его уже признали ` +
  `релевантным, когда заводили сделку — там вполне может сидеть «Директор по IT»). Не привязан → ` +
  `должность проверяем как обычно, потому что зарегистрироваться на ивент мог кто угодно из компании. ` +
  `Не вернёшь person_ids — разработчику уйдёт заход как целевому лиду.\n` +
  `- В cell, если сделка на другом человеке компании, добавь «(другая персона: <Имя>)».\n` +
  `- ⚠️ **Шаги и задачи по сделке НЕ собираем.** Они больше не влияют на вердикт: занятость считается ` +
  `по КОНТАКТУ (свежее касание или запланированная задача на нём), а не по сделке — это делает фаза Touch. ` +
  `Открытая сделка на компании нужна лишь чтобы понять, что компания целевая, и пропустить её квалификацию.\n` +
  `- Открытых нет → in_funnel=false, cell="Нет".\n` +
  `- Won: самая свежая won_time → won_month="YYYY-MM", cell="Выиграна {YYYY-MM}". ` +
  `⚠️ Won сам по себе вердикт НЕ определяет (решает занятость контакта, фаза Touch). ` +
  `Won ≠ действующий клиент: value 0 в pipe 7/49 = успех MDR-воронки, а не продажа.\n` +
  `- ⚠️⚠️ **ИСКЛЮЧЕНИЕ — WON С РЕАЛЬНОЙ ПРОДАЖЕЙ:** если у won-сделки **value > 0** ИЛИ она в ` +
  `**pipeline 53** ИЛИ на стадии «Оплата» — это ПЛАТЯЩИЙ КЛИЕНТ, даже когда в карточке орга не ` +
  `проставлен «Действующий». Верни paying=true и won_value. Такому не пишем НИКОГДА. ` +
  `Не вернёшь — заход уйдёт действующему клиенту в обход владельца сделки (класс ошибки T2).\n` +
  `\nЗапиши результат в ${OUT}/deals_${EVENT}.json (массив) И верни deals:[…], today, calls (сколько вызовов API сделал).\n` +
  `Если вызовов вышло больше 60 — значит ты сорвался в поштучный перебор: остановись и верни это как ошибку.`,
  { schema: S_DEALS, label:'deals-bulk', phase:'Deals', model:'claude-sonnet-5' }
)
const deals = (dealsOne && dealsOne.deals) || []
const TODAY = (dealsOne && dealsOne.today) || 'неизвестна'
log(`Сделки собраны пачками: ${deals.length} орг · вызовов API ${dealsOne?.calls ?? '?'} (было 1498 в поштучной схеме)`)
// Открытая сделка = БАТЧ 2: компания целевая по определению, квалифицировать её
// заново не нужно. Вердикт «пишем/нет» она НЕ выносит — его выносит тест занятости
// по контакту (фаза Touch). Раньше здесь лид отсекался целиком, и это была ошибка.
const inFunnel = deals.filter(d => d.in_funnel)
log(`Сделки: ${deals.length} орг-кандидатов · с открытой сделкой (батч 2, целевые по умолчанию) ${inFunnel.length} · дата отсчёта ${TODAY}`)

const dealMap = {}
for (const d of deals) dealMap[d.org_id] = { cell:d.cell, in_funnel:!!d.in_funnel,
  person_ids:d.person_ids || (d.person_id ? [d.person_id] : []), won_month:d.won_month || '',
  paying:!!d.paying }
const payingOrgs = deals.filter(d => d.paying)
if (payingOrgs.length) log(`Won с реальной продажей (платящие клиенты, НЕ пишем): ${payingOrgs.length} орг`)
const noPersons = deals.filter(d => d.in_funnel && !(d.person_ids || []).length && !d.person_id)
if (noPersons.length) log(`⚠️ ${noPersons.length} орг с открытой сделкой без person_ids — их регистранты пойдут через обычную проверку должности (возможно, зря)`)

// ── Фаза 3б. Классификация ПРАВИЛАМИ — до всякой модели ─────────────────────
// Правило Анны 19.07: «большинство лидов уже есть в Pipedrive со всеми полями —
// эту информацию просто надо вытаскивать». Замер на 260618: правилами закрывается
// 66% строк за 0 токенов. К модели уходит только то, для чего реально нужен сайт.
phase('Classify')
const cls = await agent(
  `Прогони детерминированную классификацию. Никаких вызовов Pipedrive и никаких суждений — только скрипт.\n` +
  `1. Сохрани сделки в ${OUT}/deals_${EVENT}.json, если файла ещё нет: ${JSON.stringify(deals).slice(0, 200)}… (полный список у тебя в ${OUT}/deals_${EVENT}.json)\n` +
  `2. python3 ${SKILL}/scripts/classify_rows.py --enriched ${ENR} --deals ${OUT}/deals_${EVENT}.json ` +
  `--refs ${SKILL}/references --out-decided ${OUT}/decided_${EVENT}.json --out-residue ${OUT}/residue_${EVENT}.json\n` +
  `   ⚠️ Остаток (residue) должен быть ПУСТОЙ: с 21.07 правила закрывают все строки, ` +
  `к модели ничего не уходит. Ненулевой residue = баг, верни это как проблему.\n` +
  `3. Верни его вывод: decided, residue, и by_work — сколько среди decided «да» / «уточнить» / «нет» ` +
  `(посчитай python-ом по ${OUT}/decided_${EVENT}.json).\n` +
  `Ничего не правь руками: если скрипт ругается — верни текст ошибки.`,
  { schema: { type:'object', properties:{ decided:{type:'number'}, residue:{type:'number'},
      by_work:{type:'object', properties:{ да:{type:'number'}, уточнить:{type:'number'}, нет:{type:'number'} }} },
    required:['decided','residue'] }, label:'classify', phase:'Classify', effort:'low', model:'claude-sonnet-5' }
)
log(`Правилами решено ${cls.decided} строк (0 токенов) · всё из полей Pipedrive, на сайты не ходим`)
if (cls.residue) log(`⚠️ Остаток ${cls.residue} строк — правила должны закрывать ВСЁ, это баг classify_rows.py`)

// ⚠️ Фазы Qualify (модель ходит на сайты) и Traffic (SimilarWeb) УДАЛЕНЫ 21.07.2026.
// Причина: вся классификация уже проставлена в Pipedrive (команда Mindbox дозаполняет
// карточки отдельным ежесуточным промптом). Наша задача — ВЫТАЩИТЬ готовое, а не
// вычислять заново. Это и была та часть прогона, которая жгла часы и лимиты токенов:
// Traffic-агент на 30 доменах работал 20+ минут, SimilarWeb на free-тарифе блокировал
// после 3 запросов. Поле пустое в Pipedrive → в таблице пусто, на сайт НЕ идём.
// Классификация теперь закрывает ВСЕ строки правилами, остатка для модели нет.
// Дальше по конвейеру строки живут в файле decided_<event>.json.
// (переменная traffic удалена вместе с фазой Traffic — SimilarWeb не собираем)
// ── Фаза 5б. Занят ли КОНТАКТ — порог 3 мес ─────────────────────────────────
// Правило маркетинг-директора 20.07: блокирует не факт активности, а ДВУСТОРОННЯЯ
// ДОГОВОРЁННОСТЬ с клиентом о контакте. Занят = двустороннее общение <3 мес ИЛИ
// договорённость о контакте в ближайшие 3 мес. Общение в одни ворота и односторонняя
// задача «попробовать зайти» НЕ занимают. Исход сделки сам по себе тоже не решает.
// ⚠️ Двусторонность и наличие договорённости — СУЖДЕНИЕ по содержанию активностей
// (агент читает note/саммари), пороги — арифметика (scripts/apply_touches.py).
//
// ⚠️ ПОЧЕМУ ТОЛЬКО ПО ШОРТ-ЛИСТУ: один getActivities на каждого из ~1000 регистрантов —
// это ровно тот поштучный перебор, который сжёг прогон 17.07 (1498 вызовов, 71% токенов).
// Тем, кто и так «нет», касание не нужно: вердикт от него не изменится.
// ⚠️ Работаем ЧЕРЕЗ ФАЙЛ, а не через `rows` в памяти: `rows` держит только строки,
// прошедшие через модель, а две трети лидов закрываются правилами и лежат в decided_.
// Правка в памяти вдобавок затёрлась бы слиянием в фазе Validate.
phase('Touch')
// ⚠️ ОКНО ЗАПРОСА — главная экономия фазы (замер 21.07 на живой персоне):
//   getActivities(person_id, limit:50) без фильтра дат = 70 917 символов (~18K токенов),
//   он же с окном 3.5 мес и limit:15   = ~4 000 символов (~1K токенов) — в 18 раз меньше.
// Причина: в note лежат ПОЛНЫЕ переписки Telegram/Intercom и саммари звонков за годы,
// а нам нужен ответ на один вопрос — было ли касание за последние 3 месяца.
// Всё старше окна и так означает «свободен», тянуть его незачем.
const TOUCH_SINCE = /^\d{4}-\d{2}-\d{2}$/.test(TODAY)
  ? iso(Date.parse(TODAY + 'T00:00:00Z') - 105 * DAY).slice(0, 10)
  : ''
const QUALIFIED = `${OUT}/qualified_${EVENT}.json`
const merged2 = await agent(
  `Слей строки квалификации в один файл и отдай шорт-лист на проверку касания.\n` +
  `1. python3 -c "import json,shutil;shutil.copy('${OUT}/decided_${EVENT}.json','${QUALIFIED}');` +
  `print(len(json.load(open('${QUALIFIED}'))))"\n` +
  `   Все строки лежат в decided_ (правила закрывают всё) — просто копируем в рабочий файл.\n` +
  `2. Верни total и shortlist — person_id тех строк, у кого work ∈ {"да","уточнить"} (им мы собираемся писать).`,
  { schema: { type:'object', properties:{ total:{type:'number'},
      shortlist:{type:'array', items:{type:'number'}} }, required:['total','shortlist'] },
    label:'merge-rows', phase:'Touch', effort:'low', model:'claude-sonnet-5' }
)
const shortlist = merged2.shortlist || []
log(`Тест занятости контакта: ${shortlist.length} из ${merged2.total} строк (остальным и так не пишем)`)
if (shortlist.length) {
  const K = 25   // персон на агента: вызовы лёгкие, но контекст ответа копится
  const kChunks = []
  for (let s = 0; s < shortlist.length; s += K) kChunks.push({ start:s, end:Math.min(s+K, shortlist.length), idx: kChunks.length })
  const S_TOUCH = { type:'object', properties:{ touches:{type:'array', items:{ type:'object', properties:{
    person_id:{type:'number'}, last_touch:{type:'string'}, kind:{type:'string'},
    one_way_touch:{type:'string'},
    planned_task:{type:'string'}, task_agreed:{type:'boolean'}, task_owner:{type:'string'},
    evidence:{type:'string'} },
    required:['person_id','last_touch','one_way_touch','planned_task','task_agreed','evidence'] } } },
    required:['touches'] }

  const kres = await parallel(kChunks.map(c => () => agent(
    `READ-ONLY. Определи, ЗАНЯТ ли контакт. Раздел «занят ли КОНТАКТ» — в ${PDFLD}.\n` +
    `Персоны (person_id): ${JSON.stringify(shortlist.slice(c.start, c.end))}\n` +
    `Загрузи ToolSearch "select:${PD}__getActivities". По каждой персоне **ДВА УЗКИХ вызова** ` +
    `(широкий вызов без фильтра дат тянет полные переписки за годы — 70K символов на человека):\n` +
    `  A) **общение в окне:** getActivities(person_id:<id>, done:true` +
    (TOUCH_SINCE ? `, updated_since:"${TOUCH_SINCE}T00:00:00Z"` : ``) +
    `, limit:20, sort_by:"add_time", sort_direction:"desc")\n` +
    `     Окно ${TOUCH_SINCE || '(не задано)'} → сегодня. Всё, что старше, и так даёт «свободен» — не тянем.\n` +
    `  Б) **открытые задачи:** getActivities(person_id:<id>, done:false, limit:10)\n` +
    `     ⚠️ Отдельным вызовом ОБЯЗАТЕЛЬНО: старая задача с будущей датой в окно обновлений НЕ попадёт.\n` +
    `\n⚠️⚠️ ГЛАВНОЕ ПРАВИЛО: блокирует не факт активности, а **ДВУСТОРОННЯЯ ДОГОВОРЁННОСТЬ ` +
    `с клиентом о контакте**. Односторонняя работа консультанта человека НЕ занимает. ` +
    `Поэтому ЧИТАЙ СОДЕРЖИМОЕ активностей (поле note, саммари звонков tl;dv, текст задачи), ` +
    `а не только даты и типы. Это не арифметика — это суждение, ради него фаза и существует.\n` +
    `\n⚠️⚠️ **РЕГИСТРАЦИЯ НА ИВЕНТ — НЕ ОБЩЕНИЕ.** Выброси активности ` +
    `type == "registratsiya_na_sobitie" (автоматика лендинга, она есть У ВСЕХ и датирована ивентом). ` +
    `Возьмёшь её за общение — весь список окажется «свежим» и мы не напишем НИКОМУ.\n` +
    `\nПо каждой персоне верни:\n` +
    `1. **last_touch** — «YYYY-MM» последнего **ДВУСТОРОННЕГО** общения В ОКНЕ (вызов А): клиент ОТВЕТИЛ ` +
    `(состоявшийся звонок/встреча, ответное письмо, зафиксированная реакция). kind = что это было. ` +
    `В окне ответов клиента нет → last_touch="" (пустая строка). Это НОРМА и значит «пишем»: ` +
    `всё, что старше окна, по правилу и так «свободен».\n` +
    `2. **one_way_touch** — «YYYY-MM» последнего касания **В ОДНИ ВОРОТА** (консультант писал/звонил, ` +
    `ответа нет). Нужен только для лога — он НЕ блокирует. Нет таких → "".\n` +
    `3. **planned_task** — «YYYY-MM-DD» ближайшей задачи из вызова Б, task_owner = владелец. ` +
    `Нет → "". ⚠️ Дату возвращай КАК ЕСТЬ, включая просроченные и далёкие — в окно 3 месяцев её укладывает скрипт.\n` +
    `4. **task_agreed** — САМОЕ ВАЖНОЕ СУЖДЕНИЕ. true, только если задача отражает ` +
    `**реальную договорённость с клиентом** о следующем контакте: клиент участвовал в разговоре ` +
    `и согласился («бюджета нет, вернуться в конце квартала в цикл бюджетирования», ` +
    `«созвониться после отпуска», «прислать КП к дате»). ` +
    `false, если задача **односторонняя** — консультант сам себе поставил «попробовать зайти», ` +
    `«прозвонить», «написать ещё раз», и клиент об этом не знает и ничего не обещал. ` +
    `Сомневаешься и в содержании нет следа ответа клиента → **false** (лид уйдёт в работу — ` +
    `это дешевле, чем держать контакт заблокированным из-за чужого напоминания себе).\n` +
    `5. **evidence** — 1 строка: на чём основано суждение (цитата/факт из note). Пусто не оставляй: ` +
    `по ней MDR проверит решение. Не выдумывай — нет содержания, так и напиши «содержания нет, только тип и дата».\n` +
    `\n6. **Запиши результат сам, в файл** ${OUT}/touches_${EVENT}_${c.idx}.json — тот же массив touches[], ` +
    `который возвращаешь в ответе. ⚠️ Пиши файл САМ (python3 json.dump), НЕ полагайся на то, что массив ` +
    `из твоего ответа кто-то перенесёт в файл позже — большой JSON, вклеенный в промпт следующего шага, ` +
    `сжигает сотни тысяч токенов впустую (замерено 23.07: 83 КБ на 265 персон = +12 минут одному агенту).\n` +
    `\nВерни touches[] по ВСЕМ переданным person_id, включая тех, у кого все поля пустые.`,
    { schema: S_TOUCH, label:`touch:${c.idx}`, phase:'Touch' }
  )))

  // Сам порог считает СКРИПТ, а не агент: «меньше трёх месяцев» на глаз = ошибки,
  // а решение здесь бинарное — пишем человеку или нет.
  // ⚠️ 23.07: touches БОЛЬШЕ НЕ передаём через промпт (JSON.stringify гнал 80+ КБ текстом
  // в один вызов — 1.3M входных токенов, 12 минут на пустом месте). Каждый touch:<idx>
  // уже написал свой файл сам — здесь только мёржим их python-ом.
  const touchesOk = kres.filter(Boolean).length
  const applied = await agent(
    `Примени порог последнего касания. Только запись файла и запуск скрипта, без суждений.\n` +
    `1. python3: слей все ${OUT}/touches_${EVENT}_*.json (их должно быть ${kChunks.length}, ` +
    `по одному на чанк персон) в один массив, запиши в ${OUT}/touches_${EVENT}.json. ` +
    `Файла не хватает или он пуст → отметь как проблему, но слей то, что есть.\n` +
    `2. python3 ${SKILL}/scripts/apply_touches.py --rows ${QUALIFIED} --touches ${OUT}/touches_${EVENT}.json --today ${TODAY}\n` +
    `3. Верни его вывод: checked, fresh (занято двусторонним общением), tasked (занято договорённостью), ` +
    `open_now (свободны), missing (не проверенные), freed_one_way, freed_unilateral_task.\n` +
    `Ничего не правь руками — скрипт ругается, верни текст ошибки.`,
    { schema: { type:'object', properties:{ checked:{type:'number'}, fresh:{type:'number'},
        tasked:{type:'number'}, open_now:{type:'number'}, missing:{type:'number'},
        freed_one_way:{type:'number'}, freed_unilateral_task:{type:'number'} }, required:['checked'] },
      label:'apply-touches', phase:'Touch', effort:'low', model:'claude-sonnet-5' }
  )
  // ⚠️ agent() возвращает null, если агент умер (лимит сессии, терминальная ошибка API).
  // Без этой проверки прогон падал TypeError'ом на applied.fresh — и терял ВСЮ работу
  // предыдущих фаз, хотя touches_<event>.json уже собран и порог можно доприменить руками.
  if (!applied) {
    log(`🛑 Фаза Touch: агент apply-touches не вернул результат (лимит сессии / ошибка API).`)
    log(`   Разметка занятости СОБРАНА по чанкам (${touchesOk}/${kChunks.length} чанков ответили) — ` +
        `лежит в ${OUT}/touches_${EVENT}_*.json, ещё не слита в один файл.`)
    log(`   Доприменить порог без агентов:`)
    log(`   python3 -c "import json,glob; d=[x for f in glob.glob('${OUT}/touches_${EVENT}_*.json') for x in json.load(open(f))]; json.dump(d, open('${OUT}/touches_${EVENT}.json','w'), ensure_ascii=False)"`)
    log(`   python3 ${SKILL}/scripts/apply_touches.py --rows ${QUALIFIED} --touches ${OUT}/touches_${EVENT}.json --today ${TODAY}`)
    return { event: EVENT, stopped: 'фаза Touch: агент не вернул результат',
      rows_path: QUALIFIED, touches_chunks: `${OUT}/touches_${EVENT}_*.json`, touches_chunks_ok: touchesOk, deals,
      resume_hint: `python3 ${SKILL}/scripts/apply_touches.py --rows ${QUALIFIED} --touches ${OUT}/touches_${EVENT}.json --today ${TODAY}` }
  }
  log(`Занято: двустороннее общение <3 мес ${applied.fresh ?? '?'} · договорённость о контакте ${applied.tasked ?? '?'} · свободны и остаются в работе ${applied.open_now ?? '?'}`)
  if (applied.freed_one_way || applied.freed_unilateral_task)
    log(`Отобрали, несмотря на активность: общение в одни ворота ${applied.freed_one_way ?? 0} · односторонняя задача без договорённости ${applied.freed_unilateral_task ?? 0}`)
  if (applied.missing) log(`⚠️ Не проверено ${applied.missing} персон (агент не вернул касание) — валидатор пометит их ERROR-ом, перезапусти фазу Touch`)
}

// ── Фаза 6. Валидация — детерминированная, скриптом ─────────────────────────
phase('Validate')
// Строки уже лежат в ${OUT}/qual_${EVENT}_*.json — НЕ пересылаем их в промпте (на 336 строках это ~100K токенов впустую).
const valid = await agent(
  `Запусти детерминированную валидацию, НЕ проверяй глазами и ничего не чини сам.\n` +
  `1. Файл ${QUALIFIED} уже собран фазой Touch (decided_ + qual_ слиты, применён порог касания). ` +
  `⚠️ **НЕ пересобирай его заново** — пересборка из decided_/qual_ затрёт результаты проверки касания ` +
  `и вернёт в работу тех, кому мы решили не писать. Просто проверь, что файл на месте и непустой ` +
  `(python3: len(json.load(...))), и верни это число.\n` +
  `2. python3 ${SKILL}/scripts/validate_rows.py --rows ${QUALIFIED} --fields ${FIELDS} --refs ${SKILL}/references --today ${TODAY} --json\n` +
  `3. Верни clean и problems[] РОВНО как их вывел скрипт (он печатает готовый JSON), включая поле level у каждой проблемы.\n` +
  `4. Плюс by_work — сколько строк в ${QUALIFIED} с work «да» / «уточнить» / «нет» (посчитай python-ом). ` +
  `Это финальная сводка прогона, она должна считаться по ИТОГОВОМУ файлу, а не по промежуточным.`,
  { schema: S_VALID, label:'validate', phase:'Validate', effort:'low', model:'claude-sonnet-5' }
)
log(`Валидация: clean=${valid.clean} · проблем ${(valid.problems || []).length}`)

// ⚠️ ГЕЙТ: ERROR валидации останавливает прогон ДО записи вкладки. Дока обещала это
// («возвращает 1 при ERROR → прогон должен падать»), но фактически вердикт только
// логировался, и таблица с браком доезжала до MDR как готовая к работе.
// args.force_table=true — осознанный обход, когда нужно посмотреть брак глазами.
// ⚠️ Гейт срабатывает по ДВУМ независимым признакам: явный clean=false (поле required
// в схеме, агент обязан его вернуть) ИЛИ найденные ERROR. Полагаться только на p.level
// нельзя: если он не доедет, гейт насчитает 0 ошибок и пропустит брак в таблицу.
const vErrors = (valid.problems || []).filter(p => String(p.level || '').toUpperCase() === 'ERROR')
const vBlocked = valid.clean === false || vErrors.length > 0
if (vBlocked && !A.force_table) {
  log(`🛑 Валидация не пройдена (clean=${valid.clean}, ERROR ${vErrors.length}) — вкладка НЕ пишется, чтобы брак не ушёл в работу:`)
  for (const p of vErrors.slice(0, 15)) log(`   [${p.person_id ?? '?'}] ${p.issue}`)
  if (vErrors.length > 15) log(`   …ещё ${vErrors.length - 15}`)
  return { event: EVENT, stopped: 'валидация вернула ERROR', errors: vErrors,
    rows_path: QUALIFIED, deals, validation: valid,
    hint: 'Почини причину и перезапусти. Нужно посмотреть таблицу как есть → args.force_table=true.' }
}

// ── Фаза 7. Таблица — вкладка ивента по канону ──────────────────────────────
// Канон: 1 ивент = 1 вкладка, имя = код без «_on_» (260618_on_midyearreview → 260618_midyearreview).
// Перевод в 23 колонки делает to_table_rows.py — раньше это делалось руками в сессии.
phase('Table')
const TAB = EVENT.replace('_on_', '_')
const table = await agent(
  `Собери вкладку ивента ${EVENT} в Google Sheet. Выполняй ровно по шагам, ничего не додумывая.\n` +
  `1. python3 ${SKILL}/scripts/to_table_rows.py --event ${EVENT} --dir ${OUT} --out ${OUT}/tablerows_${EVENT}.json\n` +
  `   Он берёт ВСЕХ регистрантов из enriched (включая действующих и отсечённых) и накладывает вердикты. ` +
  `Напечатает статистику — верни её как converted. Если скажет «БЕЗ вердикта … проверить прогон» — ` +
  `не замалчивай, вынеси в warnings.\n` +
  `2. python3 ${SKILL}/scripts/build_table.py --rows ${OUT}/tablerows_${EVENT}.json --tab ${TAB} --write\n` +
  `   Сначала БЕЗ --write (dry-run), посмотри вывод, потом с --write. Вкладка уже есть → скрипт ` +
  `упадёт без --force: НЕ передавай --force сам, верни это как проблему (канон: чужую вкладку не трогаем).\n` +
  `3. python3 ${SKILL}/scripts/format_table.py --tab ${TAB} --write\n` +
  `Верни: tab, written (сколько строк записано), converted (статистика конвертера), ` +
  `sheet_url, warnings[] (пусто, если всё чисто). Ошибку скрипта возвращай текстом, не пересказом.`,
  { schema: { type:'object', properties:{ tab:{type:'string'}, written:{type:'number'},
      converted:{type:'string'}, sheet_url:{type:'string'},
      warnings:{type:'array', items:{type:'string'}} }, required:['tab'] },
    label:'table', phase:'Table', model:'claude-sonnet-5' }
)
log(`Вкладка ${table.tab}: записано строк ${table.written ?? '?'}${table.sheet_url ? ` · ${table.sheet_url}` : ''}`)
for (const w of (table.warnings || [])) log(`   ⚠️ ${w}`)

// Сводку берём из ИТОГОВОГО файла (фаза Validate), а не из `rows`: в памяти лежат
// только строки, прошедшие через модель — без двух третей, закрытых правилами,
// и без применённого порога касания.
const buckets = valid.by_work || {}
log(`Работаем: да ${buckets.да ?? '?'} · уточнить ${buckets.уточнить ?? '?'} · нет ${buckets.нет ?? '?'}`)

return {
  event: EVENT,
  tab: table.tab, sheet_url: table.sheet_url || '',
  totals: { all: part.total, active: part.active, dead: part.dead, candidates: part.candidates },
  // ⚠️ `rows` в возврате НЕТ: строки больше не живут в памяти (их производила удалённая
  // фаза Qualify) — итоговые лежат в файле rows_path. Обращение к необъявленной `rows`
  // роняло прогон ReferenceError'ом в самом конце, после всех потраченных вызовов.
  buckets, rows_path: QUALIFIED, deals, validation: valid, table,
  next: 'Часть 2 (брифы и заходы) — ТОЛЬКО после подтверждения списка SDR. См. гейт в SKILL.md.',
}
