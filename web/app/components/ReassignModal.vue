<script setup lang="ts">
// «Переназначение заявки» (docs/design.md, кадр 1461). Кандидаты — только те, кому заявку можно
// отдать: все четыре флага `true`, по приросту пробега, текущий исполнитель первым. Места в
// маршруте и снятия назначения в макете нет (D): сервер ставит заявку в лучшее место.
// Полей «Текущее время» и «весь день заново» здесь нет — ручная правка не событие (PLAN 6.16).
import type { Bar } from '~/composables/progress'
import { km, type Plan } from '~/composables/useApi'

const props = defineProps<{ plan: Plan; requestId: string; busy: string | null; error: string | null; progress: Bar | null }>()
const emit = defineEmits<{ back: []; assign: [engineerId: string] }>()

const current = computed(() => props.plan.assignments[props.requestId] ?? null)

const rows = computed(() => {
  const engineers = new Map(props.plan.input.engineers.map((engineer) => [engineer.id, engineer]))
  return (props.plan.explanations[props.requestId]?.candidates ?? [])
    .filter((item) => item.skillOk && item.transportOk && item.equipmentOk && item.timeOk)
    .sort(
      (a, b) =>
        Number(b.engineerId === current.value) - Number(a.engineerId === current.value) ||
        (a.extraKm ?? Infinity) - (b.extraKm ?? Infinity),
    )
    .map((item) => ({
      id: item.engineerId,
      name: engineers.get(item.engineerId)?.name ?? item.engineerId,
      free: item.freeSlots.map((slot) => `${slot.start}–${slot.end}`).join(', ') || 'занят весь день',
      km: item.engineerId === current.value || item.extraKm === null
        ? null
        : km(item.extraKm, true),
    }))
})

const chosen = ref<string | null>(current.value ?? rows.value[0]?.id ?? null)
</script>

<template>
  <Modal title="Переназначение заявки" @back="emit('back')">
    <div class="card flex flex-col gap-2">
      <div class="font-medium">Кандидаты</div>
      <label
        v-for="row in rows"
        :key="row.id"
        class="flex cursor-pointer select-none items-center gap-3 rounded-xl border border-[#dde2e8] bg-panel px-3 py-2 transition-[border-color,box-shadow,background-color] duration-200 hover:bg-field/60 [&>input]:pointer-events-none [&>input]:absolute [&>input]:opacity-0"
        :class="{ 'border-accent shadow-[0_0_0_1px_#ffc800]': chosen === row.id }"
      >
        <input v-model="chosen" type="radio" :value="row.id">
        <CheckMark :checked="chosen === row.id" />
        <span class="flex flex-col gap-0.5">
          <span>{{ row.name }}<span v-if="row.id === current" class="muted small"> · сейчас</span></span>
          <span class="muted small">Свободен: {{ row.free }}</span>
          <span v-if="row.km" class="muted small">{{ row.km }}</span>
        </span>
      </label>
      <p v-if="!rows.length" class="muted small">
        Подходящих инженеров нет: ни у кого заявка не встаёт без нарушения ограничений.
      </p>
      <p v-else-if="rows.length === 1 && current" class="muted small">
        Кроме текущего исполнителя, заявку никому не отдать.
      </p>
    </div>
    <template #footer>
      <ProgressBar v-if="progress" :bar="progress" />
      <p v-if="error" class="error small">{{ error }}</p>
      <button
        class="btn primary wide"
        :disabled="!!busy || !chosen || chosen === current"
        @click="chosen && emit('assign', chosen)"
      >
        <Icon name="check" /> {{ busy ?? 'Переназначить' }}
      </button>
    </template>
  </Modal>
</template>
