<script setup lang="ts">
// Экран диспетчера по макетам (docs/design.md): карта на весь экран, поверх — шапка, левая
// панель со списками и карточками, модальные окна. Состояние дня и все запросы — здесь,
// компоненты только показывают и собирают события.
import {
  apiError,
  eventTime,
  OUTCOME_NAMES,
  useApi,
  versionAt,
  type AssistantDraft,
  type AssistantMessage,
  type AssistantReply,
  type Dataset,
  type DemoCatalog,
  type Engineer,
  type Plan,
  type PlanEvent,
  type ReplanMode,
  type Request,
} from '~/composables/useApi'
import { SHEET_VARS, useSheet, useSheetDrag } from '~/composables/sheet'
import {
  compareSec,
  DEMO_SEC,
  eventSec,
  IMPORT_SEC,
  MANUAL_SEC,
  useProgress,
  type Load,
} from '~/composables/progress'

const api = useApi()
// Полоса долгого действия над его кнопкой (ProgressBar); `reactive` — чтобы шаблон читал поля без `.value`.
const progress = reactive(useProgress())

const dataset = ref<Dataset | null>(null)
/**
 * Версии дня по слотам «базовый / оптимизированный» (блок 33): утренний план и всё, что выросло из
 * него событиями и правками, по порядку. Показывается версия, действовавшая во время дня из шапки,
 * последняя — текущее состояние дня. `shallowRef`: планы большие и на месте не меняются, новая
 * версия — всегда новый массив.
 */
type Slot = 'baseline' | 'optimized'
const chains: Record<Slot, Ref<Plan[]>> = { baseline: shallowRef([]), optimized: shallowRef([]) }
const baseline = computed(() => chains.baseline.value.at(-1) ?? null)
const optimized = computed(() => chains.optimized.value.at(-1) ?? null)
const shown = ref<Slot>('baseline')
// Открытая карточка в левой панели: одна на панель — заявка или инженер.
const selectedRequestId = ref<string | null>(null)
const selectedEngineerId = ref<string | null>(null)
// Варианты плана (PLAN 6.14): все планы одного дня из `compare`, базовый — среди них.
// Расчёт идёт только через них (docs/design.md, «Что уходит из интерфейса»).
const variants = ref<Plan[]>([])
const busy = ref<string | null>(null)
const error = ref<string | null>(null)
// Ответ сервера на действие карточки — у её кнопок; на перенос из списка — под той строкой.
const cardError = ref<string | null>(null)
const deferError = ref<{ requestId: string; text: string } | null>(null)

/**
 * Ширина левой панели — два состояния, переключает стрелка на её правом краю: закрыта (380 px)
 * и открыта на всё окно, кроме 240 px карты справа. Шире 620 px справа от инженеров появляются
 * дорожки гантта (docs/design.md, «Гантт всех инженеров»). В окне уже 860 px гантту места нет —
 * там и стрелки нет: она лишь растянула бы список.
 */
const PANEL = 380
const ganttOpen = ref(false)
const viewport = ref(0) // ширина окна; до монтирования — 0, панель закрыта
const canGantt = computed(() => viewport.value - 240 >= 620)
const panelWidth = computed(() => (ganttOpen.value && canGantt.value ? viewport.value - 240 : PANEL))
const wide = computed(() => panelWidth.value >= 620)

/**
 * Мобильная версия (docs/design.md, кадры 1504–1507, 1512–1520): уже 768 px карта на весь экран,
 * шапки нет, левая панель становится листом с ручкой в трёх положениях, внизу вкладки
 * «Инженеры / Заявки / Метрики / Датасет». Раскладку меняют responsive-классы Tailwind — телефон получает её сразу,
 * ещё с сервера; `mobile` нужен только поведению: гантту, листу, зуму карты.
 */
const mobile = useMobile()
// Положение листа общее с окнами: на телефоне они лежат тем же листом (composables/sheet.ts).
const { state: sheet, dragged, height: sheetHeight, tall, footer: sheetFooter } = useSheet()
const listTab = ref<'engineers' | 'requests'>('engineers')

// Сколько экрана снизу закрывают лист и вкладки — для рамки карты. Меряется по самому листу:
// так рамка верна и посреди жеста, и посреди анимации.
const panel = useTemplateRef<HTMLElement>('panel')
const { startMouse, cycle } = useSheetDrag(panel)
const covered = ref(0)
let observer: ResizeObserver | null = null
function measure() {
  covered.value = panel.value ? Math.round(window.innerHeight - panel.value.getBoundingClientRect().top) : 0
}
onMounted(() => {
  // Сразу, не дожидаясь первого отклика наблюдателя: он приходит с отрисовкой кадра, а рамка карты
  // может подгоняться раньше.
  measure()
  observer = new ResizeObserver(measure)
  if (panel.value) observer.observe(panel.value)
})
onBeforeUnmount(() => observer?.disconnect())

/**
 * Вкладка «Метрики» — метрики показываемого плана, как кнопка шапки. Метрики, открытые из
 * вариантов, относятся к «Датасету», как и окна датасета и вариантов; остальное — списки.
 */
const activeTab = computed(() => {
  if (modal.value?.kind === 'metrics' && modal.value.from === 'header') return 'metrics'
  return modal.value && ['dataset', 'variants', 'metrics'].includes(modal.value.kind) ? 'dataset' : listTab.value
})

function openTab(tab: 'engineers' | 'requests' | 'metrics' | 'dataset') {
  // Вкладки лежат вне затемнения окон: во время расчёта они увели бы окно, куда придёт ответ, а
  // под «Пересчётом дня» и «Изменить прошлое?» открыли бы второе окно или закрыли карточку действия.
  if (busy.value || stacked.value) return
  if (sheet.value === 'collapsed') sheet.value = 'half'
  // Пока считаются варианты, «Датасет» не уводит из их окна: во вкладке это окно и так открыто.
  if (tab === 'dataset') return openModal({ kind: busy.value === COMPARING ? 'variants' : 'dataset' })
  // Без выбранного дня метрик нет: `openModal(null)` приведёт к окну, где день выбирают.
  if (tab === 'metrics') return plan.value ? openMetrics(plan.value, 'header') : openModal(null)
  openModal(null)
  closeCard()
  listTab.value = tab
}

