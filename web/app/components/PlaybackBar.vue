<script setup lang="ts">
// Полоса проигрывания дня (блоки 63, 77 и 80, кадры 1527, 1787; на телефоне — лист «Выбор времени»,
// кадр 1560): ▶/⏸, шкала смены делениями с жёлтой меткой и временем над ней, скорость списком,
// ⟲ — к началу дня, ✕ — закрыть. Время ставит страница: шкала двигает время дня из шапки, и
// статусы со списками идут за ним, как при перемотке (блок 33). Жёлтое время над меткой — поле:
// время дня вручную вводится здесь (в шапке осталась только плашка).
import { clockMinutes, clockOf } from '~/composables/useApi'

const props = defineProps<{
  minutes: number // целая минута дня: дробную полоса не показывает
  range: [number, number]
  playing: boolean
  speed: number
  mobile: boolean
}>()

const emit = defineEmits<{
  seek: [minutes: number]
  toggle: []
  speed: [speed: number]
  close: []
  // ⟲ — к началу дня: страница сначала спрашивает подтверждение (правки 82).
  restart: []
}>()

// Минут дня за секунду; «Реальное время» — минута дня за минуту.
const SPEEDS = [
  { value: 1 / 60, label: 'Реальное время' },
  { value: 1, label: '1 мин/с' },
  { value: 5, label: '5 мин/с' },
  { value: 10, label: '10 мин/с' },
  { value: 20, label: '20 мин/с' },
]
const speedLabel = computed(() => SPEEDS.find((item) => item.value === props.speed)?.label ?? `${props.speed} мин/с`)

// Штрихи шкалы (Figma «Frame 1735», правки 82): штрих на каждые 15 минут дня, на ровном часе —
// длинный (22 px), остальные — 18 px, все по 2 px. На компьютере они растянуты на всю ширину
// шкалы, и каждый встаёт на целый пиксель: на дробном штрихи выглядели разной толщины. На
// телефоне шаг — 18 px, втрое шире макета (правки 82): метка стоит посередине, едет сама шкала.
const STEP = 15
const MOBILE_PX = 18
const scale = useTemplateRef<HTMLElement>('scale')
const width = ref(0)
const marks = computed(() => {
  const [from, to] = props.range
  const first = Math.ceil(from / STEP) * STEP
  return Array.from({ length: Math.max(0, Math.floor((to - first) / STEP) + 1) }, (_, i) => first + i * STEP)
})
// Где на шкале минута дня: компьютер — доля ширины, телефон — 6 px на 15 минут от начала дня.
function place(minute: number): number {
  const [from, to] = props.range
  if (props.mobile) return Math.round(((minute - from) / STEP) * MOBILE_PX)
  return to > from ? Math.round(((minute - from) / (to - from)) * Math.max(0, width.value - 2)) : 0
}
const ticks = computed(() => marks.value.map((minute) => ({ minute, major: minute % 60 === 0, at: place(minute) })))
const cursor = computed(() => place(props.minutes))
// Телефон: шкала сдвинута так, чтобы метка была посередине.
const shift = computed(() => (props.mobile ? Math.round(width.value / 2) - 1 - cursor.value : 0))

// Перетаскивание шкалы на телефоне: под неподвижной меткой едет день, 18 px — 15 минут.
let dragFrom: { x: number; minutes: number } | null = null
function grab(event: PointerEvent) {
  dragFrom = { x: event.clientX, minutes: props.minutes }
  ;(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId)
}
function drag(event: PointerEvent) {
  if (!dragFrom) return
  const [from, to] = props.range
  const minutes = dragFrom.minutes - ((event.clientX - dragFrom.x) / MOBILE_PX) * STEP
  emit('seek', Math.round(Math.min(to, Math.max(from, minutes))))
}
function release() {
  dragFrom = null
}

/**
 * Лист на телефоне ведёт себя, как окна (правки 82): за ручку его тянут вниз — он сворачивается до
 * строки «‹ Выбор времени», вверх — разворачивается; идёт за пальцем, на отпускании встаёт в
 * ближайшее положение, нажатие на ручку переключает. Положений два: лист невысокий.
 */
const FOLDED = 74 // поля 14 + строка заголовка 46 + 14; что ниже, у свёрнутого листа гаснет
const folded = ref(false)
const full = ref(0) // высота развёрнутого листа — по содержимому
const pulled = ref<number | null>(null) // высота под пальцем
const sheetHeight = computed(() => (full.value ? `${pulled.value ?? (folded.value ? FOLDED : full.value)}px` : undefined))
// Содержимое режется только пока лист свёрнут, тянется или едет: развёрнутому нужен список
// скоростей, который раскрывается над ним.
const moving = ref(false)
watch(folded, () => (moving.value = true))
const clipped = computed(() => folded.value || moving.value || pulled.value !== null)
function pull(down: PointerEvent) {
  const handle = down.currentTarget as HTMLElement
  handle.setPointerCapture(down.pointerId)
  const y0 = down.clientY
  const from = folded.value ? FOLDED : full.value
  let moved = false
  const move = (event: PointerEvent) => {
    const dy = event.clientY - y0
    if (!moved && Math.abs(dy) < 4) return
    moved = true
    pulled.value = Math.min(full.value, Math.max(FOLDED, from - dy))
  }
  const up = () => {
    handle.removeEventListener('pointermove', move)
    handle.removeEventListener('pointerup', up)
    handle.removeEventListener('pointercancel', up)
    folded.value = moved && pulled.value !== null ? pulled.value < (FOLDED + full.value) / 2 : !folded.value
    pulled.value = null
  }
  handle.addEventListener('pointermove', move)
  handle.addEventListener('pointerup', up)
  handle.addEventListener('pointercancel', up)
}

