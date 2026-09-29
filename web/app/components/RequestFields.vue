<script setup lang="ts">
// Поля заявки в стиле макета (docs/design.md, кадры 1511, 1469, 1474, 1476). Одни и те же у
// карточки и окна добавления: у окна сверху «Тип заявки» (он задаёт навык, нормативы и
// инструменты), у карточки — «Требуемая квалификация» и серые времена работ. Координаты ищет
// геокодер при сохранении: поле адреса только сбрасывает прежнюю точку. Подсказка 2ГИС с домом
// ставит точку сразу, и геокодер при сохранении уже не нужен (блок 34).
import { splitGear, type RequestDraft } from '~/composables/entityForms'
import {
  GEAR_NAMES,
  REQUEST_TYPES,
  requestTypeOf,
  SKILL_NAMES,
  TRANSPORT_NAMES,
  useApi,
  type AddressSuggestion,
} from '~/composables/useApi'

const draft = defineModel<RequestDraft>({ required: true })

const props = defineProps<{
  mode: 'card' | 'add'
  disabled?: boolean
  // Поля черновика, из-за которых форма не отправилась: они красные.
  invalid?: string[]
  // Начало и окончание работ стопа — только для чтения: времена считает расписание (PLAN 6.19).
  times?: { start: string; end: string } | null
}>()

// Адрес переписали — прежние координаты к нему не относятся: иначе заявка с адресом B
// уехала бы в точку A. Смена самого черновика (открыли другую заявку) — не правка адреса.
watch(
  () => [draft.value, draft.value.address] as const,
  ([current], [previous]) => {
    if (current === previous) {
      current.lat = null
      current.lon = null
    }
  },
  { flush: 'sync' },
)

// Подсказки адреса. Запрос — по вводу, а не по смене `draft.address`: открыть другую заявку
// в карточке — не повод тратить ключ 2ГИС. Дебаунс 800 мс и три символа — тоже ради ключа:
// при 400 мс запрос уходил на каждое слово, а в минуту их всего пять.
const SUGGEST_DEBOUNCE_MS = 800
const api = useApi()
const hints = ref<AddressSuggestion[]>([])
const active = ref(-1)
let timer: ReturnType<typeof setTimeout> | undefined
// Номер попытки: ушли из поля или нажали Esc — ответ, который ещё летит, уже не показываем.
let round = 0
onBeforeUnmount(close)

function close() {
  clearTimeout(timer)
  round += 1
  hints.value = []
}

function onType() {
  close()
  const query = draft.value.address.trim()
  if (props.disabled || query.length < 3) return
  const mine = round
  timer = setTimeout(async () => {
    const found = await api.suggest(query)
    // Пока ждали, адрес дописали, ушли из поля или открыли другую заявку — ответ не про них.
    if (mine !== round || draft.value.address.trim() !== query) return
    hints.value = found
    active.value = -1
  }, SUGGEST_DEBOUNCE_MS)
}

/** Адрес — первым: `watch` выше сбрасывает точку при его смене, и только потом ставится новая. */
function pick(hint: AddressSuggestion) {
  close()
  draft.value.address = hint.address
  if (hint.lat !== null && hint.lon !== null) Object.assign(draft.value, { lat: hint.lat, lon: hint.lon })
}

function onKey(event: KeyboardEvent) {
  if (!hints.value.length) return
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    const count = hints.value.length
    if (active.value < 0) active.value = event.key === 'ArrowDown' ? 0 : count - 1
    else active.value = (active.value + (event.key === 'ArrowDown' ? 1 : -1) + count) % count
  } else if (event.key === 'Enter' && hints.value[active.value]) {
    event.preventDefault()
    pick(hints.value[active.value]!)
  } else if (event.key === 'Escape') {
    event.stopPropagation()
    close()
  }
}

