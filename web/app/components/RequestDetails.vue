<script setup lang="ts">
// Карточка заявки в левой панели (docs/design.md, кадры 1511, 1485, 1515): шапка, поля,
// кандидаты с вердиктом, кнопки действий и «Сохранить изменения». События собирает сама
// карточка, отправляет pages/index.vue: план и его версии живут у одного владельца.
import { draftKey, formProblem, invalidFields, requestBody, requestDraft, type FormProblem } from '~/composables/entityForms'
import {
  apiError,
  ASSISTANT_TYPES,
  clockMinutes,
  extraMinutes,
  isUrgent,
  km,
  LIVE_EDGE,
  MISSING_NAMES,
  NO_STATUS_BADGE,
  OUTCOME_NAMES,
  overrun,
  ratingText,
  requestTitle,
  requestTypeOf,
  STATUS_BADGES,
  typeFields,
  statusAt,
  TRANSPORT_NEEDED,
  useApi,
  type AssistantEdit,
  type Candidate,
  type Engineer,
  type Plan,
  type PlanEvent,
  type Remedy,
  type Request,
  type Review,
  type EngineerRating,
} from '~/composables/useApi'

const props = defineProps<{
  plan: Plan | null
  request: Request
  // Общее время дня из шапки (блок 33): момент событий карточки и её статус.
  time: string
  busy: string | null
  // Ответ сервера на действие или сохранение — у кнопок карточки.
  error: string | null
  // Правка от ассистента (блок 59): ложится в черновик, «Сохранить изменения» — за диспетчером.
  suggestion?: { edit: AssistantEdit; n: number } | null
  // Живой рейтинг инженеров и оценка этой заявки (блок 72).
  ratings: Record<string, EngineerRating>
  review: Review | null
}>()

const emit = defineEmits<{
  back: []
  event: [event: PlanEvent]
  save: [request: Request, time: string]
  reassign: [requestId: string]
  addEngineer: [engineer: Engineer]
  rate: [requestId: string]
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
// Правка ассистента — поверх черновика. Новый тип тянет навык, норматив, инструменты и транспорт
// по `REQUEST_TYPES`, как выбор типа в форме; тот же тип, что был, ничего не сбрасывает, а
// длительность, названная во фразе, остаётся поверх нормы типа. Новый адрес сбрасывает точку —
// её найдёт «Сохранить».
watch(
  () => props.suggestion?.n,
  () => {
    const edit = props.suggestion?.edit
    if (edit?.request.id !== props.request.id) return
    const type = edit.requestType ? ASSISTANT_TYPES[edit.requestType] : null
    const changed = type && type !== requestTypeOf(props.request)
    const minutes = edit.request.serviceDurationMin !== props.request.serviceDurationMin
    draft.value = requestDraft({
      ...edit.request,
      ...(changed && typeFields(type)),
      ...(changed && minutes && {
        serviceDurationMin: edit.request.serviceDurationMin,
        baseNormMin: typeFields(type).baseNormMin - typeFields(type).serviceDurationMin + edit.request.serviceDurationMin,
      }),
      ...(edit.address && { address: edit.address, lat: null, lon: null }),
    })
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
const live = inject(LIVE_EDGE, ref(false))
const status = computed(() => props.plan && statusAt(props.plan, id.value, stop.value, props.time, live.value))

/**
 * Время работы (раздел 4.4 задания): расчётное — по скорости на начало дня; после закрытия рядом
 * факт, а пока работа идёт сверх плана — сколько минут уже добавлено. Сам факт до закрытия не
 * раскрывается: его знает только сервер.
 */
const timing = computed(() => {
  const current = stop.value
  if (!props.plan || !current) return []
  const planned = { label: 'По плану', value: `${current.workMin ?? clockMinutes(current.end) - clockMinutes(current.start)} мин` }
  const closure = props.plan.closed[id.value]
  if (closure?.outcome === 'done' && closure.time <= props.time) {
    const fact = Math.max(0, clockMinutes(closure.time) - clockMinutes(current.start))
    return [{ label: 'Фактическое время', value: `${fact} мин` }, planned]
  }
  const over = overrun(props.plan, current, props.time, live.value)
  return over ? [planned, { label: 'Сейчас', value: `работа продолжается, +${over} мин` }] : [planned]
})

/**
 * «Оценка клиента» (раздел 1.3 задания, кадр 1522) — у заявки, выполненной к времени дня. Оценка
 * уходит тому, кто закрыл работу, а не тому, кто стоял в утреннем плане. Черновик плана ещё не
 * факт дня, у него оценки нет.
 */
const rated = computed(() => {
  const closure = props.plan?.closed[id.value]
  if (!props.plan || props.plan.draft || closure?.outcome !== 'done' || closure.time > props.time) return null
  return { engineer: engineers.value.get(closure.engineerId)?.name ?? closure.engineerId }
})

const assignee = computed(() => (engineerId.value ? engineers.value.get(engineerId.value) ?? null : null))
// Риск опоздания по нормативу и с учётом сложности (блок 78): строка под причинами «Почему будет
// выполняться дольше?».
const risk = computed(() => {
  const now = props.plan?.forecast?.requests[id.value]
  const before = props.plan?.normForecast?.[id.value]
  return now && before ? { before: Math.round(before.lateProbability * 100), now: Math.round(now.lateProbability * 100) } : null
})

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
    // «Иванов — 101 мин» — сколько заявка займёт именно у него, по скорости на начало дня (блок 69);
    // рядом — его рейтинг (72.7).
    const name = engineer?.name ?? candidate.engineerId
    const rating = ratingText(props.ratings[candidate.engineerId] ?? engineer?.rating)
    return {
      id: candidate.engineerId,
      name: [candidate.durationMin == null ? name : `${name} — ${candidate.durationMin} мин`, rating].filter(Boolean).join(' · '),
      verdict: verdict(candidate, engineer),
    }
  }),
)

