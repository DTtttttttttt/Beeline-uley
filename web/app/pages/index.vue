<script setup lang="ts">
// Экран диспетчера по макетам (docs/design.md): карта на весь экран, поверх — шапка, левая
// панель со списками и карточками, модальные окна. Состояние дня и все запросы — здесь,
// компоненты только показывают и собирают события.
import {
  apiError,
  clockMinutes,
  clockOf,
  eventTime,
  LIVE_EDGE,
  OUTCOME_NAMES,
  plural,
  useApi,
  versionAt,
  type AssistantAttachment,
  type AssistantDraft,
  type AssistantEdit,
  type AssistantEngineerDraft,
  type AssistantMessage,
  type AssistantReply,
  type Dataset,
  type DaySummary,
  type DemoCatalog,
  type Engineer,
  type EngineerRating,
  type Plan,
  type PlanEvent,
  type Preview,
  type ReplanMode,
  type Request,
  type Review,
  type ReviewInput,
} from '~/composables/useApi'
import { SHEET_VARS, useSheet, useSheetDrag } from '~/composables/sheet'
import { dayRange, PLAY_MOMENT, tracks } from '~/composables/playback'
import {
  ATTACH_SEC,
  compareSec,
  DEMO_SEC,
  eventSec,
  IMPORT_SEC,
  MANUAL_SEC,
  previewSec,
  summarySec,
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
 * и открыта на всё окно, кроме 385 px карты справа (кадр 1749: панель 1151 при окне 1536). Шире
 * 620 px справа от инженеров появляются дорожки гантта (docs/design.md, «Гантт всех инженеров»).
 * В окне уже 1005 px гантту места нет — там и стрелки нет: она лишь растянула бы список.
 */
const PANEL = 380
const MAP_BESIDE_GANTT = 385
const ganttOpen = ref(false)
const viewport = ref(0) // ширина окна; до монтирования — 0, панель закрыта
const canGantt = computed(() => viewport.value - MAP_BESIDE_GANTT >= 620)
const panelWidth = computed(() => (ganttOpen.value && canGantt.value ? viewport.value - MAP_BESIDE_GANTT : PANEL))
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
// Полоса проигрывания (блок 77) — справа от панели; справа от раскрытого гантта ей тесно, и она
// ложится поверх панели, прижатая к правому краю. Ширина — по содержимому: подпись скорости
// меняет ширину полосы, а не шкалы (правки 82).
const playbarPlace = computed(() => (viewport.value - 24 - (panelWidth.value + 24) >= 640 ? beside.value : {}))

// Какое окно открыто поверх карты (docs/design.md, «Модальные окна»). Метрики помнят, откуда
// их открыли: из вариантов — с «Выбрать» и «Назад» к списку, из шапки — без «Выбрать».
type Window =
  | { kind: 'dataset' }
  | { kind: 'variants' }
  | { kind: 'metrics'; plan: Plan; from: 'header' | 'variants' }
  | { kind: 'reassign'; requestId: string }
  | { kind: 'add-request'; suggestion?: AssistantDraft }
  | { kind: 'assistant' }
  | { kind: 'add-engineer'; preset?: Engineer | AssistantEngineerDraft }
  | { kind: 'updated'; plan: Plan; previous: Plan; phrase: string; from?: 'assistant' }
  | { kind: 'summary' }
  | { kind: 'reviews'; engineerId: string }
  // Черновик оценки едет через окно ссылки: окна сменяют друг друга, и без него «‹» из QR стирала бы ввод.
  | { kind: 'rate'; requestId: string; draft?: ReviewInput }
  | { kind: 'link'; requestId: string; link: string; draft: ReviewInput }
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
 * Время дня в шапке — машина времени (блок 33): на него показываются план и статусы, с ним уходят
 * действия. Это момент проигрывания дня (`moment`, минуты дробью): он идёт всегда — по умолчанию
 * минута дня за минуту, и при закрытой полосе тоже (правки 81.7). Выбранный день начинается с
 * начала смен, а после обновления страницы время продолжается с того, на котором его оставили.
 * Дробная минута уходит карте через `provide`, мимо шаблона страницы: иначе вся страница
 * перерисовывалась бы на каждом кадре. Шаблон видит только целую минуту.
 */
const moment = shallowRef<number | null>(null)
provide(PLAY_MOMENT, moment)
const latest = computed(() => (shown.value === 'baseline' ? baseline.value : optimized.value))
const playMinute = computed(() => (moment.value === null ? null : Math.floor(moment.value)))
const time = computed(() => (playMinute.value === null ? eventTime(latest.value) : clockOf(playMinute.value)))
const plan = computed(() => {
  const chain = chains[shown.value].value
  return chain.length ? versionAt(chain, time.value) : null
})
/** Сколько версий дня позже показываемой: действие отсюда их отбросит (блок 33). */
const later = computed(() => {
  const chain = chains[shown.value].value
  return plan.value ? chain.length - 1 - chain.indexOf(plan.value) : 0
})

/**
 * Часы дня (блок 69). На живом краю — показана последняя версия, и не вариант «Пересчёта дня» —
 * сервер закрывает работы по скрытым фактам (`POST /clock`): на каждую минуту дня, сыгранную или
 * настенную, пока есть начатая незакрытая работа. Один вызов догоняет день до этой минуты —
 * версиями по порядку (блок 82), второй отвечает 204. Версии часов ложатся в цепочку молча; «План обновлён» открывает
 * уведомление, если затянувшаяся работа перестроила остаток дня. Пока ответ идёт, игра ждёт.
 * Свой флаг, а не `run`: тот снимает отказы и рисует полосу прогресса, а часы — фон.
 *
 * Работа без факта после планового конца считается идущей (`LIVE_EDGE`), только когда часы догнали
 * время в шапке — ответили «нового нет» не дальше минуты назад. Пока они догоняют день после скачка
 * (страницу открыли вечером) или если отказали, показ идёт по плану: иначе каждая ещё не закрытая
 * работа «шла бы сверх плана» на часы. Ответ засчитывается для любой версии этой цепочки, а не
 * только последней (правки 82): новая версия — от часов или события — иначе на миг
 * выключала живой край, затянувшаяся работа «кончалась по плану», и инженер на карте уезжал к
 * следующей заявке, а потом возвращался.
 */
const edge = computed(() => later.value === 0 && !previewPlan.value)
const clockedAt = shallowRef<{ id: string; time: string } | null>(null)
const live = computed(() => {
  const at = clockedAt.value
  if (!edge.value || !at || !chains[shown.value].value.some((version) => version.id === at.id)) return false
  return clockMinutes(time.value) - clockMinutes(at.time) <= 1
})
provide(LIVE_EDGE, live)
const working = computed(() => {
  const current = plan.value
  if (!current || !edge.value) return false
  return current.routes.some((route) => route.stops.some((stop) => !current.closed[stop.requestId] && stop.start <= time.value))
})
const syncing = ref(false)

async function syncClock() {
  if (syncing.value || busy.value || !edge.value || !latest.value) return
  // Начатой незакрытой работы нет — часам нечего сказать, день и так догнан.
  if (!working.value) return (clockedAt.value = { id: latest.value.id, time: time.value })
  const slot = shown.value
  const at = time.value
  let current = latest.value
  syncing.value = true
  try {
    // Сервер догоняет день до `at` за один вызов (блок 82): все версии по порядку. Следующий вызов
    // отвечает «нового нет» — значит, часы догнали. Предел только страхует от бесконечной цепочки.
    const limit = current.routes.reduce((sum, route) => sum + route.stops.length, 0)
    for (let round = 0; round <= limit; round++) {
      const next = await api.clock(current.id, at)
      // Пока шёл ответ, цепочку сменило действие диспетчера — версии часов ему уже не нужны.
      if (chains[slot].value.at(-1) !== current) break
      if (!next.length) {
        clockedAt.value = { id: current.id, time: at }
        break
      }
      chains[slot].value = [...chains[slot].value, ...next]
      current = next.at(-1)!
    }
  } catch (problem) {
    error.value = apiError(problem)
  } finally {
    syncing.value = false
  }
  if (time.value !== at) syncClock()
}
// И после действия диспетчера: пока шёл его расчёт, минута могла пройти мимо часов.
watch([time, busy, () => latest.value?.id], syncClock)


/**
 * Сводка дня (блок 68) по последней версии: текущая скорость инженеров для списка и карточки и
 * «Итоги дня». Версии неизменяемы — ответ на версию один. Не пришёл — остаётся прежний: подписи
 * скорости только показ, расчёт плана их не читает.
 */
const summary = shallowRef<{ planId: string; data: DaySummary } | null>(null)
// Пока часы закрывают работы одну за другой, промежуточные версии сводки не нужны — только итог.
watch(
  [() => latest.value?.id, syncing],
  ([id]) => {
    if (id && !syncing.value && summary.value?.planId !== id)
      api.daySummary(id).then((data) => latest.value?.id === id && (summary.value = { planId: id, data }), () => {})
  },
  { immediate: true },
)

/**
 * Живой рейтинг и оценки заявок (блок 72). Снимок `engineer.rating` в плане за день не меняется,
 * а оценка из карточки должна сразу появиться в списке — поэтому список и карточки берут рейтинг
 * отсюда. Перечитывается с новой последней версией и после оценки.
 */
const ratings = shallowRef<Record<string, EngineerRating>>({})
const reviews = shallowRef<Record<string, Review>>({})
function loadReviews() {
  const id = latest.value?.id
  if (!id) {
    ratings.value = reviews.value = {}
    return
  }
  // Ответ на прежнюю версию не перезаписывает ответ на новую.
  api.ratings().then(
    (list) => latest.value?.id === id && (ratings.value = Object.fromEntries(list.map((item) => [item.engineerId, item]))),
    () => {},
  )
  api.planReviews(id).then(
    (list) => latest.value?.id === id && (reviews.value = Object.fromEntries(list.map((item) => [item.requestId, item]))),
    () => {},
  )
}
watch(
  [() => latest.value?.id, syncing],
  ([id]) => {
    if (!syncing.value) loadReviews()
  },
  { immediate: true },
)

const speeds = computed(() => Object.fromEntries((summary.value?.data.engineers ?? []).map((item) => [item.engineerId, item.speedNow])))
/** Смены кончились — кнопка «Итоги дня» в шапке активна. Сами итоги не открываются (правки 82). */
const dayEnded = computed(() => {
  const current = plan.value
  if (!current?.input.engineers.length) return false
  return clockMinutes(time.value) >= Math.max(...current.input.engineers.map((engineer) => clockMinutes(engineer.shiftEnd)))
})
/**
 * Данные «Итогов дня»: часы догнали день и сводка последней версии пришла. До этого окно
 * показывает загрузку — сводка версии до перемотки на вечер не знает фактов за перемотанные часы.
 */
const dayTotals = computed(() => (live.value && summary.value?.planId === latest.value?.id ? summary.value.data : null))
// Итоги грузятся в фоне с той минуты, когда кончились смены: часы сами догоняют день, сводка
// приходит следом (правки 82). Запоминаем, когда это началось и сколько займёт по оценке, — окно,
// открытое позже, продолжает ту же полосу, а не начинает её с нуля.
let closing: { since: number; sec: number } | null = null
watch(
  () => dayEnded.value && !dayTotals.value,
  (loading) => {
    closing = loading && latest.value ? { since: performance.now(), sec: summarySec(latest.value) } : null
  },
  { immediate: true },
)
// Полоса прогресса в окне, пока итогов нет, — как у остальных долгих действий. Чужое действие,
// запущенное в это время, полосу не теряет: гасим только свою.
watch(
  () => modal.value?.kind === 'summary' && !dayTotals.value,
  (loading) => {
    if (loading && latest.value) {
      const load = { title: 'Собираем итоги дня', sec: closing?.sec ?? summarySec(latest.value), where: 'summary' as const }
      progress.start(load, closing?.since)
    } else if (progress.current?.where === 'summary') progress.finish()
  },
)
/**
 * Проигрывание дня (блок 63): время дня идёт само, `speed` минут дня за секунду, инженеры едут по
 * маршрутам. Игра идёт всегда, кроме паузы на открытой полосе (правки 81.7): по умолчанию —
 * «Реальное время», минута дня за минуту; полоса (`barOpen`) только показывает шкалу и меняет
 * скорость, а её ✕ и перемотка время не сбрасывают и игру не останавливают. Статусы, гантт и
 * версии дня следуют за целой минутой сами (`versionAt`).
 */
const REAL_TIME = 1 / 60
const playing = ref(false)
const speed = ref(REAL_TIME)
const barOpen = ref(false)
// Игра встала сама на конце дня, а не на ⏸: перемотка назад пускает её снова (правки 81.7).
let ended = false
let playFrame = 0
let playTimer: ReturnType<typeof setTimeout> | undefined
let playedAt = 0
let reducedMotion: MediaQueryList | null = null // заводится на первой игре: на сервере `matchMedia` нет
// Полоса и карта — по одному плану: пока открыт вариант «Пересчёта дня», маркеры едут по нему.
const playPlan = computed(() => previewPlan.value ?? plan.value)
/**
 * Затянувшаяся работа идёт и после конца смен (блок 69): пока она идёт, шкала тянется за сыгранным
 * временем, а закрытая — кончается на событии часов, которое её закрыло, и назад игра не прыгает.
 * Тянет шкалу только игра (`overtime`), а не перемотка: иначе ползунок, доведённый до конца, сам
 * отодвигал бы конец шкалы.
 */
const overtime = ref(0)
const playRange = computed((): [number, number] => {
  const current = playPlan.value
  if (!current) return [0, 24 * 60]
  const [from, to] = dayRange(current)
  const closedAt = current.event?.type === 'clock' ? clockMinutes(current.event.time) : 0
  return [from, Math.max(to, closedAt, working.value ? overtime.value : 0)]
})
// Участки маршрутов — раз на версию дня, а не на каждую сыгранную минуту; их же получает карта.
const playTracks = computed(() => (playMinute.value !== null && playPlan.value ? tracks(playPlan.value, live.value) : null))

const clampToDay = (minutes: number) => Math.min(Math.max(minutes, playRange.value[0]), playRange.value[1])

/**
 * На телефоне «Выбор времени» — единственный лист над вкладками (кадр 1560): список уходит, а
 * ракета, ассистент и кнопки карты встают над листом времени — его высота подменяет `--sheet-h`.
 */
const barHeight = ref(0)
// Лист сворачивается за ручку (правки 82) — его высоту отслеживаем, а не меряем раз на открытие.
const barSize = import.meta.client
  ? new ResizeObserver(([entry]) => {
      barHeight.value = (entry?.target as HTMLElement | undefined)?.offsetHeight ?? 0
      measure()
    })
  : undefined
watch([barOpen, mobile], async () => {
  await nextTick()
  barSize?.disconnect()
  const element = document.querySelector<HTMLElement>('[data-playback]')
  barHeight.value = element?.offsetHeight ?? 0
  if (element) barSize?.observe(element)
  measure()
})
onBeforeUnmount(() => barSize?.disconnect())

/** Нажатие на плашку времени открывает полосу: игра идёт дальше, как шла. */
function openPlayback() {
  if (plan.value && !busy.value) barOpen.value = true
}

function play() {
  if (moment.value === null || !import.meta.client) return
  reducedMotion ??= matchMedia('(prefers-reduced-motion: reduce)')
  if (moment.value >= playRange.value[1] && !working.value) seek(playRange.value[0])
  playing.value = true
  ended = false
  playedAt = performance.now()
  stopFrames()
  next()
}

function pause() {
  stopFrames()
  playing.value = false
  ended = false
}

function stopFrames() {
  cancelAnimationFrame(playFrame)
  clearTimeout(playTimer)
}

/**
 * Следующий шаг игры — каждый кадр, и в реальном времени тоже: раз в секунду инженеры на карте
 * двигались рывками (правки 82). При «уменьшить движение» — раз в секунду.
 */
function next() {
  if (reducedMotion?.matches) playTimer = setTimeout(() => step(performance.now()), 1000)
  else playFrame = requestAnimationFrame(step)
}

/** ✕ полосы: полоса уходит, время, игра, пауза и выбранная скорость остаются (правки 82). */
function closePlayback() {
  barOpen.value = false
}

/**
 * Шаг игры. Быстрая игра прибавляет не больше секунды игры за шаг: фоновая вкладка кадров не
 * рисует, и по возвращении день не скакнул бы на часы. Реальное время догоняет настенное целиком.
 * Пока отвечают часы дня или считается действие, время стоит: действие уходит со своим временем.
 */
function step(now: number) {
  if (!playing.value || moment.value === null) return
  if (syncing.value || busy.value) {
    playedAt = now
    return next()
  }
  const elapsed = Math.max(0, now - playedAt) / 1000
  playedAt = now
  seek(moment.value + speed.value * (speed.value <= REAL_TIME ? elapsed : Math.min(elapsed, 1)), true)
  if (playing.value) next()
}

/** `played` — шаг игры: только он тянет шкалу за затянувшейся работой. */
function seek(minutes: number, played = false) {
  if (played && working.value && minutes > playRange.value[1]) overtime.value = Math.floor(minutes) + 1
  const target = clampToDay(minutes)
  moment.value = target
  if (target >= playRange.value[1] && !working.value) {
    pause()
    ended = true
  }
}

/** ⟲ «К началу дня» — после подтверждения (правки 82). */
const restarting = ref(false)
function restart() {
  restarting.value = false
  seekBar(playRange.value[0])
}

/** Перемотка со шкалы: игра идёт дальше, а вставшая на конце дня — снова пошла. */
function seekBar(minutes: number) {
  seek(minutes)
  if (ended && !playing.value && moment.value !== null && moment.value < playRange.value[1]) play()
}

/** Время дня, поставленное мимо шкалы (выбор варианта, ассистент): туда же встаёт и игра. */
function setTime(clock: string) {
  seek(clockMinutes(clock))
}

/** День начался заново: время — на начало смен, игра — в реальном времени. */
function startDay(first: Plan) {
  overtime.value = 0
  speed.value = REAL_TIME
  moment.value = dayRange(first)[0]
  play()
}

watch(plan, (value) => {
  if (value) return
  barOpen.value = false
  pause()
  moment.value = null
})
onBeforeUnmount(stopFrames)

/**
 * Время дня переживает обновление страницы (правки 81.7): целая минута лежит рядом с днём, по
 * набору. После обновления игра идёт с неё в реальном времени — пропущенное не догоняется.
 */
const SAVED_TIME = 'planner:time'
watch(playMinute, (minute) => {
  if (minute === null || !dataset.value) return
  try {
    localStorage.setItem(SAVED_TIME, JSON.stringify({ datasetId: dataset.value.id, minute }))
  } catch {
    // Хранилище закрыто — время начнётся с начала смен.
  }
})
function savedMinute(datasetId: string): number | null {
  try {
    const saved = JSON.parse(localStorage.getItem(SAVED_TIME) ?? 'null') as { datasetId: string; minute: number } | null
    return saved?.datasetId === datasetId ? saved.minute : null
  } catch {
    return null
  }
}

/** Клик мимо полосы закрывает её, как окно (правки 81.7): кроме плашки времени и окон. */
function outsideBar(event: PointerEvent) {
  const target = event.target as Element | null
  if (!barOpen.value || !target?.closest) return
  if (target.closest('[data-playback], [data-tour="time"], [role="dialog"]')) return
  closePlayback()
}
onMounted(() => document.addEventListener('pointerdown', outsideBar))
onBeforeUnmount(() => document.removeEventListener('pointerdown', outsideBar))


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
// Загрузки набора — карта по ним заново вписывает рамку (правки 82).
const loads = ref(0)

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
    const first = chains[saved.shown].value[0]
    if (first) {
      startDay(first)
      const minute = savedMinute(current.id)
      if (minute !== null) seek(minute)
    }
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
// Новые шаги — новый ключ (блок 75): прошедшие прежний тур иначе их не увидели бы.
const TOURED = 'planner:tour:2'
const touring = ref(false)
let toured = false
// Вкладка списка до тура: шаги доп. ТЗ её переключают, после тура она возвращается.
let tourTab = listTab.value
// Открытая карточка прячет список, а в нём цели четырёх шагов — тур ждёт, пока её закроют.
watch([optimized, modal, busy, selectedRequestId, selectedEngineerId], () => {
  if (toured || !optimized.value || modal.value || busy.value || selectedRequestId.value || selectedEngineerId.value) return
  toured = true
  try {
    if (localStorage.getItem(TOURED)) return
  } catch {
    return
  }
  // Лист наполовину: у списка и у карты хватает места под подсказку.
  if (mobile.value) sheet.value = 'half'
  tourTab = listTab.value
  touring.value = true
})
function endTour() {
  touring.value = false
  listTab.value = tourTab
  try {
    localStorage.setItem(TOURED, '1')
  } catch {
    // хранилище закрыто — отметку не сохранить
  }
}

/**
 * Правка заявки от ассистента (блок 59), которую доделывает её карточка: адрес, тип. `n` растёт на
 * каждую новую — та же правка, присланная повторно, ложится в черновик заново. Любой другой выбор
 * карточки её снимает.
 */
const cardEdit = ref<{ edit: AssistantEdit; n: number } | null>(null)

function select(requestId: string) {
  cardEdit.value = null
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
const focus = ref<{ id: string; n: number; engineer?: boolean } | null>(null)
function pickInMenu(requestId: string) {
  select(requestId)
  focus.value = { id: requestId, n: (focus.value?.n ?? 0) + 1 }
}

/**
 * Инженера выбрали в списке — карта вписывает его маршрут и выделяет его, пока открыта карточка
 * (A18; на телефоне наведения нет, выделение — касанием строки).
 */
function selectEngineer(engineerId: string) {
  cardEdit.value = null
  if (engineerId !== selectedEngineerId.value) cardError.value = null
  selectedEngineerId.value = engineerId
  selectedRequestId.value = null
  // Раскрытый лист закрывает карту целиком, и вписанного маршрута на ней не увидеть.
  if (mobile.value && sheet.value === 'full') sheet.value = 'half'
  focus.value = { id: engineerId, n: (focus.value?.n ?? 0) + 1, engineer: true }
}

/**
 * Чей маршрут выделен на карте: инженер под курсором в списке, иначе — с открытой карточкой.
 * Карточка прячет список, и `pointerleave` с него уже не придёт, поэтому наведение снимается
 * при любом выборе.
 */
const hovered = ref<string | null>(null)
// Заявка под курсором в списке — её булавка на карте крупнее (правки 81.4).
const hoveredRequest = ref<string | null>(null)
watch([selectedRequestId, selectedEngineerId], () => (hovered.value = hoveredRequest.value = null))
const highlighted = computed(() => hovered.value ?? selectedEngineerId.value)

/**
 * Пока идёт пересчёт по событию или переназначение, на карте мигают его заявки и заявки, сменившие
 * бригаду в прошлом пересчёте (`diff` показанного плана).
 */
const pulse = ref<string[] | null>(null) // заявки идущего действия, ставит `run`
const pulsing = computed(() => {
  if (!pulse.value) return []
  const moved = plan.value?.diff?.requests.filter((item) => item.change === 'reassigned').map((item) => item.requestId) ?? []
  return [...pulse.value, ...moved]
})

/** Заявки события: у событий инженера — его маршрут, у нового инженера заявок ещё нет. */
function eventRequests(event: PlanEvent): string[] {
  const stopsOf = (engineerId: string) =>
    plan.value?.routes.find((route) => route.engineerId === engineerId)?.stops.map((stop) => stop.requestId) ?? []
  switch (event.type) {
    case 'cancel_request':
    case 'defer_request':
      return event.requestIds
    case 'close_request':
      return [event.requestId]
    case 'add_engineer':
      return []
    case 'update_engineer':
      return stopsOf(event.engineer.id)
    case 'engineer_unavailable':
      return stopsOf(event.engineerId)
    case 'clock':
      return [...event.closed.map((item) => item.requestId), ...event.running]
    default:
      return [event.request.id]
  }
}

function closeCard() {
  cardEdit.value = null
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
    case 'clock':
      return event.running.length
        ? `План перестроен из-за задержки заявки №${event.running.join(', №')}: инженер не успевал к следующей заявке.`
        : 'Работы закрыты по факту выполнения. Остаток дня сдвинут от этого момента.'
  }
}

/** Откуда считается новая версия: показываемый план, его слот и то, что от цепочки останется. */
interface Branch {
  current: Plan
  slot: Slot
  kept: Plan[]
}

/**
 * Новая версия считается от показываемой версии — той, что действовала во время дня из шапки
 * (блок 33). Если после неё в цепочке есть ещё версии, действие меняет прошлое: они отбрасываются (в
 * базе остаются — планы неизменяемы), и перед этим окно «Изменить прошлое?» называет, что уйдёт.
 * Слот запоминается до запроса, иначе ответ лёг бы поверх соседнего плана (грабли блока 16).
 */
function branch(go: (from: Branch) => void) {
  const current = plan.value
  const slot = shown.value
  if (!current) return
  const chain = chains[slot].value
  const from = { current, slot, kept: chain.slice(0, chain.indexOf(current) + 1) }
  if (from.kept.length === chain.length) return go(from)
  rewind.value = { dropped: chain.slice(from.kept.length), go: () => go(from) }
}

/**
 * Новая версия ложится в свой слот, и открывается «План обновлён» — или вместо него `done`
 * (ассистент остаётся в чате).
 */
function settle(from: Branch, next: Plan, phrase: string, done?: (next: Plan, previous: Plan, phrase: string) => void) {
  chains[from.slot].value = [...from.kept, next]
  if (done) done(next, from.current, phrase)
  else openModal({ kind: 'updated', plan: next, previous: from.current, phrase })
}

/** Новая версия плана сразу, без вариантов: ручная правка, перенос из списка, ассистент. */
function newVersion(
  what: string,
  request: (current: Plan) => Promise<Plan>,
  phrase: string,
  into: Ref<string | null>,
  load: Load,
  done?: (next: Plan, previous: Plan, phrase: string) => void,
) {
  branch((from) => run(what, async () => settle(from, await request(from.current), phrase, done), into, load))
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
 * Событие дня сразу версией, без вариантов (PLAN 6.12): перенос из списка и «Применить» у
 * ассистента. `mode` передаётся только для «с начала дня». Полоса — над кнопкой действия (`at`).
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
    { title: 'Пересчитываем день', sec: eventSec(event, mode, chains[shown.value].value), pulse: eventRequests(event), ...at },
    done,
  )
}

