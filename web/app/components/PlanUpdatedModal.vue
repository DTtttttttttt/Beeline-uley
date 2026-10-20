<script setup lang="ts">
// «План обновлён» (docs/design.md, кадры 1477, 96, 1478, 1479): фраза по действию и «Что
// изменилось» из `plan.diff`. Времена начала — по двум версиям плана, а не по записи diff:
// на заявку в diff одна запись, и у переназначенной сдвиг времени иначе потерялся бы.
// «Разворот» (блок 74, дизайн 3.4): кто поехал на аварию события, что бросил и куда ушла брошенная
// заявка. Адрес места разворота не ищем (лимит геокодера) — «по дороге к заявке №…». Имена из
// чужого файла не склоняем: «Заявка №… → Петров».
import { km, plural, starts, type Plan, type RequestChange } from '~/composables/useApi'

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

const turn = computed(() => {
  const dispatch = props.plan.diff?.dispatch
  const event = props.plan.event
  if (!dispatch || !event || !('request' in event)) return []
  const who = name(dispatch.engineerId)
  const alarm = `авария №${event.request.id}`
  if (dispatch.action === 'finish')
    return [`${who} закончит заявку №${dispatch.requestId} в ${dispatch.until}, затем выедет: ${alarm}. Прибытие ${dispatch.arrival}.`]
  if (dispatch.action === 'go') return [`${who} выезжает сразу: ${alarm}. Прибытие ${dispatch.arrival}.`]
  const left = dispatch.requestId!
  const engineer = props.plan.assignments[left]
  const start = starts(props.plan).get(left)
  return [
    `${who} развёрнут в ${event.time} по дороге к заявке №${left} → ${alarm}, прибытие ${dispatch.arrival}.`,
    engineer ? `Заявка №${left} → ${name(engineer)}, новое время ${start}.` : `Заявка №${left} осталась без инженера.`,
  ]
})

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
    <p v-if="plan.search && !plan.search.warmStarted && !plan.fallbackToBaseline" class="muted small">
      Стартовое решение не подошло — план посчитан с нуля.
    </p>
    <!-- Поиск остановила страховка по времени, а не предел решений (PLAN 6.5): тот же пересчёт
         ещё раз может дать другой план. -->
    <p v-if="plan.search?.stoppedBy === 'time' && !plan.fallbackToBaseline" class="muted small">
      Расчёт прерван по времени — повторный пересчёт может дать другой план.
    </p>
    <div v-if="turn.length" class="card flex flex-col gap-1">
      <div class="flex items-center gap-1.5 font-medium">
        <Icon name="emergency" class="text-[#f93155] [&_svg]:size-4" /> Разворот
      </div>
      <div v-for="line in turn" :key="line" class="small">{{ line }}</div>
    </div>
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
