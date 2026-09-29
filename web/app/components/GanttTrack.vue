<script setup lang="ts">
// Дорожка дня одного инженера в гантте (docs/design.md, «Гантт всех инженеров», кадр 1480):
// блоки работ с заголовком, временем работы, адресом и ⚠, между ними «38 мин / 4,2 км», полоса смены и
// линия текущего момента. Шкалу задаёт список: она общая у всех дорожек.
import { clockMinutes, dayWorks, type Engineer, type Plan } from '~/composables/useApi'

const props = defineProps<{
  plan: Plan
  engineer: Engineer
  axis: { from: number; to: number; px: number }
  now: number | null
  // Цвет инженера — тот же, что у его маршрута на карте и точки в списке.
  color: string
}>()

// Фон блока — цвет инженера, разбавленный белым; полоска слева и круг ⚠ — сам цвет.
const tint = computed(() => `color-mix(in srgb, ${props.color} 16%, white)`)
// Знак ⚠ на круге цвета инженера: тёмный на светлом (жёлтый, салатовый), белый на тёмном.
const riskInk = computed(() => {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(props.color.slice(i, i + 2), 16))
  return 0.299 * r! + 0.587 * g! + 0.114 * b! > 170 ? 'var(--color-text)' : '#fff'
})

const emit = defineEmits<{ select: [requestId: string] }>()

/** Минуты от полуночи → пиксели дорожки. */
function x(minutes: number): number {
  return ((minutes - props.axis.from) / 60) * props.axis.px
}

const width = computed(() => x(props.axis.to))

const blocks = computed(() => {
  const works = dayWorks(props.plan, props.engineer.id)
  return works.map((work, index) => {
    const left = x(work.start)
    const previous = index ? x(works[index - 1]!.end) : null
    const [min, distance] = work.travel.split(' · ')
    const gap = previous === null ? 0 : left - previous
    // По 2 px от соседних блоков: линия не прилипает к ним.
    const room = gap - 4
    const road = (work.travelMin / 60) * props.axis.px
    return {
      ...work,
      left,
      width: Math.max(x(work.end) - left, 28),
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
      class="absolute inset-y-0 bg-[repeating-linear-gradient(45deg,#e2e7ec_0_4px,transparent_4px_8px)]"
      :style="{ left: `${gone.left}px`, width: `${gone.width}px` }"
      title="Инженер недоступен: новых работ не берёт"
    />
    <template v-for="block in blocks" :key="block.id">
      <span v-if="block.gap" class="muted absolute inset-y-0 flex flex-col items-center justify-center gap-0.5 text-xs whitespace-nowrap" :style="{ left: `${block.gap.left}px`, width: `${block.gap.width}px` }">
        <span>{{ block.gap.min }}</span>
        <span class="flex h-2 items-center self-start border-x border-[#cfd6dd] px-px" :style="{ width: `${block.gap.line}px` }">
          <span class="h-px w-full bg-[#cfd6dd]" />
        </span>
        <span>{{ block.gap.distance }}</span>
      </span>
      <!-- Полоска слева — во всю высоту текста, как в макете: её отступы сверху и снизу — поля блока. -->
      <button
        class="hoverable absolute inset-y-0 flex gap-2.5 overflow-hidden rounded-card border-0 px-3 py-2.5 text-left"
        :class="{ 'pr-7': block.forecast }"
        :style="{ left: `${block.left}px`, width: `${block.width}px`, background: tint }"
        :title="`№${block.id} · ${block.title} · ${block.detail}${block.committed ? ' · закреплена' : ''}`"
        @click="emit('select', block.id)"
      >
        <span class="w-[3px] shrink-0 rounded-full" :style="{ background: color }" />
        <span class="flex min-w-0 flex-1 flex-col justify-center gap-0.5">
          <span class="flex items-center gap-1.5">
            <!-- Закреплённый стоп — работа идёт или бригада уже выехала (PLAN 6.12). -->
            <Icon v-if="block.committed" name="check" class="shrink-0 [&_svg]:size-3" />
            <span class="overflow-hidden text-ellipsis whitespace-nowrap">{{ block.title }}</span>
          </span>
          <span class="muted small overflow-hidden text-ellipsis whitespace-nowrap">{{ block.detail }}</span>
        </span>
        <!-- ⚠ — в правом верхнем углу блока, кругом цвета инженера. -->
        <RiskBadge
          v-if="block.forecast && block.request"
          bare
          class="absolute top-1.5 right-1.5 size-5 justify-center rounded-full"
          :style="{ background: color, color: riskInk }"
          :forecast="block.forecast"
          :request="block.request"
          :start="block.plannedStart"
        />
      </button>
    </template>
    <span v-if="now !== null && now >= axis.from && now <= axis.to" class="pointer-events-none absolute -inset-y-1 w-0.5 bg-accent" :style="{ left: `${x(now)}px` }" />
  </div>
</template>
