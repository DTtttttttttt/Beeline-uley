<script setup lang="ts">
// Дорожка дня одного инженера в гантте (docs/design.md, «Гантт всех инженеров», кадр 1480):
// блоки работ со значком, заголовком и «?», временем работы и адресом, между ними «38 мин / 4,2 км», полоса смены и
// линия текущего момента. Шкалу задаёт список: она общая у всех дорожек.
import { clockMinutes, dayWorks, LIVE_EDGE, type Engineer, type Plan, type Request, type RequestForecast } from '~/composables/useApi'

const props = defineProps<{
  plan: Plan
  engineer: Engineer
  axis: { from: number; to: number; px: number }
  now: number | null
  // Цвет инженера — тот же, что у его маршрута на карте и точки в списке.
  color: string
  // Время дня из шапки: затянувшаяся работа растёт до него (блок 69).
  time: string
}>()
const live = inject(LIVE_EDGE, ref(false))

// Фон блока — цвет инженера, разбавленный белым; значок и «?» — сам цвет, внутренности значков
// прозрачные (кадр 1749, блок 82).
const tint = computed(() => `color-mix(in srgb, ${props.color} 16%, white)`)

const percent = (forecast: RequestForecast) => Math.round(forecast.lateProbability * 100)
function glyph(block: { outcome: string | null; request?: Request }) {
  if (block.outcome) return block.outcome === 'done' ? 'done-filled' : 'canceled'
  return block.request?.workPriority === 'emergency' ? 'alarm' : 'clipboard'
}

const emit = defineEmits<{ select: [requestId: string] }>()

/** Минуты от полуночи → пиксели дорожки. */
function x(minutes: number): number {
  return ((minutes - props.axis.from) / 60) * props.axis.px
}

const width = computed(() => x(props.axis.to))

// Обед (блок 79, кадр 1749) — свой блок на дорожке: «Обед» с вилкой и ножом цветом инженера.
const lunch = computed(() => {
  const found = props.plan.routes.find((route) => route.engineerId === props.engineer.id)?.lunch
  if (!found) return null
  const [from, to] = [clockMinutes(found.start), clockMinutes(found.end)]
  const [left, right] = trims.value.get('lunch') ?? [0, 0]
  return { from, to, left: x(from) + left, width: x(to) - x(from) - left - right, title: `Обед ${found.start}–${found.end}` }
})

// Между соседними блоками работ и обеда — не меньше 4 px (правки 82): стык ближе раздвигается,
// каждый блок уступает половину недостающего.
const GAP = 4
const trims = computed(() => {
  const works = dayWorks(props.plan, props.engineer.id, props.time, live.value)
  const found = props.plan.routes.find((route) => route.engineerId === props.engineer.id)?.lunch
  const spans = [
    ...works.map((work) => ({ id: work.id, from: x(work.start), to: x(work.end) })),
    ...(found ? [{ id: 'lunch', from: x(clockMinutes(found.start)), to: x(clockMinutes(found.end)) }] : []),
  ].sort((a, b) => a.from - b.from)
  const result = new Map<string, [number, number]>(spans.map((span) => [span.id, [0, 0]]))
  spans.slice(1).forEach((span, index) => {
    const before = spans[index]!
    const short = GAP - (span.from - before.to)
    if (short <= 0) return
    result.get(before.id)![1] += short / 2
    result.get(span.id)![0] += short / 2
  })
  return result
})

const blocks = computed(() => {
  const works = dayWorks(props.plan, props.engineer.id, props.time, live.value)
  return works.map((work, index) => {
    const [trimLeft, trimRight] = trims.value.get(work.id) ?? [0, 0]
    const left = x(work.start) + trimLeft
    // Переезд считается от конца обеда, если обед между работами.
    const before = index ? works[index - 1]!.end : null
    const after = lunch.value && before !== null && lunch.value.from >= before && lunch.value.to <= work.start ? lunch.value.to : before
    const previous = after === null ? null : x(after)
    const [min, distance] = work.travel.split(' · ')
    const gap = previous === null ? 0 : left - previous
    // По 2 px от соседних блоков: линия не прилипает к ним.
    const room = gap - 4
    const road = (work.travelMin / 60) * props.axis.px
    return {
      ...work,
      left,
      width: Math.max(x(work.end) - trimRight - left, 28),
      detail: `${work.time} · ${work.address}`,
      // Переезд к этой работе — размерная линия от конца предыдущей длиной в саму дорогу (кадр 1480):
      // остаток промежутка до работы — ожидание окна. Подписи шире линии, если дорога короткая.
      gap:
        previous !== null && gap > 36
          ? { left: previous + 2, width: Math.min(Math.max(road, 48), room), line: Math.min(road, room), min, distance }
          : null,
    }
  })
})

// Часовые линии сетки — сквозь все дорожки (кадр 1480).
const hours = computed(() => Array.from({ length: (props.axis.to - props.axis.from) / 60 + 1 }, (_, i) => x(props.axis.from + i * 60)))

const shift = computed(() => ({
  left: x(clockMinutes(props.engineer.shiftStart)),
  width: x(clockMinutes(props.engineer.shiftEnd)) - x(clockMinutes(props.engineer.shiftStart)),
}))
const gone = computed(() =>
  props.engineer.unavailableFrom
    ? { left: x(clockMinutes(props.engineer.unavailableFrom)), width: x(clockMinutes(props.engineer.shiftEnd)) - x(clockMinutes(props.engineer.unavailableFrom)) }
    : null,
)
</script>

