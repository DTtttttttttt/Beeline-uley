<script setup lang="ts">
// «Добавление инженера» (docs/design.md, кадр 1475): событие `add_engineer`. Старт — офис или,
// с галочкой «Начинает рабочий день из дома», дом (правки 81.5); его координаты уходят в
// `startLat`/`startLon`: пустыми модель события их не примет.
import { engineerBody, engineerDraft, formProblem, invalidFields, type FormProblem } from '~/composables/entityForms'
import { apiError, useApi, type AssistantEngineerDraft, type Engineer, type Plan, type PlanEvent } from '~/composables/useApi'

// `time` — общее время дня из шапки (блок 33): момент события. `preset` — бригада из «Что
// поможет» (блок 55) или то, что назвал ассистент (блок 59): поля заполнены, идентификатор и
// старт — как у любой новой. У ассистента неназванное — `null`: его заполняют умолчания формы.
const props = defineProps<{
  plan: Plan
  time: string
  busy: string | null
  error: string | null
  preset?: Engineer | AssistantEngineerDraft
}>()
const emit = defineEmits<{ back: []; add: [event: PlanEvent] }>()

/**
 * Идентификатор генерирует фронт: первый свободный `new-N` (правки 81.5). Не `brigade-N`: под ним
 * живут демо-люди с историей отзывов и скоростью, и новичок получил бы их чужими.
 */
function freeId(): string {
  const taken = new Set(props.plan.input.engineers.map((engineer) => engineer.id))
  let next = 1
  while (taken.has(`new-${next}`)) next += 1
  return `new-${next}`
}

const preset = props.preset
const given = preset && {
  name: preset.name,
  shiftStart: preset.shiftStart,
  shiftEnd: preset.shiftEnd,
  lunchStart: 'lunchStart' in preset ? preset.lunchStart : undefined,
  lunchEnd: 'lunchEnd' in preset ? preset.lunchEnd : undefined,
  skills: preset.skills,
  transport: preset.transport,
  equipment: preset.equipment,
  tools: preset.tools,
}
// Человек из запасных приходит со своим id: под чужим он снова оказался бы в запасных.
const own = preset && 'id' in preset && !props.plan.input.engineers.some((item) => item.id === preset.id) ? preset.id : null
const draft = ref(
  engineerDraft({
    ...(given && Object.fromEntries(Object.entries(given).filter(([, value]) => value != null))),
    id: own ?? freeId(),
    startLat: props.plan.input.office.lat,
    startLon: props.plan.input.office.lon,
  }),
)
const missing = ref<FormProblem | null>(null)
const invalid = computed(() => invalidFields(missing.value, draft.value))

/**
 * Дом новичка (правки 81.5 и 82, как в карточке, кадр 1786): галочка «Начинает рабочий день из
 * дома» и под ней адрес дома. С галочкой старт — дом, без неё — офис. Дома лежит то оборудование,
 * что отмечено в форме.
 * Точку адреса ставит подсказка 2ГИС, иначе её ищет геокодер при добавлении — один запрос.
 */
const api = useApi()
const home = ref({ address: '', lat: null as number | null, lon: null as number | null })
const fromHome = ref(false)
const homeInvalid = ref(false)
watch(() => home.value.address, () => (homeInvalid.value = false))
const geocoding = ref(false)

async function submit() {
  missing.value = null
  // Без галочки адрес дома скрыт: найденный подсказкой остаётся домом, недописанный — нет.
  const address = fromHome.value || home.value.lat !== null ? home.value.address.trim() : ''
  if (fromHome.value && !address) {
    homeInvalid.value = true
    return (missing.value = formProblem('Укажите адрес дома или снимите галочку «Начинает рабочий день из дома»', draft.value))
  }
  if (address && (home.value.lat === null || home.value.lon === null)) {
    const target = home.value
    geocoding.value = true
    try {
      Object.assign(target, await api.geocode(address))
    } catch (problem) {
      homeInvalid.value = true
      return (missing.value = formProblem(apiError(problem), draft.value))
    } finally {
      geocoding.value = false
    }
    if (target !== home.value || target.address.trim() !== address) return
  }
  const place = address ? { address, lat: home.value.lat!, lon: home.value.lon!, equipment: [...draft.value.equipment] } : null
  const office = props.plan.input.office
  const body = engineerBody({
    ...draft.value,
    home: place,
    startLat: fromHome.value && place ? place.lat : office.lat,
    startLon: fromHome.value && place ? place.lon : office.lon,
  })
  if ('text' in body) return (missing.value = body)
  emit('add', { type: 'add_engineer', time: props.time, engineer: body })
}
</script>

<template>
  <Modal title="Добавление инженера" @back="emit('back')">
    <EngineerFields
      v-model="draft"
      v-model:home="home"
      v-model:from-home="fromHome"
      :disabled="!!busy"
      :office="plan.input.office.address"
      :home-invalid="homeInvalid"
      :invalid="invalid"
    />
    <template #footer>
      <p v-if="missing || error" class="error small">{{ missing?.text ?? error }}</p>
      <button class="btn primary wide" :disabled="!!busy || geocoding" @click="submit">
        <Icon name="check" /> {{ geocoding ? 'Ищем адрес…' : (busy ?? 'Добавить') }}
      </button>
    </template>
  </Modal>
</template>
