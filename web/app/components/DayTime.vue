<script setup lang="ts">
// Время дня (кадры 1527, 1787, 1560) — плашка «🕒 12:56» в шапке, в мобильной версии — над картой.
// Нажатие открывает полосу проигрывания (блоки 63, 77): там шкала, ▶ и ввод времени вручную.
// Это машина времени (блок 33): на показанное время видны план и статусы, с ним уходят действия.
defineProps<{ time: string; disabled: boolean }>()
const emit = defineEmits<{ open: [] }>()
</script>

<template>
  <!-- Сервер и браузер рисуют разную минуту часов — расхождение текста при гидратации ожидаемо. -->
  <button
    data-allow-mismatch="text"
    class="btn tabular-nums"
    :disabled="disabled"
    title="Время дня: план и статусы на этот момент. Нажмите — шкала и проигрывание дня"
    @click="emit('open')"
  >
    <Icon name="clock" />{{ time }}
  </button>
</template>
