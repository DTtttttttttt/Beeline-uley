<script setup lang="ts">
// Карточка заявки в левой панели (docs/design.md, кадры 1511, 1485, 1515): шапка, поля,
// кандидаты с вердиктом, кнопки действий и «Сохранить изменения». События собирает сама
// карточка, отправляет pages/index.vue: план и его версии живут у одного владельца.
import { draftKey, formProblem, invalidFields, requestBody, requestDraft, type FormProblem } from '~/composables/entityForms'
import {
  apiError,
  isUrgent,
  km,
  MISSING_NAMES,
  NO_STATUS_BADGE,
  OUTCOME_NAMES,
  requestTitle,
  requestTypeOf,
  STATUS_BADGES,
  statusAt,
  TRANSPORT_NEEDED,
  useApi,
  type Candidate,
  type Engineer,
  type Plan,
  type PlanEvent,
  type Request,
} from '~/composables/useApi'

const props = defineProps<{
  plan: Plan | null
  request: Request
  // Общее время дня из шапки (блок 33): момент событий карточки и её статус.
  time: string
  busy: string | null
  // Ответ сервера на действие или сохранение — у кнопок карточки.
  error: string | null
}>()

const emit = defineEmits<{
  back: []
  event: [event: PlanEvent]
  save: [request: Request, time: string]
  reassign: [requestId: string]
}>()

const api = useApi()

// Черновик пересобирается, только когда сама заявка стала другой: переключение планов и
// чужое событие дают тот же объект заново, и стереть недописанную правку было бы нельзя.
const draft = ref(requestDraft())
// Черновик, с которым карточку открыли: «Сохранить изменения» видна, только когда его правили.
const pristine = ref('')
const missing = ref<FormProblem | null>(null)
const invalid = computed(() => invalidFields(missing.value, draft.value))
const geocoding = ref(false)
watch(
  () => JSON.stringify(props.request),
  () => {
    draft.value = requestDraft(props.request)
    pristine.value = draftKey(draft.value)
    missing.value = null
  },
  { immediate: true },
)
const dirty = computed(() => draftKey(draft.value) !== pristine.value)

const id = computed(() => props.request.id)
const engineerId = computed(() => props.plan?.assignments[id.value] ?? null)
const stop = computed(() =>
  props.plan?.routes.flatMap((route) => route.stops).find((item) => item.requestId === id.value) ?? null,
)
// ⚠ — только при риске выше низкого, как в списке (PLAN 6.18).
const forecast = computed(() => {
  const item = props.plan?.forecast?.requests[id.value]
  return item && item.risk !== 'low' ? item : null
})
const engineers = computed(() => new Map((props.plan?.input.engineers ?? []).map((item) => [item.id, item])))
const status = computed(() => props.plan && statusAt(props.plan, id.value, stop.value, props.time))

/** Закрытую фактом и перенесённую заявку не правят (PLAN 6.19): вместо кнопок — строка. Факт в
 * версии «весь день заново» может быть позже времени дня (блок 33) — действовать до него всё равно
 * нельзя: сервер не пустит событие раньше факта. */
const frozen = computed(() => {
  const closure = props.plan?.closed[id.value]
  if (closure) return `${OUTCOME_NAMES[closure.outcome]} в ${closure.time}`
  const deferred = props.plan?.deferred[id.value]
  return deferred ? `Перенесена на следующий день в ${deferred}` : null
})
// Начатую работу событием не правят — сервер ответит 409 (PLAN 6.12); `started` объявлен ниже.
const readonly = computed(() => !props.plan || !!frozen.value || !!props.busy || started.value)

/**
 * Какие кнопки видны (docs/design.md, «Действия над заявкой»). Начата ли работа, решает момент,
 * в который кнопка отправит событие, — время дня в шапке (блок 33). Правило то же, что у сервера:
 * работа начата, если выезд к ней не позже события (PLAN 6.12).
 */
const now = toRef(props, 'time')

const started = computed(() => !!stop.value && stop.value.departure <= now.value)
const actions = computed(() => {
  if (!props.plan || frozen.value) return []
  if (started.value) {
    return [
      { label: 'Отменить', icon: 'cancel', type: 'close_request', outcome: 'cancelled' },
      { label: 'Выполнена', icon: 'done', type: 'close_request', outcome: 'done' },
      { label: 'Не выполнена', icon: 'failed', type: 'close_request', outcome: 'failed' },
    ] as const
  }
  return [
    { label: 'Отменить', icon: 'cancel', type: 'cancel_request', outcome: null },
    { label: 'Перенести', icon: 'defer', type: 'defer_request', outcome: null },
  ] as const
})

