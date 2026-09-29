<script setup lang="ts">
// Левая панель: списки инженеров и заявок (docs/design.md, кадры 99, 1484, 1510, 1486). Поиск и
// фильтры — на клиенте: показываемый план уже содержит и вход, и назначения.
import type { Bar } from '~/composables/progress'
import {
  clockMinutes,
  clockOf,
  duration,
  engineerColors,
  isUrgent,
  NO_STATUS_BADGE,
  plural,
  requestTitle,
  requestTypeOf,
  STATUS_BADGES,
  statusAt,
  WORK_PRIORITY_NAMES,
  type Dataset,
  type Plan,
} from '~/composables/useApi'

const props = defineProps<{
  dataset: Dataset | null
  plan: Plan | null
  // Общее время дня из шапки (блок 33): статусы заявок и линия текущего момента в гантте.
  time: string
  selectedRequest: string | null
  selectedEngineer: string | null
  busy: string | null
  // Ответ сервера на «Перенести на след. день» — под той заявкой, откуда пришло действие.
  error: { requestId: string; text: string } | null
  progress: Bar | null // перенос из строки: полоса над её кнопкой, `requestId` — чьей
  // Панель раскрыта стрелкой: справа от инженеров — дорожки гантта (кадр 1480).
  wide: boolean
}>()

const emit = defineEmits<{
  request: [requestId: string]
  engineer: [engineerId: string]
  defer: [requestId: string]
  add: [kind: 'engineers' | 'requests']
}>()

// Вкладку задаёт страница: в мобильной версии её переключают нижние вкладки (docs/design.md).
const tab = defineModel<'engineers' | 'requests'>('tab', { default: 'engineers' })
const query = ref('')
const engineerFilter = ref<'all' | 'busy' | 'idle'>('all')
const requestFilter = ref<'all' | 'assigned' | 'unassigned'>('all')

/** Поиск без учёта регистра по любому из полей. */
function matches(...fields: string[]): boolean {
  const needle = query.value.trim().toLowerCase()
  return !needle || fields.some((field) => field.toLowerCase().includes(needle))
}

const input = computed(() => props.plan?.input ?? props.dataset)
const requestsById = computed(() => new Map((input.value?.requests ?? []).map((item) => [item.id, item])))
const colors = computed(() => engineerColors(input.value?.engineers ?? []))

const engineers = computed(() => {
  const routes = new Map((props.plan?.routes ?? []).map((route) => [route.engineerId, route]))
  return (input.value?.engineers ?? []).map((engineer) => {
    const stops = routes.get(engineer.id)?.stops ?? []
    // «4 подключения · 1 локальная заявка · 1 дозаказ» — по типам BK, в порядке справочника.
    // Заявка из чужого файла вне справочника считается по типу работы.
    const counts = new Map<string, { forms: readonly [string, string, string] | null; count: number }>()
    for (const stop of stops) {
      const request = requestsById.value.get(stop.requestId)
      if (!request) continue
      const type = requestTypeOf(request)
      const key = type?.title ?? WORK_PRIORITY_NAMES[request.workPriority]
      const entry = counts.get(key) ?? { forms: type?.forms ?? null, count: 0 }
      entry.count += 1
      counts.set(key, entry)
    }
    const summary = [...counts.entries()]
      .map(([name, { forms, count }]) => (forms ? plural(count, forms) : `${name} — ${count}`))
      .join(' · ')
    return {
      id: engineer.id,
      name: engineer.name,
      busy: stops.length > 0,
      summary: props.plan ? summary || 'без заявок' : `смена ${engineer.shiftStart}–${engineer.shiftEnd}`,
    }
  })
})

const requests = computed(() => {
  const plan = props.plan
  const stops = new Map(
    (plan?.routes ?? []).flatMap((route) => route.stops.map((stop) => [stop.requestId, stop] as const)),
  )
  const unassigned = new Map((plan?.unassigned ?? []).map((item) => [item.requestId, item]))
  return [...requestsById.value.values()].map((request) => {
    const stop = stops.get(request.id)
    const status = plan && statusAt(plan, request.id, stop, props.time)
    const forecast = plan?.forecast?.requests[request.id]
    return {
      id: request.id,
      address: request.address,
      title: requestTitle(request),
      urgent: isUrgent(request),
      status: plan ? (status ? STATUS_BADGES[status] : NO_STATUS_BADGE) : null,
      // ⚠ — только при риске выше низкого (PLAN 6.18).
      forecast: forecast && forecast.risk !== 'low' ? forecast : null,
      request,
      start: stop?.start ?? null,
      time: [
        `окно: ${request.windowStart}–${request.windowEnd}`,
        ...(stop ? [`${stop.start}–${stop.end}`, duration(request.serviceDurationMin)] : []),
      ].join(' · '),
      assigned: !!plan?.assignments[request.id],
      engineer: engineersById.value.get(plan?.assignments[request.id] ?? '')?.name ?? null,
      unassigned: plan ? (unassigned.get(request.id) ?? null) : null,
      deferredAt: plan?.deferred[request.id] ?? null,
    }
  })
})

