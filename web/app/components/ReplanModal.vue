<script setup lang="ts">
// «Пересчёт дня»: с какого момента пересчитать день. Открывается после кнопки действия в карточках
// заявки и инженера и в окнах добавления — событие уходит только отсюда. «В выбранное время»
// (по умолчанию): начатое к моменту события не трогается, `mode` не передаётся — его выбирает
// сервер. «С начала дня» — режим `full`: как будто событие было известно утром. Выбор каждый раз
// начинается с «В выбранное время»: иначе следующее действие молча пересчитало бы день без
// закреплений (блок 29). В макете вместо выбора флажок «Перепланировать весь день заново» — заменён
// по просьбе заказчика: из флажка не было видно, от какого момента считается день.
// Окно открыто и во время расчёта: над «Пересчитать» идёт полоса, закрывает окно ответ сервера.
import type { Bar } from '~/composables/progress'
import type { PlanEvent, ReplanMode } from '~/composables/useApi'

const props = defineProps<{ event: PlanEvent; busy: string | null; progress: Bar | null }>()
const emit = defineEmits<{ back: []; go: [mode: ReplanMode | undefined] }>()

const full = ref(false)
const options = computed(() => [
  { full: false, label: 'В выбранное время', hint: `Начатое к ${props.event.time} не трогается` },
  { full: true, label: 'С начала дня', hint: 'Как будто событие было известно утром' },
])
</script>

<template>
  <Modal title="Пересчёт дня" :closable="!busy" @back="emit('back')">
    <p class="m-0">С какого момента пересчитать день?</p>
    <div class="flex flex-col gap-2">
      <label
        v-for="option in options"
        :key="option.label"
        class="flex cursor-pointer select-none items-center gap-3 rounded-xl border border-[#dde2e8] bg-panel px-3 py-2 transition-[border-color,box-shadow,background-color] duration-200 hover:bg-field/60 [&>input]:pointer-events-none [&>input]:absolute [&>input]:opacity-0"
        :class="{ 'border-accent shadow-[0_0_0_1px_#ffc800]': full === option.full }"
      >
        <input v-model="full" type="radio" :value="option.full" :disabled="!!busy">
        <CheckMark :checked="full === option.full" />
        <span class="flex flex-col gap-0.5">
          <span>{{ option.label }}</span>
          <span class="muted small">{{ option.hint }}</span>
        </span>
      </label>
    </div>
    <template #footer>
      <ProgressBar v-if="progress" :bar="progress" />
      <button class="btn primary wide" :disabled="!!busy" @click="emit('go', full ? 'full' : undefined)"><Icon name="check" /> Пересчитать</button>
      <button class="btn wide" :disabled="!!busy" @click="emit('back')">Отмена</button>
    </template>
  </Modal>
</template>
