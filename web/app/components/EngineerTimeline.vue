<script setup lang="ts">
// Вертикальная шкала дня инженера (docs/design.md, кадры 1509, 1482, 1483, 1488, 1513): от начала
// до конца смены по часам, блоки работ, между ними переезд: вертикальная линия с засечками,
// слева время, справа километры. Под подписью часа — тонкая линия через всю шкалу, за блоками.
import { clockMinutes, clockOf, dayWorks, type Engineer, type Plan } from '~/composables/useApi'

const props = defineProps<{ plan: Plan; engineer: Engineer }>()
const emit = defineEmits<{ select: [requestId: string] }>()

const HOUR = 60 // пикселей на час
const MIN_BLOCK = 96 // три строки подписи с отступами
const GAP = 36 // место под подпись переезда

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
  const blocks = dayWorks(props.plan, props.engineer.id).map((work, index) => {
    const [before, bottom] = points.at(-1)!
    const natural = bottom + ((work.start - before) / 60) * HOUR
    const top = index ? Math.max(natural, bottom + GAP) : natural
    const height = Math.max(((work.end - work.start) / 60) * HOUR, MIN_BLOCK)
    points.push([work.start, top], [work.end, top + height])
    const [min, distance] = work.travel.split(' · ')
    const travel = index ? { top: bottom, height: top - bottom, min, distance } : null
    return { ...work, top, height, travel }
  })
  return { points, blocks }
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
const height = computed(() => Math.max(y(to.value), ...blocks.value.map((block) => block.top + block.height)) + 12)
</script>

<template>
  <div class="relative mt-2" :style="{ height: `${height}px` }">
    <template v-for="hour in hours" :key="hour">
      <span class="muted small absolute left-0 -translate-y-1/2" :style="{ top: `${y(hour * 60)}px` }">{{ clockOf(hour * 60) }}</span>
      <span class="absolute right-0 left-12 h-px bg-field" :style="{ top: `${y(hour * 60)}px` }" />
    </template>
    <template v-for="block in blocks" :key="block.id">
      <!-- Линия переезда — по 2 px от блоков сверху и снизу, чтобы не прилипала к ним. -->
      <div v-if="block.travel" class="muted small absolute right-0 left-14 grid grid-cols-[1fr_auto_1fr] items-center gap-3" :style="{ top: `${block.travel.top + 2}px`, height: `${block.travel.height - 4}px` }">
        <span class="text-right">{{ block.travel.min }}</span>
        <span class="flex h-full w-6 flex-col items-center">
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
        @click="emit('select', block.id)"
      >
        <span class="flex gap-3">
          <span class="w-[3px] shrink-0 rounded-full bg-accent" />
          <span class="flex min-w-0 flex-1 flex-col gap-1">
            <span class="flex items-center justify-between gap-1.5">
              <span class="overflow-hidden text-ellipsis whitespace-nowrap">{{ block.title }}</span>
              <RiskBadge v-if="block.forecast && block.request" :forecast="block.forecast" :request="block.request" :start="block.plannedStart">{{ block.risk }}</RiskBadge>
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
