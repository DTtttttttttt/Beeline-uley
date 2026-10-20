<script setup lang="ts">
// Подсказка к значку (блок 75.2, раздел «Общее» задания): на компьютере — наведение или фокус, на
// телефоне — нажатие; закрывается уходом курсора, потерей фокуса или нажатием мимо. Подсказка уходит
// в `body` с фиксированной позицией: списки и карточки лежат в прокрутке панели, и внутри неё она
// обрезалась бы краем или пряталась под липкой шапкой. Пустой `title` гасит системную подсказку
// блока гантта, которая иначе вылезла бы поверх этой.
defineProps<{ label: string }>()

const root = useTemplateRef<HTMLElement>('root')
const tip = useTemplateRef<HTMLElement>('tip')
// Место остаётся и после закрытия: подсказка гаснет плавно, и ей нужно, где гаснуть.
const place = ref<{ top?: string; bottom?: string; left?: string; right?: string }>({ right: '0px' })
const open = ref(false)
const id = useId()

// Снизу, выровнена по правому краю значка; у нижнего края экрана — сверху. Значку у левого края
// экрана подсказка шириной до 260 px слева не помещается — тогда она выровнена по его левому краю,
// но не дальше, чем позволяет ширина экрана.
const WIDTH = 268 // max-w-65 и зазор от края
function show() {
  if (!root.value) return
  const rect = root.value.getBoundingClientRect()
  const side =
    rect.right < WIDTH
      ? { left: `${Math.max(8, Math.min(rect.left, window.innerWidth - WIDTH))}px` }
      : { right: `${Math.max(8, window.innerWidth - rect.right)}px` }
  place.value =
    window.innerHeight - rect.bottom < 150
      ? { bottom: `${window.innerHeight - rect.top + 6}px`, ...side }
      : { top: `${rect.bottom + 6}px`, ...side }
  open.value = true
}

// Нажатие пальцем переключает и не доходит до карточки под значком. Щелчок мышью проходит дальше,
// как раньше, и закрывает подсказку: он может открыть карточку и спрятать список со значком, а
// `pointerleave` скрытому элементу не придёт. Фокус от касания не открывает — следом придёт
// нажатие и сразу закрыло бы. Флаг касания живёт до конца нажатия: потом фокус с клавиатуры
// снова открывает подсказку.
let touch = false
const toggle = () => (open.value ? (open.value = false) : show())
function press() {
  if (touch) toggle()
}
function click(event: MouseEvent) {
  if (touch) event.stopPropagation()
  else open.value = false
  touch = false
}
function focus() {
  if (!touch) show()
}

function outside(event: PointerEvent) {
  const target = event.target as Node
  if (!root.value?.contains(target) && !tip.value?.contains(target)) open.value = false
}
watch(open, (now) => {
  if (now) document.addEventListener('pointerdown', outside)
  else document.removeEventListener('pointerdown', outside)
})
onBeforeUnmount(() => document.removeEventListener('pointerdown', outside))
</script>

<template>
  <span
    ref="root"
    class="cursor-help"
    role="button"
    tabindex="0"
    title=""
    :aria-label="label"
    :aria-expanded="open"
    :aria-describedby="open ? id : undefined"
    @pointerenter="$event.pointerType === 'mouse' && show()"
    @pointerleave="$event.pointerType === 'mouse' && (open = false)"
    @pointerdown="touch = $event.pointerType !== 'mouse'"
    @pointerup="press"
    @click="click"
    @keydown.enter.space.prevent.stop="toggle"
    @focus="focus"
    @blur="open = false"
  >
    <slot />
    <Teleport to="body">
      <Transition
        enter-active-class="transition duration-200 ease-out"
        leave-active-class="transition duration-150 ease-in"
        enter-from-class="opacity-0 scale-95"
        leave-to-class="opacity-0 scale-95"
      >
        <span
          v-if="open"
          :id="id"
          ref="tip"
          role="tooltip"
          class="fixed z-50 flex w-max max-w-65 flex-col gap-1 rounded-card bg-panel px-3.5 py-2.5 text-sm font-normal text-text shadow-panel"
          :class="place.top ? (place.left ? 'origin-top-left' : 'origin-top-right') : place.left ? 'origin-bottom-left' : 'origin-bottom-right'"
          :style="place"
        >
          <slot name="tip" />
        </span>
      </Transition>
    </Teleport>
  </span>
</template>
