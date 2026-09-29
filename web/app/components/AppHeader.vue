<script setup lang="ts">
// Шапка (docs/design.md, кадры 99, 1508): логотип, «базовый / оптимизированный», время дня, адрес
// офиса, «Метрики», «Новый датасет». Даты нет: в наборе один день (PLAN 1.3). Время дня — машина
// времени (блок 33): на него показываются план и статусы, с ним уходят действия.
import type { Dataset, Plan } from '~/composables/useApi'

const props = defineProps<{
  dataset: Dataset | null
  plan: Plan | null
  baseline: Plan | null
  optimized: Plan | null
  shown: 'baseline' | 'optimized'
  // Время дня поставлено руками — тогда рядом «Сейчас», которое вернёт его к часам.
  pinnedTime: boolean
}>()

const time = defineModel<string>('time', { required: true })

const emit = defineEmits<{
  show: [slot: 'baseline' | 'optimized']
  variants: []
  metrics: []
  dataset: []
  now: []
}>()

const office = computed(() => props.plan?.input.office.address ?? props.dataset?.office.address ?? '')

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
  <header class="@container flex items-center gap-3 rounded-[36px] bg-panel p-3.5 shadow-panel">
    <Icon name="logo" class="mr-2" />
    <Segmented
      data-tour="plan-switch"
      class="min-w-fit flex-[1_1_380px]"
      :model-value="plan ? shown : null"
      :options="[
        { value: 'baseline', label: 'базовый', icon: 'baseline', disabled: !baseline },
        { value: 'optimized', label: 'оптимизированный', icon: 'optimize' },
      ]"
      @update:model-value="(slot) => (slot === 'baseline' ? emit('show', 'baseline') : optimizedClick())"
    />
    <DayTime data-tour="time" v-model="time" :pinned="pinnedTime" :disabled="!plan" @now="emit('now')" />
    <!-- Адрес ужимается многоточием, а когда содержимое шапки уже 1080 px (адресу осталось бы
         меньше ~180 px), пропадает, и его место забирают переключатель и время дня. -->
    <span v-if="office" class="btn min-w-0 flex-[0_1_auto] cursor-default @max-[1080px]:hidden" :title="office"><Icon name="office" /><span class="overflow-hidden text-ellipsis">{{ office }}</span></span>
    <button data-tour="metrics" class="btn" :disabled="!plan" @click="emit('metrics')"><Icon name="metrics" /> Метрики</button>
    <button data-tour="dataset" class="btn primary" @click="emit('dataset')"><Icon name="dataset-new" /> Новый датасет</button>
  </header>
</template>
