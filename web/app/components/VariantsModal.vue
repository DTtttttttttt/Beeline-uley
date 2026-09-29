<script setup lang="ts">
// «Варианты плана» (docs/design.md, кадр 1489): по строке на вариант, иконка графика — метрики
// этого варианта, «Выбрать» — сделать его показываемым оптимизированным планом. Базового строки
// нет: он приходит в том же ответе `compare` и открывается переключателем «базовый».
import type { Bar } from '~/composables/progress'
import { decimal, km, plural, VARIANT_NAMES, type Plan } from '~/composables/useApi'

const props = defineProps<{
  plans: Plan[]
  // Какой вариант сейчас показан — он и отмечен, когда окно открывают повторно.
  current: Plan | null
  // После событий дня выбор варианта возвращает день к утреннему расчёту — это надо сказать.
  dayMoved: boolean
  busy: string | null
  error: string | null
  progress: Bar | null
  closable: boolean
}>()

const emit = defineEmits<{ back: []; compute: []; choose: [plan: Plan]; metrics: [plan: Plan] }>()

// Порядок строк — таблица design.md; «Аварии в приоритете» отмечен по умолчанию (ответ 15).
const ORDER = ['emergency_first', 'max_requests', 'urgent_first', 'priority_first', 'fast']

const rows = computed(() =>
  ORDER.map((variant) => props.plans.find((plan) => plan.variant === variant)).filter(
    (plan): plan is Plan => !!plan,
  ),
)

const chosen = ref<string | null>(null)
watch(
  rows,
  (list) => {
    // По варианту, а не по id: событие даёт новую версию с новым id, но тем же вариантом.
    const current = list.find((plan) => plan.variant === props.current?.variant)
    chosen.value = current?.variant ?? list[0]?.variant ?? null
  },
  { immediate: true },
)

/** «62/66 заявок · 8 инженеров · 189 км», у быстрого ещё время расчёта. */
function summary(plan: Plan): string {
  const parts = [
    `${plan.metrics.assignedCount}/${plural(plan.input.requests.length, ['заявки', 'заявок', 'заявок'])}`,
    plural(plan.metrics.engineersUsed, ['инженер', 'инженера', 'инженеров']),
    km(plan.metrics.totalKm),
  ]
  if (plan.variant === 'fast') parts.push(`${decimal(plan.computeSec, 1)} с`)
  // Солвер не нашёл решения и вернул базовый (PLAN 6.6): числа строки — не этого варианта.
  if (plan.fallbackToBaseline) parts.push('решения не нашлось — показан базовый')
  return parts.join(' · ')
}

function choose() {
  const plan = rows.value.find((item) => item.variant === chosen.value)
  if (plan) emit('choose', plan)
}
</script>

<template>
  <Modal title="Варианты плана" :closable="closable" @back="emit('back')">
    <p class="muted m-0">Сравните варианты оптимизации и выберите предпочтительный.</p>
    <!-- Расчёт идёт около минуты: вместо строк вариантов — их заготовки того же размера, чтобы окно
         не прыгало, когда строки придут. Ход расчёта — полосой над «Выбрать». -->
    <template v-if="busy">
      <div
        v-for="row in ORDER"
        :key="row"
        class="flex items-center gap-3 rounded-card border border-[#dde2e8] py-2 pr-2 pl-3.5"
        aria-hidden="true"
      >
        <span class="skeleton size-[26px] shrink-0 rounded-full" />
        <span class="flex flex-1 flex-col gap-1.5 py-0.5">
          <span class="skeleton h-3.5 w-2/5 rounded-full" />
          <span class="skeleton h-3 w-3/4 rounded-full" />
        </span>
        <span class="skeleton size-[38px] shrink-0 rounded-full" />
      </div>
    </template>
    <template v-else-if="!rows.length">
      <p class="card muted">Варианты ещё не посчитаны.</p>
      <button class="btn wide" @click="emit('compute')"><Icon name="optimize" /> Посчитать варианты</button>
    </template>
    <p v-if="dayMoved && rows.length && !busy" class="card muted small">
      В дне уже есть изменения. Выбор варианта вернёт день к утреннему расчёту — события и правки придётся
      вводить заново.
    </p>
    <!-- При пересчёте прежние строки прячутся: заготовки стоят вместо них, а не над ними. -->
    <label
      v-for="plan in busy ? [] : rows"
      :key="plan.id"
      class="flex cursor-pointer select-none items-center gap-3 rounded-card border border-[#dde2e8] py-2 pr-2 pl-3.5 transition-[border-color,box-shadow,background-color] duration-200 hover:bg-field/60 [&>input]:pointer-events-none [&>input]:absolute [&>input]:opacity-0"
      :class="{ 'border-accent shadow-[0_0_0_1px_#ffc800]': chosen === plan.variant }"
    >
      <input v-model="chosen" type="radio" :value="plan.variant">
      <CheckMark :checked="chosen === plan.variant" />
      <span class="flex flex-1 flex-col gap-0.5">
        <span>{{ VARIANT_NAMES[plan.variant ?? ''] ?? plan.variant }}</span>
        <span class="muted small">{{ summary(plan) }}</span>
      </span>
      <button class="round size-[38px]" title="Метрики варианта" @click.prevent="emit('metrics', plan)">
        <Icon name="metrics" />
      </button>
    </label>

    <template #footer>
      <ProgressBar v-if="progress" :bar="progress" />
      <p v-if="error" class="error">{{ error }}</p>
      <button class="btn primary wide" :disabled="!!busy || !chosen" @click="choose">
        <Icon name="check" /> Выбрать
      </button>
    </template>
  </Modal>
</template>
