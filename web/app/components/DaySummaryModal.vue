<script setup lang="ts">
// «Итоги дня» (блок 69, раздел 4.1 задания): открываются кнопкой в шапке, когда кончились смены
// (правки 82). Пока часы догоняют день и сводка последней версии не пришла (`summary: null`), вместо
// плашек и графиков — их заготовки того же размера, над «Ок» — полоса прогресса. Графиками, как в метриках (правки 81.8):
// время работы по плану серым и по факту жёлтым у каждого инженера (суммы выполненных заявок, без
// дороги и ожидания) и скорость выполнения на начало дня и сейчас — в процентах времени нормы.
import type { PillColumn } from '~/components/PillChart.vue'
import type { Bar } from '~/composables/progress'
import { plural, type DaySummary } from '~/composables/useApi'

const props = defineProps<{ summary: DaySummary | null; progress: Bar | null }>()
const emit = defineEmits<{ back: [] }>()

type Row = DaySummary['engineers'][number]

/** Было и стало рядом, обе пилюли от нуля; над столбиком — разница, зелёная, если стало меньше. */
function pair(rows: Row[], before: (row: Row) => number, now: (row: Row) => number, unit: string): PillColumn[] {
  const top = Math.max(1, ...rows.flatMap((row) => [before(row), now(row)]))
  return rows.map((row) => {
    const [was, is] = [before(row), now(row)]
    const diff = Math.round(is - was)
    return {
      id: row.engineerId,
      name: row.name,
      note: diff ? { text: `${diff > 0 ? '+' : '−'}${Math.abs(diff)}${unit}`, good: diff < 0 } : null,
      pills: [
        { value: Math.round(was), share: was / top, kind: 'before', z: was < is ? 2 : 0 },
        { value: Math.round(is), share: is / top, kind: 'now', z: 1 },
      ],
    }
  })
}

const minutes = computed(() => pair(props.summary?.engineers ?? [], (row) => row.planMin, (row) => row.factMin, ''))
// Скорость в тысячных — время работы к нормативу: 1120 — 112 %, на 12 % дольше нормы.
const speeds = computed(() => pair(props.summary?.engineers ?? [], (row) => row.speedStart / 10, (row) => row.speedNow / 10, '%'))

// Плашки над графиками, как в метриках (правки 82): все четыре, нули тоже.
const rest = computed(() => {
  const summary = props.summary
  if (!summary) return []
  return [
    ['Отменены', summary.cancelled.length],
    ['Перенесены на завтра', summary.deferred.length],
    ['Не выполнены', summary.failed.length],
    ['Не назначены', summary.unassigned.length],
  ] as const
})
</script>

<template>
  <Modal title="Итоги дня" wide @back="emit('back')">
    <template v-if="summary">
      <div class="grid grid-cols-2 gap-2.5">
        <div v-for="[label, count] in rest" :key="label" class="card flex flex-col gap-0.5">
          <span class="muted small">{{ label }}</span>
          <span>{{ plural(count, ['заявка', 'заявки', 'заявок']) }}</span>
        </div>
      </div>
      <PillChart
        title="Время работы по инженерам"
        caption="Выполненные заявки без дороги и ожидания, мин; над столбиком — факт минус план"
        :legend="[
          { kind: 'before', label: 'По плану' },
          { kind: 'now', label: 'По факту' },
        ]"
        :columns="minutes"
      />
      <PillChart
        title="Скорость выполнения"
        caption="Скорость работы инженеров по сравнению с нормативом, %. 100% - по нормативу, чем медленнее работает инженер, тем процент больше. Над столбиком изменение за день"
        :legend="[
          { kind: 'before', label: 'Было' },
          { kind: 'now', label: 'Стало' },
        ]"
        :columns="speeds"
      />
    </template>
    <!-- Пока часы догоняют день: заготовки плашек и графиков, чтобы окно не прыгало. -->
    <template v-else>
      <div class="grid grid-cols-2 gap-2.5" role="status" aria-label="Итоги дня загружаются">
        <div v-for="index in 4" :key="index" class="card flex flex-col gap-1.5" aria-hidden="true">
          <span class="skeleton h-3 w-3/5 rounded-full" />
          <span class="skeleton h-3.5 w-2/5 rounded-full" />
        </div>
      </div>
      <div v-for="index in 2" :key="index" class="skeleton h-64 rounded-card" aria-hidden="true" />
    </template>
    <template #footer>
      <ProgressBar v-if="progress" :bar="progress" />
      <button class="btn primary wide" @click="emit('back')"><Icon name="check" /> Ок</button>
    </template>
  </Modal>
</template>
