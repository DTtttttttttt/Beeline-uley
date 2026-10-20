<script setup lang="ts">
// Полоса долгого действия над кнопкой, которой его запустили: «Выбрать», «Пересчитать»,
// «Переназначить» и т. д. Идёт по оценке (composables/progress.ts) и до ответа не доходит до конца.
// Под полосой — сменяющаяся фраза хода расчёта (composables/phrases.ts); название действия остаётся
// в `aria-label`, а фраза диктору не зачитывается: она меняется каждые пару секунд.
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
      <Transition name="appear" mode="out-in">
        <span :key="bar.phrase" aria-live="off">{{ bar.phrase }}</span>
      </Transition>
      <span v-if="bar.remaining" class="shrink-0">осталось около {{ bar.remaining }} с</span>
    </p>
  </div>
</template>
