<script setup lang="ts">
// Бейдж ⚠ риска опоздания (PLAN 6.18) и подсказка к нему — при наведении или фокусе.
// Подсказка уходит в `body` с фиксированной позицией: списки и карточки лежат в прокрутке
// панели, и внутри неё она обрезалась бы краем или пряталась под липкой шапкой. Пустой `title`
// гасит системную подсказку блока гантта, которая иначе вылезла бы поверх этой.
import type { Request, RequestForecast } from '~/composables/useApi'

// `bare` — один значок без плашки: на блоке работы в гантте плашка не помещается.
const props = defineProps<{ forecast: RequestForecast; request: Request; start: string | null; bare?: boolean }>()

const root = useTemplateRef<HTMLElement>('root')
// Место остаётся и после ухода курсора: подсказка гаснет плавно, и ей нужно, где гаснуть.
const place = ref<{ top?: string; bottom?: string; right: string }>({ right: '0px' })
const open = ref(false)

// Снизу, выровнена по правому краю бейджа; у нижнего края экрана — сверху.
function show() {
  if (!root.value) return
  const rect = root.value.getBoundingClientRect()
  const right = `${Math.max(8, window.innerWidth - rect.right)}px`
  place.value =
    window.innerHeight - rect.bottom < 150
      ? { bottom: `${window.innerHeight - rect.top + 6}px`, right }
      : { top: `${rect.bottom + 6}px`, right }
  open.value = true
}

const percent = computed(() => Math.round(props.forecast.lateProbability * 100))
</script>

<template>
  <span
    ref="root"
    class="shrink-0 cursor-help"
    :class="bare ? 'inline-flex items-center' : 'badge accent'"
    tabindex="0"
    title=""
    :aria-label="`Риск опоздания ${percent} %`"
    @mouseenter="show"
    @mouseleave="open = false"
    @focus="show"
    @blur="open = false"
  >
    <Icon name="risk" />
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
          role="tooltip"
          class="pointer-events-none fixed z-50 flex w-max max-w-65 flex-col gap-1 rounded-card bg-panel px-3.5 py-2.5 text-sm shadow-panel"
          :class="place.top ? 'origin-top-right' : 'origin-bottom-right'"
          :style="place"
        >
          <span class="font-medium">Риск опоздания {{ percent }} %</span>
          <span v-if="start">По плану начало в {{ start }}, окно до {{ request.windowEnd }}.</span>
          <span>В 9 из 10 прогонов начало не позже {{ forecast.p90Start }}.</span>
          <span class="muted small">Модельная оценка: отклонения работ и переездов заданы нами, а не взяты из истории.</span>
        </span>
      </Transition>
    </Teleport>
  </span>
</template>