const engineerCounts = computed(() => ({
  all: engineers.value.length,
  busy: engineers.value.filter((item) => item.busy).length,
  idle: engineers.value.filter((item) => !item.busy).length,
}))
const requestCounts = computed(() => ({
  all: requests.value.length,
  assigned: requests.value.filter((item) => item.assigned).length,
  unassigned: requests.value.filter((item) => !item.assigned).length,
}))

/** Чипы фильтра текущей вкладки. Без плана назначать нечего — по назначенности фильтр выключен. */
const filters = computed(() =>
  tab.value === 'engineers'
    ? [
        { value: 'all', label: `все · ${engineerCounts.value.all}`, needsPlan: false },
        { value: 'busy', label: `с назначениями · ${engineerCounts.value.busy}`, needsPlan: true },
        { value: 'idle', label: `без назначений · ${engineerCounts.value.idle}`, needsPlan: true },
      ]
    : [
        { value: 'all', label: `все · ${requestCounts.value.all}`, needsPlan: false },
        { value: 'assigned', label: `назначенные · ${requestCounts.value.assigned}`, needsPlan: true },
        { value: 'unassigned', label: `не назначенные · ${requestCounts.value.unassigned}`, needsPlan: true },
      ],
)
const filter = computed({
  get: (): string => (tab.value === 'engineers' ? engineerFilter.value : requestFilter.value),
  set(value: string) {
    if (tab.value === 'engineers') engineerFilter.value = value as typeof engineerFilter.value
    else requestFilter.value = value as typeof requestFilter.value
  },
})

const shownEngineers = computed(() =>
  engineers.value.filter(
    (item) =>
      (engineerFilter.value === 'all' || item.busy === (engineerFilter.value === 'busy')) &&
      matches(item.name, item.id),
  ),
)
const shownRequests = computed(() =>
  requests.value.filter(
    (item) =>
      (requestFilter.value === 'all' || item.assigned === (requestFilter.value === 'assigned')) &&
      matches(item.id, item.address),
  ),
)

/**
 * Гантт всех инженеров (docs/design.md): только на вкладке инженеров и при посчитанном плане.
 * Шкала общая у всех дорожек — от самого раннего начала смен до самого позднего конца, целыми
 * часами (логика прежнего DayGantt).
 */
const CARD_COLUMN = 280 // колонка инженеров в гантте, px
const ZOOM = { min: 60, max: 720 } // пикселей на час
const pxPerHour = ref(180)
const gantt = computed(() => props.wide && !!props.plan && tab.value === 'engineers')
const axis = computed(() => {
  const plan = props.plan
  const engineers = plan?.input.engineers ?? []
  const starts = engineers.map((engineer) => clockMinutes(engineer.shiftStart))
  const ends = [
    ...engineers.map((engineer) => clockMinutes(engineer.shiftEnd)),
    ...(plan?.routes ?? []).flatMap((route) => route.stops.map((stop) => clockMinutes(stop.end))),
  ]
  const from = Math.floor(Math.min(...starts, 9 * 60) / 60) * 60
  const to = Math.ceil(Math.max(...ends, 18 * 60) / 60) * 60
  return { from, to, px: pxPerHour.value, hours: Array.from({ length: (to - from) / 60 + 1 }, (_, i) => from / 60 + i) }
})
// Гантт срезается слева только ниже шапки — для этого нужна её высота.
const head = useTemplateRef<HTMLElement>('head')
const headHeight = ref(0)
let observer: ResizeObserver | undefined
onMounted(() => {
  observer = new ResizeObserver(() => (headHeight.value = head.value?.offsetHeight ?? 0))
  if (head.value) observer.observe(head.value)
})
onBeforeUnmount(() => {
  observer?.disconnect()
})
/**
 * Масштаб гантта — Ctrl + колесо (и щипок на тачпаде: браузер шлёт его так же) или кнопки + и −.
 * Точка шкалы под курсором (у кнопок — середина видимых дорожек) остаётся на месте: иначе масштаб
 * уносил бы из-под глаз то, что разглядывают. Дорожки начинаются после левого отступа панели
 * (14 px), колонки инженеров и зазора (8 px).
 */