// Уголок сверху смотрит на часы в шапке (кадр 1787): его место — под их серединой.
const bar = useTemplateRef<HTMLElement>('bar')
const notch = ref<number | null>(null)
function aim() {
  width.value = scale.value?.offsetWidth ?? 0
  if (props.mobile && bar.value) full.value = bar.value.scrollHeight
  const clock = [...document.querySelectorAll<HTMLElement>('[data-tour="time"]')].find((item) => item.offsetParent)
  const box = bar.value?.getBoundingClientRect()
  if (!clock || !box || props.mobile) return (notch.value = null)
  const middle = clock.getBoundingClientRect().left + clock.offsetWidth / 2 - box.left
  notch.value = middle > 36 && middle < box.width - 36 ? middle : null
}
let observer: ResizeObserver | undefined
onMounted(() => {
  aim()
  observer = new ResizeObserver(aim)
  if (bar.value) observer.observe(bar.value)
  // Шкала меняет ширину и без полосы — вместе с подписью скорости (правки 82).
  if (scale.value) observer.observe(scale.value)
})
onBeforeUnmount(() => observer?.disconnect())

// Список скоростей: закрывается выбором, кликом мимо полосы и Escape. На компьютере он висит под
// полосой у её правого края (кадр 1527), поэтому «мимо» — мимо всей полосы.
const open = ref(false)
function outside(event: PointerEvent) {
  if (!bar.value?.contains(event.target as Node)) open.value = false
}
function escape(event: KeyboardEvent) {
  if (event.key === 'Escape') open.value = false
}
watch(open, (now) => {
  const method = now ? 'addEventListener' : 'removeEventListener'
  document[method]('pointerdown', outside)
  document[method]('keydown', escape)
})
onBeforeUnmount(() => (open.value = false))
function pick(value: number) {
  emit('speed', value)
  open.value = false
}
</script>