/**
 * Действие карточки или окна добавления открывает «Пересчёт дня» и сразу считает варианты реакции
 * (PLAN 6.12, блок 53): сервер возвращает их черновиками, версии ещё нет. Выбранный вариант виден на
 * карте вместо текущего плана; «Выбрать» делает его версией, «Назад» ничего не оставляет —
 * черновики удалит сервер. «Изменить прошлое?» спрашивается до расчёта: варианты считаются от той
 * версии, что останется. Отказ сервера печатается там, откуда пришло действие, и окно уходит.
 * Окно — поверх текущего: окно добавления со своим черновиком остаётся под ним.
 */
const asking = shallowRef<{
  event: PlanEvent
  into: Ref<string | null>
  from: Branch
  preview: Preview | null
} | null>(null)
const chosen = ref<string | null>(null)
const replanError = ref<string | null>(null)
// Авария днём: сначала «Кто поедет на аварию?» (блок 77, кадр 1788), затем «Пересчёт дня».
const crewStep = ref(false)
const isAlarm = (event: PlanEvent) =>
  (event.type === 'add_request' || event.type === 'urgent_request') && event.request.workPriority === 'emergency'
const previewPlan = computed(
  () => asking.value?.preview?.options.find((option) => option.plan.id === chosen.value)?.plan ?? null,
)

