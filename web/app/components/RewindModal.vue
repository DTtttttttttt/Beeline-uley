<script setup lang="ts">
// «Изменить прошлое?» (блок 33): действие во время дня раньше последних версий применяется к версии,
// действовавшей в тот момент, а всё, что было после неё, отбрасывается. Окно называет, что именно
// уйдёт, — молча переписанная история дня хуже лишнего нажатия.
import { eventName, type Plan } from '~/composables/useApi'

defineProps<{ time: string; dropped: Plan[] }>()
const emit = defineEmits<{ back: []; go: [] }>()

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
