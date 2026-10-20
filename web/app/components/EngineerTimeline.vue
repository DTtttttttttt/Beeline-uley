<script setup lang="ts">
// Вертикальная шкала дня инженера (docs/design.md, кадры 1509, 1482, 1483, 1488, 1513): от начала
// до конца смены по часам, блоки работ, между ними переезд: вертикальная линия с засечками,
// слева время, справа километры. Под подписью часа — тонкая линия через всю шкалу, за блоками.
import { clockMinutes, clockOf, dayWorks, LIVE_EDGE, type Engineer, type Plan } from '~/composables/useApi'

// `time` — время дня из шапки: затянувшаяся работа растёт до него (блок 69).
const props = defineProps<{ plan: Plan; engineer: Engineer; time: string }>()
const live = inject(LIVE_EDGE, ref(false))
const emit = defineEmits<{ select: [requestId: string] }>()

const HOUR = 60 // пикселей на час
const MIN_BLOCK = 96 // три строки подписи с отступами
const GAP = 36 // место под подпись переезда
const LABEL = 16 // строка подписи переезда
const LUNCH_BLOCK = 52 // строка «Обед» с полями

const from = computed(() => Math.floor(clockMinutes(props.engineer.shiftStart) / 60) * 60)
const to = computed(() => Math.ceil(clockMinutes(props.engineer.shiftEnd) / 60) * 60)
const hours = computed(() => Array.from({ length: (to.value - from.value) / 60 + 1 }, (_, i) => from.value / 60 + i))

/**
 * Шкала неравномерная, но честная. Получасовой работе в масштабе часа не хватает места на три
 * строки подписи, а переезду в четыре минуты — на свою. Поэтому такие куски растягиваются, и
 * время переводится в пиксели по опорным точкам «минуты → px»: начало и конец каждой работы.
 * Подписи часов ставит та же функция, так что блок всегда стоит напротив своего времени, а не
 * сползает вниз за растянутыми соседями. Шкала только растягивается — часы не сближаются.
 */
const layout = computed(() => {
  const points: [number, number][] = [[from.value, 0]]
  // Обед (блок 79) — блок шкалы между работами, как в гантте (правки 81.5): переезд к следующей
  // работе считается от его конца.
  const found = props.plan.routes.find((route) => route.engineerId === props.engineer.id)?.lunch
  let lunch = found ? { start: clockMinutes(found.start), end: clockMinutes(found.end), range: `${found.start}–${found.end}`, top: 0, height: 0 } : null
  let placed = false
  const place = (start: number, end: number, min: number, gap: number) => {
    const [before, bottom] = points.at(-1)!
    const natural = bottom + ((start - before) / 60) * HOUR
    const top = points.length > 1 ? Math.max(natural, bottom + gap) : natural
    const height = Math.max(((end - start) / 60) * HOUR, min)
    points.push([start, top], [end, top + height])
    return { top, height, bottom }
  }
  const putLunch = () => {
    if (!lunch || placed) return
    placed = true
    lunch = { ...lunch, ...place(lunch.start, lunch.end, LUNCH_BLOCK, 8) }
  }
  const blocks = dayWorks(props.plan, props.engineer.id, props.time, live.value).map((work, index) => {
    if (lunch && lunch.start <= work.start) putLunch()
    const { top, height, bottom } = place(work.start, work.end, MIN_BLOCK, GAP)
    const [min, distance] = work.travel.split(' · ')
    // Переезд — как в гантте (правки 82): от конца предыдущего блока линия длиной в саму дорогу,
    // остаток промежутка до работы — ожидание, пустое место. Подписи не ниже строки текста.
    const room = top - bottom - 4
    const road = Math.min((work.travelMin / 60) * HOUR, room)
    const travel = index || placed ? { top: bottom, height: Math.min(Math.max(road, LABEL), room), line: road, min, distance } : null
    return { ...work, top, height, travel }
  })
  putLunch()
  return { points, blocks, lunch }
})

