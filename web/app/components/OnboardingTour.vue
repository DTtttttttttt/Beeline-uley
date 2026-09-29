<script setup lang="ts">
// Обучалка первого захода (docs/design.md, «Обучалка»): экран затемнён, элемент шага подсвечен
// вырезом, рядом карточка с текстом и изогнутой стрелкой к элементу. Цель шага — элемент с
// `data-tour`; из одноимённых берётся видимый (шапка и нижние вкладки телефона несут одни ключи),
// шаг без видимой цели пропускается — например, язычок гантта в узком окне.
const props = defineProps<{ mobile: boolean }>()
const emit = defineEmits<{ done: [] }>()

interface Step {
  target: string
  text: string
}
const DESKTOP: Step[] = [
  { target: 'plan-switch', text: 'Переключайтесь между базовым и оптимизированным планом, чтобы сравнить их' },
  { target: 'time', text: 'Время дня: поставьте любое — план и статусы покажутся на этот момент' },
  { target: 'lists', text: 'Инженеры и заявки: поиск, фильтры, клик по строке открывает карточку' },
  { target: 'map', text: 'Маршруты бригад на карте. Нажмите на точку — откроется заявка' },
  { target: 'gantt', text: 'Раскройте график дня по всем инженерам' },
  { target: 'metrics', text: 'Метрики плана и сравнение с базовым' },
  { target: 'assistant', text: 'Ассистент: напишите обычными словами, что случилось за день, или спросите про план. Действие применяете вы' },
  { target: 'dataset', text: 'Загрузите свои заявки и бригады или выберите демо-набор' },
]
const MOBILE: Step[] = [
  { target: 'variants', text: 'Варианты плана: сравните базовый и оптимизированный и выберите лучший' },
  { target: 'time', text: 'Время дня: поставьте любое — план и статусы покажутся на этот момент' },
  { target: 'lists', text: 'Инженеры и заявки: нажмите на строку — откроется карточка' },
  { target: 'map', text: 'Маршруты бригад на карте. Нажмите на точку — откроется заявка' },
  { target: 'sheet', text: 'Потяните лист вверх, чтобы развернуть список, или вниз — к карте' },
  { target: 'metrics', text: 'Метрики плана и сравнение с базовым' },
  { target: 'assistant', text: 'Ассистент: напишите обычными словами, что случилось за день, или спросите про план. Действие применяете вы' },
  { target: 'dataset', text: 'Загрузите свои заявки и бригады или выберите демо-набор' },
]

const GAP = 52 // от выреза до карточки — место под стрелку
const PAD = 6 // вырез шире цели
const MARGIN = 16 // от края экрана

function find(key: string) {
  return [...document.querySelectorAll<HTMLElement>(`[data-tour="${key}"]`)].find((el) => {
    const rect = el.getBoundingClientRect()
    return rect.width > 0 && rect.height > 0
  })
}

const steps = ref<Step[]>([])
const index = ref(0)
const card = useTemplateRef<HTMLElement>('card')
// Вырез, карточка и стрелка — из одного кадра: цель двигается (лист, панель), и они едут вместе.
const hole = ref({ left: 0, top: 0, width: 0, height: 0, radius: 0 })
const place = ref({ left: 0, top: 0 })
const arrow = ref('')

const clamp = (value: number, min: number, max: number) => Math.min(Math.max(value, min), Math.max(min, max))