/** Тип BK однозначно задаёт поля заявки (docs/design.md, «Тип заявки»). */
const type = computed({
  get: () => {
    const found = requestTypeOf(draft.value)
    return found ? REQUEST_TYPES.indexOf(found) : -1
  },
  set: (index: number) => {
    const chosen = REQUEST_TYPES[index]
    if (!chosen) return
    Object.assign(draft.value, {
      skill: chosen.skill,
      workPriority: chosen.workPriority,
      serviceDurationMin: chosen.serviceDurationMin,
      baseNormMin: chosen.baseNormMin,
      requiredTransport: chosen.requiredTransport,
      requiredTools: [...chosen.requiredTools],
    })
  },
})

/**
 * «Требуемая квалификация» в карточке. Тип BK задаёт поля целиком (docs/design.md, «Тип заявки»),
 * поэтому смена навыка переводит заявку в тип с этим навыком со всеми его полями: с тем же типом
 * работы, если такой есть, иначе в первый с этим навыком. Сменить только навык — получить, например,
 * «Локальную заявку» с нормативом дозаказа и без тестера.
 */
const skill = computed({
  get: () => draft.value.skill,
  set: (value: RequestDraft['skill']) => {
    const same = REQUEST_TYPES.findIndex((item) => item.skill === value && item.workPriority === draft.value.workPriority)
    const index = same >= 0 ? same : REQUEST_TYPES.findIndex((item) => item.skill === value)
    if (index >= 0) type.value = index
    else draft.value.skill = value
  },
})

const gear = computed({
  get: () => [...draft.value.requiredEquipment, ...draft.value.requiredTools],
  set: (codes: string[]) => {
    const { equipment, tools } = splitGear(codes)
    draft.value.requiredEquipment = equipment
    draft.value.requiredTools = tools
  },
})
</script>

<template>
  <div class="flex flex-col gap-2">
    <SelectField
      v-if="mode === 'add'"
      v-model="type"
      label="Тип заявки"
      :options="REQUEST_TYPES.map((item, index) => ({ value: index, label: item.title }))"
      :disabled="disabled"
    />
    <div>
      <label class="field" :class="{ invalid: invalid?.includes('address') }">
        <span>{{ mode === 'add' ? 'Адрес' : 'Адрес подключения' }}</span>
        <input
          v-model="draft.address"
          type="text"
          :disabled="disabled"
          placeholder="Москва, улица, дом"
          autocomplete="off"
          @input="onType"
          @keydown="onKey"
          @blur="close"
        >
      </label>
      <!-- mousedown.prevent: иначе поле теряет фокус раньше клика, и список исчезает под рукой -->
      <div v-if="hints.length" class="mt-1 flex flex-col rounded-card bg-field">
        <button
          v-for="(hint, index) in hints"
          :key="index"
          type="button"
          class="cursor-pointer border-0 border-t border-[#e2e7ec] px-3.5 py-2.5 text-left first:border-t-0"
          :class="index === active ? 'bg-[#e2e7ec]' : 'bg-transparent'"
          @mousedown.prevent="pick(hint)"
        >
          {{ hint.address }}
        </button>
      </div>
    </div>
    <div class="grid grid-cols-2 gap-2">
      <label class="field" :class="{ invalid: invalid?.includes('windowStart') }">
        <span>Начало окна</span>
        <TimeInput v-model="draft.windowStart" :disabled="disabled" />
      </label>
      <label class="field" :class="{ invalid: invalid?.includes('windowEnd') }">
        <span>Окончание окна</span>
        <TimeInput v-model="draft.windowEnd" :disabled="disabled" />
      </label>
    </div>
    <SelectField
      v-model="draft.requiredTransport"
      label="Способ передвижения"
      :options="[{ value: null, label: 'не требуется' }, ...Object.entries(TRANSPORT_NAMES).map(([value, label]) => ({ value, label }))]"
      :disabled="disabled"
    />
    <div v-if="times" class="grid grid-cols-2 gap-2">
      <div class="field readonly"><span>Начало работ</span>{{ times.start }}</div>
      <div class="field readonly"><span>Окончание работ</span>{{ times.end }}</div>
    </div>
    <SelectField v-model="gear" label="Оборудование" :options="GEAR_NAMES" multiple empty="не требуется" :disabled="disabled" />
    <SelectField v-if="mode === 'card'" v-model="skill" label="Требуемая квалификация" :options="SKILL_NAMES" :disabled="disabled" />
  </div>
</template>
