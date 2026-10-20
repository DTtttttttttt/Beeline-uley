<script setup lang="ts">
// Карточка инженера в левой панели (docs/design.md, «Карточка инженера»): «данные» — поля
// с «Сделать недоступным» и «Сохранить изменения», «график» — сводка, почему у него эти заявки
// и такой маршрут, шкала дня. Черновик один на карточку: «Сохранить» на «графике» сохраняет
// правку «данных».
import { draftKey, engineerBody, engineerDraft, formProblem, invalidFields, type FormProblem } from '~/composables/entityForms'
import { EQUIPMENT_NAMES, apiError, decimal, km, plural, speedText, useApi, type DaySummary, type Engineer, type Plan, type Equipment, type PlanEvent, type Rating } from '~/composables/useApi'

const props = defineProps<{
  plan: Plan | null
  engineer: Engineer
  // Общее время дня из шапки (блок 33): момент событий карточки.
  time: string
  office: { address: string; lat: number; lon: number } | null
  busy: string | null
  error: string | null
  // Его строка «Итогов дня» (блок 69): скорость сейчас и выполненные заявки план / факт.
  day: DaySummary['engineers'][number] | null
  // Живой рейтинг (блок 72); без отзывов — `null`.
  rating: Rating | null
}>()

const emit = defineEmits<{
  back: []
  select: [requestId: string]
  event: [event: PlanEvent]
  save: [engineer: Engineer, time: string]
  reviews: [engineerId: string]
}>()

const tab = ref<'data' | 'graph'>('data')

// Черновик пересобирается, только когда сам инженер стал другим (см. RequestDetails).
const draft = ref(engineerDraft())
// Черновик, с которым карточку открыли: «Сохранить изменения» видна, только когда его правили.
const pristine = ref('')
const missing = ref<FormProblem | null>(null)
const invalid = computed(() => invalidFields(missing.value, draft.value))
const near = (a: { lat: number; lon: number }, lat: number | null, lon: number | null) =>
  lat !== null && lon !== null && Math.abs(a.lat - lat) < 1e-6 && Math.abs(a.lon - lon) < 1e-6

/**
 * Дом и начало дня (правки 82, как в окне добавления): галочка «Начинает рабочий день из дома»
 * есть всегда. С ней ниже — «Адрес дома» с подсказками, его можно поменять; без неё — «Адрес
 * офиса» набора, только для чтения. Флага в модели нет (блок 73): из дома — это старт в точке
 * дома. Галочка переносит старт и отмечает оборудование: из офиса — всё, из дома — то, что лежит
 * дома, а у нового адреса — отмеченное в форме. Снятая галочка дом не стирает. Сохраняется той
 * же правкой инженера; точку нового адреса без подсказки ищет геокодер — один запрос.
 */
const api = useApi()
const home = ref({ address: '', lat: null as number | null, lon: null as number | null })
const fromHome = ref(false)
const homeInvalid = ref(false)
const geocoding = ref(false)
watch(() => home.value.address, () => (homeInvalid.value = false))

watch(
  () => JSON.stringify(props.engineer),
  () => {
    draft.value = engineerDraft({ ...props.engineer, address: '' })
    pristine.value = draftKey(draft.value)
    missing.value = null
    const own = props.engineer.home
    home.value = { address: own?.address ?? '', lat: own?.lat ?? null, lon: own?.lon ?? null }
    fromHome.value = !!own && near(own, props.engineer.startLat, props.engineer.startLon)
  },
  { immediate: true },
)
// Правленый, но ещё не найденный адрес дома — тоже правка: черновик меняется только при сохранении.
const dirty = computed(() => draftKey(draft.value) !== pristine.value || home.value.address !== (props.engineer.home?.address ?? ''))

/** Оборудование дома: прежнего — то, что лежит там; нового адреса — отмеченное в форме. */
function homeGear(address: string): Equipment[] {
  const own = props.engineer.home
  return own && own.address === address ? [...own.equipment] : [...draft.value.equipment]
}

function startFrom(value: boolean) {
  fromHome.value = value
  if (!value) {
    if (!props.office) return
    draft.value.startLat = props.office.lat
    draft.value.startLon = props.office.lon
    draft.value.equipment = Object.keys(EQUIPMENT_NAMES) as Equipment[]
    return
  }
  const { lat, lon, address } = home.value
  if (lat === null || lon === null) return
  draft.value.startLat = lat
  draft.value.startLon = lon
  draft.value.equipment = homeGear(address)
}

// Подсказка поставила точку нового адреса — с галочкой старт сразу переезжает туда.
watch(
  () => [home.value.lat, home.value.lon],
  ([lat, lon]) => {
    if (!fromHome.value || lat == null || lon == null) return
    draft.value.startLat = lat
    draft.value.startLon = lon
  },
)

const route = computed(() => props.plan?.routes.find((item) => item.engineerId === props.engineer.id) ?? null)
const why = computed(() => props.plan?.routeExplanations[props.engineer.id] ?? null)
const readonly = computed(() => !props.plan || !!props.busy)

/** Тексты объяснений — списком по предложениям, как пункты в макете. */
function points(text: string | undefined): string[] {
  return (text ?? '')
    .split(/(?<=\.)\s+/)
    .map((part) => part.trim())
    .filter(Boolean)
}

