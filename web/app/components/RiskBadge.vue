<script setup lang="ts">
// Бейдж ⚠ риска опоздания (PLAN 6.18) и подсказка к нему — общим `Tip` (блок 75.2).
import type { Request, RequestForecast } from '~/composables/useApi'

// `bare` — один значок без плашки: на блоке работы в гантте плашка не помещается.
// `before` и `extraMin` — с учётом сложности (блок 67): риск тех же маршрутов по нормативу и
// сколько минут к нормативу добавили сложность объекта и скорость инженера.
const props = defineProps<{
  forecast: RequestForecast
  request: Request
  start: string | null
  bare?: boolean
  before?: RequestForecast | null
  extraMin?: number
}>()

const percent = computed(() => Math.round(props.forecast.lateProbability * 100))
const beforePercent = computed(() => (props.before ? Math.round(props.before.lateProbability * 100) : null))
</script>

<template>
  <Tip class="shrink-0" :class="bare ? 'inline-flex items-center' : 'badge accent'" :label="`Риск опоздания ${percent} %`">
    <Icon name="risk" />
    <slot />
    <template #tip>
      <span class="font-medium">Риск опоздания {{ percent }} %</span>
      <span v-if="beforePercent !== null && beforePercent !== percent">
        По нормативу было {{ beforePercent }} % → стало {{ percent }} %<template v-if="extraMin">: +{{ extraMin }} мин за сложность объекта и скорость</template>.
      </span>
      <span v-else-if="extraMin">С учётом сложности и скорости работа длиннее норматива на {{ extraMin }} мин.</span>
      <span v-if="start">По плану начало в {{ start }}, окно до {{ request.windowEnd }}.</span>
      <span>В 9 из 10 прогонов начало не позже {{ forecast.p90Start }}.</span>
      <span class="muted small">Модельная оценка: отклонения работ и переездов заданы нами, а не взяты из истории.</span>
    </template>
  </Tip>
</template>
