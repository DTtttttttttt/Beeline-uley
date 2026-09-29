<script setup lang="ts">
// «Метрики плана «…»» (docs/design.md): из вариантов — кадр 95 (карточки, сравнение пробега и
// времени работы), из шапки — кадр 1526 (пробег и «переезд / ожидание / работа» по бригадам,
// те же два сравнения, без карточек — как в макете). Столбики — обычная вёрстка (`PillChart`).
import type { PillColumn } from '~/components/PillChart.vue'
import {
  ALGORITHM_NAMES,
  dayWorks,
  decimal,
  duration,
  plural,
  VARIANT_NAMES,
  VARIANT_NOTES,
  type Plan,
} from '~/composables/useApi'

const props = defineProps<{
  plan: Plan
  baseline: Plan | null
  // Откуда открыто — раскладка: из вариантов — кадр 95, из шапки — кадр 1526 с «Ок».
  from: 'header' | 'variants'
  // «Выбрать» — только у варианта из ответа сравнения. Вариант, по которому уже идёт день,
  // открывается текущей версией: он из вариантов, но выбирать его нечего.
  choose: boolean
}>()

const emit = defineEmits<{ back: []; choose: [plan: Plan] }>()

const name = computed(() =>
  props.plan.variant ? (VARIANT_NAMES[props.plan.variant] ?? props.plan.variant) : ALGORITHM_NAMES[props.plan.algorithm],
)
// Порядок критериев у каждого варианта свой (кадр 1491 верен только для «Максимум заявок»).
const note = computed(() => (props.plan.variant ? (VARIANT_NOTES[props.plan.variant] ?? '') : ''))

// Разница — только с посчитанным базовым и только если показан не он сам.
const base = computed(() => (props.baseline && props.baseline.id !== props.plan.id ? props.baseline : null))
// Базовый — утренний расчёт; у версии после события в разницу входит и само событие.
const eventNote = computed(() =>
  base.value && props.plan.event
    ? `План пересчитан после события в ${props.plan.event.time}, базовый — утренний: разница включает и само событие.`
    : null,
)

const comma = decimal

/** «+4», «−2», «−25 км» — со знаком; ноль разницы — «как у базового». */
function delta(value: number, digits = 0, unit = ''): string {
  const rounded = Number(value.toFixed(digits))
  if (!rounded) return 'как у базового'
  return `${rounded > 0 ? '+' : '−'}${comma(Math.abs(rounded), digits)}${unit} к базовому`
}

const cards = computed(() => {
  const m = props.plan.metrics
  const b = base.value?.metrics
  return {
    assigned: {
      value: `${m.assignedCount}/${plural(props.plan.input.requests.length, ['заявки', 'заявок', 'заявок'])} назначено`,
      delta: b ? delta(m.assignedCount - b.assignedCount) : null,
    },
    engineers: {
      value: `${plural(m.engineersUsed, ['инженер', 'инженера', 'инженеров'])} задействовано`,
      delta: b ? delta(m.engineersUsed - b.engineersUsed) : null,
    },
    km: {
      value: `${comma(m.totalKm, 1)} км общий пробег`,
      delta: b ? delta(m.totalKm - b.totalKm, 1, ' км') : null,
    },
  }
})

/** «экономия 25 км (−11,7 %)» — или перерасход, если плану пришлось ездить больше. */
const saving = computed(() => {
  if (!base.value) return null
  const before = base.value.metrics.totalKm
  const diff = props.plan.metrics.totalKm - before
  const word = diff <= 0 ? 'экономия' : 'перерасход'
  const text = `${word} ${comma(Math.abs(diff), 1)} км`
  return before ? `${text} (${diff <= 0 ? '−' : '+'}${comma(Math.abs((diff / before) * 100), 1)}%)` : text
})