/** Касание свёрнутого листа (поиск, карточка) раскрывает его наполовину; ручка ходит своим путём. */
function expandSheet() {
  if (mobile.value && sheet.value === 'collapsed') sheet.value = 'half'
}

// Один обработчик на окно: ширина открытой панели и высота листа для рамки карты.
const onResize = () => {
  viewport.value = window.innerWidth
  measure()
}
onMounted(() => {
  viewport.value = window.innerWidth
  window.addEventListener('resize', onResize)
})
onBeforeUnmount(() => window.removeEventListener('resize', onResize))
const beside = computed(() => ({ marginLeft: `${panelWidth.value + 24}px`, maxWidth: `calc(100% - ${panelWidth.value + 24}px)` }))

// Какое окно открыто поверх карты (docs/design.md, «Модальные окна»). Метрики помнят, откуда
// их открыли: из вариантов — с «Выбрать» и «Назад» к списку, из шапки — без «Выбрать».
type Window =
  | { kind: 'dataset' }
  | { kind: 'variants' }
  | { kind: 'metrics'; plan: Plan; from: 'header' | 'variants' }
  | { kind: 'reassign'; requestId: string }
  | { kind: 'add-request'; suggestion?: AssistantDraft }
  | { kind: 'assistant' }
  | { kind: 'add-engineer' }
  | { kind: 'updated'; plan: Plan; previous: Plan; phrase: string; from?: 'assistant' }
const modal = ref<Window | null>(null)
// Ответ сервера печатается внутри окна, из которого пришло действие. Если окно закрыли, пока
// шёл запрос, отказ показывается под шапкой — иначе минутный расчёт упал бы молча.
const modalError = ref<string | null>(null)
const COMPARING = 'Считаем варианты · около минуты'

/**
 * Пока вариант не выбран, дня нет, и показывать без окна нечего (блок 36): «Новый датасет» и
 * «Варианты плана» не закрываются. «‹» ведёт между ними — к вариантам, если они уже посчитаны, —
 * а во время расчёта её нет вовсе. Смена набора сбрасывает выбор, и замок встаёт снова.
 */
const locked = computed(() => !optimized.value)

/** Открыть или закрыть окно; прежний отказ относится к прежнему окну и снимается. */
function openModal(next: Window | null) {
  // Закрыть окно без выбранного варианта нельзя: вместо этого — окно, где его выбирают. Сюда же
  // приходят вкладки мобильной версии — они лежат вне затемнения.
  if (!next && locked.value) {
    next = variants.value.length || busy.value === COMPARING ? { kind: 'variants' } : { kind: 'dataset' }
  }
  // На телефоне окно ложится в лист: в свёрнутом от окна видна была бы одна шапка.
  if (next && sheet.value === 'collapsed') sheet.value = 'half'
  modal.value = next
  if (!busy.value) modalError.value = null
}

/**
 * Время дня в шапке — машина времени (блок 33). Пока его не ставили руками, оно идёт за часами
 * (`eventTime`: не раньше последнего события) и обновляется раз в минуту; поставленное руками
 * держится, пока не нажали «сейчас» или не сменился набор. На это время показываются план и
 * статусы, с ним же уходят действия.
 */
const pinnedTime = ref<string | null>(null)
const minute = ref(0) // часы машины не реактивны — пересчёт раз в минуту
let ticking: ReturnType<typeof setInterval> | undefined
onMounted(() => (ticking = setInterval(() => (minute.value += 1), 60_000)))
onBeforeUnmount(() => clearInterval(ticking))
const latest = computed(() => (shown.value === 'baseline' ? baseline.value : optimized.value))
const time = computed({
  get: () => {
    void minute.value
    return pinnedTime.value ?? eventTime(latest.value)
  },
  set: (value: string) => (pinnedTime.value = value),
})
const plan = computed(() => {
  const chain = chains[shown.value].value
  return chain.length ? versionAt(chain, time.value) : null
})
/** Сколько версий дня позже показываемой: действие отсюда их отбросит (блок 33). */
const later = computed(() => {
  const chain = chains[shown.value].value
  return plan.value ? chain.length - 1 - chain.indexOf(plan.value) : 0
})
// Утренний базовый план из того же ответа `compare`: с ним сравнивают метрики любого варианта
// и любой версии дня, а не с тем, что лежит в слоте «базовый» после событий.
const morningBaseline = computed(() => variants.value.find((item) => item.variant === 'baseline') ?? null)
// День ушёл от утреннего расчёта: выбор варианта заново вернёт его к утру.
const dayMoved = computed(() => !!(optimized.value?.parentPlanId || baseline.value?.parentPlanId))

const input = computed(() => plan.value?.input ?? dataset.value)
const selectedRequest = computed(
  () => input.value?.requests.find((request) => request.id === selectedRequestId.value) ?? null,
)
const selectedEngineer = computed(
  () => input.value?.engineers.find((engineer) => engineer.id === selectedEngineerId.value) ?? null,
)

/** Чьи километры и времена посчитаны по прямой (PLAN 5.6): флаг без текста ничего не говорит. */
const approximate = computed(() => {
  if (!plan.value?.approximate) return null
  return plan.value.approximateNote ?? 'Километры и времена посчитаны по прямой: Valhalla не ответила.'
})

// Демо-наборы для окна «Новый датасет» (PLAN 6.20): список не меняется, берётся один раз.
const demos = ref<DemoCatalog | null>(null)

