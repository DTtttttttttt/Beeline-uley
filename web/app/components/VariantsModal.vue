<script setup lang="ts">
// «Варианты плана» (docs/design.md, кадры 1489 и 1559): по строке на вариант, иконка графика — метрики
// этого варианта, «Выбрать» — сделать его показываемым оптимизированным планом. Базового строки
// нет: он приходит в том же ответе `compare` и открывается переключателем «базовый».
import type { Bar } from '~/composables/progress'
import { decimal, duration, km, plural, VARIANT_NAMES, type Plan } from '~/composables/useApi'

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

// Порядок строк — таблица design.md, «Рейтинг в приоритете» вторым (правки 81.3); «Аварии в
// приоритете» отмечен по умолчанию (ответ 15).
const ORDER = ['emergency_first', 'rating_first', 'emergency_asap', 'max_requests', 'urgent_first', 'priority_first', 'fast']

const MAIN = 'emergency_first'

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
    // Снимок рейтинга на момент расчёта, до сотых (раздел 1.5 задания): иначе варианты не различить.
    ...(plan.metrics.ratingAvg != null ? [`средний рейтинг ${decimal(plan.metrics.ratingAvg, 2)}`] : []),
    km(plan.metrics.totalKm),
  ]
  // Сколько ждут аварии (блок 49): разница «Аварий в приоритете» и «Аварий сразу» — в ней.
  // У планов до блока 49 метрики нет, а без назначенных аварий ждать некому.
  const emergencies = plan.metrics.assignedByWorkPriority.emergency ?? 0
  if (plan.metrics.emergencyWaitMin != null && emergencies)
    parts.push(`аварии ждут в сумме ${duration(plan.metrics.emergencyWaitMin)}`)
  if (plan.variant === 'fast') parts.push(`${decimal(plan.computeSec, 1)} с`)
  // Солвер не нашёл решения и вернул базовый (PLAN 6.6): числа строки — не этого варианта.
  if (plan.fallbackToBaseline) parts.push('решения не нашлось — показан базовый')
  // Поиск остановила страховка по времени, а не предел решений (PLAN 6.5): повторный расчёт
  // этого же дня может дать другой план.
  if (plan.search?.stoppedBy === 'time') parts.push('расчёт прерван по времени')
  return parts.join(' · ')
}

function choose() {
  const plan = rows.value.find((item) => item.variant === chosen.value)
  if (plan) emit('choose', plan)
}
</script>

<template>
  <Modal title="Варианты плана" wide :closable="closable" @back="emit('back')">
    <p class="muted m-0">Сравните варианты оптимизации и выберите предпочтительный.</p>
    <!-- Расчёт идёт около минуты: вместо строк вариантов — их заготовки того же размера, чтобы окно
         не прыгало, когда строки придут. Ход расчёта — полосой над «Выбрать». -->
    <template v-if="busy">
      <div
        v-for="row in ORDER"
        :key="row"
        class="choice"
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
      class="choice cursor-pointer select-none hover:bg-field/60 [&>input]:pointer-events-none [&>input]:absolute [&>input]:opacity-0"
      :class="{ '!border-accent': chosen === plan.variant }"
    >
      <input v-model="chosen" type="radio" :value="plan.variant">
      <CheckMark :size="24" :checked="chosen === plan.variant" />
      <span class="flex flex-1 flex-col gap-0.5">
        <span class="flex flex-wrap items-center gap-2">
          {{ VARIANT_NAMES[plan.variant ?? ''] ?? plan.variant }}
          <!-- Основной вариант команды (блок 56): авария важнее всего — ответ 15. -->
          <span
            v-if="plan.variant === MAIN"
            class="badge bg-accent"
            title="По ТЗ и ответам организаторов: авария важнее всего"
          >основной</span>
          <!-- Резерв (блок 71): вопрос не «сколько успеем», а «кому отдать». -->
          <span v-if="plan.variant === 'rating_first'" class="badge">без лимита инженеров</span>
        </span>
        <span class="muted text-sm">{{ summary(plan) }}</span>
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
