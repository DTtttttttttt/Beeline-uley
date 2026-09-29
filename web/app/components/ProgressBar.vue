<script setup lang="ts">
// Полоса долгого действия над кнопкой, которой его запустили: «Выбрать», «Пересчитать»,
// «Переназначить» и т. д. Идёт по оценке (composables/progress.ts) и до ответа не доходит до конца.
import type { Bar } from '~/composables/progress'

const props = defineProps<{ bar: Bar }>()

const percent = computed(() => Math.round(props.bar.value * 100))
</script>

<template>
  <div class="flex flex-col gap-1">
    <div
      class="h-1.5 overflow-hidden rounded-full bg-field"
      role="progressbar"
      :aria-label="bar.title"
      aria-valuemin="0"
      aria-valuemax="100"
      :aria-valuenow="percent"
    >
      <div class="h-full rounded-full bg-accent transition-[width] duration-200 ease-linear motion-reduce:transition-none" :style="{ width: `${percent}%` }" />
    </div>
    <p class="muted small m-0 flex justify-between gap-2">
      <span>{{ bar.title }}</span>
      <span class="shrink-0">{{ bar.remaining ? `осталось около ${bar.remaining} с` : 'почти готово' }}</span>
    </p>
  </div>
</template>
