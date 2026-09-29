<script setup lang="ts">
// Поля инженера в стиле макета (docs/design.md, кадры 1508, 1475). Одни у карточки и окна
// добавления; у карточки ещё ID и стартовая точка — только для чтения. Участка и количеств
// оборудования нет (P): участка в приложении нет, вместимость задана по транспорту.
import { splitGear, type EngineerDraft } from '~/composables/entityForms'
import { GEAR_NAMES, SKILL_NAMES, TRANSPORT_NAMES } from '~/composables/useApi'

const draft = defineModel<EngineerDraft>({ required: true })

defineProps<{
  disabled?: boolean
  // Поля черновика, из-за которых форма не отправилась: они красные.
  invalid?: string[]
  // Только у карточки: ID — ключ, стартовая точка — текстом («Офис: …» или «Дом: lat, lon»).
  start?: string | null
}>()

const gear = computed({
  get: () => [...draft.value.equipment, ...draft.value.tools],
  set: (codes: string[]) => {
    const { equipment, tools } = splitGear(codes)
    draft.value.equipment = equipment
    draft.value.tools = tools
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
    <template v-if="start !== undefined">
      <div class="field readonly"><span>ID</span>{{ draft.id }}</div>
      <div class="field readonly"><span>Стартовая точка</span>{{ start }}</div>
    </template>
    <SelectField v-model="draft.transport" label="Способ передвижения" :options="TRANSPORT_NAMES" :disabled="disabled" />
    <div class="grid grid-cols-2 gap-2">
      <label class="field" :class="{ invalid: invalid?.includes('shiftStart') }">
        <span>Начало смены</span>
        <TimeInput v-model="draft.shiftStart" :disabled="disabled" />
      </label>
      <label class="field" :class="{ invalid: invalid?.includes('shiftEnd') }">
        <span>Окончание смены</span>
        <TimeInput v-model="draft.shiftEnd" :disabled="disabled" />
      </label>
    </div>
    <SelectField v-model="skills" label="Навыки" :options="SKILL_NAMES" multiple :disabled="disabled" :invalid="invalid?.includes('skills')" />
    <SelectField v-model="gear" label="Оборудование" :options="GEAR_NAMES" multiple empty="нет" :disabled="disabled" />
  </div>
</template>