function y(minutes: number): number {
  const points = layout.value.points
  for (let i = 1; i < points.length; i++) {
    const [end, endY] = points[i]!
    if (minutes > end) continue
    const [start, startY] = points[i - 1]!
    return end === start ? startY : startY + ((minutes - start) / (end - start)) * (endY - startY)
  }
  const [last, lastY] = points.at(-1)!
  return lastY + ((minutes - last) / 60) * HOUR
}

const blocks = computed(() => layout.value.blocks)
const lunch = computed(() => layout.value.lunch)
const height = computed(() =>
  Math.max(y(to.value), ...blocks.value.map((block) => block.top + block.height), lunch.value ? lunch.value.top + lunch.value.height : 0) + 12,
)
</script>

<template>
  <div class="relative mt-2" :style="{ height: `${height}px` }">
    <template v-for="hour in hours" :key="hour">
      <span class="muted small absolute left-0 -translate-y-1/2" :style="{ top: `${y(hour * 60)}px` }">{{ clockOf(hour * 60) }}</span>
      <span class="absolute right-0 left-12 h-px bg-field" :style="{ top: `${y(hour * 60)}px` }" />
    </template>
    <!-- Обед (правки 81.5) — как в гантте: вилка с ножом и «Обед» на светлом фоне блока. -->
    <div
      v-if="lunch"
      class="absolute right-0 left-14 flex items-center gap-2 overflow-hidden rounded-card bg-[#fff4c7] px-4"
      :style="{ top: `${lunch.top}px`, height: `${lunch.height}px` }"
      :title="`Обед ${lunch.range}`"
    >
      <Icon name="lunch" class="shrink-0" />
      <span>Обед</span>
      <span class="muted small ml-auto">{{ lunch.range }}</span>
    </div>
    <template v-for="block in blocks" :key="block.id">
      <!-- Линия переезда — в 2 px от предыдущего блока, длиной в саму дорогу; подписи — по её середине. -->
      <div v-if="block.travel" class="muted small absolute right-0 left-14 grid grid-cols-[1fr_auto_1fr] items-center gap-3" :style="{ top: `${block.travel.top + 2}px`, height: `${block.travel.height}px` }">
        <span class="text-right">{{ block.travel.min }}</span>
        <span class="flex w-6 flex-col items-center self-start" :style="{ height: `${block.travel.line}px` }">
          <span class="h-px w-full bg-field" />
          <span class="w-px flex-1 bg-field" />
          <span class="h-px w-full bg-field" />
        </span>
        <span>{{ block.travel.distance }}</span>
      </div>
      <!-- Жёлтая полоска слева — во всю высоту текста (кадр 1509): отступы сверху и снизу — поля блока. -->
      <button
        class="hoverable absolute right-0 left-14 overflow-hidden rounded-card border-0 bg-[#fff4c7] p-4 text-left"
        :style="{ top: `${block.top}px`, height: `${block.height}px` }"
        :title="`Расчётно ${block.planMin} мин · ${block.state}`"
        @click="emit('select', block.id)"
      >
        <span class="relative flex gap-3">
          <span class="w-[3px] shrink-0 rounded-full bg-accent" />
          <span class="flex min-w-0 flex-1 flex-col gap-1">
            <span class="flex items-center justify-between gap-1.5">
              <span class="flex min-w-0 items-center gap-1.5">
                <span class="overflow-hidden text-ellipsis whitespace-nowrap">{{ block.title }}</span>
              </span>
              <RiskBadge v-if="block.forecast && block.request" :forecast="block.forecast" :request="block.request" :start="block.plannedStart" :before="block.normForecast" :extra-min="block.extraMin">{{ block.risk }}</RiskBadge>
            </span>
            <span class="muted small overflow-hidden text-ellipsis whitespace-nowrap">
              {{ block.time }}<template v-if="block.committed"> · закреплена</template>
            </span>
            <span class="muted small overflow-hidden text-ellipsis whitespace-nowrap">{{ block.address }}</span>
          </span>
        </span>
      </button>
    </template>
  </div>
</template>