/**
 * «Что поможет» неназначенной (PLAN 6.8, блок 55): средства проверены расчётом, карточка их только
 * показывает. Перенесённой и закрытой не советуем — сервер их и не считает.
 */
const remedies = computed(() => (frozen.value || engineerId.value ? [] : props.plan?.explanations[id.value]?.remedies ?? []))

/** «Перенести окно» — та же правка заявки, что «Сохранить изменения», только с окном из средства.
 * Пока карточку правят, кнопка выключена: правка поверх `props.request` молча стёрла бы черновик. */
function moveWindow(remedy: Remedy) {
  missing.value = null
  emit('save', { ...props.request, windowStart: remedy.windowStart!, windowEnd: remedy.windowEnd! }, now.value)
}

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
      <!-- Кадр 1522: плашка 44 px, поля 20. -->
      <span class="flex min-w-0 flex-1 items-center rounded-full bg-field px-5"><span class="truncate">Заявка №{{ request.id }}</span></span>
    </div>

    <div class="flex flex-col gap-2">
      <!-- Шапка (кадр 1522): номер 14 серым, бейджи через 4, название 20 / 20. -->
      <div class="card flex flex-col gap-1">
        <div class="flex flex-wrap items-center gap-1">
          <span class="muted text-sm leading-[13px]">№{{ request.id }}</span>
          <span v-if="plan" class="badge ml-auto">{{ status ? STATUS_BADGES[status] : NO_STATUS_BADGE }}</span>
          <span class="badge" :class="[isUrgent(request) ? 'urgent' : 'accent', { 'ml-auto': !plan }]">
            {{ isUrgent(request) ? 'Срочная' : 'Обычная' }}
          </span>
          <RiskBadge
            v-if="forecast"
            :forecast="forecast"
            :request="request"
            :start="stop?.start ?? null"
            :before="plan?.normForecast?.[request.id] ?? null"
            :extra-min="stop ? extraMinutes(stop, request) : 0"
          />
        </div>
        <div class="text-xl leading-5 font-medium">{{ requestTitle(request) }}</div>
        <div v-if="request.hdType" class="muted small">{{ request.hdType }}</div>
      </div>

      <p v-if="!plan" class="muted small">Поля можно будет править после расчёта плана.</p>
      <!-- Заявка из чужого файла вне справочника BK: её поля типом не задаются (docs/design.md). -->
      <p v-else-if="!requestTypeOf(request)" class="muted small">Тип заявки не из справочника — поля заданы файлом.</p>
      <!-- «Инженер ›» (кадр 1522, блок 80): как строки-сводки; нажатие — окно переназначения (1791). -->
      <button
        v-if="plan"
        type="button"
        class="field hoverable relative w-full border-0 pr-[45px] text-left disabled:cursor-default [&>span:first-child]:!text-[13px] [&>span:nth-child(2)]:!my-0 [&>span:nth-child(2)]:text-[15px] [&>span:nth-child(2)]:leading-[18px]"
        :disabled="!canReassign || !!busy"
        @click="emit('reassign', request.id)"
      >
        <span>Инженер</span>
        <span>{{ engineerId ? (engineers.get(engineerId)?.name ?? engineerId) : 'не назначен' }}</span>
        <Icon v-if="canReassign" name="chevron" class="absolute top-1/2 right-[21px] -translate-y-1/2 -rotate-90" />
      </button>
      <!-- Без факта время по плану — во всю ширину (правки 81.6). -->
      <div v-if="timing.length" class="grid grid-cols-2 gap-2 [&>:only-child]:col-span-full">
        <div v-for="item in timing" :key="item.label" class="field"><span>{{ item.label }}</span><span>{{ item.value }}</span></div>
      </div>
      <RequestFields
        v-model="draft"
        mode="card"
        :disabled="readonly"
        :times="stop"
        :invalid="invalid"
        :speed="assignee?.speed"
        :duration-mode="plan?.input.durationMode ?? 'norm'"
        :risk="risk"
      />

      <!-- Оценка клиента (кадр 1522): «4,5» 20 / 20 и звёзды 24 через 4 или контурная звезда 16 и
           «оценить выполнение»; справа шеврон 14. Нажатие — окно оценки. -->
      <button v-if="rated" class="card hoverable flex items-center justify-between gap-2.5 border-0 text-left text-text" @click="emit('rate', request.id)">
        <span v-if="review" class="flex items-center gap-2.5">
          <span class="text-xl leading-5">{{ review.stars }}</span>
          <Stars :value="review.stars" />
        </span>
        <span v-else class="flex items-center gap-1.5 leading-5"><Icon name="star-outline" />оценить выполнение</span>
        <Icon name="chevron" class="-rotate-90" />
      </button>

      <!-- Кандидаты и «Переназначить» — один блок (кадр 1522): между заголовком, списком и кнопкой
           12, кандидаты через 10, имя и вердикт 14 / 13 через 4; кнопка 38 px. -->
      <div v-if="candidates.length || canReassign" class="card flex flex-col gap-3">
        <details v-if="candidates.length" class="group">
          <summary class="flex cursor-pointer list-none items-center justify-between leading-4 [&::-webkit-details-marker]:hidden">Кандидаты <Icon name="chevron" class="transition-transform duration-250 group-open:rotate-180" /></summary>
          <div class="mt-3 flex flex-col gap-2.5 text-sm leading-[13px]">
            <div v-for="item in candidates" :key="item.id" class="flex flex-col gap-1">
              <span class="truncate">{{ item.name }}</span>
              <span class="muted">{{ item.verdict }}</span>
            </div>
          </div>
        </details>
        <button v-if="canReassign" class="btn primary wide !min-h-[38px] !px-[13px]" :disabled="!!busy" @click="emit('reassign', request.id)">
          <Icon name="reassign" /> Переназначить
        </button>
      </div>
      <div v-if="remedies.length" class="card flex flex-col gap-3">
        <span>Что поможет</span>
        <div v-for="remedy in remedies" :key="remedy.kind" class="flex flex-col gap-2">
          <span class="muted small">{{ remedy.text }}</span>
          <button v-if="remedy.kind === 'window'" class="btn primary wide" :disabled="!!busy || dirty" :title="dirty ? 'Сначала сохраните изменения заявки' : undefined" @click="moveWindow(remedy)">
            Перенести окно на {{ remedy.windowStart }}–{{ remedy.windowEnd }}
          </button>
          <button v-else-if="remedy.kind === 'engineer' && remedy.engineer" class="btn primary wide" :disabled="!!busy" @click="emit('addEngineer', remedy.engineer)">
            <Icon name="plus" /> Добавить инженера
          </button>
        </div>
      </div>
      <p v-if="(stop?.committed || started) && !frozen" class="muted small">Работа уже начата — назначение не пересматривается.</p>
      <p v-else-if="plan?.pinned[request.id]" class="muted small">Закреплена вручную — пересчёт её не переставит.</p>
    </div>

    <div class="fade-footer flex flex-col gap-2">
      <p v-if="frozen" class="card muted">{{ frozen }}</p>
      <div v-if="!frozen && actions.length" class="grid grid-cols-[repeat(auto-fit,minmax(0,1fr))] gap-2">
        <!-- Кадр 1522: иконка 18 и подпись 16 / 13 через 4, поля 14. -->
        <button v-for="action in actions" :key="action.label" class="pressable flex cursor-pointer flex-col items-center gap-1 rounded-card border-0 bg-field px-1 py-3.5 leading-[13px] disabled:cursor-default disabled:text-muted" :disabled="!!busy" @click="act(action)">
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
