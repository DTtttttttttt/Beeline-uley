<script setup lang="ts">
// «Новый датасет» (docs/design.md, кадры 1500, 94, 1498, 1499): демо-набор или свой файл, затем
// расчёт всех вариантов. Запросов окно не делает — их делает pages/index.vue, как и раньше
// у DataBar; окно только собирает FormData и показывает, что идёт и что не вышло.
import type { Bar } from '~/composables/progress'
import { plural, TRANSPORT_NAMES, type Dataset, type DemoCatalog } from '~/composables/useApi'

const props = defineProps<{
  dataset: Dataset | null
  demos: DemoCatalog | null
  busy: string | null
  error: string | null
  progress: Bar | null
  closable: boolean
}>()
const emit = defineEmits<{ back: []; demo: [requests: string, crew: string]; load: [form: FormData] }>()

const FORMATS = ['csv', 'json'] as const
const tab = ref<'demo' | 'upload'>('demo')
const format = ref<'csv' | 'json'>('csv')
const files = reactive<{ requests: File | null; engineers: File | null; json: File | null }>({
  requests: null,
  engineers: null,
  json: null,
})
// Чего не хватает для загрузки: кнопка, которая молча ничего не делает, неотличима от
// зависшего запроса.
const missing = ref<string | null>(null)
// Отказ сервера относится к тому выбору, с которым загружали: сменили файл, формат или
// вкладку — прежний текст уже не про него и снимается.
const dismissed = ref(false)
watch(() => props.error, () => (dismissed.value = false))
watch([tab, format], () => {
  dismissed.value = true
  missing.value = null
})

/**
 * Демо-набор — пара «заявки участка + состав инженеров» (PLAN 6.20). Выбор открывается на
 * текущей паре (`demo:<заявки>:<состав>`), иначе на встроенной — «Восток», штатный состав.
 */
const REQUEST_FORMS = ['заявка', 'заявки', 'заявок'] as const
const ENGINEER_FORMS = ['инженер', 'инженера', 'инженеров'] as const
const [, currentRequests, currentCrew] = props.dataset?.id.startsWith('demo:') ? props.dataset.id.split(':') : []
const requestsId = ref(currentRequests ?? 'vostok')
const crewId = ref(currentCrew ?? 'staff')
const requestOptions = computed(() =>
  (props.demos?.requests ?? []).map((item) => ({ value: item.id, label: `${item.name} · ${plural(item.count, REQUEST_FORMS)}` })),
)
const crewOptions = computed(() =>
  (props.demos?.crews ?? []).map((item) => ({ value: item.id, label: `${item.name} · ${plural(item.count, ENGINEER_FORMS)}` })),
)
const chosenRequests = computed(() => props.demos?.requests.find((item) => item.id === requestsId.value) ?? null)
const chosenCrew = computed(() => props.demos?.crews.find((item) => item.id === crewId.value) ?? null)
// Пары текущего набора в каталоге может не оказаться (пару переименовали):
// тогда встроенная, иначе первая — пустой выбор с выключенной кнопкой ничего не объяснял бы.
watch(
  () => props.demos,
  (demos) => {
    if (!demos) return
    const fallback = (list: { id: string }[], preferred: string) =>
      (list.find((item) => item.id === preferred) ?? list[0])?.id ?? preferred
    if (!chosenRequests.value) requestsId.value = fallback(demos.requests, 'vostok')
    if (!chosenCrew.value) crewId.value = fallback(demos.crews, 'staff')
  },
  { immediate: true },
)

/**
 * Свойства выбранной пары — числами из каталога, а не текстом макета (L): в макете
 * «10 инженеров», а их 12.
 */
const facts = computed(() => {
  const qualitative = [
    'пересекающиеся временные окна',
    'есть конфликтные заявки',
    'есть данные для сравнения базового и оптимизированного плана',
  ]
  const requests = chosenRequests.value
  const crew = chosenCrew.value
  if (!requests || !crew) return qualitative
  const skills = crew.skills.length
  return [
    plural(requests.count, REQUEST_FORMS),
    plural(crew.count, ENGINEER_FORMS),
    skills === 3 ? 'все 3 типа квалификаций' : `квалификаций: ${skills} из 3`,
    `транспорт: ${crew.transports.map((transport) => TRANSPORT_NAMES[transport].toLowerCase()).join(', ')}`,
    ...qualitative,
  ]
})