/**
 * Расчёт переживает обновление страницы (блок 36). В `localStorage` лежат только идентификаторы:
 * варианты утреннего расчёта и версии дня по слотам. Сами планы неизменяемы и лежат в базе, их
 * отдаёт `GET /api/plans/{id}`. Сохраняется только выбранный день — без варианта нечего
 * восстанавливать; смена набора стирает запись.
 */
const SAVED = 'planner:day'
interface Saved {
  datasetId: string
  variants: string[]
  baseline: string[]
  optimized: string[]
  shown: Slot
}
const ids = (plans: Plan[]) => plans.map((item) => item.id)
watch([variants, chains.baseline, chains.optimized, shown], () => {
  try {
    if (!dataset.value || !optimized.value) return localStorage.removeItem(SAVED)
    const saved: Saved = {
      datasetId: dataset.value.id,
      variants: ids(variants.value),
      baseline: ids(chains.baseline.value),
      optimized: ids(chains.optimized.value),
      shown: shown.value,
    }
    localStorage.setItem(SAVED, JSON.stringify(saved))
  } catch {
    // Хранилище закрыто (приватное окно, запрет сайта) — день просто не переживёт обновление.
  }
})

/**
 * Сколько раз день начинали заново (новый набор, новый расчёт вариантов). Восстановление идёт
 * секунды — несколько больших планов, — и шапка в это время живая: если диспетчер за это время
 * загрузил другой набор, опоздавший ответ не должен лечь поверх него.
 */
let epoch = 0

/** Поднять сохранённый день. Он о другом наборе или его планов нет в базе — запись стирается. */
async function restore(current: Dataset, started: number) {
  try {
    const saved = JSON.parse(localStorage.getItem(SAVED) ?? 'null') as Saved | null
    if (!saved || saved.datasetId !== current.id) return localStorage.removeItem(SAVED)
    const wanted = [...new Set([...saved.variants, ...saved.baseline, ...saved.optimized])]
    const plans = new Map((await Promise.all(wanted.map(api.getPlan))).map((item) => [item.id, item]))
    if (epoch !== started) return
    const pick = (list: string[]) => list.map((id) => plans.get(id)!)
    variants.value = pick(saved.variants)
    chains.baseline.value = pick(saved.baseline)
    chains.optimized.value = pick(saved.optimized)
    shown.value = saved.shown
  } catch {
    try {
      localStorage.removeItem(SAVED)
    } catch {
      // хранилище закрыто — стирать нечего
    }
  }
}

/**
 * Каталог демо — порознь с набором: без текущего набора (404 на пустой базе) пару всё равно должно
 * быть из чего выбрать. Не пришёл — спрашиваем снова: окно выбора закрыть нельзя, и без каталога
 * из него был бы один выход — перезагрузка (бэкенд, поднятый позже фронта, — обычный случай).
 */
const demosError = ref<string | null>(null)
function loadDemos() {
  api.demos().then(
    (catalog) => {
      demos.value = catalog
      demosError.value = null
    },
    (problem) => {
      demosError.value = `Список демо-наборов не загрузился, пробуем снова: ${apiError(problem)}`
      setTimeout(loadDemos, 3000)
    },
  )
}

// Дня нет — сразу окно «Новый датасет», и закрыть его нельзя, пока вариант не выбран.
onMounted(async () => {
  loadDemos()
  const started = epoch
  try {
    const loaded = await api.getDataset()
    if (epoch !== started) return
    dataset.value = loaded
    await restore(loaded, started)
  } catch (problem) {
    error.value = apiError(problem)
  }
  if (epoch === started && locked.value && !modal.value) openModal({ kind: 'dataset' })
})

/**
 * Обучалка (OnboardingTour) — один раз, когда впервые показан посчитанный день и поверх карты
 * ничего не открыто: раньше половина её целей пуста. Пройдена или пропущена — отметка в
 * `localStorage`; хранилище закрыто — не показываем вовсе, иначе она встречала бы каждый заход.
 */
const TOURED = 'planner:tour'
const touring = ref(false)
let toured = false
watch([optimized, modal, busy], () => {
  if (toured || !optimized.value || modal.value || busy.value) return
  toured = true
  try {
    if (localStorage.getItem(TOURED)) return
  } catch {
    return
  }
  // Лист наполовину: у списка и у карты хватает места под подсказку.
  if (mobile.value) sheet.value = 'half'
  touring.value = true
})
function endTour() {
  touring.value = false
  try {
    localStorage.setItem(TOURED, '1')
  } catch {
    // хранилище закрыто — отметку не сохранить
  }
}

function select(requestId: string) {
  // Отказ сервера относился к прежней карточке — к новой он отношения не имеет.
  if (requestId !== selectedRequestId.value) cardError.value = null
  selectedRequestId.value = requestId
  selectedEngineerId.value = null
  // Выбрали с карты при свёрнутом листе — карточку надо показать; из списка при раскрытом на весь
  // экран — лист закрывает карту целиком, и выбранную заявку на ней не увидеть.
  if (mobile.value && sheet.value !== 'half') sheet.value = 'half'
}

// Заявку выбрали не на карте (список, гантт, карточка инженера) — карта приближается к её булавке.
// Клик по самой булавке идёт мимо: он уже на карте и сдвигать её из-под руки незачем.
const focus = ref<{ id: string; n: number } | null>(null)
function pickInMenu(requestId: string) {
  select(requestId)
  focus.value = { id: requestId, n: (focus.value?.n ?? 0) + 1 }
}

function selectEngineer(engineerId: string) {
  if (engineerId !== selectedEngineerId.value) cardError.value = null
  selectedEngineerId.value = engineerId
  selectedRequestId.value = null
}

function closeCard() {
  cardError.value = null
  selectedRequestId.value = null
  selectedEngineerId.value = null
}