function ask(event: PlanEvent, into: Ref<string | null> = cardError) {
  branch(async (from) => {
    const pending = { event, into, from, preview: null }
    asking.value = pending
    crewStep.value = isAlarm(event)
    replanError.value = null
    const load = { title: 'Считаем варианты', sec: previewSec(event), where: 'replan' as const, pulse: eventRequests(event) }
    await run('Считаем варианты…', async () => {
      const preview = await api.previewEvent(from.current.id, event)
      // Окно закрыли «назад» браузера, пока шёл расчёт: черновики никому не нужны.
      if (asking.value !== pending) return
      // Выбирать не из чего: единственный вариант — режим по умолчанию (отказов нет).
      const only = preview.options.length === 1 && !Object.keys(preview.refused).length
      if (only && !crewStep.value) return settle(from, await api.commitPlan(preview.options[0]!.plan.id), phraseOf(event))
      chosen.value = preview.options.find((option) => option.mode === preview.recommended)?.plan.id ?? null
      asking.value = { ...pending, preview }
    }, into, load)
    if (!asking.value?.preview) asking.value = null
  })
}

/**
 * «Кто поедет на аварию» (блок 74): диспетчер выбрал другого инженера — те же варианты заново, с ним
 * в событии. Пока считается, прежние варианты остаются в окне, полоса — над «Выбрать».
 */
