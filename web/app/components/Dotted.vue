<script setup lang="ts">
// Части через точку с отступами 6 (Figma): строка «4 подключения · 1 локальная заявка» делится
// по « · ». Точка 2 px чуть ниже середины строки — подпись инженера и чипы «все · 12» (кадр 4);
// точка 3 px по середине — «70 → 94 мин · +24 мин» (кадры 1522, 1786). Цвет — цвет текста;
// длинная строка переносится по частям.
const props = withDefaults(defineProps<{ text: string; size?: 2 | 3 }>(), { size: 2 })
const parts = computed(() => props.text.split(' · '))
</script>

<template>
  <span class="inline-flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
    <template v-for="(part, index) in parts" :key="index">
      <span
        v-if="index"
        class="shrink-0 rounded-full bg-current"
        :class="size === 2 ? 'relative top-[1.5px] size-0.5' : 'size-[3px]'"
        aria-hidden="true"
      />
      <span class="whitespace-nowrap">{{ part }}</span>
    </template>
  </span>
</template>
