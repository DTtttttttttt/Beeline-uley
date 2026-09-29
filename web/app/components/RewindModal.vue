<script setup lang="ts">
// «Изменить прошлое?» (блок 33): действие во время дня раньше последних версий применяется к версии,
// действовавшей в тот момент, а всё, что было после неё, отбрасывается. Окно называет, что именно
// уйдёт, — молча переписанная история дня хуже лишнего нажатия.
import { OUTCOME_NAMES, type Engineer, type Plan, type PlanEvent } from '~/composables/useApi'

defineProps<{ time: string; dropped: Plan[] }>()
const emit = defineEmits<{ back: []; go: [] }>()

/** Событие одной строкой: «Отмена заявки №47670». Имя бригады — по входу плана, где она есть. */
function eventName(event: PlanEvent, engineers: Engineer[]): string {
  const name = (id: string) => engineers.find((engineer) => engineer.id === id)?.name ?? id
  switch (event.type) {
    case 'add_request':
    case 'urgent_request':
      return `Новая заявка №${event.request.id}`
    case 'add_engineer':
      return `Новый инженер: ${event.engineer.name}`
    case 'update_request':
      return `Правка заявки №${event.request.id}`
    case 'update_engineer':
      return `Правка инженера: ${event.engineer.name}`
    case 'cancel_request':
      return event.requestIds.length > 1 ? `Отмена заявок: ${event.requestIds.length}` : `Отмена заявки №${event.requestIds[0]}`
    case 'defer_request':
      return event.requestIds.length > 1 ? `Перенос заявок: ${event.requestIds.length}` : `Перенос заявки №${event.requestIds[0]}`
    case 'close_request':
      return `${OUTCOME_NAMES[event.outcome]}: заявка №${event.requestId}`
    case 'engineer_unavailable':
      return `Недоступен: ${name(event.engineerId)}`
  }
}

/** «15:00 — Отмена заявки №47670». Ручная правка наследует событие родителя — её узнаём по алгоритму. */
function label(plan: Plan): string {
  const event = plan.event
  if (plan.algorithm === 'manual') return 'Ручное переназначение'
  if (!event) return 'Пересчёт дня'
  const when = plan.replanMode === 'full' ? `${event.time}, с начала дня` : event.time
  return `${when} — ${eventName(event, plan.input.engineers)}`
}
</script>

<template>
  <Modal title="Изменить прошлое?" @back="emit('back')">
    <p class="m-0">Действие в {{ time }} применится к плану, который действовал в этот момент. Всё, что было позже, будет отброшено:</p>
    <div class="card flex flex-col gap-2">
      <div v-for="plan in dropped" :key="plan.id">{{ label(plan) }}</div>
    </div>
    <template #footer>
      <button class="btn primary wide" @click="emit('go')"><Icon name="check" /> Продолжить</button>
      <button class="btn wide" @click="emit('back')">Отмена</button>
    </template>
  </Modal>
</template>