const nav = useTemplateRef<HTMLElement>('nav')
const scrollLeft = ref(0) // сдвиг строки часов в шапке вслед за дорожками
const ORIGIN = 14 + CARD_COLUMN + 8
function zoomAt(factor: number, at: number) {
  const scroller = nav.value
  if (!scroller) return
  const hours = (scroller.scrollLeft + at - ORIGIN) / pxPerHour.value
  pxPerHour.value = Math.min(ZOOM.max, Math.max(ZOOM.min, pxPerHour.value * factor))
  nextTick(() => (scroller.scrollLeft = ORIGIN + hours * pxPerHour.value - at))
}
function wheelZoom(event: WheelEvent) {
  if (!gantt.value || !nav.value || !(event.ctrlKey || event.metaKey)) return
  event.preventDefault()
  zoomAt(Math.exp(-event.deltaY * 0.002), event.clientX - nav.value.getBoundingClientRect().left)
}
function buttonZoom(factor: number) {
  if (nav.value) zoomAt(factor, (ORIGIN + nav.value.clientWidth) / 2)
}

// Линия текущего момента — время дня из шапки: то же, что уйдёт с ближайшим действием (блок 33).
const now = computed(() => (props.plan ? clockMinutes(props.time) : null))
const engineersById = computed(() => new Map((input.value?.engineers ?? []).map((engineer) => [engineer.id, engineer])))

// Без плана назначать нечего: фильтры по назначенности сбрасываются на «все».
watch(
  () => props.plan,
  (plan) => {
    if (plan) return
    engineerFilter.value = 'all'
    requestFilter.value = 'all'
  },
)

// Пока тянут, курсор «сжатая рука» и на кнопках-фильтрах тоже, текст не выделяется.
const DRAGGING = ['cursor-grabbing', 'select-none', '[&_*]:!cursor-grabbing']

/**
 * Перетаскивание зажатой мышью: ряд фильтров листается вбок (полосы прокрутки на десктопе у него
 * нет), гантт — во все стороны. Значение директивы — что прокручивать: без значения сам элемент,
 * `null` — ничего. Касание не трогаем: пальцем прокрутка работает и так. Сдвиг больше 4 px —
 * перетаскивание, и отпускание ничего не нажимает. Вложенные элементы с директивой (фильтры внутри
 * гантта) забирают жест себе — внешний его уже не видит.
 */
const scrollers = new WeakMap<HTMLElement, HTMLElement | null | undefined>()
let claimed: PointerEvent | null = null
const vDragScroll = {
  mounted(element: HTMLElement, binding: { value?: HTMLElement | null }) {
    scrollers.set(element, binding.value)
    element.addEventListener('pointerdown', (down) => {
      const target = scrollers.get(element) === undefined ? element : scrollers.get(element)
      if (!target || claimed === down || down.pointerType !== 'mouse' || down.button !== 0) return
      claimed = down
      const x0 = down.clientX
      const y0 = down.clientY
      const left0 = target.scrollLeft
      const top0 = target.scrollTop
      let dragged = false
      const move = (event: PointerEvent) => {
        const dx = event.clientX - x0
        const dy = event.clientY - y0
        if (!dragged && Math.hypot(dx, dy) < 4) return
        if (!dragged) {
          element.classList.add(...DRAGGING)
          getSelection()?.removeAllRanges()
        }
        dragged = true
        target.scrollLeft = left0 - dx
        target.scrollTop = top0 - dy
      }
      const swallow = (click: MouseEvent) => {
        click.preventDefault()
        click.stopPropagation()
      }
      const up = () => {
        window.removeEventListener('pointermove', move)
        window.removeEventListener('pointerup', up)
        if (!dragged) return
        element.classList.remove(...DRAGGING)
        // Клик, который браузер пришлёт следом за отпусканием, — конец перетаскивания, а не выбор.
        element.addEventListener('click', swallow, true)
        setTimeout(() => element.removeEventListener('click', swallow, true))
      }
      window.addEventListener('pointermove', move)
      window.addEventListener('pointerup', up)
    })
  },
  updated(element: HTMLElement, binding: { value?: HTMLElement | null }) {
    scrollers.set(element, binding.value)
  },
}
</script>