/** Фраза окна «План обновлён» по действию (docs/design.md, W — по аналогии с макетом). */
function phraseOf(event: PlanEvent): string {
  switch (event.type) {
    case 'add_request':
    case 'urgent_request':
      return 'Новая заявка добавлена. Мы пересчитали маршруты с учётом нового адреса.'
    case 'add_engineer':
      return 'Инженер добавлен. Мы распределили на него подходящие заявки.'
    case 'update_request':
      return 'Изменения заявки сохранены. Мы пересчитали план с учётом новых данных.'
    case 'update_engineer':
      return 'Изменения инженера сохранены. Мы пересчитали его маршрут и связанные заявки.'
    case 'cancel_request':
      return event.requestIds.length > 1
        ? `Заявки отменены (${event.requestIds.length}). Мы пересчитали маршруты инженеров и связанные с ними заявки.`
        : 'Заявка отменена. Мы пересчитали маршрут инженера и связанные с ним заявки.'
    case 'defer_request':
      return event.requestIds.length > 1
        ? `Заявки перенесены на следующий день (${event.requestIds.length}). Остаток дня пересчитан без них.`
        : 'Заявка перенесена на следующий день. Остаток дня пересчитан без неё.'
    case 'close_request':
      return `Заявка закрыта: ${OUTCOME_NAMES[event.outcome].toLowerCase()}. Остаток дня пересчитан от этого момента.`
    case 'engineer_unavailable':
      return 'Инженер отмечен недоступным. Его незакреплённые заявки распределены заново.'
  }
}

/**
 * Новая версия плана (событие или ручная правка) ложится в тот слот, откуда ушёл запрос:
 * слот запоминается до запроса, иначе ответ лёг бы поверх соседнего плана (грабли блока 16).
 * После неё открывается «План обновлён».
 *
 * Считается она от показываемой версии — той, что действовала во время дня из шапки (блок 33).
 * Если после неё в цепочке есть ещё версии, действие меняет прошлое: они отбрасываются (в базе
 * остаются — планы неизменяемы), и перед этим окно «Изменить прошлое?» называет, что уйдёт.
 */
function newVersion(
  what: string,
  request: (current: Plan) => Promise<Plan>,
  phrase: string,
  into: Ref<string | null>,
  load: Load,
  // Что сделать с новой версией вместо окна «План обновлён» (ассистент остаётся в чате).
  done?: (next: Plan, previous: Plan, phrase: string) => void,
) {
  const current = plan.value
  const slot = shown.value
  if (!current) return
  const chain = chains[slot].value
  const kept = chain.slice(0, chain.indexOf(current) + 1)
  const go = async () => {
    await run(what, async () => {
      const next = await request(current)
      chains[slot].value = [...kept, next]
      if (done) done(next, current, phrase)
      else openModal({ kind: 'updated', plan: next, previous: current, phrase })
    }, into, load)
    // «Пересчёт дня» держит полосу над «Пересчитать» до ответа и уходит вместе с ним.
    asking.value = null
  }
  if (kept.length === chain.length) return go()
  rewind.value = { dropped: chain.slice(kept.length), go }
}

/** «Изменить прошлое?» поверх текущего окна: окно добавления со своим черновиком остаётся под ним. */
const rewind = shallowRef<{ dropped: Plan[]; go: () => void } | null>(null)
/**
 * Окна не накладываются: под «Пересчётом дня» и «Изменить прошлое?» окно, откуда пришло действие,
 * прячется, но остаётся смонтированным — черновик добавления переживает отказ, и окно
 * возвращается с ответом сервера у своей кнопки.
 */
const stacked = computed(() => !!asking.value || !!rewind.value)
function confirmRewind() {
  const go = rewind.value?.go
  rewind.value = null
  go?.()
}

/**
 * Событие дня (PLAN 6.12). `mode` передаётся только для «с начала дня». Полоса — над
 * «Пересчитать», а у переноса из списка — над его кнопкой в строке (`at`).
 */
function applyEvent(
  event: PlanEvent,
  mode?: ReplanMode,
  into: Ref<string | null> = cardError,
  at: Pick<Load, 'where' | 'requestId'> = { where: 'replan' },
  done?: (next: Plan, previous: Plan, phrase: string) => void,
) {
  return newVersion(
    'Пересчитываем день…',
    (current) => api.sendEvent(current.id, event, mode),
    phraseOf(event),
    into,
    { title: 'Пересчитываем день', sec: eventSec(event, mode, chains[shown.value].value), ...at },
    done,
  )
}

/**
 * Действие карточки или окна добавления сначала открывает «Пересчёт дня»: там выбирают, с какого
 * момента пересчитать день, и только после этого уходит событие. Отказ печатается там, откуда
 * пришло действие. Окно — поверх текущего: окно добавления со своим черновиком остаётся под ним.
 */
const asking = shallowRef<{ event: PlanEvent; into: Ref<string | null> } | null>(null)
function ask(event: PlanEvent, into: Ref<string | null> = cardError) {
  asking.value = { event, into }
}
/** Окно остаётся открытым до ответа: полоса идёт над его кнопкой, закрывает его `newVersion`. */
function replan(mode: ReplanMode | undefined) {
  const pending = asking.value
  if (pending) applyEvent(pending.event, mode, pending.into)
}

/**
 * «Перенести на след. день» из списка — отказ печатается под той строкой. Отказ пишется прямо в
 * `deferError`: после «Изменить прошлое?» запрос уходит позже, и ждать его здесь нечем.
 */
function defer(requestId: string) {
  const failure = computed<string | null>({
    get: () => deferError.value?.text ?? null,
    set: (text) => (deferError.value = text ? { requestId, text } : null),
  })
  applyEvent({ type: 'defer_request', time: time.value, requestIds: [requestId] }, undefined, failure, { where: 'defer', requestId })
}

/** «Сохранить изменения» в карточке — событие правки во время дня из шапки. */
function saveRequest(request: Request, time: string) {
  ask({ type: 'update_request', time, request })
}

function saveEngineer(engineer: Engineer, time: string) {
  ask({ type: 'update_engineer', time, engineer })
}

