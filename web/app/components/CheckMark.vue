<script setup lang="ts">
// Отметка «выбрано» — круг, как в кадре 1474: тонкое кольцо, у отмеченного жёлтая заливка и тёмная
// галочка. Одна на весь интерфейс: списки выбора, варианты, переназначение, формат загрузки,
// «весь день заново». Плавная: заливка проявляется, галочка прорисовывается штрихом. Кольцо —
// тень внутрь, а не рамка: так размер не зависит от толщины обводки.
withDefaults(defineProps<{ checked: boolean; size?: number }>(), { size: 26 })
</script>

<template>
  <span
    class="flex shrink-0 items-center justify-center rounded-full transition-[background-color,box-shadow] duration-200"
    :class="checked ? 'bg-accent shadow-[inset_0_0_0_1.5px_var(--color-accent)]' : 'shadow-[inset_0_0_0_1.5px_#c5ccd6]'"
    :style="{ width: `${size}px`, height: `${size}px` }"
    aria-hidden="true"
  >
    <svg viewBox="0 0 12 12" class="w-[55%]">
      <path
        d="M2.5 6.3 5 8.6l4.5-5.1"
        pathLength="1"
        fill="none"
        stroke="currentColor"
        stroke-width="1.8"
        stroke-linecap="round"
        stroke-linejoin="round"
        class="transition-[stroke-dashoffset] duration-200 ease-out [stroke-dasharray:1]"
        :class="checked ? '[stroke-dashoffset:0]' : '[stroke-dashoffset:1]'"
      />
    </svg>
  </span>
</template>
