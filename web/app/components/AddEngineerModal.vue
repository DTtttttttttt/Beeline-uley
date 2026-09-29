<script setup lang="ts">
// «Добавление инженера» (docs/design.md, кадр 1475): событие `add_engineer`. Адреса старта в
// макете нет — старт офис, и в `startLat`/`startLon` уходят его координаты: пустыми модель
// события их не примет.
import { engineerBody, engineerDraft, invalidFields, type FormProblem } from '~/composables/entityForms'
import type { Plan, PlanEvent } from '~/composables/useApi'

// `time` — общее время дня из шапки (блок 33): момент события.
const props = defineProps<{ plan: Plan; time: string; busy: string | null; error: string | null }>()
const emit = defineEmits<{ back: []; add: [event: PlanEvent] }>()

/** Идентификатор генерирует фронт: первый свободный `brigade-N`. */
function freeId(): string {
  const taken = new Set(props.plan.input.engineers.map((engineer) => engineer.id))
  let next = props.plan.input.engineers.length + 1
  while (taken.has(`brigade-${next}`)) next += 1
  return `brigade-${next}`
}

const draft = ref(
  engineerDraft({
    id: freeId(),
    startLat: props.plan.input.office.lat,
    startLon: props.plan.input.office.lon,
  }),
)
const missing = ref<FormProblem | null>(null)
const invalid = computed(() => invalidFields(missing.value, draft.value))

function submit() {
  missing.value = null
  const body = engineerBody(draft.value)
  if ('text' in body) return (missing.value = body)
  emit('add', { type: 'add_engineer', time: props.time, engineer: body })
}
</script>

<template>
  <Modal title="Добавление инженера" @back="emit('back')">
    <EngineerFields v-model="draft" :disabled="!!busy" :invalid="invalid" />
    <template #footer>
      <p v-if="missing || error" class="error small">{{ missing?.text ?? error }}</p>
      <button class="btn primary wide" :disabled="!!busy" @click="submit">
        <Icon name="check" /> {{ busy ?? 'Добавить' }}
      </button>
    </template>
  </Modal>
</template>