/** «+3 ч работы (+6,1 %)» — разница суммарного времени работы, из точных минут метрик. */
const workGain = computed(() => {
  if (!base.value) return null
  const before = base.value.metrics.workMin
  const diff = props.plan.metrics.workMin - before
  if (!diff) return 'время работы как у базового'
  const sign = diff > 0 ? '+' : '−'
  const text = `${sign}${duration(Math.abs(diff))} работы`
  // У базового без единой работы процент не определён — только часы.
  return before ? `${text} (${sign}${comma((Math.abs(diff) / before) * 100, 1)}%)` : text
})

/**
 * Минуты дня инженера — суммы по его работам (`dayWorks`). Работа точная. Ожидание считается
 * по часам стопов, а они усечены до минуты: прибытие усекается вниз, а начало после ожидания
 * обычно ровно начало окна, поэтому ожидание завышено — до минуты на каждый стоп с ожиданием.
 */
function dayMinutes(plan: Plan) {
  return new Map(
    plan.routes.map((route) => [
      route.engineerId,
      dayWorks(plan, route.engineerId).reduce(
        (day, work) => ({ work: day.work + work.workMin, wait: day.wait + work.waitMin, travel: day.travel + work.travelMin }),
        { work: 0, wait: 0, travel: 0 },
      ),
    ]),
  )
}

const minutes = computed(() => dayMinutes(props.plan))
const baseMinutes = computed(() => (base.value ? dayMinutes(base.value) : null))

// Инженеры — в порядке входа плана.
const engineers = computed(() => props.plan.input.engineers)

/** Одна пилюля на инженера — «Пробег по бригадам». */
function single(values: Record<string, number>): PillColumn[] {
  const top = Math.max(1, ...Object.values(values))
  return engineers.value.map((engineer) => {
    const value = values[engineer.id] ?? 0
    return { id: engineer.id, name: engineer.name, pills: [{ value: Math.round(value), share: value / top, kind: 'now', z: 1 }] }
  })
}

/** Переезд, ожидание и работа стопкой: каждая пилюля от нуля до своей накопленной высоты, нижняя поверх. */
function stacked(): PillColumn[] {
  const days = engineers.value.map((engineer) => minutes.value.get(engineer.id) ?? { work: 0, wait: 0, travel: 0 })
  const top = Math.max(1, ...days.map((day) => day.work + day.wait + day.travel))
  return engineers.value.map((engineer, index) => {
    const { work, wait, travel } = days[index]!
    const layers = [
      { value: travel, share: (work + wait + travel) / top, kind: 'travel' as const, z: 0 },
      { value: wait, share: (work + wait) / top, kind: 'wait' as const, z: 1 },
      { value: work, share: work / top, kind: 'work' as const, z: 2 },
    ]
    // Пустой слой и число тонкого слоя убирает `PillChart`: он знает свою геометрию.
    return { id: engineer.id, name: engineer.name, pills: layers }
  })
}

/**
 * Базовый и выбранный рядом, обе пилюли от нуля. У бригады, вышедшей событием, базового
 * значения нет. Цвет разницы — по направлению «куда лучше»: пробег лучше меньше, работа — больше.
 */
function compare(now: (id: string) => number, before: ((id: string) => number | null) | null, moreIsBetter: boolean, digits: number): PillColumn[] {
  const rows = engineers.value.map((engineer) => ({ engineer, now: now(engineer.id), before: before?.(engineer.id) ?? null }))
  const top = Math.max(1, ...rows.flatMap((row) => [row.now, row.before ?? 0]))
  return rows.map(({ engineer, now: value, before: was }) => {
    const diff = was === null ? 0 : Number((value - was).toFixed(digits))
    const pills: PillColumn['pills'] = [{ value: Math.round(value), share: value / top, kind: 'now', z: 1 }]
    if (was !== null) pills.unshift({ value: Math.round(was), share: was / top, kind: 'before', z: was < value ? 2 : 0 })
    return {
      id: engineer.id,
      name: engineer.name,
      note: diff ? { text: `${diff > 0 ? '+' : '−'}${comma(Math.abs(diff), digits)}`, good: diff > 0 === moreIsBetter } : null,
      pills,
    }
  })
}

