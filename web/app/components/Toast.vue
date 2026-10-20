<script setup lang="ts">
// Уведомление под шапкой (блок 69): одно на страницу — сейчас об оценке заявки. «План перестроен
// из-за задержки» убрано (блок 82): перестройку видно в «План обновлён» и в гантте. Нажатие —
// действие уведомления, ✕ убирает; гаснет само через 10 с.
const props = defineProps<{ text: string }>()
const emit = defineEmits<{ open: []; close: [] }>()

let timer: ReturnType<typeof setTimeout> | undefined
watch(
  () => props.text,
  () => {
    clearTimeout(timer)
    timer = setTimeout(() => emit('close'), 10_000)
  },
  { immediate: true },
)
onBeforeUnmount(() => clearTimeout(timer))
</script>

<template>
  <div role="status" class="flex w-fit items-center gap-1 rounded-xl bg-panel py-1 pr-1 pl-3 shadow-panel">
    <button class="small cursor-pointer border-0 bg-transparent p-0 text-left text-text underline-offset-2 hover:underline" @click="emit('open')">
      {{ text }}
    </button>
    <button class="round size-7 shrink-0" title="Закрыть" aria-label="Закрыть" @click="emit('close')"><Icon name="close" /></button>
  </div>
</template>
