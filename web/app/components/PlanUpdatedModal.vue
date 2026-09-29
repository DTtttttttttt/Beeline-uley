<script setup lang="ts">
// «План обновлён» (docs/design.md, кадры 1477, 96, 1478, 1479): фраза по действию и «Что
// изменилось» из `plan.diff`. Времена начала — по двум версиям плана, а не по записи diff:
// на заявку в diff одна запись, и у переназначенной сдвиг времени иначе потерялся бы.
import { km, plural, type Plan, type RequestChange } from '~/composables/useApi'

const props = defineProps<{ plan: Plan; previous: Plan; phrase: string }>()
const emit = defineEmits<{ back: [] }>()

const names = computed(
  () =>
    new Map(
      [...props.previous.input.engineers, ...props.plan.input.engineers].map((engineer) => [engineer.id, engineer.name]),
    ),
)

function name(id: string | null | undefined): string {
  return id ? (names.value.get(id) ?? id) : ''
}

function starts(plan: Plan): Map<string, string> {
  return new Map(plan.routes.flatMap((route) => route.stops.map((stop) => [stop.requestId, stop.start] as const)))
}

const reasons = computed(() => new Map(props.plan.unassigned.map((item) => [item.requestId, item.reasonText])))

function detail(change: RequestChange, before: Map<string, string>, after: Map<string, string>): string {
  const id = change.requestId
  switch (change.change) {
    case 'reassigned': {
      const who = change.from ? `${name(change.from)} → ${name(change.to)}` : `Была без назначения → ${name(change.to)}`
      // На заявку в diff одна запись: у переназначенной сдвиг начала иначе не виден нигде.
      const old = before.get(id)
      const now = after.get(id)
      return old && now && old !== now ? `${who} · начало ${old} → ${now}` : who
    }
    case 'added': {
      const engineer = props.plan.assignments[id]
      return engineer ? `Добавлена → ${name(engineer)}` : 'Добавлена · без назначения'
    }
    case 'became_unassigned':
      return `${name(change.from) || 'Была в плане'} → без назначения`
    case 'time_changed':
    case 'order_changed': {
      const old = before.get(id) ?? change.oldStart
      const now = after.get(id) ?? change.newStart
      return old && now && old !== now
        ? `Начало работы: ${old} → ${now}`
        : `Место в маршруте: №${(change.oldPosition ?? 0) + 1} → №${(change.newPosition ?? 0) + 1}`
    }
    case 'cancelled':
      return 'Отменена'
    case 'deferred':
      return 'Перенесена на следующий день'
  }
}

const rows = computed(() => {
  const diff = props.plan.diff
  if (!diff) return []
  const before = starts(props.previous)
  const after = starts(props.plan)
  const known = new Set(props.previous.input.engineers.map((engineer) => engineer.id))
  const requests = diff.requests.map((change) => ({
    key: `r${change.requestId}`,
    title: `№${change.requestId}`,
    text: detail(change, before, after),
    reason: change.change === 'became_unassigned' ? (reasons.value.get(change.requestId) ?? null) : null,
  }))
  // Километры — по инженерам, а не по заявкам: diff считает их по бригадам (L, кадр 1477).
  const engineers = diff.engineers.map((change) => ({
    key: `e${change.engineerId}`,
    title: name(change.engineerId),
    text: known.has(change.engineerId)
      ? `${km(change.oldKm)} → ${km(change.newKm)}`
      : `Добавлен в план · ${plural(change.newCount, ['заявка', 'заявки', 'заявок'])}`,
    reason: null,
  }))
  return [...requests, ...engineers]
})
</script>

<template>
  <Modal title="План обновлён" @back="emit('back')">
    <p class="m-0">{{ phrase }}</p>
    <p v-if="plan.approximate" class="muted small">{{ plan.approximateNote ?? 'Километры и времена посчитаны по прямой.' }}</p>
    <div class="card flex flex-col gap-2">
      <div class="font-medium">Что изменилось</div>
      <div v-for="row in rows" :key="row.key" class="row">
        <div>{{ row.title }}</div>
        <div class="muted small">{{ row.text }}</div>
        <div v-if="row.reason" class="muted small">{{ row.reason }}</div>
      </div>
      <div v-if="!rows.length" class="muted small">Назначения и время работ не изменились.</div>
    </div>
    <template #footer>
      <button class="btn primary wide" @click="emit('back')"><Icon name="check" /> Ок</button>
    </template>
  </Modal>
</template>
