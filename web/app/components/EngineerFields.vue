<script setup lang="ts">
// Поля инженера в стиле макета (docs/design.md, кадры 1508, 1475). Одни у карточки и окна
// добавления; у карточки ещё ID — только для чтения. Участка и количеств оборудования нет (P):
// участка в приложении нет, вместимость задана по транспорту. Начало дня одинаковое у обоих
// (правки 82): галочка «Начинает рабочий день из дома», под ней с галочкой — «Адрес дома» с
// подсказками, без неё — «Адрес офиса» набора, только для чтения.
import { splitGear, type EngineerDraft } from '~/composables/entityForms'
import { GEAR_NAMES, SHIFT_PRESETS, SKILL_NAMES, TRANSPORT_NAMES } from '~/composables/useApi'

const draft = defineModel<EngineerDraft>({ required: true })
const home = defineModel<{ address: string; lat: number | null; lon: number | null }>('home', { required: true })
const fromHome = defineModel<boolean>('fromHome', { required: true })

defineProps<{
  disabled?: boolean
  // Поля черновика, из-за которых форма не отправилась: они красные.
  invalid?: string[]
  // Только у карточки: ID — ключ, его не правят.
  showId?: boolean
  office: string
  homeInvalid?: boolean
}>()

const gear = computed({
  get: () => [...draft.value.equipment, ...draft.value.tools],
  set: (codes: string[]) => {
    const { equipment, tools } = splitGear(codes)
    draft.value.equipment = equipment
    draft.value.tools = tools
  },
})

// График смены (п. 74): пресет заполняет оба поля. Выбор вычисляется по полям: часы не из
// пресета — «свой». Поля начала и конца смены видны только у «свой» (правки 81.5). Явный выбор
// «свой» при часах, совпавших с пресетом, запоминается — иначе пункт был бы виден, но не
// выбирался. Держится, пока форма показывает ту же бригаду.
const CUSTOM = 'custom'
const SHIFT_OPTIONS = {
  ...Object.fromEntries(Object.entries(SHIFT_PRESETS).map(([id, preset]) => [id, preset.name])),
  [CUSTOM]: 'свой',
}
const custom = ref(false)
watch(
  () => draft.value.id,
  () => (custom.value = false),
)
const shift = computed({
  get: () =>
    custom.value
      ? CUSTOM
      : (Object.entries(SHIFT_PRESETS).find(
          ([, preset]) => preset.start === draft.value.shiftStart && preset.end === draft.value.shiftEnd,
        )?.[0] ?? CUSTOM),
  set: (id: string) => {
    const preset = SHIFT_PRESETS[id]
    custom.value = !preset
    if (!preset) return // «свой» — поля остаются, какие есть
    draft.value.shiftStart = preset.start
    draft.value.shiftEnd = preset.end
  },
})

const skills = computed({
  get: () => draft.value.skills as string[],
  set: (codes: string[]) => (draft.value.skills = codes as EngineerDraft['skills']),
})
</script>

<template>
  <div class="flex flex-col gap-2">
    <label class="field" :class="{ invalid: invalid?.includes('name') }">
      <span>Фамилия</span>
      <input v-model="draft.name" type="text" :disabled="disabled" placeholder="Соколов">
    </label>
    <!-- ID и адрес офиса не правятся — вид отключённого поля, как «Способ передвижения» у
         завершённой заявки (правки 82). -->
    <div v-if="showId" class="field"><span>ID</span><span class="text-muted">{{ draft.id }}</span></div>
    <!-- Кадр 1786: строка 28 px, подпись 14 / 18. -->
    <label class="option text-sm leading-[18px]">
      <input v-model="fromHome" type="checkbox" :disabled="disabled">
      <CheckMark :checked="fromHome" :size="28" />
      Начинает рабочий день из дома
    </label>
    <AddressField v-if="fromHome" v-model="home" label="Адрес дома" :disabled="disabled" :invalid="homeInvalid" />
    <div v-else class="field"><span>Адрес офиса</span><span class="truncate text-muted">{{ office }}</span></div>
    <SelectField v-model="draft.transport" label="Способ передвижения" :options="TRANSPORT_NAMES" :disabled="disabled" />
    <SelectField v-model="shift" label="График" :options="SHIFT_OPTIONS" :disabled="disabled" />
    <div v-if="shift === CUSTOM" class="grid grid-cols-2 gap-2">
      <label class="field" :class="{ invalid: invalid?.includes('shiftStart') }">
        <span>Начало смены</span>
        <TimeInput v-model="draft.shiftStart" :disabled="disabled" />
      </label>
      <label class="field" :class="{ invalid: invalid?.includes('shiftEnd') }">
        <span>Окончание смены</span>
        <TimeInput v-model="draft.shiftEnd" :disabled="disabled" />
      </label>
    </div>
    <!-- Окно обеда (блок 79, кадр 1786): обед — час внутри него, между работами. -->
    <div class="grid grid-cols-2 gap-2">
      <label class="field" :class="{ invalid: invalid?.includes('lunchStart') }">
        <span>Начало окна обеда</span>
        <TimeInput v-model="draft.lunchStart" :disabled="disabled" />
      </label>
      <label class="field" :class="{ invalid: invalid?.includes('lunchEnd') }">
        <span>Окончание окна обеда</span>
        <TimeInput v-model="draft.lunchEnd" :disabled="disabled" />
      </label>
    </div>
    <SelectField v-model="skills" label="Навыки" :options="SKILL_NAMES" multiple :disabled="disabled" :invalid="invalid?.includes('skills')" />
    <SelectField v-model="gear" label="Оборудование" :options="GEAR_NAMES" multiple empty="нет" :disabled="disabled" />
  </div>
</template>