const zones = computed(() =>
  format.value === 'csv'
    ? [
        {
          key: 'requests' as const,
          label: 'Заявки',
          title: 'Загрузите файл с заявками',
          hint: 'Перетащите CSV сюда или выберите файл. Офис — последней строкой: «Адрес офиса;<адрес>»',
        },
        { key: 'engineers' as const, label: 'Инженеры', title: 'Загрузите файл с инженерами', hint: 'Перетащите CSV сюда или выберите файл' },
      ]
    : [
        {
          key: 'json' as const,
          label: 'Датасет',
          title: 'Загрузите файл датасета',
          hint: 'Один JSON с заявками, инженерами и офисом',
        },
      ],
)

function pick(key: 'requests' | 'engineers' | 'json', list: FileList | null | undefined) {
  files[key] = list?.[0] ?? null
  missing.value = null
  dismissed.value = true
}

/** Один JSON («file») или два CSV («requests», «engineers») — имена полей обработчика (PLAN 5.4). */
function upload() {
  const form = new FormData()
  if (format.value === 'json') {
    if (!files.json) return (missing.value = 'Выберите файл JSON с набором')
    form.append('file', files.json)
  } else {
    if (!files.requests || !files.engineers) return (missing.value = 'Нужны оба файла: заявки и инженеры')
    form.append('requests', files.requests)
    form.append('engineers', files.engineers)
  }
  missing.value = null
  emit('load', form)
}
</script>

<template>
  <Modal title="Новый датасет" :closable="closable" @back="emit('back')">
    <Segmented
      v-model="tab"
      :options="[
        { value: 'demo', label: 'демо-набор', icon: 'demo' },
        { value: 'upload', label: 'загрузить данные', icon: 'upload' },
      ]"
    />

    <template v-if="tab === 'demo'">
      <p class="muted m-0">
        Выберите демо-датасет: заявки одного из участков и состав инженеров, по которым рассчитаем распределение
        заявок и маршруты.
      </p>
      <template v-if="demos">
        <SelectField v-model="requestsId" label="Заявки" :options="requestOptions" />
        <SelectField v-model="crewId" label="Инженеры" :options="crewOptions" />
      </template>
      <ul class="card muted m-0 flex list-disc flex-col gap-1.5 py-3 pr-3.5 pl-[34px]">
        <li v-for="fact in facts" :key="fact">{{ fact }}</li>
      </ul>
    </template>

    <template v-else>
      <div class="flex flex-col gap-2">
        <label v-for="option in FORMATS" :key="option" class="option">
          <input v-model="format" type="radio" :value="option">
          <CheckMark :checked="format === option" />
          {{ option.toUpperCase() }}
        </label>
      </div>
      <div v-for="zone in zones" :key="zone.key" class="flex flex-col gap-1.5">
        <span>{{ zone.label }}</span>
        <label
          class="relative flex cursor-pointer flex-col items-center gap-1 rounded-card border-2 border-dashed border-[#b9c1ce] bg-field/50 p-4 transition-colors duration-200 hover:border-muted text-center [&_input]:pointer-events-none [&_input]:absolute [&_input]:opacity-0 [&_strong]:break-all [&_strong]:font-medium"
          @dragover.prevent
          @drop.prevent="pick(zone.key, ($event as DragEvent).dataTransfer?.files)"
        >
          <input
            type="file"
            :accept="format === 'csv' ? '.csv,text/csv' : '.json,application/json'"
            @change="pick(zone.key, ($event.target as HTMLInputElement).files)"
          >
          <span class="mb-2 flex size-[84px] items-center justify-center rounded-full bg-[#fbefc4]"><Icon name="dropzone" /></span>
          <strong>{{ files[zone.key]?.name ?? zone.title }}</strong>
          <span class="muted small">{{ files[zone.key] ? 'Выбрать другой файл' : zone.hint }}</span>
        </label>
      </div>
    </template>

    <template #footer>
      <!-- Ход загрузки — полосой над кнопкой; отказ прошлой попытки на это время прячется. -->
      <ProgressBar v-if="progress" :bar="progress" />
      <p v-if="!busy && (missing || (error && !dismissed))" class="error">{{ missing ?? error }}</p>
      <button
        v-if="tab === 'demo'"
        class="btn primary wide"
        :disabled="!!busy || !chosenRequests || !chosenCrew"
        @click="emit('demo', requestsId, crewId)"
      >
        <Icon name="check" /> Использовать демо-набор
      </button>
      <button v-else class="btn primary wide" :disabled="!!busy" @click="upload">
        <Icon name="check" /> Загрузить и рассчитать
      </button>
    </template>
  </Modal>
</template>
