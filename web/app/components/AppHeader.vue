<script setup lang="ts">
// Шапка (docs/design.md, кадры 99, 1508): логотип, «базовый / оптимизированный», участок,
// время дня, «Метрики», «Итоги дня» (правки 81.8), «Выгрузить» (блок 56), «Новый датасет». Даты нет: в наборе один день (PLAN 1.3). Время дня — машина
// времени (блок 33): на него показываются план и статусы, с ним уходят действия.
import { apiError, type Dataset, type Plan } from '~/composables/useApi'

const props = defineProps<{
  dataset: Dataset | null
  plan: Plan | null
  baseline: Plan | null
  optimized: Plan | null
  shown: 'baseline' | 'optimized'
  time: string
  // «Итоги дня» доступны: смены кончились (правки 81.8).
  summary: boolean
}>()

const emit = defineEmits<{
  show: [slot: 'baseline' | 'optimized']
  variants: []
  metrics: []
  dataset: []
  play: []
  summary: []
  error: [text: string]
}>()

const { exportPlan } = useApi()

/** «Выгрузить» (блок 56): файл показанной версии; отказ сервера — строкой под шапкой. */
async function download() {
  if (!props.plan) return
  try {
    const { blob, name } = await exportPlan(props.plan.id)
    const link = document.createElement('a')
    link.href = URL.createObjectURL(blob)
    link.download = name
    link.click()
    URL.revokeObjectURL(link.href)
  } catch (problem) {
    emit('error', apiError(problem))
  }
}

// Участок вместо адреса офиса: у демо-набора имя «Восток · штатный состав», состав в шапке не нужен.
const office = computed(() => props.dataset?.name.split(' · ')[0] ?? '')

/**
 * «Оптимизированный» (W): варианта ещё нет — окно «Варианты плана»; вариант есть, но показан
 * базовый — просто переключить; уже показан — повторное нажатие снова открывает список.
 */
function optimizedClick() {
  if (props.optimized && props.shown !== 'optimized') emit('show', 'optimized')
  else emit('variants')
}
</script>

<template>
  <!-- Кадр 1527: между элементами 20 px; у плашек времени и адреса поля 20, у кнопок — 24.
       Узкая шапка: сначала многоточием ужимаются участок и подписи кнопок, а когда содержимое уже
       1300 px — подписи пропадают, и в кнопке остаётся иконка по центру (подсказка — в title).
       Переключатель подписи не теряет: в режиме иконок он сам ужимается многоточием. -->
  <header class="@container flex items-center gap-2 rounded-[36px] bg-panel p-3.5 shadow-panel">
    <Icon name="logo" />
    <Segmented
      data-tour="plan-switch"
      class="min-w-fit flex-[1_1_380px] @max-[1300px]:min-w-0 @max-[1300px]:[&>button]:min-w-0"
      :model-value="plan ? shown : null"
      :options="[
        { value: 'baseline', label: 'базовый', icon: 'baseline', disabled: !baseline, title: 'Базовый по ТЗ (п. 2.3)' },
        { value: 'optimized', label: 'оптимизированный', icon: 'optimize', title: 'Оптимизированный' },
      ]"
      @update:model-value="(slot) => (slot === 'baseline' ? emit('show', 'baseline') : optimizedClick())"
    />
    <!-- Участок ужимается раньше кнопок (сжатие ×4), а когда содержимое шапки уже 900 px, пропадает,
         и его место забирает переключатель. -->
    <span v-if="office" class="btn min-w-0 flex-[0_4_auto] cursor-default !px-5 @max-[900px]:hidden" :title="office"><Icon name="office" /><span class="overflow-hidden text-ellipsis">{{ office }}</span></span>
    <!-- Время дня — справа от адреса (правки 82): полоса проигрывания встаёт под ним, и её хвостик
         смотрит на часы. -->
    <DayTime data-tour="time" class="!px-5" :time="time" :disabled="!plan" @open="emit('play')" />
    <button data-tour="metrics" class="btn min-w-0 !px-6 @max-[1300px]:shrink-0 @max-[1300px]:!px-3.5" title="Метрики" :disabled="!plan" @click="emit('metrics')"><Icon name="metrics" /><span class="truncate @max-[1300px]:hidden">Метрики</span></button>
    <button class="btn min-w-0 !px-6 @max-[1300px]:shrink-0 @max-[1300px]:!px-3.5" :disabled="!summary" :title="summary ? 'Итоги дня: работа по плану и по факту, скорость инженеров' : 'Итоги дня откроются, когда кончатся смены'" @click="emit('summary')"><Icon name="flag" /><span class="truncate @max-[1300px]:hidden">Итоги дня</span></button>
    <button class="btn min-w-0 !px-6 @max-[1300px]:shrink-0 @max-[1300px]:!px-3.5" :disabled="!plan" title="Выгрузить план показанной версии файлом CSV для Excel" @click="download"><Icon name="export" /><span class="truncate @max-[1300px]:hidden">Выгрузить</span></button>
    <button data-tour="dataset" class="btn primary min-w-0 !px-6 @max-[1300px]:shrink-0 @max-[1300px]:!px-3.5" title="Новый датасет" @click="emit('dataset')"><Icon name="dataset-new" /><span class="truncate @max-[1300px]:hidden">Новый датасет</span></button>
  </header>
</template>