<template>
  <!-- Линии часов и текущего момента вылезают на 4 px вверх и вниз — в зазор между строками:
       так они идут сквозь весь гантт, а не обрываются у каждой дорожки. -->
  <div class="relative h-full min-h-14 cursor-grab" :style="{ width: `${width}px` }">
    <span class="absolute inset-y-0 rounded-card bg-[#f7f9fa]" :style="{ left: `${shift.left}px`, width: `${shift.width}px` }" />
    <span v-for="left in hours" :key="left" class="absolute -inset-y-1 w-px bg-[#e8ecf0]" :style="{ left: `${left}px` }" />
    <span
      v-if="gone && gone.width > 0"
      class="hatch absolute inset-y-0"
      :style="{ left: `${gone.left}px`, width: `${gone.width}px` }"
      title="Инженер недоступен: новых работ не берёт"
    />
    <span
      v-if="lunch"
      class="absolute inset-y-0 flex items-center gap-2 overflow-hidden rounded-card p-3 text-sm"
      :style="{ left: `${lunch.left}px`, width: `${lunch.width}px`, background: tint }"
      :title="lunch.title"
    >
      <Icon name="lunch" class="shrink-0" :style="{ color }" />
      <span class="overflow-hidden text-ellipsis whitespace-nowrap">Обед</span>
    </span>
    <template v-for="block in blocks" :key="block.id">
      <!-- Переезд (кадр 1749): минуты и километры 14 серым, между ними скобка в цвет поля. -->
      <span v-if="block.gap" class="muted absolute inset-y-0 flex flex-col items-center justify-center gap-0.5 text-sm whitespace-nowrap" :style="{ left: `${block.gap.left}px`, width: `${block.gap.width}px` }">
        <span>{{ block.gap.min }}</span>
        <span class="flex h-2 items-center self-start border-x border-field px-px" :style="{ width: `${block.gap.line}px` }">
          <span class="h-px w-full bg-field" />
        </span>
        <span>{{ block.gap.distance }}</span>
      </span>
      <!-- Полоска слева — во всю высоту текста, как в макете: её отступы сверху и снизу — поля блока. -->
      <button
        class="hoverable @container absolute inset-y-0 flex gap-2 overflow-hidden rounded-card border-0 p-3 text-left text-sm"
        :style="{ left: `${block.left}px`, width: `${block.width}px`, background: tint }"
        :title="`№${block.id} · ${block.title} · ${block.detail} · расчётно ${block.planMin} мин${block.extraMin ? `, +${block.extraMin} за сложность` : ''} · ${block.state}${block.committed ? ' · закреплена' : ''}`"
        @click="emit('select', block.id)"
      >
        <!-- Значок слева цветом инженера, 18 px (кадр 1749): ✓ выполнена, ✕ отменена или не
             выполнена, иначе — как булавка на карте: «!» у аварии, планшет у заявки. Внутренности
             значка прозрачные — сквозь них виден фон блока. Узкий блок (сильное отдаление, правки 82)
             плавно гасит все подписи разом — заголовок с «?», время и адрес, — и остаётся один значок
             посередине. Порог — по ширине содержимого блока (без полей 12 px). -->
        <Icon :name="glyph(block)" class="relative self-center @max-[72px]:mx-auto" :style="{ color }" />
        <span class="relative flex min-w-0 flex-1 flex-col justify-center gap-0.5 transition-opacity duration-200 @max-[72px]:-ml-2 @max-[72px]:pointer-events-none @max-[72px]:w-0 @max-[72px]:flex-none @max-[72px]:overflow-hidden @max-[72px]:opacity-0">
          <!-- «?» прижат к правому краю (кадр 1749): заголовок занимает всё до него. -->
          <span class="flex items-center gap-0.5">
            <span class="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap">{{ block.title }}</span>
            <!-- «?» — риск опоздания, сложность и скорость (блок 77); подсказка — общим `Tip`. -->
            <Tip v-if="block.request" class="flex shrink-0" :style="{ color }" label="Подробнее о заявке">
              <Icon name="help" />
              <template #tip>
                <template v-if="block.forecast">
                  <span class="font-medium">Риск опоздания {{ percent(block.forecast) }} %</span>
                  <span v-if="block.normForecast && percent(block.normForecast) !== percent(block.forecast)">По нормативу было {{ percent(block.normForecast) }} % → стало {{ percent(block.forecast) }} %.</span>
                  <span>В 9 из 10 прогонов начало не позже {{ block.forecast.p90Start }}, окно до {{ block.request.windowEnd }}.</span>
                </template>
                <span>Расчётно {{ block.planMin }} мин<template v-if="block.extraMin">, из них +{{ block.extraMin }} мин за сложность объекта и скорость инженера</template>.</span>
                <span v-if="block.state">Фактически: {{ block.state }}.</span>
                <span v-if="block.forecast" class="muted small">Риск — модельная оценка: отклонения работ и переездов заданы нами, а не взяты из истории.</span>
              </template>
            </Tip>
          </span>
          <span class="muted small overflow-hidden text-ellipsis whitespace-nowrap">{{ block.detail }}</span>
        </span>
      </button>
    </template>
    <span v-if="now !== null && now >= axis.from && now <= axis.to" class="pointer-events-none absolute -inset-y-1 w-0.5 bg-accent" :style="{ left: `${x(now)}px` }" />
  </div>
</template>
