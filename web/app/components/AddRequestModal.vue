<script setup lang="ts">
// «Добавление заявки» (docs/design.md, кадры 1469, 1474, 1476): событие `add_request` с
// `urgent: null` — срочна только авария (PLAN 6.12). Режим выбирают в «Пересчёте дня» после
// «Добавить»; «в выбранное время» — без `mode`: обычную заявку сервер встроит, аварию пересчитает
// с момента события.
import { formProblem, invalidFields, requestBody, requestDraft, type FormProblem } from '~/composables/entityForms'
import {
  apiError,
  ASSISTANT_TYPES,
  clockMinutes,
  clockOf,
  REQUEST_TYPES,
  useApi,
  type AssistantDraft,
  type Plan,
  type PlanEvent,
} from '~/composables/useApi'

// `time` — общее время дня из шапки (блок 33): момент события. `suggestion` — заявка, которую
// ассистент разобрал из текста диспетчера (блок 42): форма открывается заполненной, координаты и
// проверку адреса по-прежнему делает она сама, а добавляет диспетчер.
const props = defineProps<{ plan: Plan; time: string; busy: string | null; error: string | null; suggestion?: AssistantDraft }>()
const emit = defineEmits<{ back: []; add: [event: PlanEvent] }>()

const api = useApi()

/**
 * Номер генерирует фронт — по часам, а не «следующий за наибольшим»: отменённая заявка уходит из
 * входа плана, и «следующий» выдал бы её номер снова, смешав две заявки в истории дня.
 * Совпадение с заявкой плана всё равно проверяется.
 */
function freeId(): string {
  const taken = new Set(props.plan.input.requests.map((request) => request.id))
  let next = Math.floor(Date.now() / 1000)
  while (taken.has(String(next))) next += 1
  return String(next)
}

/** Окно по умолчанию — два часа от текущего момента, не дальше конца суток и не пустое. */
function defaultWindow(clock: string): { windowStart: string; windowEnd: string } {
  const end = Math.min(clockMinutes(clock) + 120, 23 * 60 + 59)
  return { windowStart: clockOf(Math.min(clockMinutes(clock), end - 1)), windowEnd: clockOf(end) }
}

const time = toRef(props, 'time')
const first = props.suggestion ? ASSISTANT_TYPES[props.suggestion.requestType] : REQUEST_TYPES[0]!
const draft = ref(
  requestDraft({
    id: freeId(),
    skill: first.skill,
    workPriority: first.workPriority,
    serviceDurationMin: first.serviceDurationMin,
    baseNormMin: first.baseNormMin,
    requiredTransport: first.requiredTransport,
    requiredTools: first.requiredTools,
    ...defaultWindow(time.value),
    ...(props.suggestion && {
      address: props.suggestion.address,
      requiredEquipment: props.suggestion.equipment,
      // Окно из фразы — только целиком: половина окна хуже, чем два часа по умолчанию.
      ...(props.suggestion.windowStart && props.suggestion.windowEnd
        ? { windowStart: props.suggestion.windowStart, windowEnd: props.suggestion.windowEnd }
        : {}),
    }),
  }),
)
const missing = ref<FormProblem | null>(null)
const invalid = computed(() => invalidFields(missing.value, draft.value))
const geocoding = ref(false)

// Окно по умолчанию идёт за временем дня в шапке, пока диспетчер его не трогал: поправили время —
// окно сдвигается с ним; введённое руками окно не затирается.
watch(time, (clock, previous) => {
  const before = defaultWindow(previous)
  if (draft.value.windowStart !== before.windowStart || draft.value.windowEnd !== before.windowEnd) return
  Object.assign(draft.value, defaultWindow(clock))
})

/** Координаты — геокодером до отправки: событию нужны `lat`/`lon` (docs/design.md). */
async function submit() {
  missing.value = null
  const target = draft.value
  const address = target.address.trim()
  if (!address) return (missing.value = formProblem('Нужен адрес', target, 'address'))
  if (target.lat === null || target.lon === null) {
    geocoding.value = true
    try {
      const point = await api.geocode(address)
      if (target.address.trim() !== address) return
      Object.assign(target, point)
    } catch (problem) {
      return (missing.value = formProblem(apiError(problem), target, 'address'))
    } finally {
      geocoding.value = false
    }
  }
  const body = requestBody(target)
  if ('text' in body) return (missing.value = body)
  emit('add', { type: 'add_request', time: time.value, urgent: null, request: body })
}
</script>

<template>
  <Modal title="Добавление заявки" @back="emit('back')">
    <RequestFields v-model="draft" mode="add" :disabled="!!busy" :invalid="invalid" />
    <template #footer>
      <p v-if="missing || error" class="error small">{{ missing?.text ?? error }}</p>
      <button class="btn primary wide" :disabled="!!busy || geocoding" @click="submit">
        <Icon name="check" /> {{ geocoding ? 'Ищем адрес…' : (busy ?? 'Добавить') }}
      </button>
    </template>
  </Modal>
</template>