/** Вырез плавно догоняет цель, карточка встаёт с той стороны, где ей хватает места. */
function layout(el: HTMLElement) {
  const rect = el.getBoundingClientRect()
  const goal = { left: rect.left - PAD, top: rect.top - PAD, width: rect.width + 2 * PAD, height: rect.height + 2 * PAD }
  const now = hole.value
  const first = !now.width
  const ease = (from: number, to: number) => (first || Math.abs(to - from) < 0.5 ? to : from + (to - from) * 0.25)
  const radius = Math.min(parseFloat(getComputedStyle(el).borderTopLeftRadius) || 0, rect.height / 2) + PAD
  hole.value = {
    left: ease(now.left, goal.left),
    top: ease(now.top, goal.top),
    width: ease(now.width, goal.width),
    height: ease(now.height, goal.height),
    radius,
  }
  const h = hole.value
  const box = { left: h.left, top: h.top, right: h.left + h.width, bottom: h.top + h.height }
  const cx = (box.left + box.right) / 2
  const cy = (box.top + box.bottom) / 2
  const vw = window.innerWidth
  const vh = window.innerHeight
  const w = card.value?.offsetWidth ?? 0
  const ch = card.value?.offsetHeight ?? 0

  const room = { bottom: vh - box.bottom, top: box.top, right: vw - box.right, left: box.left }
  const need = { bottom: ch, top: ch, right: w, left: w }
  const sides = ['bottom', 'top', 'right', 'left'] as const
  const side =
    sides.find((s) => room[s] >= need[s] + GAP + MARGIN) ?? [...sides].sort((a, b) => room[b] - need[b] - (room[a] - need[a]))[0]!

  // Начало стрелки — у ближнего к цели края карточки, со сдвигом вбок: так линия выходит дугой.
  let left: number, top: number, sx: number, sy: number, ex: number, ey: number, nx: number, ny: number
  if (side === 'bottom' || side === 'top') {
    left = clamp(cx + 24 - 36, MARGIN, vw - w - MARGIN)
    top = side === 'bottom' ? box.bottom + GAP : box.top - GAP - ch
    top = clamp(top, MARGIN, vh - ch - MARGIN)
    sx = left + 36
    ex = clamp(sx - 40, box.left + 16, box.right - 16)
    ny = side === 'bottom' ? 1 : -1
    nx = 0
    sy = side === 'bottom' ? top - 6 : top + ch + 6
    ey = side === 'bottom' ? box.bottom + 6 : box.top - 6
  } else {
    left = side === 'right' ? box.right + GAP : box.left - GAP - w
    left = clamp(left, MARGIN, vw - w - MARGIN)
    top = clamp(cy + 24 - ch / 2, MARGIN, vh - ch - MARGIN)
    sy = top + ch / 2
    ey = clamp(sy - 40, box.top + 16, box.bottom - 16)
    nx = side === 'right' ? 1 : -1
    ny = 0
    sx = side === 'right' ? left - 6 : left + w + 6
    ex = side === 'right' ? box.right + 6 : box.left - 6
  }
  place.value = { left, top }

  // Кубическая дуга: выходит из карточки к цели и входит в цель по нормали её края.
  const k = GAP * 0.6
  const head = (angle: number) => {
    const ux = -nx * Math.cos(angle) + ny * Math.sin(angle)
    const uy = -ny * Math.cos(angle) - nx * Math.sin(angle)
    return `M${ex - ux * 9} ${ey - uy * 9}L${ex} ${ey}`
  }
  arrow.value = `M${sx} ${sy}C${sx - nx * k} ${sy - ny * k} ${ex + nx * k} ${ey + ny * k} ${ex} ${ey}` + head(0.5) + head(-0.5)
}

let frame = 0
function track() {
  const step = steps.value[index.value]
  const el = step && find(step.target)
  if (el) layout(el)
  frame = requestAnimationFrame(track)
}

function next() {
  if (index.value < steps.value.length - 1) index.value += 1
  else emit('done')
}

function onKey(event: KeyboardEvent) {
  if (event.key === 'Escape') emit('done')
  else if (event.key === 'Enter' || event.key === 'ArrowRight') next()
}

onMounted(() => {
  steps.value = (props.mobile ? MOBILE : DESKTOP).filter((step) => find(step.target))
  if (!steps.value.length) return emit('done')
  window.addEventListener('keydown', onKey)
  track()
})
onBeforeUnmount(() => {
  cancelAnimationFrame(frame)
  window.removeEventListener('keydown', onKey)
})
</script>

<template>
  <div class="fixed inset-0 z-[60]" role="dialog" aria-label="Знакомство с планировщиком">
    <!-- Затемнение — тень выреза: подсвеченный элемент виден, но нажать его нельзя, клики ловит слой. -->
    <div
      class="pointer-events-none fixed shadow-[0_0_0_200vmax_rgb(40_48_63_/_60%)]"
      :style="{
        left: `${hole.left}px`,
        top: `${hole.top}px`,
        width: `${hole.width}px`,
        height: `${hole.height}px`,
        borderRadius: `${hole.radius}px`,
      }"
    />
    <svg class="pointer-events-none fixed inset-0 size-full" aria-hidden="true">
      <path :d="arrow" fill="none" stroke="#fff" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
    </svg>
    <section
      ref="card"
      class="fixed flex w-[280px] max-w-[calc(100vw-32px)] flex-col gap-3 rounded-card bg-panel p-3.5 shadow-panel"
      :style="{ left: `${place.left}px`, top: `${place.top}px` }"
    >
      <p class="m-0">{{ steps[index]?.text }}</p>
      <div class="flex items-center gap-2">
        <span class="small flex-1 text-muted">{{ index + 1 }} / {{ steps.length }}</span>
        <button class="btn !bg-transparent !px-2 text-muted" @click="emit('done')">Пропустить</button>
        <button class="btn primary" @click="next">{{ index < steps.length - 1 ? 'Далее' : 'Готово' }}</button>
      </div>
    </section>
  </div>
</template>