async function pickEngineer(engineerId: string) {
  const pending = asking.value
  if (!pending?.preview || (pending.event.type !== 'add_request' && pending.event.type !== 'urgent_request')) return
  const event: PlanEvent = { ...pending.event, engineerId }
  const load = { title: 'Считаем варианты', sec: previewSec(event), where: 'replan' as const, pulse: eventRequests(event) }
  await run('Считаем варианты…', async () => {
    const preview = await api.previewEvent(pending.from.current.id, event)
    if (asking.value !== pending) return
    chosen.value = preview.options.find((option) => option.mode === preview.recommended)?.plan.id ?? null
    asking.value = { ...pending, event, preview }
  }, replanError, load)
}

/** «Выбрать» в «Кто поедет на аварию?»: другой инженер — варианты заново с ним, затем «Пересчёт дня». */
async function chooseCrew(engineerId: string | null) {
  const pending = asking.value
  const sent = pending?.preview?.emergencyCandidates.find((crew) => crew.recommended)?.engineerId ?? null
  if (engineerId && engineerId !== sent) await pickEngineer(engineerId)
  if (asking.value?.preview && !replanError.value) crewStep.value = false
}

/** «Выбрать»: черновик становится версией цепочки. Отказ (варианты устарели) — у кнопки. */
async function applyPreview() {
  const pending = asking.value
  const id = chosen.value
  if (!pending || !id) return
  replanError.value = null
  await run('Применяем вариант…', async () => {
    settle(pending.from, await api.commitPlan(id), phraseOf(pending.event))
    asking.value = null
  }, replanError)
}