/** Переназначение (PLAN 6.16): сервер ставит заявку в лучшее место, `position: null`. */
function assign(requestId: string, engineerId: string) {
  newVersion(
    'Переназначаем…',
    (current) => api.manual(current.id, requestId, engineerId, null),
    'Заявка успешно переназначена. Мы пересчитали маршрут инженера и связанные с ним заявки.',
    modalError,
    { title: 'Переназначаем', sec: MANUAL_SEC, where: 'reassign' },
  )
}

/** Добавление из окна: отказ — в самом окне; удачное — окно сменяется «Планом обновлён». */
function add(event: PlanEvent) {
  ask(event, modalError)
}

/** Все варианты дня одним запросом (PLAN 6.14): около минуты, окно всё это время открыто. */
async function compareAll() {
  epoch += 1
  openModal({ kind: 'variants' })
  await run(COMPARING, async () => {
    variants.value = await api.compare()
  }, modalError, { title: 'Считаем варианты', sec: compareSec(variants.value), where: 'variants' })
}

/**
 * «оптимизированный» без выбранного варианта: окно вариантов, а если их ещё не считали —
 * сразу расчёт (W, docs/design.md «Шапка»).
 */
function openVariants() {
  if (variants.value.length || busy.value) openModal({ kind: 'variants' })
  else compareAll()
}

/**
 * Метрики из вариантов у того варианта, по которому идёт день, показывают его текущую версию —
 * после событий утренний план выдавал бы старые числа за текущие. Вкладка «Метрики» мобильной
 * версии открывает их так же, как кнопка шапки.
 */
function openMetrics(shownPlan: Plan | null, from: 'header' | 'variants') {
  if (!shownPlan) return
  const current = from === 'variants' && optimized.value?.variant === shownPlan.variant ? optimized.value : shownPlan
  openModal({ kind: 'metrics', plan: current, from })
}

/**
 * «Выбрать» — вариант становится показываемым оптимизированным планом, а базовый из того же
 * ответа — базовым (docs/design.md, «Варианты плана»). Это не новый расчёт, а готовые планы.
 */
function chooseVariant(chosen: Plan) {
  chains.baseline.value = morningBaseline.value ? [morningBaseline.value] : []
  chains.optimized.value = [chosen]
  // День начался заново: время, поставленное под прежний день, к нему не относится.
  pinnedTime.value = null
  shown.value = 'optimized'
  openModal(null)
}

/**
 * «Новый датасет»: набор меняется и сразу считается целиком (L, docs/design.md). Два шага —
 * два окна: отказ разбора файла (400 со списком строк) печатается в окне загрузки, а отказ
 * расчёта — в окне вариантов, где его можно повторить. Иначе после сменившегося набора
 * диспетчер загружал бы тот же файл второй раз, хотя нужен был только расчёт.
 */
async function newDataset(load: () => Promise<Dataset>, sec: number) {
  let loaded = false
  await run('Загружаем набор…', async () => {
    switched(await load())
    loaded = true
  }, modalError, { title: 'Загружаем набор', sec, where: 'dataset' })
  if (loaded) await compareAll()
}

/**
 * Ассистент (блок 42): переписка живёт здесь, а не в окне, — она переживает его закрытие. Ответ
 * ассистента — предложение: событие уходит через «Пересчёт дня», а заявка — через форму добавления,
 * как если бы диспетчер сам всё заполнил.
 */
const assistantOn = ref(false)
const said = ref<AssistantMessage[]>([])
const HISTORY = 8 // сервер принимает не больше десяти реплик
onMounted(async () => (assistantOn.value = await api.assistantOn()))

async function askAssistant(text: string) {
  const current = plan.value
  if (!current) return
  // Последние реплики уходят вместе с фразой: без них «какие у него заявки?» не к кому отнести.
  const history = said.value.slice(-HISTORY)
  said.value.push({ from: 'me', text })
  await run('Ассистент думает…', async () => {
    const reply = await api.assist(current.id, text, time.value, history)
    said.value.push({ from: 'bot', text: reply.text, reply })
  }, modalError)
}

/** «Новый чат»: переписка и отказ прошлого запроса начинаются заново. */
function resetAssistant() {
  said.value = []
  modalError.value = null
}

function applyAssistant(message: AssistantMessage) {
  const reply = message.reply
  if (!reply) return
  // Событие уходит сразу, без «Пересчёта дня»: диспетчер уже прочитал предложение и нажал
  // «Применить», режим выбирает сервер (`replan.default_mode`). «Изменить прошлое?» остаётся —
  // это подтверждение другого рода. Отказ сервера печатается в окне ассистента. Чат остаётся
  // открытым: кнопка под предложением становится «Применено», а «План обновлён» с изменениями
  // и списком «Сообщить клиентам» открывается отдельной кнопкой рядом.
  if (reply.event)
    applyEvent(reply.event, reply.mode ?? undefined, modalError, { where: 'replan' }, (next, previous, phrase) => {
      message.applied = true
      message.changes = markRaw({ plan: next, previous, phrase })
    })
  else if (reply.draft) openModal({ kind: 'add-request', suggestion: reply.draft })
}

/** Набор сменился — прежние планы к нему не относятся. */
function switched(loaded: Dataset) {
  epoch += 1
  said.value = []
  dataset.value = loaded
  chains.baseline.value = []
  chains.optimized.value = []
  pinnedTime.value = null
  variants.value = []
  shown.value = 'baseline'
  closeCard()
}

/**
 * Долгое действие: `busy` блокирует кнопки, над кнопкой `load.where` идёт полоса. Текст `busy` не
 * меняется по ходу — на `busy === COMPARING` завязаны замок окон и вкладки. Без `load` полосы нет:
 * ассистент показывает ожидание скелетоном ответа.
 */
async function run(what: string, action: () => Promise<void>, into: Ref<string | null>, load?: Load) {
  busy.value = what
  error.value = null
  cardError.value = null
  deferError.value = null
  modalError.value = null
  if (load) progress.start(load)
  try {
    await action()
  } catch (problem) {
    into.value = apiError(problem)
  } finally {
    busy.value = null
    if (load) progress.finish()
  }
}

