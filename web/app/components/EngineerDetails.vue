<script setup lang="ts">
// Карточка инженера в левой панели (docs/design.md, «Карточка инженера»): «данные» — поля
// с «Сделать недоступным» и «Сохранить изменения», «график» — сводка, почему у него эти заявки
// и такой маршрут, шкала дня. Черновик один на карточку: «Сохранить» на «графике» сохраняет
// правку «данных».
import { draftKey, engineerBody, engineerDraft, invalidFields, type FormProblem } from '~/composables/entityForms'
import { km, plural, type Engineer, type Plan, type PlanEvent } from '~/composables/useApi'

const props = defineProps<{
  plan: Plan | null
  engineer: Engineer
  // Общее время дня из шапки (блок 33): момент событий карточки.
  time: string
  office: { address: string; lat: number; lon: number } | null
  busy: string | null
  error: string | null
}>()

const emit = defineEmits<{
  back: []
  select: [requestId: string]
  event: [event: PlanEvent]
  save: [engineer: Engineer, time: string]
}>()

const tab = ref<'data' | 'graph'>('data')

// Черновик пересобирается, только когда сам инженер стал другим (см. RequestDetails).
const draft = ref(engineerDraft())
// Черновик, с которым карточку открыли: «Сохранить изменения» видна, только когда его правили.
const pristine = ref('')
const missing = ref<FormProblem | null>(null)
const invalid = computed(() => invalidFields(missing.value, draft.value))
watch(
  () => JSON.stringify(props.engineer),
  () => {
    draft.value = engineerDraft({ ...props.engineer, address: '' })
    pristine.value = draftKey(draft.value)
    missing.value = null
  },
  { immediate: true },
)
const dirty = computed(() => draftKey(draft.value) !== pristine.value)

const route = computed(() => props.plan?.routes.find((item) => item.engineerId === props.engineer.id) ?? null)
const why = computed(() => props.plan?.routeExplanations[props.engineer.id] ?? null)
const readonly = computed(() => !props.plan || !!props.busy)

/** Адреса у инженера в модели нет, только координаты: офис узнаём по совпадению с ним. */
const start = computed(() => {
  const { startLat, startLon } = props.engineer
  const office = props.office
  if (office && Math.abs(office.lat - startLat) < 1e-6 && Math.abs(office.lon - startLon) < 1e-6) {
    return `Офис: ${office.address}`
  }
  return `Дом: ${startLat.toFixed(5)}, ${startLon.toFixed(5)}`
})

/** Тексты объяснений — списком по предложениям, как пункты в макете. */
function points(text: string | undefined): string[] {
  return (text ?? '')
    .split(/(?<=\.)\s+/)
    .map((part) => part.trim())
    .filter(Boolean)
}

const summary = computed(() => {
  const stops = route.value?.stops.length ?? 0
  if (!stops) return 'без заявок'
  return `${plural(stops, ['заявка', 'заявки', 'заявок'])} · ${km(route.value!.km)}`
})

// Момент обоих событий карточки — время дня в шапке; с какого момента пересчитать день, спрашивает
// «Пересчёт дня» после кнопки (pages/index.vue).
function unavailable() {
  if (!props.plan) return
  missing.value = null
  emit('event', { type: 'engineer_unavailable', time: props.time, engineerId: props.engineer.id })
}

/** «Сохранить изменения» — событие `update_engineer` во время дня. */
function save() {
  const body = engineerBody(draft.value)
  if ('text' in body) return (missing.value = body)
  missing.value = null
  emit('save', body, props.time)
}
</script>

<template>
  <div class="fade-scroll">
    <div class="fade-header flex gap-2">
      <button class="round" title="Назад" @click="emit('back')"><Icon name="back" /></button>
      <Segmented
        v-model="tab"
        class="flex-1"
        :options="[
          { value: 'data', label: 'данные', icon: 'data' },
          { value: 'graph', label: 'график', icon: 'schedule' },
        ]"
      />
    </div>

    <div class="flex flex-col gap-2">
      <template v-if="tab === 'data'">
        <p v-if="!plan" class="muted small">Поля можно будет править после расчёта плана.</p>
        <EngineerFields v-model="draft" :disabled="readonly" :start="start" :invalid="invalid" />
      </template>

      <template v-else-if="plan">
        <div class="card text-center">{{ summary }}</div>
        <!-- Не «Почему назначено {имя}» из макета: имена приходят из файла, склонять их нечем. -->
        <details v-if="why?.assignment" class="card group">
          <summary class="flex cursor-pointer list-none items-center justify-between [&::-webkit-details-marker]:hidden">Почему назначены эти заявки? <Icon name="chevron" class="transition-transform duration-250 group-open:rotate-180" /></summary>
          <ul class="muted small mt-1.5 mb-0 pl-[18px]"><li v-for="point in points(why.assignment)" :key="point">{{ point }}</li></ul>
        </details>
        <details v-if="why?.order" class="card group">
          <summary class="flex cursor-pointer list-none items-center justify-between [&::-webkit-details-marker]:hidden">Почему выбран этот маршрут? <Icon name="chevron" class="transition-transform duration-250 group-open:rotate-180" /></summary>
          <ul class="muted small mt-1.5 mb-0 pl-[18px]"><li v-for="point in points(why.order)" :key="point">{{ point }}</li></ul>
        </details>
        <EngineerTimeline :plan="plan" :engineer="engineer" @select="emit('select', $event)" />
      </template>
      <p v-else class="muted small">План ещё не посчитан.</p>
    </div>

    <div class="fade-footer flex flex-col gap-2">
      <template v-if="tab === 'data' && plan">
        <p v-if="engineer.unavailableFrom" class="card muted">Недоступен с {{ engineer.unavailableFrom }}</p>
        <button v-else class="btn wide" :disabled="!!busy" @click="unavailable">
          <Icon name="unavailable" /> Сделать недоступным
        </button>
      </template>
      <p v-if="missing || error" class="error small">{{ missing?.text ?? error }}</p>
      <Transition name="appear">
        <button v-if="plan && dirty" class="btn primary wide" :disabled="!!busy" @click="save">
          <Icon name="check" /> Сохранить изменения
        </button>
      </Transition>
    </div>
  </div>
</template>
