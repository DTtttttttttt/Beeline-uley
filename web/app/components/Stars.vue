<script setup lang="ts">
// Звёзды рейтинга для показа (кадры 1481, 1522, 1753): дробная оценка — с долей звезды.
// Жёлтый ряд поверх серого, обрезанный по `value / 5`: 4,5 — четыре с половиной.
import { decimal } from '~/composables/useApi'

defineProps<{ value: number }>()
</script>

<template>
  <!-- Звезда — `--star` (24 px), промежуток — `--star-gap` (Figma: 4 px в заявке, 6 px у инженера);
       дробная доля режется по ширине с учётом промежутков. Серый — #c0c7d2 (Figma). -->
  <span class="relative inline-flex shrink-0 [&_svg]:size-[var(--star,24px)]" :aria-label="`${decimal(value, 1)} из 5`" role="img">
    <span class="flex gap-[var(--star-gap,4px)] text-[#c0c7d2]"><Icon v-for="index in 5" :key="index" name="star" /></span>
    <span
      class="absolute inset-y-0 left-0 flex gap-[var(--star-gap,4px)] overflow-hidden text-accent"
      :style="{ width: `calc(${value} * var(--star, 24px) + ${Math.min(4, Math.floor(value))} * var(--star-gap, 4px))` }"
    >
      <Icon v-for="index in 5" :key="index" name="star" class="shrink-0" />
    </span>
  </span>
</template>