/** «Назад»: черновики сервер удалит сам. */
function refuse() {
  asking.value = null
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
  // Правка ассистента ушла в событие: переоткрытая карточка не должна наложить её снова.
  cardEdit.value = null
  ask({ type: 'update_request', time, request })
}

function saveEngineer(engineer: Engineer, time: string) {
  ask({ type: 'update_engineer', time, engineer })
}

const REASSIGNED = 'Заявка успешно переназначена. Мы пересчитали маршрут инженера и связанные с ним заявки.'

/**
 * Переназначение (PLAN 6.16): сервер ставит заявку в лучшее место, `position: null`. Им же
 * пользуется ассистент (блок 61): полоса у него — не над кнопкой окна переназначения.
 */
function assign(
  requestId: string,
  engineerId: string,
  where: Load['where'] = 'reassign',
  done?: (next: Plan, previous: Plan, phrase: string) => void,
) {
  newVersion(
    'Переназначаем…',
    (current) => api.manual(current.id, requestId, engineerId, null),
    REASSIGNED,
    modalError,
    { title: 'Переназначаем', sec: MANUAL_SEC, where, pulse: [requestId] },
    done,
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
  }, modalError, { title: 'Считаем варианты', sec: compareSec(), where: 'variants' })
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
  // День начался заново (правки 81.3): время — на начало его смен.
  barOpen.value = false
  shown.value = 'optimized'
  startDay(chosen)
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

