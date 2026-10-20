<script setup lang="ts">
// Поля заявки в стиле макета (docs/design.md, кадры 1511, 1469, 1474, 1476; блок 78 — кадр 1522).
// Одни и те же у карточки и окна добавления: у окна сверху «Тип заявки» (он задаёт навык, нормативы
// и инструменты), у карточки — «Требуемая квалификация» и серые времена работ. Адрес и объект —
// строкой «Информация о здании», условия работы — строкой «Условия работы»: каждая открывает своё
// окно. Под окном заявки — «Почему будет выполняться дольше?». Координаты ищет геокодер при
// сохранении; подсказка 2ГИС с домом ставит точку сразу (блок 34).
import { splitGear, type RequestDraft } from '~/composables/entityForms'
import {
  ACCESS_NAMES,
  BUILDING_TYPE_NAMES,
  CONDITION_NAMES,
  GEAR_NAMES,
  REQUEST_TYPES,
  requestTypeOf,
  typeFields,
  SKILL_NAMES,
  TRANSPORT_NAMES,
  type DurationMode,
} from '~/composables/useApi'

const draft = defineModel<RequestDraft>({ required: true })

const props = defineProps<{
  mode: 'card' | 'add'
  disabled?: boolean
  // Поля черновика, из-за которых форма не отправилась: они красные.
  invalid?: string[]
  // Начало и окончание работ стопа — только для чтения: времена считает расписание (PLAN 6.19).
  times?: { start: string; end: string } | null
  // Скорость назначенного инженера (тысячные): «Почему будет выполняться дольше?» — с ней (PLAN 9.1).
  speed?: number
  // По нормативу сложность не применяется: блока «Почему…» тогда нет.
  durationMode: DurationMode
  // Риск опоздания по нормативу и с учётом сложности — у заявки в плане (блок 78).
  risk?: { before: number; now: number } | null
}>()

// Окно «Информация о здании» или «Условия работы» поверх формы (блок 78).
const editing = ref<'building' | 'conditions' | null>(null)
watch(() => props.disabled, (now) => now && (editing.value = null))

/** «Москва, ул. Профсоюзная, д. 56, к. 1, 10 этаж, можно подъехать близко» (кадр 1522). */
const building = computed(() => {
  const item = draft.value.object
  const parts = [draft.value.address.trim()]
  if (item && item.buildingType !== 'apartment') parts.push(BUILDING_TYPE_NAMES[item.buildingType].toLowerCase())
  if (item?.floor && item.buildingType !== 'private') parts.push(`${item.floor} этаж`)
  if (item) parts.push(ACCESS_NAMES[item.access].toLowerCase())
  return parts.filter(Boolean).join(', ')
})
const conditions = computed(() => {
  const text = (Object.keys(CONDITION_NAMES) as (keyof typeof CONDITION_NAMES)[])
    .filter((key) => draft.value[key])
    .map((key) => CONDITION_NAMES[key].title.toLowerCase())
    .join(', ')
  return text && text[0]!.toUpperCase() + text.slice(1)
})

function saveBuilding(patch: Pick<RequestDraft, 'address' | 'lat' | 'lon' | 'object'>) {
  Object.assign(draft.value, patch)
  editing.value = null
}
function saveConditions(patch: Pick<RequestDraft, 'gigabit' | 'repeatVisit' | 'cable' | 'drilling'>) {
  Object.assign(draft.value, patch)
  editing.value = null
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
    Object.assign(draft.value, typeFields(chosen))
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
    <!-- Строки-сводки (кадр 1522): подпись 13, текст 15 / 18, шеврон 14 в 21 px от края; нажатие
         открывает окно с полями. -->
    <button
      type="button"
      class="field hoverable relative w-full border-0 pr-[45px] text-left disabled:cursor-default [&>span:first-child]:!text-[13px] [&>span:nth-child(2)]:!my-0 [&>span:nth-child(2)]:text-[15px] [&>span:nth-child(2)]:leading-[18px]"
      :class="{ invalid: invalid?.includes('address') || invalid?.includes('object') }"
      :disabled="disabled"
      @click="editing = 'building'"
    >
      <span>Информация о здании</span>
      <span :class="{ 'text-muted': !building }">{{ building || 'адрес не указан' }}</span>
      <Icon v-if="!disabled" name="chevron" class="absolute top-1/2 right-[21px] -translate-y-1/2 -rotate-90" />
    </button>
    <button
      type="button"
      class="field hoverable relative w-full border-0 pr-[45px] text-left disabled:cursor-default [&>span:first-child]:!text-[13px] [&>span:nth-child(2)]:!my-0 [&>span:nth-child(2)]:text-[15px] [&>span:nth-child(2)]:leading-[18px]"
      :disabled="disabled"
      @click="editing = 'conditions'"
    >
      <span>Условия работы</span>
      <span>{{ conditions || 'без особых условий' }}</span>
      <Icon v-if="!disabled" name="chevron" class="absolute top-1/2 right-[21px] -translate-y-1/2 -rotate-90" />
    </button>
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
    <DurationWhy v-if="durationMode === 'complexity'" :fields="draft" :speed="speed" :risk="risk" />
    <div v-if="times" class="grid grid-cols-2 gap-2">
      <!-- Расчётные времена не правятся — вид отключённого поля, как у остальных (правки 82). -->
      <div class="field"><span>Начало работ</span><span class="text-muted">{{ times.start }}</span></div>
      <div class="field"><span>Окончание работ</span><span class="text-muted">{{ times.end }}</span></div>
    </div>
    <SelectField
      v-model="draft.requiredTransport"
      label="Способ передвижения"
      :options="[{ value: null, label: 'не требуется' }, ...Object.entries(TRANSPORT_NAMES).map(([value, label]) => ({ value, label }))]"
      :disabled="disabled"
    />
    <SelectField v-model="gear" label="Оборудование" :options="GEAR_NAMES" multiple empty="не требуется" :disabled="disabled" />
    <SelectField v-if="mode === 'card'" v-model="skill" label="Требуемая квалификация" :options="SKILL_NAMES" :disabled="disabled" />

    <BuildingModal v-if="editing === 'building'" :draft="draft" :speed="speed" :invalid="invalid?.includes('address')" @back="editing = null" @save="saveBuilding" />
    <ConditionsModal v-if="editing === 'conditions'" :draft="draft" :speed="speed" @back="editing = null" @save="saveConditions" />
  </div>
</template>
