<script setup lang="ts">
// «Почему по плану дольше норматива?» (блок 78, правки 81.6, кадры 1522, 1783, 1785): свёрнуто — «70 → 94 мин ·
// +24 мин», раскрыто — причины в минутах с пояснением серым и риск опоздания «было → стало».
// Формула — на сервере (`POST /api/requests/duration`), блок спрашивает после паузы во вводе; ответ
// на устаревший ввод отбрасывается по номеру попытки. Совпала длительность с нормативом — блока нет.
import { useApi, type DurationAnswer, type Equipment, type RequestObject } from '~/composables/useApi'

const props = defineProps<{
  fields: {
    serviceDurationMin: number
    documentsMin: number
    object: RequestObject | null
    gigabit: boolean
    repeatVisit: boolean
    cable: boolean
    drilling: boolean
    requiredEquipment: Equipment[]
  }
  // Скорость назначенного инженера (тысячные): без неё — норма.
  speed?: number
  // Риск опоздания по нормативу и с учётом сложности, проценты; у заявки вне плана его нет.
  risk?: { before: number; now: number } | null
}>()

const DEBOUNCE_MS = 400
const api = useApi()
const work = ref<DurationAnswer | null>(null)
let timer: ReturnType<typeof setTimeout> | undefined
let round = 0
onBeforeUnmount(() => clearTimeout(timer))

function ask() {
  clearTimeout(timer)
  const mine = ++round
  const { serviceDurationMin, documentsMin, object, gigabit, repeatVisit, cable, drilling, requiredEquipment } = props.fields
  if (!(serviceDurationMin > 0)) return (work.value = null)
  timer = setTimeout(async () => {
    try {
      const answer = await api.requestDuration({
        serviceDurationMin,
        documentsMin,
        object,
        gigabit,
        repeatVisit,
        cable,
        drilling,
        requiredEquipment,
        ...(props.speed ? { speed: props.speed } : {}),
      })
      if (mine === round) work.value = answer
    } catch {
      // Поле ещё не дописано (этаж 0, документы длиннее работы) — блока нет, форма скажет сама.
      if (mine === round) work.value = null
    }
  }, DEBOUNCE_MS)
}
watch(() => [JSON.stringify(props.fields), props.speed], ask, { immediate: true })

const signed = (minutes: number) => `${minutes < 0 ? '−' : '+'}${Math.abs(minutes)} мин`
const longer = computed(() => !work.value || work.value.minutes >= work.value.normMin)
</script>

<template>
  <details v-if="work && work.minutes !== work.normMin" class="card group">
    <summary class="flex cursor-pointer list-none items-center justify-between gap-2 [&::-webkit-details-marker]:hidden">
      <span class="flex flex-col gap-0.5">
        {{ longer ? 'Почему по плану дольше норматива?' : 'Почему по плану быстрее норматива?' }}
        <Dotted class="muted" :text="`${work.normMin} → ${work.minutes} мин · ${signed(work.minutes - work.normMin)}`" :size="3" />
      </span>
      <Icon name="chevron" class="-rotate-90 transition-transform duration-250 group-open:rotate-180" />
    </summary>
    <ul class="mt-2.5 mb-0 flex list-disc flex-col gap-1.5 pl-6">
      <li v-for="reason in work.reasons" :key="reason.text">
        {{ signed(reason.minutes) }} — {{ reason.text }}
        <span class="muted mt-0.5 block">{{ reason.hint }}</span>
      </li>
    </ul>
    <p v-if="risk && risk.before !== risk.now" class="muted mt-2.5 mb-0">
      {{ risk.now < risk.before ? 'Риск опоздания снижен благодаря учёту' : 'Риск опоздания с учётом' }} сложности заявки и скорости
      инженера<br><span class="text-text">{{ risk.before }}% → {{ risk.now }}%</span>
    </p>
  </details>
</template>