/**
 * Файлы, приложенные к ещё не отправленной фразе (блок 60). Живут здесь, как и переписка: окно
 * ассистента можно закрыть и открыть, не потеряв их. Загружаются сразу — к отправке у них уже id.
 */
const pending = ref<AssistantAttachment[]>([])

// Загрузка идёт мимо `run`: это не действие над днём, и `busy` на это время превратил бы
// кнопку под предложением ассистента в «Загружаем файл…», а скелетон ответа — в ложный.
const uploading = ref(false)
async function attach(files: File[]) {
  modalError.value = null
  uploading.value = true
  progress.start({ title: 'Загружаем файл', sec: ATTACH_SEC, where: 'attach' })
  try {
    pending.value.push(...(await Promise.all(files.map((file) => api.attach(file)))))
  } catch (problem) {
    modalError.value = apiError(problem)
  } finally {
    uploading.value = false
    progress.finish()
  }
}

function detach(id: string) {
  pending.value = pending.value.filter((file) => file.id !== id)
}

async function askAssistant(text: string) {
  const current = plan.value
  if (!current) return
  // Последние реплики уходят вместе с фразой: без них «какие у него заявки?» не к кому отнести.
  const history = said.value.slice(-HISTORY)
  const files = pending.value
  pending.value = []
  said.value.push({ from: 'me', text, files })
  await run('Ассистент думает…', async () => {
    const reply = await api.assist(current.id, text, time.value, history, files.map((file) => file.id))
    said.value.push({ from: 'bot', text: reply.text, reply, planId: current.id })
  }, modalError)
}

/** «Новый чат»: переписка и отказ прошлого запроса начинаются заново. */
function resetAssistant() {
  said.value = []
  pending.value = []
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
  const applied = (next: Plan, previous: Plan, phrase: string) => {
    message.applied = true
    message.changes = markRaw({ plan: next, previous, phrase })
  }
  // «Что будет, если…» (блок 61): день уже посчитан черновиком — применить его, а не считать заново.
  // Посчитан он от той версии, о которой спрашивали; ушёл день дальше — черновик о другом дне.
  if (reply.draftPlanId) {
    // Проверка — до `newVersion`: иначе «Изменить прошлое?» спросило бы отбросить версии ради
    // действия, которое всё равно не состоится.
    if (plan.value?.id !== message.planId) {
      modalError.value = 'План с тех пор изменился — спросите ассистента ещё раз'
      return
    }
    const id = reply.draftPlanId
    const phrase = reply.event ? phraseOf(reply.event) : REASSIGNED
    newVersion('Применяем вариант…', () => api.commitPlan(id), phrase, modalError, { title: 'Применяем вариант', sec: MANUAL_SEC, where: 'replan' }, applied)
  } else if (reply.reassign) assign(reply.reassign.requestId, reply.reassign.engineerId, 'replan', applied)
  else if (reply.event) applyEvent(reply.event, reply.mode ?? undefined, modalError, { where: 'replan' }, applied)
  else if (reply.draft) openModal({ kind: 'add-request', suggestion: reply.draft })
  else if (reply.engineer) openModal({ kind: 'add-engineer', preset: reply.engineer })
  else if (reply.kind === 'time') {
    // Смена вида, а не дня: ничего не пересчитывается, но следующие действия уйдут с этим временем.
    setTime(reply.time)
    message.applied = true
  } else if (reply.edit) {
    // Адрес и тип доделывает карточка заявки: окно ассистента уходит, чтобы её было видно.
    const edit = reply.edit
    const n = (cardEdit.value?.n ?? 0) + 1
    openModal(null)
    select(edit.request.id)
    cardEdit.value = { edit, n }
  }
}