const kmColumns = computed(() => single(props.plan.metrics.kmByEngineer))
const dayColumns = computed(() => stacked())
const kmCompare = computed(() =>
  compare(
    (id) => props.plan.metrics.kmByEngineer[id] ?? 0,
    base.value ? (id) => base.value!.metrics.kmByEngineer[id] ?? null : null,
    false,
    1,
  ),
)
const workCompare = computed(() =>
  compare(
    (id) => minutes.value.get(id)?.work ?? 0,
    baseMinutes.value ? (id) => baseMinutes.value!.get(id)?.work ?? null : null,
    true,
    0,
  ),
)
</script>

<template>
  <Modal :title="`Метрики плана «${name}»`" wide @back="emit('back')">
    <p v-if="note && from === 'variants'" class="muted m-0">{{ note }}</p>
    <p v-if="plan.fallbackToBaseline" class="error small">Решения не нашлось — показан базовый план.</p>
    <p v-if="eventNote" class="muted small m-0">{{ eventNote }}</p>
    <!-- Карточки — только кадр 95: в кадре 1526 (из шапки) их нет. -->
    <template v-if="from === 'variants'">
      <div class="card flex flex-col gap-0.5">
        <span>{{ cards.assigned.value }}</span>
        <span v-if="cards.assigned.delta" class="muted small">{{ cards.assigned.delta }}</span>
      </div>
      <div class="grid grid-cols-2 gap-2.5">
        <div class="card flex flex-col gap-0.5">
          <span>{{ cards.engineers.value }}</span>
          <span v-if="cards.engineers.delta" class="muted small">{{ cards.engineers.delta }}</span>
        </div>
        <div class="card flex flex-col gap-0.5">
          <span>{{ cards.km.value }}</span>
          <span v-if="cards.km.delta" class="muted small">{{ cards.km.delta }}</span>
        </div>
      </div>
    </template>

    <!-- Кадр 1526 (из шапки); без базового — и из вариантов, иначе окно осталось бы без графиков. -->
    <template v-if="from === 'header' || !base">
      <PillChart
        title="Пробег по инженерам"
        caption="Суммарное расстояние, которое каждый инженер преодолеет для выполнения назначенных заявок, км"
        :legend="[]"
        :columns="kmColumns"
      />
      <PillChart
        title="Переезд, ожидание и работа по инженерам"
        caption="Распределение времени на выполнение заявок, переезды и ожидание, мин"
        :legend="[
          { kind: 'work', label: 'Работа' },
          { kind: 'wait', label: 'Ожидание' },
          { kind: 'travel', label: 'Переезд' },
        ]"
        :columns="dayColumns"
      />
    </template>
    <template v-if="base">
      <PillChart
        title="Сравнение пробега инженеров"
        :caption="`Базовый и «${name}» · ${saving}`"
        :legend="[
          { kind: 'before', label: 'Базовый, км' },
          { kind: 'now', label: 'Новый, км' },
        ]"
        :columns="kmCompare"
      />
      <PillChart
        title="Сравнение времени работы инженеров"
        :caption="`Базовый и «${name}» · ${workGain}`"
        :legend="[
          { kind: 'before', label: 'Базовый, мин' },
          { kind: 'now', label: 'Новый, мин' },
        ]"
        :columns="workCompare"
      />
    </template>

    <template #footer>
      <button v-if="from === 'header'" class="btn primary w-full" @click="emit('back')"><Icon name="check" /> Ок</button>
      <div v-else class="grid grid-cols-2 gap-2.5 [&>:only-child]:col-span-full">
        <button class="btn" @click="emit('back')"><Icon name="arrow-left" /> Назад</button>
        <button v-if="choose" class="btn primary" @click="emit('choose', plan)"><Icon name="check" /> Выбрать</button>
      </div>
    </template>
  </Modal>
</template>