<template>
  <div
    ref="bar"
    class="flex bg-panel"
    :class="
      mobile
        ? ['flex-col rounded-t-[36px] px-3.5 pt-3.5 pb-3.5 shadow-panel', { 'overflow-hidden': clipped, 'transition-[height] duration-200': pulled === null }]
        : 'relative items-center gap-3.5 rounded-full p-3.5 drop-shadow-[0_2px_5px_rgb(40_48_63_/_25%)]'
    "
    :style="mobile ? { height: sheetHeight } : undefined"
    @transitionend.self="moving = false"
  >
    <svg v-if="notch !== null" class="absolute -top-[7px] h-2 w-5 -translate-x-1/2" :style="{ left: `${notch}px` }" viewBox="0 0 20 8" aria-hidden="true">
      <path d="M9.79452 0L10.405 0.789659C12.0431 2.9083 14.0899 4.6764 16.4241 5.98908L20 8H0L3.21467 6.13784C5.6477 4.72846 7.75621 2.82189 9.40261 0.542562L9.79452 0Z" fill="white" />
    </svg>
    <!-- Ручка листа — та же, что у окон на телефоне (правки 82). -->
    <SheetHandle v-if="mobile" @pointerdown="pull" />
    <div v-if="mobile" class="flex items-center gap-2">
      <button class="round" title="Закрыть" aria-label="Закрыть проигрывание" @click="emit('close')"><Icon name="back" /></button>
      <h2 class="m-0 mr-[52px] flex-1 text-center text-xl font-medium">Выбор времени</h2>
    </div>
    <button v-if="!mobile" class="round" :aria-label="playing ? 'Пауза' : 'Играть'" :title="playing ? 'Пауза' : 'Играть'" @click="emit('toggle')">
      <Icon :name="playing ? 'pause' : 'play'" />
    </button>

    <!-- Шкала (кадры 1527, 1560; Figma «Frame 1735»): штрих 2 px на каждые 15 минут, часовой — 22 px,
         прочие — 18; позади метки штрихи тёмные, впереди серые. Метка — 26 px, время над ней жёлтым.
         Компьютер: поверх — невидимый ползунок (перетаскивание и клавиатура), поле времени — над ним.
         Телефон (кадр 1560): метка посередине, шкала едет под ней и гаснет к краям листа. -->
    <!-- На компьютере шкала — 504 px (Figma «Frame 1735»): подпись скорости меняет ширину всей
         полосы, а не шкалы (правки 82); полосе не хватает места — шкала ужимается. -->
    <div class="relative min-w-0 transition-opacity duration-200" :class="[mobile ? '-mx-3.5 mt-1 mb-3.5 flex-1 pt-[18px]' : 'w-[504px] shrink py-2', { 'opacity-0': mobile && folded }]">
      <!-- На компьютере время лежит в верхнем поле полосы (кадр 1527: полоса 72 px, шкала по её середине). -->
      <TimeInput
        class="absolute z-[1] w-[38px] -translate-x-1/2 border-0 bg-transparent p-0 text-center font-[inherit] text-sm leading-[13px] text-accent tabular-nums outline-0"
        :class="mobile ? 'top-0' : '-top-[9px]'"
        :style="{ left: `clamp(19px, ${cursor + shift + 1}px, calc(100% - 19px))` }"
        :model-value="clockOf(minutes)"
        aria-label="Время дня"
        title="Время дня — можно ввести вручную"
        @update:model-value="emit('seek', clockMinutes($event))"
      />
      <div
        ref="scale"
        class="relative h-7"
        :class="{ 'touch-none overflow-hidden [mask-image:linear-gradient(90deg,transparent,#000_28px,#000_calc(100%-28px),transparent)]': mobile }"
        aria-hidden="true"
        @pointerdown="mobile && grab($event)"
        @pointermove="drag"
        @pointerup="release"
        @pointercancel="release"
      >
        <span
          v-for="tick in ticks"
          :key="tick.minute"
          class="absolute top-1/2 w-0.5 -translate-y-1/2 rounded-full"
          :class="[tick.major ? 'h-[22px]' : 'h-[18px]', tick.minute <= minutes ? (tick.major ? 'bg-text' : 'bg-text/80') : tick.major ? 'bg-text/70' : 'bg-[#c5c9cc]']"
          :style="{ left: `${tick.at + shift}px` }"
        />
        <span class="absolute top-px h-[26px] w-0.5 rounded-full bg-accent" :style="{ left: `${cursor + shift}px` }" />
      </div>
      <input
        v-if="!mobile"
        class="absolute inset-0 size-full cursor-pointer opacity-0"
        type="range"
        :min="range[0]"
        :max="range[1]"
        step="1"
        :value="minutes"
        aria-label="Время дня"
        :aria-valuetext="clockOf(minutes)"
        @input="emit('seek', Number(($event.target as HTMLInputElement).value))"
      >
    </div>

    <!-- Справа от шкалы — 12 px, между кнопками — 8 (кадр 1527). -->
    <div class="flex items-center transition-opacity duration-200" :class="[mobile ? 'gap-2.5' : '-ml-0.5 gap-2', { 'opacity-0': mobile && folded }]">
      <button v-if="mobile" class="round" :aria-label="playing ? 'Пауза' : 'Играть'" :title="playing ? 'Пауза' : 'Играть'" @click="emit('toggle')">
        <Icon :name="playing ? 'pause' : 'play'" />
      </button>
      <div :class="mobile ? 'relative flex-1' : 'contents'">
        <button class="btn gap-2 !px-[14.5px] text-base font-medium" :class="{ wide: mobile }" :aria-expanded="open" title="Скорость проигрывания" @click="open = !open">
          {{ speedLabel }}
          <Icon name="chevron" class="transition-transform duration-200" :class="{ 'rotate-180': open }" />
        </button>
        <!-- Список — под полосой у её правого края, в 11 px (кадр 1527); на телефоне — над кнопкой. -->
        <Transition
          enter-active-class="transition duration-200 ease-out motion-reduce:transition-none"
          enter-from-class="opacity-0 scale-[0.97]"
          leave-active-class="transition duration-150 ease-in motion-reduce:transition-none"
          leave-to-class="opacity-0 scale-[0.97]"
        >
          <div
            v-if="open"
            class="dropdown absolute z-30 w-[219px]"
            :class="mobile ? 'bottom-full left-1/2 mb-1 -translate-x-1/2 origin-bottom' : 'top-[calc(100%+11px)] right-0 origin-top-right'"
            role="listbox"
            aria-label="Скорость проигрывания"
          >
            <button
              v-for="item in SPEEDS"
              :key="item.label"
              type="button"
              role="option"
              :aria-selected="item.value === speed"
              class="dropdown-item"
              @click="pick(item.value)"
            >
              <span class="radio" :class="{ 'radio-on': item.value === speed }"><span /></span>
              {{ item.label }}
            </button>
          </div>
        </Transition>
      </div>
      <button class="round" title="К началу дня" aria-label="К началу дня" @click="emit('restart')"><Icon name="reset" /></button>
      <button v-if="!mobile" class="round" title="Закрыть" aria-label="Закрыть проигрывание" @click="emit('close')"><Icon name="close" /></button>
    </div>
  </div>
</template>

