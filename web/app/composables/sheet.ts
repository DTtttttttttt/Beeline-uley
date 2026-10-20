/**
 * Лист мобильной версии (docs/design.md, «Мобильная версия»). Лист на экране один: список с
 * карточками и окна, которые на телефоне лежат тем же листом, делят одно положение из трёх.
 * Лист идёт за пальцем из любого своего места. На отпускании он встаёт в ближайшее положение,
 * а быстрый бросок доводит его до следующего.
 */
import { MOBILE_QUERY } from '~/composables/useApi'

export const SHEETS = ['collapsed', 'half', 'full'] as const
export type Sheet = (typeof SHEETS)[number]

// Видимая высота листа над вкладками. Свёрнутый лист показывает только поиск (кадр 1507),
// наполовину раскрытый занимает 52 % экрана, раскрытый — всё до 16 px от верха.
const COLLAPSED = 76
const HALF = 0.52
const TOP_GAP = 16
const TABS = 64
// Подвал гаснет на последних 120 px пути к свёрнутому: ниже 196 px он ложится на шапку —
// шапка окна 72 px, подвал с кнопкой столько же.
const FOOTER_FADE = 120

/** Те же числа для вёрстки: сервер рисует лист без JS, поэтому высоты — CSS. */
export const SHEET_VARS = {
  '--tabs-h': `${TABS}px`,
  '--sheet-collapsed': `${COLLAPSED}px`,
  '--sheet-half': `${HALF * 100}dvh`,
  '--sheet-full': `calc(100dvh - ${TOP_GAP + TABS}px)`,
}

function heights(): Record<Sheet, number> {
  const screen = window.innerHeight
  return { collapsed: COLLAPSED, half: Math.round(screen * HALF), full: screen - TOP_GAP - TABS }
}

/**
 * Положение листа и высота под пальцем. `--sheet-h` — видимая высота листа над вкладками: во
 * время жеста — пиксели пальца, иначе переменная положения. По ней встают лист, окна и ракета.
 */
export function useSheet() {
  const state = useState<Sheet>('sheet', () => 'half')
  const dragged = useState<number | null>('sheet-dragged', () => null)
  const height = computed(() => (dragged.value === null ? `var(--sheet-${state.value})` : `${dragged.value}px`))
  /**
   * Непрозрачность липкого подвала (`--sheet-footer`). Свёрнутый лист подвала не показывает: кнопка
   * легла бы на шапку. Поэтому по пути к свёрнутому подвал гаснет, а при подъёме из него
   * проявляется, а не выскакивает целиком в начале или в конце жеста.
   */
  const footer = computed(() => {
    if (dragged.value === null) return state.value === 'collapsed' ? 0 : 1
    return Math.min(1, Math.max(0, (dragged.value - COLLAPSED) / FOOTER_FADE))
  })
  /** Лист выше половины экрана: ракете над ним места нет. */
  const tall = computed(() => (dragged.value === null ? state.value === 'full' : dragged.value > heights().half + 40))
  return { state, dragged, height, tall, footer }
}

/** Прокрутка по вертикали, внутри которой начался жест: от неё зависит, листать её или тянуть лист. */
function scrollerOf(target: EventTarget | null, sheet: HTMLElement): HTMLElement | null {
  for (let node = target as HTMLElement | null; node && node !== sheet.parentElement; node = node.parentElement) {
    const overflow = getComputedStyle(node).overflowY
    if ((overflow === 'auto' || overflow === 'scroll') && node.scrollHeight > node.clientHeight) return node
  }
  return null
}

/**
 * Жест листа на элементе `sheet`: палец — из любого места, мышь — за ручку (`startMouse`).
 *
 * Тянуть лист или листать содержимое, решает первое движение. Горизонтальное — всегда прокрутка:
 * у чипов своя лента. Вертикальное двигает лист, если он не раскрыт целиком. Раскрытый лист
 * листается, и только движение вниз с верха прокрутки снова берёт лист. Решение нужно принять на
 * первом `touchmove`: не отменённое, оно отдаёт жест прокрутке браузера, и дальше её не остановить.
 */
