<script setup lang="ts">
// Поле-карточка с выпадающим списком (docs/design.md, кадры 1474 и 1476): тип заявки, способ
// передвижения, квалификация, навыки и оборудование. Список всплывает над содержимым, а не
// раздвигает его: полупрозрачный, с размытием и тенью. Одиночный выбор — радиобаттоны,
// множественный — общий чекбокс `CheckMark`; появление списка и отметки — плавные.
export interface Choice {
  value: unknown
  label: string
}

const model = defineModel<unknown>({ required: true })

const props = defineProps<{
  label: string
  // Справочник «код → название» или готовый список, если значения не строки (null, индекс).
  options: Choice[] | Record<string, string>
  multiple?: boolean
  disabled?: boolean
  empty?: string
  invalid?: boolean
}>()

const choices = computed<Choice[]>(() =>
  Array.isArray(props.options)
    ? props.options
    : Object.entries(props.options).map(([value, label]) => ({ value, label })),
)
const picked = (value: unknown) =>
  props.multiple ? (model.value as unknown[]).includes(value) : model.value === value
const shown = computed(
  () =>
    choices.value
      .filter((choice) => picked(choice.value))
      .map((choice) => choice.label)
      .join(', ') ||
    props.empty ||
    'не выбрано',
)

const open = ref(false)
// Вверх — когда снизу список не помещается в видимую часть окна или панели, а сверху места
// больше: иначе у поля внизу окна он обрезался бы краем и закрывал кнопки подвала.
const up = ref(false)
const root = useTemplateRef<HTMLElement>('root')
const list = useTemplateRef<HTMLElement>('list')

/** Видимая область: ближайший предок, который прокручивает или обрезает, — и экран. */
function visibleArea(element: HTMLElement) {
  let top = 0
  let bottom = window.innerHeight
  for (let node = element.parentElement; node; node = node.parentElement) {
    if (getComputedStyle(node).overflowY === 'visible') continue
    const rect = node.getBoundingClientRect()
    top = Math.max(top, rect.top)
    bottom = Math.min(bottom, rect.bottom)
  }
  return { top, bottom }
}

// Поле поднято над соседями, пока список открыт **или ещё уходит**: снять слой вместе с `open`
// значило бы, что исчезающий список 150 мс рисуется под полями ниже.
const raised = ref(false)

async function toggle() {
  open.value = !open.value
  if (!open.value) return
  raised.value = true
  up.value = false
  await nextTick()
  if (!root.value || !list.value) return
  const field = root.value.getBoundingClientRect()
  const area = visibleArea(root.value)
  const below = area.bottom - field.bottom
  const above = field.top - area.top
  up.value = below < list.value.offsetHeight + 4 && above > below
}

function pick(value: unknown) {
  // Список после выбора не закрывается — ни одиночный, ни множественный: клик по полю, мимо или Escape.
  if (!props.multiple) {
    model.value = value
    return
  }
  const chosen = model.value as unknown[]
  model.value = chosen.includes(value) ? chosen.filter((item) => item !== value) : [...chosen, value]
}

// Закрывается кликом мимо и Escape; слушатели висят, только пока список открыт.
function outside(event: PointerEvent) {
  if (!root.value?.contains(event.target as Node)) open.value = false
}
function escape(event: KeyboardEvent) {
  if (event.key === 'Escape') open.value = false
}
watch(open, (now) => {
  const method = now ? 'addEventListener' : 'removeEventListener'
  document[method]('pointerdown', outside)
  document[method]('keydown', escape)
})
watch(() => props.disabled, (now) => now && (open.value = false))
onBeforeUnmount(() => (open.value = false))
</script>

<template>
  <div ref="root" class="relative" :class="{ 'z-30': raised }">
    <button
      type="button"
      class="field group relative w-full cursor-pointer border-0 text-left disabled:cursor-default"
      :class="{ invalid }"
      :disabled="disabled"
      :aria-expanded="open"
      @click="toggle"
    >
      <span>{{ label }}</span>
      <span class="pr-[33px] group-disabled:text-muted">{{ shown }}</span>
      <!-- Шеврон 14 px в 23 px от правого края (кадр 1786). -->
      <Icon
        v-if="!disabled"
        name="chevron"
        class="absolute top-1/2 right-[23px] -translate-y-1/2 transition-transform duration-200"
        :class="{ 'rotate-180': open }"
      />
    </button>
    <!-- Появляется от края у поля: прозрачность и лёгкое масштабирование. -->
    <Transition
      enter-active-class="transition duration-200 ease-out motion-reduce:transition-none"
      enter-from-class="opacity-0 scale-[0.97]"
      leave-active-class="transition duration-150 ease-in motion-reduce:transition-none"
      leave-to-class="opacity-0 scale-[0.97]"
      @after-leave="raised = open"
    >
      <div
        v-if="open"
        ref="list"
        class="dropdown absolute right-0 left-0"
        :class="up ? 'bottom-full mb-1 origin-bottom' : 'top-full mt-1 origin-top'"
        role="listbox"
        :aria-multiselectable="multiple"
      >
        <button
          v-for="choice in choices"
          :key="String(choice.value)"
          type="button"
          role="option"
          :aria-selected="picked(choice.value)"
          class="dropdown-item"
          @click="pick(choice.value)"
        >
          <CheckMark v-if="multiple" :checked="picked(choice.value)" :size="28" />
          <!-- Радиобаттон: при выборе кольцо плавно толще и жёлтое, точка вырастает из центра. -->
          <span v-else class="radio" :class="{ 'radio-on': picked(choice.value) }"><span /></span>
          <span>{{ choice.label }}</span>
        </button>
      </div>
    </Transition>
  </div>
</template>
