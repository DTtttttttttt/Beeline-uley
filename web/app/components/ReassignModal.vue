<script setup lang="ts">
// «Переназначение заявки» (блок 80, кадр 1791) — как «Кто поедет на аварию?»: карточки с отметкой.
// Кандидаты — только те, кому заявку можно отдать: все четыре флага `true`; исполнитель первым,
// остальные по приросту пробега. У ближайшего по пробегу — «рекомендуется», у других — «+6,8 км».
// Строки: «Прибудет в 12:15» и что инженер делает на время дня. «Развернётся по дороге» и
// «Доделает и поедет» — только у аварии: обычное переназначение никого не разворачивает.
// Места в маршруте и снятия назначения нет (D): сервер ставит заявку в лучшее место.
import type { Bar } from '~/composables/progress'
import { clockMinutes, km, type Plan } from '~/composables/useApi'

const props = defineProps<{ plan: Plan; requestId: string; time: string; busy: string | null; error: string | null; progress: Bar | null }>()
const emit = defineEmits<{ back: []; assign: [engineerId: string] }>()

const current = computed(() => props.plan.assignments[props.requestId] ?? null)

/** Что инженер делает на время дня — по его маршруту и смене: едет, на заявке, на обеде, свободен. */
function doing(engineerId: string): string {
  const now = clockMinutes(props.time)
  const engineer = props.plan.input.engineers.find((item) => item.id === engineerId)
  const route = props.plan.routes.find((item) => item.engineerId === engineerId)
  const stops = route?.stops ?? []
  const working = stops.find((stop) => clockMinutes(stop.start) <= now && now < clockMinutes(stop.end))
  if (working) return `Работает на заявке №${working.requestId} до ${working.end}`
  const waiting = stops.find((stop) => clockMinutes(stop.arrival) <= now && now < clockMinutes(stop.start))
  if (waiting) return `На месте, заявка №${waiting.requestId} с ${waiting.start}`
  const driving = stops.find((stop) => clockMinutes(stop.departure) <= now && now < clockMinutes(stop.arrival))
  if (driving) return `Едет к заявке №${driving.requestId}`
  if (route?.lunch && clockMinutes(route.lunch.start) <= now && now < clockMinutes(route.lunch.end)) return `На обеде до ${route.lunch.end}`
  if (engineer?.unavailableFrom && clockMinutes(engineer.unavailableFrom) <= now) return `Недоступен с ${engineer.unavailableFrom}`
  if (engineer && (now < clockMinutes(engineer.shiftStart) || now >= clockMinutes(engineer.shiftEnd)))
    return `Смена ${engineer.shiftStart}–${engineer.shiftEnd}`
  return 'Свободен'
}

const rows = computed(() => {
  const engineers = new Map(props.plan.input.engineers.map((engineer) => [engineer.id, engineer]))
  const fit = (props.plan.explanations[props.requestId]?.candidates ?? [])
    .filter((item) => item.skillOk && item.transportOk && item.equipmentOk && item.timeOk)
    .sort(
      (a, b) =>
        Number(b.engineerId === current.value) - Number(a.engineerId === current.value) ||
        (a.extraKm ?? Infinity) - (b.extraKm ?? Infinity),
    )
  const best = fit.find((item) => item.engineerId !== current.value)?.engineerId ?? null
  return fit.map((item) => ({
    id: item.engineerId,
    name: engineers.get(item.engineerId)?.name ?? item.engineerId,
    badge:
      item.engineerId === current.value
        ? { text: 'назначен', accent: false }
        : item.engineerId === best
          ? { text: 'рекомендуется', accent: true }
          : item.extraKm === null
            ? null
            : { text: km(item.extraKm, true), accent: false },
    arrival: item.arrival ? `Прибудет в ${item.arrival}` : null,
    doing: doing(item.engineerId),
  }))
})

// По умолчанию — рекомендованный; пропал из списка (новая версия плана) — снова он или исполнитель.
const fallback = () => rows.value.find((row) => row.id !== current.value)?.id ?? current.value
const chosen = ref<string | null>(fallback())
watch(rows, (list) => !list.some((row) => row.id === chosen.value) && (chosen.value = fallback()))
</script>

<template>
  <Modal title="Переназначение заявки" wide @back="emit('back')">
    <label
      v-for="row in rows"
      :key="row.id"
      class="choice cursor-pointer select-none hover:bg-field/60 [&>input]:pointer-events-none [&>input]:absolute [&>input]:opacity-0"
      :class="{ '!border-accent': chosen === row.id }"
    >
      <input v-model="chosen" type="radio" :value="row.id">
      <CheckMark :size="24" :checked="chosen === row.id" />
      <span class="flex flex-1 flex-col gap-1">
        <span class="flex flex-wrap items-center gap-2 text-base leading-4">
          {{ row.name }}
          <span v-if="row.badge" class="badge" :class="{ '!bg-accent': row.badge.accent }">{{ row.badge.text }}</span>
        </span>
        <span v-if="row.arrival" class="text-sm leading-[14px] text-muted">{{ row.arrival }}</span>
        <span class="text-sm leading-[14px] text-muted">{{ row.doing }}</span>
      </span>
    </label>
    <p v-if="!rows.length" class="card muted m-0">
      Подходящих инженеров нет: ни у кого заявка не встаёт без нарушения ограничений.
    </p>
    <p v-else-if="rows.length === 1 && current" class="card muted m-0">
      Кроме текущего исполнителя, заявку никому не отдать.
    </p>
    <template #footer>
      <ProgressBar v-if="progress" :bar="progress" />
      <p v-if="error" class="error small">{{ error }}</p>
      <button
        class="btn primary wide"
        :disabled="!!busy || !chosen || chosen === current"
        @click="chosen && emit('assign', chosen)"
      >
        <Icon name="check" /> {{ busy ?? 'Выбрать' }}
      </button>
    </template>
  </Modal>
</template>
