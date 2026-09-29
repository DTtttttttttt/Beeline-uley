// Прогресс долгого действия — полоса над кнопкой, которой его запустили (ProgressBar). Сервер
// отвечает одним куском, без промежуточных ответов, поэтому полоса идёт по оценке. Длительность
// почти предсказуема: её задают лимиты солвера (backend/app/planning/variants.py,
// REPLAN_TIME_LIMIT_SEC), а у каждого плана есть фактическое `computeSec` — по нему и оценивается
// следующий такой же расчёт.
import type { Plan, PlanEvent, ReplanMode } from '~/composables/useApi'

/**
 * Что считается и над какой кнопкой полоса: «Использовать демо-набор» / «Загрузить и рассчитать»,
 * «Выбрать» в вариантах, «Пересчитать», «Переназначить», «Перенести» в строке списка заявок.
 */
export interface Load {
  title: string
  sec: number
  where: 'dataset' | 'variants' | 'replan' | 'reassign' | 'defer'
  requestId?: string // у переноса из списка — чья строка
}

/** Что рисует полоса. */
export interface Bar {
  title: string
  value: number
  remaining: number | null
  requestId?: string
}

// Оценки без истории, с. Варианты — шесть расчётов по очереди, около 40 с (docs/overview.md, 4.4);
// событие — лимит перепланирования 10 с плюс объяснения, прогноз и линии маршрутов.
const COMPARE_SEC = 45
const REPLAN_SEC = 12
export const INSERT_SEC = 3
export const MANUAL_SEC = 3
export const DEMO_SEC = 2
// Импорт файла ищет адреса без координат геокодером — дольше демо-пары.
export const IMPORT_SEC = 5

// Быстрый ответ не мигает полосой; после ответа она успевает дойти до конца.
const SHOW_AFTER_MS = 250
const HIDE_AFTER_MS = 300
const TICK_MS = 100
// Все оценки растянуты на 20 %: полоса, упёршаяся в «почти готово», хуже чуть отстающей.
const SLOWER = 1.2

/**
 * Доля к моменту `t` при ожидаемых `expected` секундах: до оценки линейно до 90 %, дальше
 * асимптотически к 99 % — полоса не стоит на месте и не упирается в конец раньше ответа.
 */
export function share(t: number, expected: number): number {
  if (t < expected) return (0.9 * t) / expected
  return 0.9 + 0.09 * (1 - Math.exp(-(t - expected) / expected))
}

/** Расчёт вариантов: столько, сколько занял прошлый такой же расчёт, а без него — 45 с. */
export function compareSec(variants: Plan[]): number {
  if (!variants.length) return COMPARE_SEC
  // Сверх `computeSec` — таблицы переездов и ответ из шести полных планов.
  return variants.reduce((sum, plan) => sum + plan.computeSec, 0) + 2
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
 * Пересчёт дня по событию. Встраивание — секунды. Остаток дня считается тем же лимитом, что и
 * последняя версия цепочки, посчитанная солвером (событие наследует её настройку, `_setup_of`),
 * поэтому её `computeSec` — лучшая оценка; ручная правка и встраивание солвер не запускают.
 */
export function eventSec(event: PlanEvent, mode: ReplanMode | undefined, chain: Plan[]): number {
  if (inserting(event, mode)) return INSERT_SEC
  const solved = chain.findLast((plan) => plan.replanMode !== 'insert' && plan.algorithm !== 'manual')
  return solved ? solved.computeSec + 1 : REPLAN_SEC
}

export function useProgress() {
  const current = ref<Load | null>(null)
  const value = ref(0)
  // Сколько секунд осталось по оценке; оценка вышла — `null`, «почти готово».
  const remaining = ref<number | null>(null)
  const visible = ref(false)
  let started = 0
  let tick: ReturnType<typeof setInterval> | undefined
  let show: ReturnType<typeof setTimeout> | undefined
  let hide: ReturnType<typeof setTimeout> | undefined

  function update() {
    const expected = current.value?.sec ?? 1
    const t = (performance.now() - started) / 1000
    value.value = share(t, expected)
    remaining.value = t < expected ? Math.max(1, Math.ceil(expected - t)) : null
  }

  function stop() {
    clearInterval(tick)
    clearTimeout(show)
    clearTimeout(hide)
  }

  function start(load: Load) {
    stop()
    current.value = { ...load, sec: Math.max(load.sec * SLOWER, 0.5) }
    started = performance.now()
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
    return { title: load.title, value: value.value, remaining: remaining.value, requestId: load.requestId }
  }

  onBeforeUnmount(stop)
  return { current, value, remaining, visible, start, finish, bar }
}