export function useSheetDrag(sheet: Readonly<Ref<HTMLElement | null>>) {
  const { state, dragged } = useSheet()

  let gesture: {
    x: number
    y: number
    from: number
    scroller: HTMLElement | null
    mode: 'sheet' | 'native' | null
    last: [number, number][] // [время, высота] — скорость броска
  } | null = null

  function begin(x: number, y: number, target: EventTarget | null) {
    const element = sheet.value
    if (!element || !window.matchMedia(MOBILE_QUERY).matches) return (gesture = null)
    // Высоту берём у листа на экране, а не из положения: жест мог прервать анимацию на полпути.
    const from = window.innerHeight - TABS - element.getBoundingClientRect().top
    gesture = { x, y, from, scroller: scrollerOf(target, element), mode: null, last: [] }
  }

  /** Возвращает `true`, если жест забрал лист и событие надо отменить. */
  function follow(x: number, y: number): boolean {
    if (!gesture) return false
    const dx = x - gesture.x
    const dy = y - gesture.y
    if (!gesture.mode) {
      if (Math.abs(dx) < 6 && Math.abs(dy) < 6) return false
      const scrollable = state.value === 'full' && gesture.scroller && (dy < 0 || gesture.scroller.scrollTop > 0)
      gesture.mode = Math.abs(dx) > Math.abs(dy) || scrollable ? 'native' : 'sheet'
    }
    if (gesture.mode !== 'sheet') return false
    const { collapsed, full } = heights()
    const wanted = gesture.from - dy
    // За крайними положениями лист идёт втрое туже, чем палец: видно, что дальше некуда.
    const height = wanted < collapsed ? collapsed - (collapsed - wanted) / 3 : wanted > full ? full + (wanted - full) / 3 : wanted
    dragged.value = Math.round(height)
    gesture.last = [...gesture.last.slice(-4), [performance.now(), height]]
    return true
  }

  function finish() {
    const current = gesture
    gesture = null
    if (current?.mode !== 'sheet' || dragged.value === null) return
    // Отпустили медленно — ближайшее положение; бросили — следующее по ходу пальца, через одно не перескакивает.
    const [first, last] = [current.last[0]!, current.last.at(-1)!]
    const speed = last[0] > first[0] ? (last[1] - first[1]) / (last[0] - first[0]) : 0 // px/мс, «+» — вверх
    const height = dragged.value
    const levels = heights()
    const ahead = SHEETS.filter((name) => (speed > 0 ? levels[name] > height : levels[name] < height))
    state.value =
      Math.abs(speed) > 0.4 && ahead.length
        ? speed > 0 ? ahead[0]! : ahead.at(-1)!
        : SHEETS.reduce((best, name) => (Math.abs(levels[name] - height) < Math.abs(levels[best] - height) ? name : best))
    dragged.value = null
    // Лист двигали, а не нажимали: клик по строке или кнопке под пальцем не должен сработать.
    const swallow = (event: Event) => event.stopPropagation()
    window.addEventListener('click', swallow, { capture: true, once: true })
    setTimeout(() => window.removeEventListener('click', swallow, { capture: true }), 350)
  }

  function cancel() {
    gesture = null
    dragged.value = null
  }

  const onTouchStart = (event: TouchEvent) =>
    event.touches.length === 1 ? begin(event.touches[0]!.clientX, event.touches[0]!.clientY, event.target) : cancel()
  const onTouchMove = (event: TouchEvent) => {
    if (follow(event.touches[0]!.clientX, event.touches[0]!.clientY) && event.cancelable) event.preventDefault()
  }

  /** Мышь тянет лист только за ручку: из любого места она выделяла бы текст и жала кнопки. */
  function startMouse(event: PointerEvent) {
    if (event.pointerType !== 'mouse') return
    begin(event.clientX, event.clientY, null)
    const move = (next: PointerEvent) => follow(next.clientX, next.clientY)
    const up = () => {
      window.removeEventListener('pointermove', move)
      finish()
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up, { once: true })
  }

  let attached: HTMLElement | null = null
  function attach(element: HTMLElement | null) {
    if (attached) {
      attached.removeEventListener('touchstart', onTouchStart)
      attached.removeEventListener('touchmove', onTouchMove)
      attached.removeEventListener('touchend', finish)
      attached.removeEventListener('touchcancel', cancel)
    }
    attached = element
    if (!element) return
    element.addEventListener('touchstart', onTouchStart, { passive: true })
    // Не пассивный: отменой первого движения лист забирает жест у прокрутки.
    element.addEventListener('touchmove', onTouchMove, { passive: false })
    element.addEventListener('touchend', finish)
    element.addEventListener('touchcancel', cancel)
  }
  onMounted(() => watch(sheet, attach, { immediate: true }))
  onBeforeUnmount(() => {
    attach(null)
    cancel()
  })

  /** Касание ручки без жеста — следующее положение по кругу. */
  function cycle() {
    state.value = SHEETS[(SHEETS.indexOf(state.value) + 1) % SHEETS.length]!
  }

  return { startMouse, cycle }
}