/**
 * Кнопка «назад» браузера и жест «назад» телефона закрывают верхний слой экрана, как его «‹».
 * Слои — открытая карточка, окно, «Пересчёт дня» и «Изменить прошлое?»; у каждого своя запись
 * в истории с номером `layer`. Запертые окна выбора дня (блок 36) — не слой: за ними ничего нет,
 * и «назад» там уходит со страницы. Записи копируют `history.state` — его поля ведёт vue-router.
 */
const modalDepth = computed(() => {
  const open = modal.value
  if (!open) return 0
  if (open.kind === 'dataset' || open.kind === 'variants') return locked.value ? 0 : 1
  if (open.kind === 'metrics' && open.from === 'variants') return locked.value ? 1 : 2
  return 1
})
const depth = computed(
  () => +!!(selectedRequestId.value || selectedEngineerId.value) + modalDepth.value + +!!asking.value + +!!rewind.value,
)

function closeTop() {
  if (rewind.value) rewind.value = null
  else if (asking.value) asking.value = null
  else if (modal.value?.kind === 'metrics' && modal.value.from === 'variants') openModal({ kind: 'variants' })
  else if (modalDepth.value) openModal(null)
  else closeCard()
}

/** Записей в истории столько же, сколько слоёв: лишние уходят назад, недостающие дописываются. */
function syncHistory() {
  const at = history.state?.layer ?? 0
  for (let layer = at + 1; layer <= depth.value; layer++) history.pushState({ ...history.state, layer }, '')
  if (depth.value < at) history.go(depth.value - at)
}

// Ушли назад — закрыть слои выше записи. Пришли на запись выше слоёв (кнопка «вперёд», записи,
// оставшиеся от прошлой загрузки страницы) — вернуться: закрытое заново не открыть.
function onPopState() {
  // Во время расчёта слои не закрываются: ушло бы окно с полосой, куда придёт ответ.
  if (busy.value) return syncHistory()
  const at = history.state?.layer ?? 0
  for (let guard = depth.value; depth.value > at && guard > 0; guard--) closeTop()
  syncHistory()
}
onMounted(() => {
  syncHistory()
  watch(depth, syncHistory)
  window.addEventListener('popstate', onPopState)
})
onBeforeUnmount(() => window.removeEventListener('popstate', onPopState))
</script>

