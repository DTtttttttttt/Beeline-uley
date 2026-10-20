<script setup lang="ts">
// «Кто поедет на аварию?» (блоки 74 и 77, кадр 1788): отдельное окно перед «Пересчётом дня». Кандидаты
// — из предпросмотра события, по времени прибытия: что делает сейчас и что будет. Рекомендованный
// отмечен и выбран сразу; «Выбрать» с другим инженером пересчитывает варианты с ним. Пока считается —
// заготовки карточек и полоса над кнопкой.
import type { Bar } from '~/composables/progress'
import type { EmergencyCandidate, Plan, Preview } from '~/composables/useApi'

const props = defineProps<{
  plan: Plan
  preview: Preview | null
  busy: string | null
  progress: Bar | null
  error: string | null
}>()
const emit = defineEmits<{ back: []; choose: [engineerId: string | null] }>()

const names = computed(() => new Map(props.plan.input.engineers.map((engineer) => [engineer.id, engineer.name])))
const crews = computed(() => props.preview?.emergencyCandidates ?? [])

const chosen = ref<string | null>(null)
watch(crews, (list) => (chosen.value = (list.find((crew) => crew.recommended) ?? list[0])?.engineerId ?? null), { immediate: true })

function doing(crew: EmergencyCandidate): string {
  if (crew.now === 'en_route') return `Едет к заявке №${crew.requestId}`
  if (crew.now === 'on_site') return `Работает на заявке до ${crew.until}`
  return 'Свободен'
}
const NEXT: Record<EmergencyCandidate['action'], string> = {
  divert: 'Развернётся по дороге',
  finish: 'Доделает и поедет',
  go: 'Поедет сразу',
}
</script>

<template>
  <Modal title="Кто поедет на аварию?" wide :closable="!busy" @back="emit('back')">
    <template v-if="!preview">
      <div v-for="row in 3" :key="row" class="choice" aria-hidden="true">
        <span class="skeleton size-[26px] shrink-0 rounded-full" />
        <span class="flex flex-1 flex-col gap-1.5 py-0.5">
          <span class="skeleton h-3.5 w-2/5 rounded-full" />
          <span class="skeleton h-3 w-3/5 rounded-full" />
          <span class="skeleton h-3 w-3/4 rounded-full" />
        </span>
      </div>
    </template>
    <p v-else-if="!crews.length" class="card muted m-0">
      Нет инженеров, которые могут выполнить аварию с учётом навыков, оборудования, дороги и ограничений смены.
      Авария останется неназначенной с объяснением причины.
    </p>
    <label
      v-for="crew in crews"
      :key="crew.engineerId"
      class="choice cursor-pointer select-none hover:bg-field/60 [&>input]:pointer-events-none [&>input]:absolute [&>input]:opacity-0"
      :class="{ '!border-accent': chosen === crew.engineerId }"
    >
      <input v-model="chosen" type="radio" :value="crew.engineerId" :disabled="!!busy">
      <CheckMark :size="24" :checked="chosen === crew.engineerId" />
      <span class="flex flex-1 flex-col gap-1">
        <span class="flex flex-wrap items-center gap-2 text-base leading-4">
          {{ names.get(crew.engineerId) ?? crew.engineerId }}
          <span v-if="crew.recommended" class="badge !bg-accent">рекомендуется</span>
        </span>
        <span class="text-sm leading-[14px] text-muted">Прибудет на место аварии в {{ crew.arrival }}</span>
        <span class="text-sm leading-[14px] text-muted">{{ doing(crew) }} · {{ NEXT[crew.action] }}</span>
      </span>
    </label>

    <template #footer>
      <ProgressBar v-if="progress" :bar="progress" />
      <p v-if="error" class="error">{{ error }}</p>
      <button class="btn primary wide" :disabled="!!busy || !preview" @click="emit('choose', chosen)">
        <Icon name="check" /> Выбрать
      </button>
    </template>
  </Modal>
</template>
