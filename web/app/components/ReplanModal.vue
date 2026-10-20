<script setup lang="ts">
// «Пересчёт дня» (PLAN 6.12, блок 53): варианты реакции на событие. Открывается после кнопки действия
// в карточках заявки и инженера и в окнах добавления и сразу считает варианты — пока идёт расчёт,
// вместо карточек их заготовки, полоса — над кнопками. Каждый вариант — черновик плана на
// сервере. Выбранная карточка видна на карте вместо текущего плана. «рекомендуется» — режим по
// умолчанию, если другой не выполняет больше аварий или заявок; «весь день заново» не рекомендуется
// никогда. Режим, не принявший событие, стоит серой карточкой с причиной.
// В карточке — было → стало по трём числам (правки 82): без назначения, инженеров, путь; число,
// которое не изменилось, печатается одно. Кому сообщить новое время — в «План обновлён» после «Выбрать».
// Кого отправить на аварию, спрашивает окно перед этим — «Кто поедет на аварию?» (блок 77).
import type { Bar } from '~/composables/progress'
import { km, type Plan, type PlanEvent, type Preview, type PreviewOption, type ReplanMode } from '~/composables/useApi'

const props = defineProps<{
  event: PlanEvent
  plan: Plan
  preview: Preview | null
  busy: string | null
  progress: Bar | null
  error: string | null
}>()
const emit = defineEmits<{ back: []; apply: [] }>()
const chosen = defineModel<string | null>('chosen', { required: true })

const TITLES: Record<ReplanMode, (time: string) => string> = {
  insert: () => 'Встроить в свободное время',
  from_event: (time) => `Пересчитать с ${time}`,
  full: () => 'Пересчитать весь день',
}

const change = (old: string, now: string) => (old === now ? now : `${old} → ${now}`)

/** Было → стало по варианту: без назначения, инженеров, путь. */
function metrics(option: PreviewOption): string[] {
  const before = props.plan.metrics
  const after = option.plan.metrics
  return [
    `Заявок без назначений ${change(String(before.unassignedCount), String(after.unassignedCount))}`,
    `Задействованных инженеров ${change(String(before.engineersUsed), String(after.engineersUsed))}`,
    `Путь ${change(km(before.totalKm), km(after.totalKm))}`,
  ]
}

const refused = computed(() => Object.entries(props.preview?.refused ?? {}) as [ReplanMode, string][])
</script>

<template>
  <Modal title="Пересчёт дня" wide :closable="!busy" @back="emit('back')">
    <!-- Пока считается: заготовки карточек того же размера, чтобы окно не прыгало. -->
    <template v-if="!preview">
      <div
        v-for="row in 2"
        :key="row"
        class="choice"
        aria-hidden="true"
      >
        <span class="skeleton size-[26px] shrink-0 rounded-full" />
        <span class="flex flex-1 flex-col gap-1.5 py-0.5">
          <span class="skeleton h-3.5 w-2/5 rounded-full" />
          <span class="skeleton h-3 w-3/5 rounded-full" />
          <span class="skeleton h-3 w-3/5 rounded-full" />
          <span class="skeleton h-3 w-2/5 rounded-full" />
        </span>
      </div>
    </template>
    <template v-else>
      <label
        v-for="option in preview.options"
        :key="option.plan.id"
        class="choice cursor-pointer select-none hover:bg-field/60 [&>input]:pointer-events-none [&>input]:absolute [&>input]:opacity-0"
        :class="{ '!border-accent': chosen === option.plan.id }"
      >
        <input v-model="chosen" type="radio" :value="option.plan.id" :disabled="!!busy">
        <CheckMark :size="24" :checked="chosen === option.plan.id" />
        <span class="flex flex-1 flex-col gap-1">
          <span class="flex flex-wrap items-center gap-2 text-base leading-5">
            {{ TITLES[option.mode](event.time) }}
            <span v-if="option.mode === preview.recommended" class="badge !bg-accent">рекомендуется</span>
          </span>
          <span v-for="line in metrics(option)" :key="line" class="text-sm leading-4 text-muted">{{ line }}</span>
        </span>
      </label>
      <div
        v-for="[mode, reason] in refused"
        :key="mode"
        class="flex flex-col gap-0.5 rounded-[22px] border border-dashed border-[#dde2e8] py-2 pr-2 pl-[13px] opacity-70"
      >
        <span>{{ TITLES[mode](event.time) }}</span>
        <span class="text-sm text-muted">Нельзя: {{ reason }}</span>
      </div>
    </template>
    <template #footer>
      <ProgressBar v-if="progress" :bar="progress" />
      <p v-if="error" class="error">{{ error }}</p>
      <div class="grid grid-cols-2 gap-2">
        <button class="btn" :disabled="!!busy" @click="emit('back')"><Icon name="arrow-left" /> Назад</button>
        <button class="btn primary" :disabled="!!busy || !preview || !chosen" @click="emit('apply')">
          <Icon name="check" /> Выбрать
        </button>
      </div>
    </template>
  </Modal>
</template>