/** Время события — время дня в шапке на момент нажатия; с какого момента пересчитать день, спрашивает «Пересчёт дня». */
function act(action: (typeof actions.value)[number]) {
  const time = now.value
  missing.value = null
  const requestId = id.value
  emit(
    'event',
    action.type === 'close_request'
      ? { type: 'close_request', time, requestId, outcome: action.outcome! }
      : { type: action.type, time, requestIds: [requestId] },
  )
}

/** Вердикт кандидата одной фразой — первое совпадение сверху (docs/design.md, «Вердикт»). */
function verdict(candidate: Candidate, engineer: Engineer | undefined): string {
  const request = props.request
  if (candidate.engineerId === engineerId.value) return 'Выбран'
  if (!candidate.skillOk) return 'Нет нужной квалификации'
  if (!candidate.transportOk) {
    return request.requiredTransport ? TRANSPORT_NEEDED[request.requiredTransport] : 'Нужен другой транспорт'
  }
  if (!candidate.equipmentOk) {
    const has = new Set<string>([...(engineer?.equipment ?? []), ...(engineer?.tools ?? [])])
    const absent = [...request.requiredEquipment, ...request.requiredTools].find((code) => !has.has(code))
    if (absent) return `Нет ${MISSING_NAMES[absent] ?? absent}`
    // Вид есть, но дневная вместимость занята (NO_CAPACITY): называем, сколько уже везёт и
    // сколько нужно заявке, — «не унесёт» без чисел непонятно.
    return `Нет места для оборудования: везёт ${carried(candidate.engineerId)} ед., нужно ещё ${request.requiredEquipment.length}`
  }
  if (!candidate.timeOk) {
    if (engineer?.unavailableFrom) return `Недоступен с ${engineer.unavailableFrom}`
    // Вышел в течение дня позже, чем кончается окно (W: в таблице design.md этого случая нет).
    if (engineer?.availableFrom && engineer.availableFrom >= request.windowEnd) return `Выходит в ${engineer.availableFrom}`
    if (engineer && engineer.shiftStart >= request.windowEnd) return `Смена начинается в ${engineer.shiftStart}`
    if (engineer && engineer.shiftEnd <= request.windowStart) return `Смена до ${engineer.shiftEnd}`
    return 'Не успевает'
  }
  return candidate.extraKm === null ? 'Подходит' : km(candidate.extraKm, true)
}

/**
 * Сколько единиц оборудования бригада уже везёт за день: по одной каждого вида на заявку её
 * маршрута, вместе с закреплёнными — пополнения нет (PLAN 6.2).
 */
function carried(engineerId: string): number {
  const requests = new Map((props.plan?.input.requests ?? []).map((item) => [item.id, item]))
  const stops = props.plan?.routes.find((route) => route.engineerId === engineerId)?.stops ?? []
  return stops.reduce((sum, stop) => sum + (requests.get(stop.requestId)?.requiredEquipment.length ?? 0), 0)
}

// Начатую ко времени дня работу не переставляют (блок 33): сервер берёт момент показываемой
// версии и такую перестановку пропустил бы, хотя бригада к заявке уже выехала.
const canReassign = computed(() => !!props.plan && !frozen.value && !stop.value?.committed && !started.value)

const candidates = computed(() =>
  (props.plan?.explanations[id.value]?.candidates ?? []).map((candidate) => {
    const engineer = engineers.value.get(candidate.engineerId)
    return { id: candidate.engineerId, name: engineer?.name ?? candidate.engineerId, verdict: verdict(candidate, engineer) }
  }),
)

/**
 * «Сохранить изменения» — событие `update_request` во время дня из шапки. Адрес переписали — точку ищет геокодер;
 * ответ применяется, только если за время запроса не открыли другую заявку и не переписали
 * адрес снова (грабли блока 29).
 */
async function save() {
  missing.value = null
  const target = draft.value
  if (target.lat === null || target.lon === null) {
    const address = target.address.trim()
    if (!address) return (missing.value = formProblem('Нужен адрес', target, 'address'))
    geocoding.value = true
    try {
      const point = await api.geocode(address)
      if (draft.value !== target || target.address.trim() !== address) return
      Object.assign(target, point)
    } catch (problem) {
      return (missing.value = formProblem(apiError(problem), target, 'address'))
    } finally {
      geocoding.value = false
    }
  }
  const body = requestBody(target)
  if ('text' in body) return (missing.value = body)
  emit('save', body, now.value)
}
</script>

