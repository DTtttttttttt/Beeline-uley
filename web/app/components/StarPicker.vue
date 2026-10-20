<script setup lang="ts">
// Выбор оценки звёздами — страница клиента (кадр 1779) и окно «Оценка выполнения заявки» (кадр 1754).
const stars = defineModel<number>({ required: true })
const hovered = ref(0)
</script>

<template>
  <div class="flex justify-center gap-1" role="radiogroup" aria-label="Оценка" @pointerleave="hovered = 0">
    <button
      v-for="value in 5"
      :key="value"
      type="button"
      role="radio"
      :aria-checked="stars === value"
      :aria-label="`${value} из 5`"
      class="cursor-pointer border-0 bg-transparent p-1 transition-colors duration-150"
      :class="value <= (hovered || stars) ? 'text-accent' : 'text-field'"
      @pointerenter="hovered = value"
      @click="stars = value"
    >
      <Icon name="star" />
    </button>
  </div>
</template>