/** Набор сменился — прежние планы к нему не относятся. */
function switched(loaded: Dataset) {
  epoch += 1
  loads.value += 1
  said.value = []
  pending.value = []
  dataset.value = loaded
  chains.baseline.value = []
  chains.optimized.value = []
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
  pulse.value = load?.pulse ?? null
  if (load) progress.start(load)
  try {
    await action()
  } catch (problem) {
    into.value = apiError(problem)
  } finally {
    busy.value = null
    pulse.value = null
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
  return open.kind === 'link' ? 2 : 1
})
const depth = computed(
  () =>
    +!!(selectedRequestId.value || selectedEngineerId.value) + modalDepth.value + +!!asking.value + +!!rewind.value + +restarting.value,
)

function closeTop() {
  if (restarting.value) restarting.value = false
  else if (rewind.value) rewind.value = null
  else if (asking.value) refuse()
  else if (modal.value?.kind === 'metrics' && modal.value.from === 'variants') openModal({ kind: 'variants' })
  else if (modal.value?.kind === 'link') openModal({ kind: 'rate', requestId: modal.value.requestId, draft: modal.value.draft })
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
  <!-- `#sheet-vars` — цель Teleport окон поверх окна: вне этой обёртки у листа нет `--sheet-h`. -->
  <div id="sheet-vars" :style="{ ...SHEET_VARS, '--sheet-h': mobile && barOpen && barHeight ? `${barHeight}px` : sheetHeight, '--sheet-footer': sheetFooter }">
    <div class="relative h-screen overflow-hidden max-md:h-dvh">
      <!-- `z-0` замыкает слои карты: её кнопки (`z-10`) не всплывают поверх развёрнутого листа. -->
      <PlanMap
        class="absolute inset-0 z-0"
        :dataset="dataset"
        :plan="previewPlan ?? plan"
        :selected="selectedRequestId"
        :focus="focus"
        :highlighted="highlighted"
        :pulsing="pulsing"
        :hovered-request="hoveredRequest"
        :inset="mobile ? { top: 20, left: 0, bottom: covered } : { top: 80, left: panelWidth, bottom: 0 }"
        :loads="loads"
        :mobile="mobile"
        :crew="playTracks"
        @select="select"
        @engineer="selectEngineer"
      />

      <!-- Полоса над картой прозрачна для касаний: на телефоне под ней справа линейка карты.
           Высокий лист закрывает карту, и пилюля времени легла бы на его поиск — она уходит, как ракета. -->
      <div
        class="pointer-events-none absolute top-3 right-3 left-3 z-[3] transition-[opacity,visibility] duration-200 [&>*]:pointer-events-auto"
        :class="{ 'max-md:invisible max-md:opacity-0': tall }"
      >
        <AppHeader
          class="max-md:hidden"
          :dataset="dataset"
          :plan="plan"
          :baseline="baseline"
          :optimized="optimized"
          :shown="shown"
          :time="time"
          @play="openPlayback"
          :summary="dayEnded"
          @summary="openModal({ kind: 'summary' })"
          @show="shown = $event"
          @variants="openVariants"
          @metrics="openMetrics(plan, 'header')"
          @error="error = $event"
          @dataset="openModal({ kind: 'dataset' })"
        />
        <!-- Полоса проигрывания (блоки 77 и 80, кадры 1527 и 1787): под шапкой, посередине карты справа
             от панели, до 813 px; уголок — под часами. -->
        <Transition name="bar">
          <div v-if="!mobile && barOpen && playMinute !== null" data-playback class="mt-3 flex" :class="playbarPlace === beside ? 'justify-center' : 'justify-end'" :style="playbarPlace">
            <PlaybackBar
              class="max-w-full"
              :mobile="false"
              :minutes="playMinute"
              :range="playRange"
              :playing="playing"
              :speed="speed"
              @seek="seekBar"
              @toggle="playing ? pause() : play()"
              @speed="speed = $event"
              @close="closePlayback"
              @restart="restarting = true"
            />
          </div>
        </Transition>
        <!-- Время дня в мобильной версии: шапки там нет, пилюля стоит над картой (блок 33). Справа —
             круглая «Итоги дня» (правки 82): кнопки шапки на телефоне нет. Обе — 48 px, белые, с тенью,
             как кнопка ассистента; серой плашки внутри пилюли нет. -->
        <div class="hidden w-fit gap-2 max-md:flex">
          <DayTime data-tour="time" class="!h-12 !min-h-12 !rounded-full !bg-panel !px-4 shadow-panel" :time="time" :disabled="!plan" @open="openPlayback" />
          <button
            class="pressable flex size-12 shrink-0 cursor-pointer items-center justify-center rounded-full border-0 bg-panel text-text shadow-panel disabled:cursor-default disabled:text-muted"
            :disabled="!dayEnded"
            :aria-label="dayEnded ? 'Итоги дня' : 'Итоги дня откроются, когда кончатся смены'"
            @click="openModal({ kind: 'summary' })"
          >
            <Icon name="flag" />
          </button>
        </div>
        <!-- Решение заказчика: строку «посчитано по прямой» оставить (docs/design.md, «Шапка»). -->
        <p v-if="approximate" class="small mt-1.5 w-fit rounded-xl bg-field px-3 py-1 text-muted max-md:!ml-0 max-md:!max-w-[calc(100%-64px)]" :style="beside">{{ approximate }}</p>
        <p
          v-if="error || (!modal && modalError)"
          class="error small mt-1.5 w-fit rounded-xl bg-field px-3 py-1 max-md:!ml-0 max-md:!max-w-[calc(100%-64px)]"
          :style="beside"
        >
          {{ error || modalError }}
        </p>
      </div>

      <!-- Полоса проигрывания на телефоне — лист «Выбор времени» над вкладками (кадр 1560). -->
      <Transition name="sheet-bar">
        <PlaybackBar
          v-if="mobile && barOpen && playMinute !== null"
          data-playback
          class="absolute inset-x-0 bottom-[var(--tabs-h)] z-[4]"
          mobile
          :minutes="playMinute"
          :range="playRange"
          :playing="playing"
          :speed="speed"
          @seek="seekBar"
          @toggle="playing ? pause() : play()"
          @speed="speed = $event"
          @close="closePlayback"
          @restart="restarting = true"
        />
      </Transition>

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
          'max-md:invisible': barOpen,
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
            :day="summary?.data.engineers.find((item) => item.engineerId === selectedEngineer!.id) ?? null"
            :rating="ratings[selectedEngineer.id] ?? selectedEngineer.rating ?? null"
            :busy="busy"
            :error="cardError"
            @back="closeCard"
            @select="pickInMenu"
            @event="ask"
            @save="saveEngineer"
            @reviews="openModal({ kind: 'reviews', engineerId: $event })"
          />
          <RequestDetails
            :time="time"
            v-else-if="selectedRequest"
            :key="`r${selectedRequest.id}`"
            :plan="plan"
            :request="selectedRequest"
            :busy="busy"
            :error="cardError"
            :suggestion="cardEdit"
            :ratings="ratings"
            :review="reviews[selectedRequest.id] ?? null"
            @back="closeCard"
            @event="ask"
            @save="saveRequest"
            @reassign="openModal({ kind: 'reassign', requestId: $event })"
            @add-engineer="openModal({ kind: 'add-engineer', preset: $event })"
            @rate="openModal({ kind: 'rate', requestId: $event })"
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
            :speeds="speeds"
            :ratings="ratings"
            :reviews="reviews"
            :wide="wide && !mobile"
            @request="pickInMenu"
            @engineer="selectEngineer"
            @hover="hovered = $event"
            @hover-request="hoveredRequest = $event"
            @seek="seekBar"
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
        @demo="(requests, crew) => newDataset(() => api.loadDemo(requests, crew, 'complexity'), DEMO_SEC)"
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
        :main="variants.find((item) => item.variant === 'emergency_first') ?? null"
        :choose="modal.from === 'variants' && variants.includes(modal.plan)"
        @back="openModal(modal.from === 'variants' ? { kind: 'variants' } : null)"
        @choose="chooseVariant"
      />
      <ReassignModal
        v-else-if="modal?.kind === 'reassign' && plan"
        :plan="plan"
        :request-id="modal.requestId"
        :time="time"
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
        :pending="pending"
        :uploading="uploading"
        :progress="progress.bar('attach')"
        :busy="busy"
        :error="modalError"
        :class="{ '!hidden': stacked }"
        @back="openModal(null)"
        @send="askAssistant"
        @reset="resetAssistant"
        @attach="attach"
        @detach="detach"
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
        :preset="modal.preset"
        :busy="busy"
        :error="modalError"
        @back="openModal(null)"
        @add="add"
      />
      <DaySummaryModal v-else-if="modal?.kind === 'summary'" :summary="dayTotals" :progress="progress.bar('summary')" @back="openModal(null)" />
      <ReviewsModal
        v-else-if="modal?.kind === 'reviews' && plan"
        :engineer-id="modal.engineerId"
        :rating="ratings[modal.engineerId] ?? null"
        @back="openModal(null)"
      />
      <RateModal
        v-else-if="modal?.kind === 'rate' && plan"
        :plan-id="plan.id"
        :request-id="modal.requestId"
        :draft="modal.draft ?? null"
        @back="openModal(null)"
        @link="(link, draft) => openModal({ kind: 'link', requestId: (modal as { requestId: string }).requestId, link, draft })"
        @saved="openModal(null), loadReviews()"
      />
      <ClientLinkModal
        v-else-if="modal?.kind === 'link'"
        :link="modal.link"
        @back="closeTop"
      />
      <PlanUpdatedModal
        v-else-if="modal?.kind === 'updated'"
        :plan="modal.plan"
        :previous="modal.previous"
        :phrase="modal.phrase"
        @back="openModal(modal.from === 'assistant' ? { kind: 'assistant' } : null)"
      />
    </Transition>
    <EmergencyCrewModal
      v-if="asking && crewStep"
      :plan="asking.from.current"
      :preview="asking.preview"
      :busy="busy"
      :progress="progress.bar('replan')"
      :error="replanError"
      @back="refuse"
      @choose="chooseCrew"
    />
    <ReplanModal
      v-else-if="asking"
      v-model:chosen="chosen"
      :event="asking.event"
      :plan="asking.from.current"
      :preview="asking.preview"
      :busy="busy"
      :progress="progress.bar('replan')"
      :error="replanError"
      @back="refuse"
      @apply="applyPreview"
    />
    <RewindModal v-if="rewind" :time="time" :dropped="rewind.dropped" @back="rewind = null" @go="confirmRewind" />
    <!-- ⟲ на полосе времени — только с подтверждением (правки 82). -->
    <Modal v-if="restarting" title="К началу дня" @back="restarting = false">
      <p class="m-0">Вы уверены, что хотите переместиться к началу дня?</p>
      <template #footer>
        <div class="grid grid-cols-2 gap-2.5">
          <button class="btn wide" @click="restarting = false">Отмена</button>
          <button class="btn primary wide" @click="restart"><Icon name="check" /> Подтвердить</button>
        </div>
      </template>
    </Modal>
    <OnboardingTour v-if="touring" :mobile="mobile" @tab="listTab = $event" @done="endTour" />
  </div>
</template>