<template>
  <div class="fade-scroll">
    <div class="fade-header flex gap-2">
      <button class="round" title="Назад" @click="emit('back')"><Icon name="back" /></button>
      <span class="card flex min-w-0 flex-1 items-center rounded-[22px]"><span class="truncate">Заявка №{{ request.id }}</span></span>
    </div>

    <div class="flex flex-col gap-2">
      <div class="card flex flex-col gap-1">
        <div class="flex flex-wrap items-center gap-1.5">
          <span class="muted">№{{ request.id }}</span>
          <span v-if="plan" class="badge ml-auto">{{ status ? STATUS_BADGES[status] : NO_STATUS_BADGE }}</span>
          <span class="badge" :class="[isUrgent(request) ? 'urgent' : 'accent', { 'ml-auto': !plan }]">
            {{ isUrgent(request) ? 'Срочная' : 'Обычная' }}
          </span>
          <RiskBadge v-if="forecast" :forecast="forecast" :request="request" :start="stop?.start ?? null" />
        </div>
        <div class="text-lg font-medium">{{ requestTitle(request) }}</div>
        <div v-if="request.hdType" class="muted small">{{ request.hdType }}</div>
      </div>

      <p v-if="!plan" class="muted small">Поля можно будет править после расчёта плана.</p>
      <!-- Заявка из чужого файла вне справочника BK: её поля типом не задаются (docs/design.md). -->
      <p v-else-if="!requestTypeOf(request)" class="muted small">Тип заявки не из справочника — поля заданы файлом.</p>
      <div v-if="engineerId" class="field"><span>Инженер</span>{{ engineers.get(engineerId)?.name ?? engineerId }}</div>
      <RequestFields v-model="draft" mode="card" :disabled="readonly" :times="stop" :invalid="invalid" />

      <!-- Кандидаты и «Переназначить» — один блок, кнопка под списком (кадр 1511). -->
      <div v-if="candidates.length || canReassign" class="card flex flex-col gap-3">
        <details v-if="candidates.length" class="group">
          <summary class="flex cursor-pointer list-none items-center justify-between [&::-webkit-details-marker]:hidden">Кандидаты <Icon name="chevron" class="transition-transform duration-250 group-open:rotate-180" /></summary>
          <div v-for="item in candidates" :key="item.id" class="pt-2">
            <div>{{ item.name }}</div>
            <div class="muted small">{{ item.verdict }}</div>
          </div>
        </details>
        <button v-if="canReassign" class="btn primary wide min-h-12" :disabled="!!busy" @click="emit('reassign', request.id)">
          <Icon name="reassign" /> Переназначить
        </button>
      </div>
      <p v-if="(stop?.committed || started) && !frozen" class="muted small">Работа уже начата — назначение не пересматривается.</p>
      <p v-else-if="plan?.pinned[request.id]" class="muted small">Закреплена вручную — пересчёт её не переставит.</p>
    </div>

    <div class="fade-footer flex flex-col gap-2">
      <p v-if="frozen" class="card muted">{{ frozen }}</p>
      <div v-if="!frozen && actions.length" class="grid grid-cols-[repeat(auto-fit,minmax(0,1fr))] gap-2">
        <button v-for="action in actions" :key="action.label" class="pressable flex cursor-pointer flex-col items-center gap-1 rounded-card border-0 bg-field px-1 py-2 text-sm disabled:cursor-default disabled:text-muted" :disabled="!!busy" @click="act(action)">
          <Icon :name="action.icon" />
          <span>{{ action.label }}</span>
        </button>
      </div>
      <p v-if="missing || error" class="error small">{{ missing?.text ?? error }}</p>
      <p v-if="started && !frozen" class="muted small">Работа начата — поля не правятся, остаётся закрыть её фактом.</p>
      <Transition name="appear">
        <button v-if="plan && !frozen && !started && dirty" class="btn primary wide" :disabled="!!busy || geocoding" @click="save">
          <Icon name="check" /> {{ geocoding ? 'Ищем адрес…' : 'Сохранить изменения' }}
        </button>
      </Transition>
    </div>
  </div>
</template>