<template>
  <div :style="{ ...SHEET_VARS, '--sheet-h': sheetHeight, '--sheet-footer': sheetFooter }">
    <div class="relative h-screen overflow-hidden max-md:h-dvh">
      <!-- `z-0` замыкает слои карты: её кнопки (`z-10`) не всплывают поверх развёрнутого листа. -->
      <PlanMap
        class="absolute inset-0 z-0"
        :dataset="dataset"
        :plan="plan"
        :selected="selectedRequestId"
        :focus="focus"
        :inset="mobile ? { top: 20, left: 0, bottom: covered } : { top: 80, left: panelWidth, bottom: 0 }"
        :mobile="mobile"
        @select="select"
      />

      <!-- Полоса над картой прозрачна для касаний: на телефоне под ней справа линейка карты.
           Высокий лист закрывает карту, и пилюля времени легла бы на его поиск — она уходит, как ракета. -->
      <div
        class="pointer-events-none absolute top-3 right-3 left-3 z-[2] transition-[opacity,visibility] duration-200 [&>*]:pointer-events-auto"
        :class="{ 'max-md:invisible max-md:opacity-0': tall }"
      >
        <AppHeader
          class="max-md:hidden"
          :dataset="dataset"
          :plan="plan"
          :baseline="baseline"
          :optimized="optimized"
          :shown="shown"
          v-model:time="time"
          :pinned-time="!!pinnedTime"
          @now="pinnedTime = null"
          @show="shown = $event"
          @variants="openVariants"
          @metrics="openMetrics(plan, 'header')"
          @dataset="openModal({ kind: 'dataset' })"
        />
        <!-- Время дня в мобильной версии: шапки там нет, пилюля стоит над картой (блок 33). -->
        <div data-tour="time" class="hidden w-fit rounded-[28px] bg-panel p-1.5 shadow-panel max-md:flex">
          <DayTime v-model="time" :pinned="!!pinnedTime" :disabled="!plan" @now="pinnedTime = null" />
        </div>
        <!-- Решение заказчика: строку «посчитано по прямой» оставить (docs/design.md, «Шапка»). -->
        <p v-if="approximate" class="small mt-1.5 w-fit rounded-xl bg-field px-3 py-1 text-muted max-md:!ml-0 max-md:!max-w-[calc(100%-64px)]" :style="beside">{{ approximate }}</p>
        <p v-if="later" class="small mt-1.5 w-fit rounded-xl bg-field px-3 py-1 text-muted max-md:!ml-0 max-md:!max-w-[calc(100%-64px)]" :style="beside">
          Показан план на {{ time }}. Изменений позже: {{ later }} — действие отсюда их отменит.
        </p>
        <p
          v-if="error || (!modal && modalError)"
          class="error small mt-1.5 w-fit rounded-xl bg-field px-3 py-1 max-md:!ml-0 max-md:!max-w-[calc(100%-64px)]"
          :style="beside"
        >
          {{ error || modalError }}
        </p>
      </div>

      <!-- Видимая часть карты — цель шага обучалки «Карта»: сама карта лежит под панелью и шапкой. -->
      <div
        data-tour="map"
        class="pointer-events-none absolute top-24 right-3 max-md:top-20"
        :style="mobile ? { left: '12px', bottom: `${covered + 12}px` } : { left: `${panelWidth + 24}px`, bottom: '12px' }"
      />

      <aside
        ref="panel"
        data-tour="lists"
        class="absolute top-24 bottom-3 left-3 z-[1] flex flex-col gap-3 rounded-[36px] bg-panel p-3.5 shadow-panel transition-[width] duration-300 ease-out max-md:top-auto max-md:right-0 max-md:bottom-0 max-md:left-0 max-md:!w-auto max-md:gap-2.5 max-md:overflow-hidden max-md:rounded-b-none max-md:rounded-t-[36px] max-md:h-[calc(var(--sheet-h)+var(--tabs-h))] max-md:p-3.5 max-md:pt-[18px] max-md:duration-200 max-md:[&_.fade-scroll]:pb-[var(--tabs-h)]"
        :class="{
          'max-md:[&_.fade-footer]:hidden': sheet === 'collapsed' && dragged === null,
          'max-md:transition-[height]': dragged === null,
          'max-md:transition-none': dragged !== null,
        }"
        :style="{ width: `${panelWidth}px` }"
        @click="expandSheet"
      >
        <!-- Ручка лежит поверх верхнего отступа листа, а не строкой над поиском: в макете между краем
             листа и поиском 16 px (кадры 1504–1507). Тянуть лист можно из любого места, ручка
             ещё и переключает положение касанием. -->
        <SheetHandle data-tour="sheet" @pointerdown="startMouse" @click.stop="cycle" />
        <!-- Язычок из края панели: скругление только снаружи, тень срезана со стороны панели —
             на стыке нет ни шва, ни полосы тени, а тень панели продолжается вокруг него. Левые
             14 px язычка лежат на правом отступе панели: шеврон стоит посередине белого поля,
             и отступы слева и справа от него одинаковые. Тень — отдельным слоем под выступающей
             частью: срезка самой кнопки обрезала бы и шеврон. -->
        <button
          v-if="canGantt"
          data-tour="gantt"
          class="absolute top-1/2 -right-[18px] flex h-12 w-8 -translate-y-1/2 cursor-pointer items-center justify-center rounded-r-[12px] border-0 bg-panel p-0 before:absolute before:inset-y-0 before:right-0 before:left-3.5 before:-z-10 before:rounded-r-[12px] before:shadow-panel before:content-[''] before:[clip-path:inset(-24px_-24px_-24px_0)] max-md:hidden"
          :title="ganttOpen ? 'Свернуть график дня' : 'Открыть график дня'"
          @click="ganttOpen = !ganttOpen"
        >
          <Icon name="chevron" class="transition-transform duration-200" :class="ganttOpen ? 'rotate-90' : '-rotate-90'" />
        </button>
        <!-- Ключ по сущности: новая карточка открывается сверху, с чистым черновиком. -->
        <Transition name="appear">
          <EngineerDetails
            :time="time"
            v-if="selectedEngineer"
            :key="`e${selectedEngineer.id}`"
            :plan="plan"
            :engineer="selectedEngineer"
            :office="input?.office ?? null"
            :busy="busy"
            :error="cardError"
            @back="closeCard"
            @select="pickInMenu"
            @event="ask"
            @save="saveEngineer"
          />
          <RequestDetails
            :time="time"
            v-else-if="selectedRequest"
            :key="`r${selectedRequest.id}`"
            :plan="plan"
            :request="selectedRequest"
            :busy="busy"
            :error="cardError"
            @back="closeCard"
            @event="ask"
            @save="saveRequest"
            @reassign="openModal({ kind: 'reassign', requestId: $event })"
          />
        </Transition>
        <!-- v-show, а не v-else: вкладка, поиск и фильтр списка переживают открытую карточку. -->
        <Transition name="appear">
          <SideLists
            :time="time"
            v-show="!selectedEngineer && !selectedRequest"
            v-model:tab="listTab"
            :dataset="dataset"
            :plan="plan"
            :selected-request="selectedRequestId"
            :selected-engineer="selectedEngineerId"
            :busy="busy"
            :error="deferError"
            :progress="progress.bar('defer')"
            :wide="wide && !mobile"
            @request="pickInMenu"
            @engineer="selectEngineer"
            @defer="defer"
            @add="openModal({ kind: $event === 'engineers' ? 'add-engineer' : 'add-request' })"
          />
        </Transition>
      </aside>

      <!-- Мобильная версия: ракета, ассистент и нижние вкладки; на широком экране их прячут responsive-классы. -->
        <!-- Ассистент (блок 42): белая круглая кнопка справа внизу, как чат поддержки на сайте; пока
             чат открыт, она над затемнением и превращается в «✕». На телефоне встаёт слева над листом —
             с другой стороны от ракеты, того же размера — и уходит вместе с ней, когда лист раскрыт. -->
        <button
          v-if="assistantOn"
          data-tour="assistant"
          class="pressable absolute right-4 bottom-10 z-[2] flex size-[42px] cursor-pointer items-center justify-center rounded-full border-0 bg-panel text-text shadow-panel disabled:cursor-default disabled:opacity-50 max-md:right-auto max-md:left-3.5 max-md:size-12 max-md:bottom-[calc(var(--tabs-h)+var(--sheet-h)+14px)] max-md:duration-200"
          :class="[
            dragged === null ? 'max-md:transition-[bottom,opacity]' : 'max-md:transition-opacity',
            { 'max-md:pointer-events-none max-md:opacity-0': tall, 'md:z-30': modal?.kind === 'assistant' },
          ]"
          :title="modal?.kind === 'assistant' ? 'Закрыть чат' : 'Ассистент'"
          :aria-label="modal?.kind === 'assistant' ? 'Закрыть чат' : 'Ассистент'"
          :disabled="!plan"
          @click="openModal(modal?.kind === 'assistant' ? null : { kind: 'assistant' })"
        >
          <Icon :name="modal?.kind === 'assistant' ? 'close' : 'assistant'" class="[&_svg]:size-[18px]" />
        </button>
        <!-- Кнопка с ракетой на карте — «Варианты плана» (кадр 1504). Календаря нет (P). -->
        <button
          class="absolute right-3.5 bottom-[calc(var(--tabs-h)+var(--sheet-h)+14px)] z-[2] hidden size-12 items-center justify-center rounded-full border-0 bg-accent shadow-panel duration-200 max-md:flex"
          :class="[dragged === null ? 'transition-[bottom,opacity]' : 'transition-opacity', { 'pointer-events-none opacity-0': tall }]"
          data-tour="variants"
          title="Варианты плана"
          @click="openVariants"
        >
          <Icon name="optimize" />
        </button>
        <!-- Навбар полупрозрачный (кадры 1504–1506): лист уходит под него, и список проступает сквозь
             размытие. Активная вкладка тёмная, остальные серые; `!` — поверх `[&>button]:text-muted`.
             Верх без скругления: под навбаром всегда белый лист, и скруглённые углы с тенью
             читались на нём вырезами. -->
        <nav class="absolute right-0 bottom-0 left-0 z-[3] hidden h-[var(--tabs-h)] bg-panel/80 shadow-[0_-4px_20px_rgb(40_48_63_/_8%)] backdrop-blur-lg max-md:flex [&>button]:flex [&>button]:flex-1 [&>button]:cursor-pointer [&>button]:flex-col [&>button]:items-center [&>button]:justify-center [&>button]:gap-1 [&>button]:border-0 [&>button]:bg-transparent [&>button]:text-[13px] [&>button]:text-muted [&>button]:transition-colors [&>button]:duration-200">
          <button :class="{ '!text-text': activeTab === 'engineers' }" :aria-current="activeTab === 'engineers' || undefined" @click="openTab('engineers')">
            <Icon name="tab-engineers" /> Инженеры
          </button>
          <button :class="{ '!text-text': activeTab === 'requests' }" :aria-current="activeTab === 'requests' || undefined" @click="openTab('requests')">
            <Icon name="tab-requests" /> Заявки
          </button>
          <button data-tour="metrics" :class="{ '!text-text': activeTab === 'metrics' }" :aria-current="activeTab === 'metrics' || undefined" @click="openTab('metrics')">
            <Icon name="tab-metrics" /> Метрики
          </button>
          <button data-tour="dataset" :class="{ '!text-text': activeTab === 'dataset' }" :aria-current="activeTab === 'dataset' || undefined" @click="openTab('dataset')">
            <Icon name="tab-dataset" /> Датасет
          </button>
        </nav>
    </div>

    <Transition name="modal" mode="out-in">
      <DatasetModal
        v-if="modal?.kind === 'dataset'"
        :dataset="dataset"
        :demos="demos"
        :busy="busy"
        :error="modalError ?? error ?? demosError"
        :progress="progress.bar('dataset')"
        :closable="!locked || (!!variants.length && !busy)"
        @back="openModal(null)"
        @demo="(requests, crew) => newDataset(() => api.loadDemo(requests, crew), DEMO_SEC)"
        @load="(form) => newDataset(() => api.importDataset(form), IMPORT_SEC)"
      />
      <VariantsModal
        v-else-if="modal?.kind === 'variants'"
        :plans="variants"
        :current="optimized"
        :day-moved="dayMoved"
        :busy="busy"
        :error="modalError"
        :progress="progress.bar('variants')"
        :closable="!locked || !busy"
        @back="openModal(locked ? { kind: 'dataset' } : null)"
        @compute="compareAll"
        @choose="chooseVariant"
        @metrics="openMetrics($event, 'variants')"
      />
      <MetricsModal
        v-else-if="modal?.kind === 'metrics'"
        :plan="modal.plan"
        :from="modal.from"
        :baseline="morningBaseline"
        :choose="modal.from === 'variants' && variants.includes(modal.plan)"
        @back="openModal(modal.from === 'variants' ? { kind: 'variants' } : null)"
        @choose="chooseVariant"
      />
      <ReassignModal
        v-else-if="modal?.kind === 'reassign' && plan"
        :plan="plan"
        :request-id="modal.requestId"
        :busy="busy"
        :error="modalError"
        :progress="progress.bar('reassign')"
        :class="{ '!hidden': stacked }"
        @back="openModal(null)"
        @assign="(engineerId) => modal?.kind === 'reassign' && assign(modal.requestId, engineerId)"
      />
      <AssistantModal
        v-else-if="modal?.kind === 'assistant' && plan"
        :plan="plan"
        :log="said"
        :busy="busy"
        :error="modalError"
        :class="{ '!hidden': stacked }"
        @back="openModal(null)"
        @send="askAssistant"
        @reset="resetAssistant"
        @changes="(changes) => openModal({ kind: 'updated', ...changes, from: 'assistant' })"
        @apply="applyAssistant"
      />
      <AddRequestModal
        :time="time"
        v-else-if="modal?.kind === 'add-request' && plan"
        :class="{ '!hidden': stacked }"
        :plan="plan"
        :suggestion="modal.suggestion"
        :busy="busy"
        :error="modalError"
        @back="openModal(null)"
        @add="add"
      />
      <AddEngineerModal
        :time="time"
        v-else-if="modal?.kind === 'add-engineer' && plan"
        :class="{ '!hidden': stacked }"
        :plan="plan"
        :busy="busy"
        :error="modalError"
        @back="openModal(null)"
        @add="add"
      />
      <PlanUpdatedModal
        v-else-if="modal?.kind === 'updated'"
        :plan="modal.plan"
        :previous="modal.previous"
        :phrase="modal.phrase"
        @back="openModal(modal.from === 'assistant' ? { kind: 'assistant' } : null)"
      />
    </Transition>
    <ReplanModal
      v-if="asking && plan"
      :event="asking.event"
      :busy="busy"
      :progress="progress.bar('replan')"
      :class="{ '!hidden': rewind }"
      @back="asking = null"
      @go="replan"
    />
    <RewindModal v-if="rewind" :time="time" :dropped="rewind.dropped" @back="rewind = null" @go="confirmRewind" />
    <OnboardingTour v-if="touring" :mobile="mobile" @done="endTour" />
  </div>
</template>
