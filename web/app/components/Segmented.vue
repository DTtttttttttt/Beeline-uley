<script setup lang="ts" generic="T extends string">
// Переключатель-сегмент: «базовый / оптимизированный», «инженеры / заявки», «данные / график»,
// «демо-набор / загрузить данные». Белая подложка выбранного пункта плавно переезжает к нему,
// текст плавно темнеет. Положение подложки — по самой кнопке: ширина у кнопок разная.
// Клик отдаётся и по уже выбранному пункту: в шапке повторное нажатие открывает варианты.
const props = defineProps<{
  modelValue: T | null // null — ничего не выбрано, подложки нет
  options: { value: T; label: string; icon: string; disabled?: boolean; title?: string }[]
}>()

const emit = defineEmits<{ 'update:modelValue': [value: T] }>()

const root = useTemplateRef<HTMLElement>('root')
const thumb = ref<{ left: number; width: number } | null>(null)
// Первое положение ставится без анимации: иначе подложка выезжала бы от края при каждом показе.
const ready = ref(false)

function measure() {
  const index = props.options.findIndex((option) => option.value === props.modelValue)
  // Кнопки — из DOM по порядку: массив ref из v-for порядок списка не обещает.
  const button = index >= 0 ? root.value?.querySelectorAll('button')[index] : null
  if (button) thumb.value = { left: button.offsetLeft, width: button.offsetWidth }
  else thumb.value = null
}

let observer: ResizeObserver | null = null
onMounted(() => {
  measure()
  requestAnimationFrame(() => (ready.value = true))
  observer = new ResizeObserver(measure)
  if (root.value) observer.observe(root.value)
})
onBeforeUnmount(() => observer?.disconnect())
watch(() => [props.modelValue, props.options.length], () => nextTick(measure))
</script>

<template>
  <div ref="root" class="segmented relative isolate">
    <span
      aria-hidden="true"
      class="absolute top-[2px] bottom-[2px] -z-10 rounded-full bg-panel ease-out"
      :class="[ready && 'transition-[left,width,opacity] duration-300', !thumb && 'opacity-0']"
      :style="thumb ? { left: `${thumb.left}px`, width: `${thumb.width}px` } : undefined"
    />
    <button
      v-for="option in options"
      :key="option.value"
      type="button"
      class="transition-colors duration-300"
      :class="{ '!text-text': option.value === modelValue }"
      :disabled="option.disabled"
      :title="option.title"
      @click="emit('update:modelValue', option.value)"
    >
      <Icon :name="option.icon" /><span class="truncate">{{ option.label }}</span>
    </button>
  </div>
</template>
