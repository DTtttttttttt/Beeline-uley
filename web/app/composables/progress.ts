// Прогресс долгого действия — полоса над кнопкой, которой его запустили (ProgressBar). Сервер
// отвечает одним куском, без промежуточных ответов, поэтому полоса идёт по оценке. Длительность
// для вариантов фиксирована на 60 с; для пересчёта по событию используется фактическое
// `computeSec` предыдущего пересчёта.
import { HOLD_MS, PHRASES, phraseIndex } from '~/composables/phrases'
import type { Plan, PlanEvent, ReplanMode } from '~/composables/useApi'

/**
 * Что считается и над какой кнопкой полоса: «Использовать демо-набор» / «Загрузить и рассчитать»,
 * «Выбрать» в вариантах, «Пересчитать», «Переназначить», «Перенести» в строке списка заявок.
 */
export interface Load {
  title: string
  sec: number
  where: 'dataset' | 'variants' | 'replan' | 'reassign' | 'defer' | 'attach' | 'summary'
  requestId?: string // у переноса из списка — чья строка
  pulse?: string[] // заявки события или переназначения: их булавки мигают на карте, пока идёт расчёт
}

/** Что рисует полоса: `title` — название действия для диктора, `phrase` — сменяющаяся подпись (блок 58). */
export interface Bar {
  title: string
  phrase: string
  value: number
  remaining: number | null
  requestId?: string
}

// Варианты — фиксированная оценка 60 с. Остальные оценки без истории — по замерам:
// пересчёт остатка дня по событию — 5 с, переназначение — 2 с, демо-набор — доли секунды.
const COMPARE_SEC = 60
const REPLAN_SEC = 6
export const INSERT_SEC = 3
export const MANUAL_SEC = 2
export const DEMO_SEC = 1
// Импорт файла ищет адреса без координат геокодером — дольше демо-пары.
export const IMPORT_SEC = 5
// Вложение ассистента — файл до 1 МБ: чтение и сводка на сервере, без геокодера (блок 60).
export const ATTACH_SEC = 1

/**
 * «Итоги дня» после перемотки на вечер (блок 82): часы закрывают незакрытые работы по одной версии
 * на работу, ~1,3 с на версию (замер 20.10.2026, «Восток», штатный состав, без Valhalla: 51 версия
 * за 67 с), плюс сводка.
 */
export function summarySec(plan: Plan): number {
  const open = plan.routes.reduce((sum, route) => sum + route.stops.filter((stop) => !plan.closed[stop.requestId]).length, 0)
  return open * 1.3 + 1
}

// Быстрый ответ не мигает полосой; после ответа она успевает дойти до конца.
const SHOW_AFTER_MS = 250
const HIDE_AFTER_MS = 300
const TICK_MS = 100
// Остальные оценки растянуты на 20 %; у расчёта вариантов фиксированная оценка 60 с.
const SLOWER = 1.2

/**
 * Доля к моменту `t` при ожидаемых `expected` секундах: до оценки линейно до 90 %, дальше
 * асимптотически к 99 % — полоса не стоит на месте и не упирается в конец раньше ответа.
 */
export function share(t: number, expected: number): number {
  if (t < expected) return (0.9 * t) / expected
  return 0.9 + 0.09 * (1 - Math.exp(-(t - expected) / expected))
}

/** Расчёт вариантов после загрузки набора: фиксированная оценка, независимо от истории поиска. */
export function compareSec(): number {
  return COMPARE_SEC
}

/**
 * Встраивается ли заявка события — повтор `replan.default_mode` (backend/app/planning/replan.py):
 * без `mode` обычная заявка встраивается, срочная и остальные события пересчитывают остаток дня.
 */
function inserting(event: PlanEvent, mode: ReplanMode | undefined): boolean {
  if (mode) return mode === 'insert'
  if (event.type !== 'add_request' && event.type !== 'urgent_request') return false
  const urgent =
    event.urgent ??
    (event.request.urgent || event.request.workPriority === 'emergency' || event.type === 'urgent_request')
  return !urgent
}

