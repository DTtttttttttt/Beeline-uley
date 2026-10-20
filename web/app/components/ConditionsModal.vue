<script setup lang="ts">
// «Условия работы» (блок 78, кадр 1785): четыре карточки-флажка с пояснением, отмеченная — в
// жёлтой рамке, как вариант в «Вариантах плана». Правит копию: «Сохранить» отдаёт её заявке.
import type { RequestDraft } from '~/composables/entityForms'
import { CONDITION_NAMES } from '~/composables/useApi'

type Conditions = Pick<RequestDraft, 'gigabit' | 'repeatVisit' | 'cable' | 'drilling'>

const props = defineProps<{ draft: RequestDraft; speed?: number }>()
const emit = defineEmits<{ back: []; save: [patch: Conditions] }>()

const OPTIONS = (Object.keys(CONDITION_NAMES) as (keyof Conditions)[]).map((key) => ({ key, ...CONDITION_NAMES[key] }))
const picked = ref<Conditions>({
  gigabit: props.draft.gigabit,
  repeatVisit: props.draft.repeatVisit,
  cable: props.draft.cable,
  drilling: props.draft.drilling,
})
const fields = computed(() => ({ ...props.draft, ...picked.value }))
</script>

<template>
  <Teleport to="#sheet-vars">
    <Modal title="Условия работы" wide @back="emit('back')">
      <p class="muted m-0">Укажите особенности выезда, которые могут повлиять на время работы</p>
      <label
        v-for="option in OPTIONS"
        :key="option.key"
        class="choice cursor-pointer select-none hover:bg-field/60 [&>input]:pointer-events-none [&>input]:absolute [&>input]:opacity-0"
        :class="{ '!border-accent': picked[option.key] }"
      >
        <input v-model="picked[option.key]" type="checkbox">
        <CheckMark :size="24" :checked="picked[option.key]" />
        <span class="flex flex-col gap-1">
          <span class="text-base leading-4">{{ option.title }}</span>
          <span class="text-sm leading-[14px] text-muted">{{ option.hint }}</span>
        </span>
      </label>
      <!-- Перед блоком — 14 px, а не 8 (кадры 1783, 1785). -->
      <DurationWhy class="mt-1.5" :fields="fields" :speed="speed" />
      <template #footer>
        <div class="grid grid-cols-2 gap-2">
          <button class="btn" @click="emit('back')"><Icon name="arrow-left" /> Назад</button>
          <button class="btn primary" @click="emit('save', { ...picked })"><Icon name="check" /> Сохранить</button>
        </div>
      </template>
    </Modal>
  </Teleport>
</template>
