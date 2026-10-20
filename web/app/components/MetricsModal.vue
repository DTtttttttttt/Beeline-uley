<script setup lang="ts">
// «Метрики плана «…»» (docs/design.md): из вариантов — кадр 95 (карточки, сравнение пробега и
// времени работы), из шапки — кадр 1526 (пробег и «переезд / ожидание / работа» по бригадам,
// те же два сравнения, без карточек — как в макете). Столбики — обычная вёрстка (`PillChart`).
import type { PillColumn } from '~/components/PillChart.vue'
import {
  ALGORITHM_NAMES,
  COMPLEXITY_LEVEL_NAMES,
  dayWorks,
  decimal,
  duration,
  plural,
  VARIANT_NAMES,
  VARIANT_NOTES,
  type ComplexityLevel,
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
  // Основной вариант («Аварии в приоритете») — к нему разница рейтинга (раздел 1.6 задания).
  main: Plan | null
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

// Выполнимые (блок 50) — в обеих раскладках: у планов до блока их нет.
const reach = computed(() => {
  const m = props.plan.metrics
  if (m.requestsCeiling == null) return null
  const share = m.requestsCeiling ? Math.round((m.assignedCount / m.requestsCeiling) * 100) : 100
  return `Выполнено ${m.assignedCount} из ${m.requestsCeiling} выполнимых (${share} %)`
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

// Сложность объектов (PLAN 9.1, блок 67) — только у плана, посчитанного с учётом сложности.
const complexity = computed(() => {
  const plan = props.plan
  if (plan.input.durationMode !== 'complexity') return null
  const levels = plan.metrics.objectsByLevel ?? {}
  const top = Math.max(1, ...Object.values(levels).map((value) => value ?? 0))
  const extra = plan.metrics.complexityExtraMin ?? 0
  const norm = plan.normMetrics
  const line = (assigned: number, used: number, totalKm: number) =>
    `${assigned} назначено · ${plural(used, ['инженер', 'инженера', 'инженеров'])} · ${comma(totalKm, 1)} км`
  return {
    extra: `Добавлено из-за сложности: ${extra < 0 ? '−' : '+'}${Math.abs(extra)} мин`,
    columns: (Object.keys(COMPLEXITY_LEVEL_NAMES) as ComplexityLevel[]).map((level) => {
      const value = levels[level] ?? 0
      return { id: level, name: COMPLEXITY_LEVEL_NAMES[level], pills: [{ value, share: value / top, kind: 'now' as const, z: 1 }] }
    }),
    compare: norm
      ? {
          norm: line(norm.assignedCount, norm.engineersUsed, norm.totalKm),
          now: line(plan.metrics.assignedCount, plan.metrics.engineersUsed, plan.metrics.totalKm),
        }
      : null,
  }
})

/**
 * Рейтинг (блок 72) — только у «Рейтинга в приоритете»: две цифры с разницей к основному варианту и
 * заявки у инженеров слева направо по рейтингу — видно, что лучшие загружены сильнее. Рейтинг —
 * снимок на момент расчёта, тот же, по которому считал оптимизатор.
 */
const rating = computed(() => {
  const plan = props.plan
  if (plan.variant !== 'rating_first') return null
  const main = props.main && props.main.id !== plan.id ? props.main.metrics : null
  const change = (now: number | null | undefined, was: number | null | undefined, digits: number) => {
    if (now == null || was == null) return null
    const diff = Number((now - was).toFixed(digits))
    return diff ? `${diff > 0 ? '+' : '−'}${comma(Math.abs(diff), digits)} к основному` : 'как у основного'
  }
  const stops = new Map(plan.routes.map((route) => [route.engineerId, route.stops.length]))
  // При равной оценке — той, что подписана над столбиком, до десятых, — левее тот, у кого больше
  // заявок (правки 81.3).
  const shown = (stars: number | undefined) => Math.round((stars ?? 0) * 10)
  const loaded = engineers.value
    .filter((engineer) => stops.get(engineer.id))
    .sort((a, b) => shown(b.rating?.stars) - shown(a.rating?.stars) || stops.get(b.id)! - stops.get(a.id)!)
  const top = Math.max(1, ...stops.values())
  const avg = plan.metrics.ratingAvg
  return {
    avg: avg == null ? 'нет оценок' : comma(avg, 2),
    avgDelta: change(avg, main?.ratingAvg, 2),
    top: String(plan.metrics.topRatedCount ?? 0),
    topDelta: change(plan.metrics.topRatedCount, main?.topRatedCount, 0),
    columns: loaded.map((engineer): PillColumn => {
      const value = stops.get(engineer.id)!
      const stars = engineer.rating?.stars
      return {
        id: engineer.id,
        name: engineer.name,
        // Оценка над столбиком без цвета (правки 81.3): это подпись, а не «лучше / хуже».
        note: { text: stars == null ? '—' : `★${comma(stars, 1)}`, good: null },
        pills: [{ value, share: value / top, kind: 'now', z: 1 }],
      }
    }),
  }
})

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

    <template v-if="rating">
      <div class="grid grid-cols-2 gap-2.5">
        <div class="card flex flex-col gap-0.5">
          <span class="muted small">Средний рейтинг исполнителя</span>
          <span>{{ rating.avg }}</span>
          <span v-if="rating.avgDelta" class="muted small">{{ rating.avgDelta }}</span>
        </div>
        <div class="card flex flex-col gap-0.5">
          <span class="muted small">У инженеров с рейтингом 4.5+</span>
          <span>{{ rating.top }}</span>
          <span v-if="rating.topDelta" class="muted small">{{ rating.topDelta }}</span>
        </div>
      </div>
      <PillChart
        title="Заявок у инженера"
        caption="Инженеры по убыванию рейтинга"
        :legend="[]"
        :columns="rating.columns"
      />
    </template>

    <!-- Дорога до первой заявки (блок 73): что даёт начало дня из дома. -->
    <div v-if="plan.metrics.firstLegMin != null" class="card flex flex-col gap-0.5">
      <span>Дорога до первой заявки: в среднем {{ plan.metrics.firstLegMin }} мин</span>
      <span class="muted small">От офиса или дома до первой заявки</span>
    </div>

    <div v-if="reach" class="card flex flex-col gap-0.5">
      <span>{{ reach }}</span>
      <span class="muted small">Выполнимая — может быть сделана, хотя бы одним из инженеров</span>
    </div>

    <template v-if="complexity">
      <div class="card flex flex-col gap-0.5">
        <span>{{ complexity.extra }}</span>
        <span class="muted small">Сверх норматива у назначенных заявок: сложность объектов и скорость инженеров</span>
      </div>
      <div v-if="complexity.compare" class="grid grid-cols-2 gap-2.5">
        <div class="card flex flex-col gap-0.5">
          <span class="muted small">По нормативу</span>
          <span>{{ complexity.compare.norm }}</span>
        </div>
        <div class="card flex flex-col gap-0.5">
          <span class="muted small">С учётом сложности</span>
          <span>{{ complexity.compare.now }}</span>
        </div>
      </div>
      <PillChart
        title="Объекты по сложности"
        caption="Заявки дня: простые — ×1,0 и меньше, средние — до ×1,4, сложные — больше"
        :legend="[]"
        :columns="complexity.columns"
      />
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
