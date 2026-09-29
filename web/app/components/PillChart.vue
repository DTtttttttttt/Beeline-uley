<script setup lang="ts">
// Столбики-пилюли окна метрик (кадры 95 и 1526): одна вёрстка на все четыре графика.
// Высоты и порядок слоёв считает окно — здесь только раскладка. Пилюли растут от нуля;
// меньшая лежит поверх большей, иначе её число скрылось бы (кадр 95, «+1»).
export type PillKind = 'now' | 'before' | 'work' | 'wait' | 'travel'

export interface PillColumn {
  id: string
  name: string
  // Разница над столбиком: «−7,8» зелёным, «+1» красным.
  note?: { text: string; good: boolean } | null
  pills: { value: number; share: number; kind: PillKind; z: number }[]
}

const props = defineProps<{
  title: string
  caption: string
  legend: { kind: PillKind; label: string }[]
  columns: PillColumn[]
}>()

// Геометрия столбика — та же, что в классах разметки: высота `h-[260px]` (в макете столбики
// высокие), пилюля не ниже `min-h-7`, число строкой в 13 px с полями занимает около 24 px.
const HEIGHT = 260
const MIN_HEIGHT = 28
const LABEL = 24

/**
 * Высоты пилюль столбика снизу вверх. Пилюля, поднятая до `MIN_HEIGHT`, пропорций уже не держит,
 * и бо́льшая над ней встаёт на полосу числа выше: иначе у малых значений (10 и 18,7 км при шкале
 * до 240 км) обе поднимались бы до одной высоты, и бо́льшая пряталась бы под меньшей целиком.
 * Без подъёма высоты пропорциональны, и у близких значений видна только меньшая.
 */
function heights(pills: PillColumn['pills']) {
  const lifted = new Map<PillColumn['pills'][number], number>()
  let below: { share: number; px: number; raised: boolean } | null = null
  for (const pill of [...pills].sort((a, b) => a.share - b.share)) {
    const natural = pill.share * HEIGHT
    // Равная доля — та же высота: слой без минут в стопке не выше соседа.
    const floor = !below ? 0 : pill.share === below.share ? below.px : below.raised ? below.px + LABEL : 0
    const px = Math.max(natural, MIN_HEIGHT, floor)
    lifted.set(pill, px)
    below = { share: pill.share, px, raised: px > natural }
  }
  return lifted
}

/**
 * Пустая пилюля не рисуется: у простаивающей бригады «0» на жёлтом выглядел бы значением.
 * Видимая полоса пилюли — от её верха до верха самой высокой пилюли, что лежит поверх;
 * если в полосе не помещается число, оно залезло бы на соседа — пилюля остаётся без подписи.
 */
const shown = computed(() =>
  props.columns.map((column) => {
    const drawn = column.pills.filter((pill) => pill.share > 0)
    const px = heights(drawn)
    const pills = drawn.map((pill) => ({ ...pill, px: px.get(pill)! }))
    return {
      ...column,
      // Разница стоит над верхом столбика, как в макете, а не строкой над всем графиком.
      top: Math.max(0, ...pills.map((pill) => pill.px)),
      pills: pills.map((pill) => {
        const cover = Math.max(0, ...pills.filter((other) => other.z > pill.z).map((other) => other.px))
        // Число — по центру видимой полосы, как в макете, а не у верха пилюли.
        return { ...pill, label: pill.px - cover >= LABEL, middle: (pill.px - cover) / 2 }
      }),
    }
  }),
)

// Полные имена классов: Tailwind собирает только те, что написаны в исходниках целиком.
const COLOR: Record<PillKind, string> = {
  now: 'bg-accent',
  work: 'bg-accent',
  before: 'bg-[#dfe4ea]',
  wait: 'bg-[#dfe4ea]',
  travel: 'bg-muted text-white',
}
</script>

<template>
  <div class="card flex flex-col gap-1">
    <div class="font-medium">{{ title }}</div>
    <div class="muted small">{{ caption }}</div>
    <div class="muted small flex gap-4">
      <span v-for="item in legend" :key="item.kind">
        <i class="inline-block size-2.5 rounded-full align-[-1px]" :class="COLOR[item.kind]" /> {{ item.label }}
      </span>
    </div>
    <div class="no-scrollbar flex gap-1.5 overflow-x-auto pt-1.5">
      <div v-for="column in shown" :key="column.id" class="flex min-w-[26px] flex-[1_1_0] flex-col items-center gap-1" :title="column.name">
        <div class="relative h-[260px] w-full" :class="{ 'mt-5': columns.some((item) => item.note) }">
          <span
            v-if="column.note"
            class="small absolute right-0 left-0 text-center leading-4"
            :class="column.note.good ? 'text-[#2e9e5b]' : 'text-danger'"
            :style="{ bottom: `${column.top + 4}px` }"
          >{{ column.note.text }}</span>
          <span
            v-for="pill in column.pills"
            :key="pill.kind"
            class="absolute right-0 bottom-0 left-0 min-h-7 rounded-[17px]"
            :class="COLOR[pill.kind]"
            :style="{ height: `${pill.px}px`, zIndex: pill.z }"
          >
            <span
              v-if="pill.label"
              class="absolute right-0 left-0 -translate-y-1/2 text-center text-[13px] leading-4"
              :style="{ top: `${pill.middle}px` }"
            >{{ pill.value }}</span>
          </span>
        </div>
        <span class="muted small h-[60px] overflow-hidden text-ellipsis whitespace-nowrap [writing-mode:vertical-rl] rotate-180">{{ column.name }}</span>
      </div>
    </div>
  </div>
</template>
