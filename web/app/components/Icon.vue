<script setup lang="ts">
// Иконка из `assets/icons` (docs/design.md, «Исходники оформления»). SVG вставляется
// разметкой, а не картинкой: контур нарисован `currentColor` и красится цветом текста —
// тёмная на сером, белая на тёмном, серая у неактивной кнопки.
const SOURCES = import.meta.glob('~/assets/icons/*.svg', { query: '?raw', import: 'default', eager: true }) as Record<
  string,
  string
>
const ICONS = Object.fromEntries(
  Object.entries(SOURCES).map(([path, svg]) => [path.split('/').pop()!.replace('.svg', ''), svg]),
)

const props = defineProps<{ name: string }>()
const svg = computed(() => ICONS[props.name] ?? '')
</script>

<template>
  <!-- eslint-disable-next-line vue/no-v-html -- свои файлы из репозитория, не ввод пользователя -->
  <span class="inline-flex shrink-0 [&>svg]:block" aria-hidden="true" v-html="svg" />
</template>