/**
 * Пересчёт дня по событию. Встраивание — секунды. Остаток дня считается пределом
 * перепланирования (REPLAN_SOLUTION_LIMIT) и у варианта сравнения тоже, поэтому лучшая оценка —
 * `computeSec` прошлого пересчёта по событию в цепочке, а не расчёта дня с нуля. Ручная правка и
 * встраивание солвер не запускают. События в кэш решений не пишутся, их `computeSec` честный.
 */
export function eventSec(event: PlanEvent, mode: ReplanMode | undefined, chain: Plan[]): number {
  if (inserting(event, mode)) return INSERT_SEC
  const solved = chain.findLast(
    (plan) => !!plan.replanMode && plan.replanMode !== 'insert' && plan.algorithm !== 'manual',
  )
  return solved ? solved.computeSec + 1 : REPLAN_SEC
}

/**
 * Предпросмотр события (блок 53): варианты считаются параллельно, и ждать приходится самый долгий —
 * пересчёт всего дня, у аварии ещё и пробные расписания «Кто поедет». Замер 19.10.2026 («Восток»,
 * штатный состав, 12:00): авария — 36 с, обычная заявка — 26 с, новый инженер — 13 с, отмена — 9 с.
 * История цепочки здесь не помогает: её пересчёт по событию — один режим, а не все варианты.
 */
export function previewSec(event: PlanEvent): number {
  if (event.type !== 'add_request' && event.type !== 'urgent_request') return 12
  return event.request.workPriority === 'emergency' ? 37 : 27
}

export function useProgress() {
  const current = ref<Load | null>(null)
  const value = ref(0)
  // Сколько секунд осталось по оценке; оценка вышла — `null`, «почти готово».
  const remaining = ref<number | null>(null)
  const visible = ref(false)
  // Номер фразы из PHRASES[where] и когда она появилась: фразы идут только вперёд.
  const phrase = ref(0)
  let since = 0
  let started = 0
  let tick: ReturnType<typeof setInterval> | undefined
  let show: ReturnType<typeof setTimeout> | undefined
  let hide: ReturnType<typeof setTimeout> | undefined

  function update() {
    const expected = current.value?.sec ?? 1
    const t = (performance.now() - started) / 1000
    value.value = share(t, expected)
    remaining.value = t < expected ? Math.max(1, Math.ceil(expected - t)) : null
    const target = current.value ? phraseIndex(t, expected, PHRASES[current.value.where].length) : 0
    const now = performance.now()
    if (target > phrase.value && now - since >= HOLD_MS) {
      phrase.value = target
      since = now
    }
  }

  function stop() {
    clearInterval(tick)
    clearTimeout(show)
    clearTimeout(hide)
  }

  /** `since` — когда действие на самом деле началось, если раньше показа полосы (фоновая загрузка). */
  function start(load: Load, since = performance.now()) {
    stop()
    current.value = { ...load, sec: Math.max(load.sec * (load.where === 'variants' ? 1 : SLOWER), 0.5) }
    started = since
    phrase.value = 0
    since = started
    update()
    tick = setInterval(update, TICK_MS)
    // Полоса прошлого шага (загрузка набора → расчёт вариантов) не гаснет, а продолжается.
    if (!visible.value) show = setTimeout(() => (visible.value = true), SHOW_AFTER_MS)
  }

  function finish() {
    stop()
    if (!visible.value) {
      current.value = null
      return
    }
    value.value = 1
    remaining.value = null
    hide = setTimeout(() => {
      visible.value = false
      current.value = null
    }, HIDE_AFTER_MS)
  }

  /** Полоса для кнопки `where`, если сейчас считается её действие. */
  function bar(where: Load['where']): Bar | null {
    const load = current.value
    if (!visible.value || load?.where !== where) return null
    return {
      title: load.title,
      phrase: PHRASES[where][phrase.value]!,
      value: value.value,
      remaining: remaining.value,
      requestId: load.requestId,
    }
  }

  onBeforeUnmount(stop)
  return { current, value, remaining, visible, start, finish, bar }
}