<template>
  <!-- Гантт прокручивается вбок, и дорожки уезжали бы в боковые отступы панели — и под шапкой,
       и по бокам подвала с кнопкой. Ниже шапки оба отступа срезаются; выше их закрывает тень шапки. -->
  <nav
    ref="nav"
    class="fade-scroll"
    :style="gantt ? { clipPath: `polygon(0 0, 100% 0, 100% ${headHeight}px, calc(100% - 14px) ${headHeight}px, calc(100% - 14px) 100%, 14px 100%, 14px ${headHeight}px, 0 ${headHeight}px)` } : undefined"
    @wheel="wheelZoom"
    @scroll="scrollLeft = nav?.scrollLeft ?? 0"
  >
    <!-- В гантте шапка сплошная: строки уходят под строку часов, а не проступают сквозь неё. -->
    <div
      ref="head"
      class="fade-header flex flex-col gap-3"
      :class="{ 'bg-panel shadow-[14px_0_0_var(--color-panel),-14px_0_0_var(--color-panel)]': gantt }"
    >
      <!-- Широкая панель: вкладки шириной с колонку инженеров, поиск справа (кадр 1480). -->
      <div :class="wide ? 'flex gap-2' : 'flex flex-col gap-3'">
        <Segmented
          v-model="tab"
          class="max-md:hidden"
          :class="{ 'shrink-0': wide }"
          :style="wide ? { width: `${CARD_COLUMN}px` } : undefined"
          :options="[
            { value: 'engineers', label: 'инженеры', icon: 'engineers' },
            { value: 'requests', label: 'заявки', icon: 'requests' },
          ]"
        />
        <label class="flex min-h-11 flex-1 items-center gap-2 rounded-[22px] bg-field px-3.5 text-text [&_input]:min-w-0 [&_input]:flex-1 [&_input]:border-0 [&_input]:bg-transparent [&_input]:outline-0 [&_input::placeholder]:text-muted">
          <Icon name="search" />
          <input v-model="query" type="search" :placeholder="tab === 'engineers' ? 'поиск по инженерам' : 'поиск по заявкам'">
        </label>
      </div>
      <!-- В гантте фильтры и часы — одна строка (кадр 1480). Шапка к прокрутке вбок не липнет,
           поэтому часы сдвигаются вслед за дорожками сами. Ячейка фильтров доходит до низа шапки:
           её граница продолжает разделитель колонок. -->
      <div :class="{ flex: gantt }">
        <div
          v-drag-scroll
          class="no-scrollbar flex gap-2 overflow-x-auto"
          :class="gantt ? '-mb-3.5 shrink-0 border-r border-[#e8ecf0] pr-2 pb-3.5' : '-mx-3.5 px-3.5'"
          :style="gantt ? { width: `${CARD_COLUMN + 8}px` } : undefined"
        >
          <button
            v-for="option in filters"
            :key="option.value"
            class="chip"
            :class="{ 'bg-accent text-text': filter === option.value }"
            :disabled="option.needsPlan && !plan"
            @click="filter = option.value"
          >
            {{ option.label }}
          </button>
        </div>
        <div v-if="gantt" class="relative min-w-0 flex-1 overflow-hidden" title="Масштаб — кнопки или Ctrl + колесо мыши, прокрутка — перетаскиванием">
          <!-- Кнопки масштаба — у правого края строки часов, поверх подписей. -->
          <div class="absolute top-0 right-0 z-1 flex gap-1 bg-panel pl-2">
            <button class="round !size-9 disabled:cursor-default disabled:text-muted" title="Отдалить" :disabled="axis.px <= ZOOM.min" @click="buttonZoom(1 / 1.5)">
              <Icon name="map-zoom-out" />
            </button>
            <button class="round !size-9 disabled:cursor-default disabled:text-muted" title="Приблизить" :disabled="axis.px >= ZOOM.max" @click="buttonZoom(1.5)">
              <Icon name="map-zoom-in" />
            </button>
          </div>
          <span
            class="relative block h-9"
            :style="{ width: `${((axis.to - axis.from) / 60) * axis.px}px`, transform: `translateX(${-scrollLeft}px)` }"
          >
            <!-- Подпись часа — над его линией сетки; крайние прижаты внутрь шкалы, чтобы не срезались. -->
            <span
              v-for="(hour, index) in axis.hours"
              :key="hour"
              class="absolute top-1/2 -translate-y-1/2 text-sm text-muted"
              :class="index === 0 ? '' : index === axis.hours.length - 1 ? '-translate-x-full' : '-translate-x-1/2'"
              :style="{ left: `${((hour * 60 - axis.from) / 60) * axis.px}px` }"
            >
              {{ clockOf(hour * 60) }}
            </span>
          </span>
        </div>
      </div>
    </div>

    <template v-if="tab === 'engineers'">
      <!-- В гантте строка — ячейка инженера и его дорожка. Ячейка липнет к левому краю и на 4 px
           заходит в зазоры сверху, снизу и справа: так она закрывает уехавшие под неё блоки, а её
           правая граница рисует сплошной разделитель колонок. Без гантта широкая панель
           раскладывает карточки сеткой, а не растягивает их на всю ширину. -->
      <ul
        v-drag-scroll="gantt ? nav : null"
        class="m-0 list-none gap-2 p-0"
        :class="gantt ? 'flex flex-col select-none [&>li]:grid [&>li]:w-max [&>li]:gap-2' : wide ? 'grid grid-cols-[repeat(auto-fill,minmax(280px,1fr))]' : 'flex flex-col'"
        :style="gantt ? { '--column': `${CARD_COLUMN}px` } : undefined"
      >
        <li v-for="item in shownEngineers" :key="item.id" :class="{ 'grid-cols-[var(--column)_max-content]': gantt }">
          <div :class="gantt ? 'sticky left-0 z-1 -my-1 -mr-2 border-r border-[#e8ecf0] bg-panel py-1 pr-2' : 'contents'">
            <div
              class="card hoverable flex items-center gap-2"
              :class="{ 'shadow-[inset_0_0_0_2px_#ffc800]': item.id === selectedEngineer }"
              @click="emit('engineer', item.id)"
            >
              <div class="min-w-0 flex-1">
                <div>{{ item.name }}</div>
                <div class="muted small">{{ item.summary }}</div>
              </div>
              <span v-if="plan" class="size-3.5 shrink-0 rounded-full" :style="{ background: colors.get(item.id) }" />
            </div>
          </div>
          <GanttTrack
            v-if="gantt && plan"
            :plan="plan"
            :engineer="engineersById.get(item.id)!"
            :axis="axis"
            :now="now"
            :color="colors.get(item.id)!"
            @select="emit('request', $event)"
          />
        </li>
        <li v-if="!shownEngineers.length" class="muted px-3.5 py-2">Никого не нашлось.</li>
      </ul>
      <div class="fade-footer">
        <button class="btn primary wide" :disabled="!plan || !!busy" @click="emit('add', 'engineers')">
          <Icon name="plus" /> Добавить инженера
        </button>
      </div>
    </template>

    <template v-else>
      <ul class="m-0 list-none gap-2 p-0" :class="wide ? 'grid grid-cols-[repeat(auto-fill,minmax(320px,1fr))] items-start' : 'flex flex-col'">
        <li
          v-for="item in shownRequests"
          :key="item.id"
          class="card hoverable flex flex-col gap-[3px] [&_.btn]:mt-1.5"
          :class="{ 'shadow-[inset_0_0_0_2px_#ffc800]': item.id === selectedRequest }"
          @click="emit('request', item.id)"
        >
          <div class="flex flex-wrap items-center gap-1.5">
            <span class="muted">№{{ item.id }}</span>
            <span v-if="item.status" class="badge ml-auto">{{ item.status }}</span>
            <span class="badge" :class="[item.urgent ? 'urgent' : 'accent', { 'ml-auto': !item.status }]">{{ item.urgent ? 'Срочная' : 'Обычная' }}</span>
            <RiskBadge v-if="item.forecast" :forecast="item.forecast" :request="item.request" :start="item.start" />
          </div>
          <div class="font-medium">{{ item.title }}</div>
          <div class="muted small">{{ item.time }}</div>
          <div class="muted small"><span v-if="item.engineer">{{ item.engineer }} · </span>{{ item.address }}</div>
          <template v-if="item.unassigned">
            <div class="small flex items-center gap-1.5 text-muted" :title="item.unassigned.reasonText">
              <Icon name="alert" /><span class="overflow-hidden text-ellipsis whitespace-nowrap">{{ item.unassigned.reasonText }}</span>
            </div>
            <div v-if="item.deferredAt" class="muted small">Перенесена в {{ item.deferredAt }}</div>
            <template v-else>
              <div v-if="item.unassigned.deferNextDay" class="muted small">требуется перенос на следующий день</div>
              <ProgressBar v-if="progress?.requestId === item.id" :bar="progress" />
              <button class="btn primary wide" :disabled="!!busy" @click.stop="emit('defer', item.id)">
                <Icon name="defer" /> Перенести на след. день
              </button>
            </template>
            <p v-if="error?.requestId === item.id" class="error small">{{ error.text }}</p>
          </template>
        </li>
        <li v-if="!shownRequests.length" class="muted px-3.5 py-2">Ничего не нашлось.</li>
      </ul>
      <div class="fade-footer">
        <button class="btn primary wide" :disabled="!plan || !!busy" @click="emit('add', 'requests')">
          <Icon name="plus" /> Добавить заявку
        </button>
      </div>
    </template>
  </nav>
</template>