/**
 * «Почему работает медленнее нормы?» (кадр 1786): выполненные заявки дня — план и факт работы,
 * по ним скорость и уточнялась; сверху сумма «210 → 248 мин · +38 мин». Скорость сейчас — из
 * профиля, на начало дня — та, по которой построен график.
 */
const percent = (speed: number) => Math.round((speed - 1000) / 10)
const speedNow = computed(() => props.day?.speedNow ?? props.engineer.speed ?? 1000)
const speedStart = computed(() => props.day?.speedStart ?? props.engineer.speed ?? 1000)
const done = computed(() =>
  [...(props.day?.requests ?? [])].sort((a, b) => Math.abs(b.factMin - b.planMin) - Math.abs(a.factMin - a.planMin)),
)
const signed = (minutes: number) => `${minutes < 0 ? '−' : '+'}${Math.abs(minutes)} мин`
const speedWhy = computed(() => {
  const now = percent(speedNow.value)
  const title = now > 0 ? 'Почему работает медленнее нормы?' : 'Почему работает быстрее нормы?'
  const plan = done.value.reduce((sum, item) => sum + item.planMin, 0)
  const fact = done.value.reduce((sum, item) => sum + item.factMin, 0)
  const total = done.value.length ? `${plan} → ${fact} мин · ${signed(fact - plan)}` : 'выполненных заявок пока нет'
  // «на 8 % медленнее нормы, сейчас — на 12 %»: при той же стороне от нормы второй раз её не называем.
  const first = percent(speedStart.value)
  const nowText = Math.sign(first) === Math.sign(now) && now ? `на ${Math.abs(now)} %` : speedText(speedNow.value).text
  return { title, total, startText: speedText(speedStart.value).text, nowText }
})

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
async function save() {
  missing.value = null
  const address = home.value.address.trim()
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
  const changed = address !== (props.engineer.home?.address ?? '')
  const place = address
    ? { address, lat: home.value.lat!, lon: home.value.lon!, equipment: changed ? [...draft.value.equipment] : [...props.engineer.home!.equipment] }
    : null
  const body = engineerBody({
    ...draft.value,
    home: place,
    ...(fromHome.value && place ? { startLat: place.lat, startLon: place.lon } : {}),
  })
  if ('text' in body) return (missing.value = body)
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
        <EngineerFields
          v-model="draft"
          v-model:home="home"
          :from-home="fromHome"
          :disabled="readonly"
          show-id
          :office="office?.address ?? ''"
          :home-invalid="homeInvalid"
          :invalid="invalid"
          @update:from-home="startFrom"
        />
        <!-- Рейтинг (кадр 1786): нажатие открывает «Отзывы»; без отзывов — «нет оценок» и серые звёзды. -->
        <!-- Кадр 1786: «4,5» 44 / 38, подпись 12, звёзды 24 через 6; стрелка — обычная «›» (правки 82). -->
        <button v-if="rating" class="card hoverable flex items-center gap-2.5 border-0 text-left text-text" @click="emit('reviews', engineer.id)">
          <span class="text-[44px] leading-[38px]">{{ decimal(rating.stars, 1) }}</span>
          <span class="flex flex-1 flex-col">
            <span class="muted small">{{ plural(rating.count, ['оценка', 'оценки', 'оценок']) }}</span>
            <Stars :value="rating.stars" class="[--star-gap:6px]" />
          </span>
          <Icon name="chevron" class="-rotate-90" />
        </button>
        <div v-else class="card flex items-center justify-between gap-2.5">
          <span class="muted small">нет оценок</span>
          <Stars :value="0" class="[--star-gap:6px]" />
        </div>
        <!-- Скорость по норме — блока нет (правки 82). -->
        <details v-if="plan && percent(speedNow)" class="card group">
          <summary class="flex cursor-pointer list-none items-center justify-between gap-2 [&::-webkit-details-marker]:hidden">
            <span class="flex flex-col gap-0.5">
              {{ speedWhy.title }}
              <Dotted class="muted" :text="speedWhy.total" :size="3" />
            </span>
            <Icon name="chevron" class="-rotate-90 transition-transform duration-250 group-open:rotate-180" />
          </summary>
          <!-- Раскрыто (кадр 1786): пункты через 6, строки пункта через 2, затем пояснение — через 10. -->
          <ul v-if="done.length" class="mt-2.5 mb-0 flex list-disc flex-col gap-1.5 pl-6">
            <li v-for="item in done" :key="item.requestId">
              {{ signed(item.factMin - item.planMin) }} — заявка №{{ item.requestId }}
              <span class="muted mt-0.5 block">по плану {{ item.planMin }} мин, по факту {{ item.factMin }} мин</span>
            </li>
          </ul>
          <p class="muted mt-2.5 mb-0">
            В начале рабочего дня <span class="text-text">{{ speedWhy.startText }}</span>, сейчас — <span class="text-text">{{ speedWhy.nowText }}</span>.
            Скорость уточняется после каждой выполненной заявки: сравнивается время работы по плану и по факту, дорога и документы
            не учитываются. График на сегодня построен по утренней скорости.
          </p>
        </details>
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
        <EngineerTimeline :plan="plan" :engineer="engineer" :time="time" @select="emit('select', $event)" />
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
        <button v-if="plan && dirty" class="btn primary wide" :disabled="!!busy || geocoding" @click="save">
          <Icon name="check" /> {{ geocoding ? 'Ищем адрес…' : 'Сохранить изменения' }}
        </button>
      </Transition>
    </div>
  </div>
</template>
