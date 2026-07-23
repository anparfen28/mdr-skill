export const meta = {
  name: 'mdr-part2',
  description: 'Часть 2 MDR: по подтверждённым SDR лидам — бриф → кейс → заход (TG+email) → запись в ту же вкладку Sheet. Конвейер по лиду целиком, без барьера (каждый долетает до Sheet независимо от остальных).',
  whenToUse: 'ТОЛЬКО после явного подтверждения SDR/MDR списка «Работаем». Workflow({scriptPath: "~/.claude/skills/mdr/workflow_part2.js", args: {tab, leads}})',
  phases: [{ title: 'Outreach', detail: 'по каждому лиду: бриф из Pipedrive → кейс → заход A/B → запись в Sheet' }],
}

// ⚠️ Тестовый/baseline-прогон (23.07): замеряем время и токены на маленькой пачке,
// прежде чем гнать все подтверждённые строки — Часть 1 уже поймала два дорогих
// антипаттерна (раздутый промпт, ложный гейт), для Части 2 хотим то же самое:
// сначала данные, потом решение по модели/архитектуре, а не наоборот.

const SKILL = (typeof args === 'object' && args && args.skill_dir) || '/Users/anna/.claude/skills/mdr'
const A = typeof args === 'string' ? JSON.parse(args) : (args || {})
const TAB = A.tab || (() => { throw new Error('args.tab обязателен (имя вкладки события)') })()
const LEADS = A.leads || (() => { throw new Error(
  'args.leads обязателен: [{row, person_id, org_id, fio}] — подтверждённые SDR строки') })()
const OUT = A.out_dir || (() => { throw new Error('args.out_dir обязателен') })()
const PD = 'mcp__555487e0-0932-4938-8d02-6124bc9b2521'

const BRIEF = `${SKILL}/references/outreach_brief.md`
const MESSAGE = `${SKILL}/references/outreach_message.md`
const PRODUCT = `${SKILL}/references/product.md`
const CASES = `${SKILL}/references/case_library.md`
const EXAMPLES = `${SKILL}/references/zahody_examples.md`
const RESTRICT = `${SKILL}/references/restrictions.md`

const S_LEAD = { type:'object', properties:{
  row:{type:'number'}, fio:{type:'string'}, written:{type:'boolean'},
  brief_len:{type:'number'}, tg_len:{type:'number'}, email_len:{type:'number'} },
  required:['row','fio','written'] }
const S_BATCH = { type:'object', properties:{ leads:{type:'array', items: S_LEAD } }, required:['leads'] }

// ⚠️ 23.07: 1 агент = 1 лид держит расход на ~120K токенов/лид ПОСТОЯННЫМ (замерено на
// 12 живых лидах, 3 разных прогона) — независимо от фиксов внутри (запрет Read на дампы
// не изменил цифру, см. память). Причина — 6 референс-файлов скилла (~57 КБ) читаются
// ЗАНОВО каждым агентом. Батчинг (несколько лидов в одном агенте, референсы читаются раз
// на пачку) — единственный оставшийся структурный рычаг, проверяем здесь.
// ⚠️ 23.07: сравнили batch=1 (~120K ток/лид) · batch=4 (51K) · batch=5 (45K) на живых
// лидах 260323_on_retentionweek — отдача уже убывающая (4→5 дал всего −12%, а wall-clock
// на лида внутри пачки начал расти). batch=5 зафиксирован как рабочий дефолт: почти вся
// доступная экономия уже взята, риск потерь на упавшем агенте и просадки внимания на
// длинных сериях выше не оправдан. Переопределяй args.batch_size только осознанно.
const BATCH_SIZE = A.batch_size || 5
const batches = []
for (let i = 0; i < LEADS.length; i += BATCH_SIZE) batches.push(LEADS.slice(i, i + BATCH_SIZE))

phase('Outreach')
log(`Часть 2: ${LEADS.length} лидов · пачками по ${BATCH_SIZE} (${batches.length} агентов) · вкладка ${TAB}`)

const batchResults = await pipeline(
  batches,
  batch => agent(
    `Часть 2 MDR — ${batch.length} лид${batch.length===1?'':'а(ов)'} ПОДРЯД В ОДНОМ ВЫЗОВЕ (референсы читаешь ОДИН раз ` +
    `на всю пачку, не на каждого — экономия и есть смысл батчинга). Каждый лид всё равно идёт ` +
    `от брифа до записи в Sheet СРАЗУ по готовности, не жди, пока разберёшь всех.\n` +
    `Прочитай ОДИН РАЗ ПЕРЕД началом: ${BRIEF} (что тянуть и как собрать бриф), ${MESSAGE} (режимы A/Б, стиль, ` +
    `анти-паттерны), ${PRODUCT} (продукт Mindbox), ${CASES} (кейсы по модели клиента), ${EXAMPLES} (эталоны ✅/❌), ` +
    `${RESTRICT} (стоп-факторы).\n` +
    `Загрузи ToolSearch "select:${PD}__getPerson,${PD}__getOrganization,${PD}__getActivities,${PD}__getNotes" ОДИН раз.\n` +
    `\n⚠️⚠️ getActivities/getNotes отдают 1.5-3 МБ сырого текста ЗА ОДИН вызов — это уходит В ФАЙЛ, а не в твой ответ. ` +
    `**НИКОГДА не читай такой файл целиком инструментом Read** (даже кусками/с offset) — разбирай ТОЛЬКО python3 ` +
    `(grep/regex по ключевым словам, датам, "Конфликт привязки", note/tl;dv) и вытаскивай в бриф только релевантные ` +
    `цитаты. Полнота истории требует прочитать ВСЁ python-ом, а не сократить объём — просто не тащи сырой файл в контекст.\n` +
    `\nЛиды пачки:\n` + batch.map(l =>
      `- row=${l.row}, person_id=${l.person_id}, org_id=${l.org_id}, ФИО="${l.fio}"`).join('\n') + `\n` +
    `\nПо КАЖДОМУ лиду, одному за другим:\n` +
    `1. Собери бриф (боль явная + косвенная, факты не выдумывай) — правила из ${BRIEF}.\n` +
    `2. Кейс из ${CASES} по МОДЕЛИ клиента (снапшот прокачан — в браузер не ходить).\n` +
    `3. Заход — Telegram (короткий) и Email (тема+текст), режим/стиль по ${MESSAGE}.\n` +
    `4. Запиши в файл ${OUT}/res_<row>.json строго по контракту write_part2.py: ` +
    `{"row": <row>, "fio": "<ФИО>", "brief": "...", "cases": "...", "tg": "...", "email": "..."}\n` +
    `5. **Сразу же**, не дожидаясь остальных лидов пачки: python3 ${SKILL}/scripts/write_part2.py --dir ${OUT} --tab ${TAB} --write\n` +
    `   (ФИО сверяется скриптом сам — можно гонять на весь каталог, уже записанные строки не тронет).\n` +
    `\nВерни leads[] — по каждому: row, fio, written (записалось ли, судя по выводу скрипта), brief_len/tg_len/email_len.`,
    { schema: S_BATCH, label:`batch:${batch.map(l=>l.row).join(',')}`, phase:'Outreach' }
  )
)

const results = batchResults.filter(Boolean).flatMap(b => b.leads || [])
log(`Готово: ${results.filter(r => r.written).length}/${LEADS.length} записано в Sheet`)
if (batchResults.some(b => !b)) log(`   ⚠️ хотя бы один агент-пачка не вернул результат — проверить вручную`)

return { tab: TAB, leads: LEADS.length, batch_size: BATCH_SIZE, written: results.filter(r => r.written).length, results }
