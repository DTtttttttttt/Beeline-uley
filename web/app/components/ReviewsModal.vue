<script setup lang="ts">
// «Отзывы» об инженере (кадр 1753, блок 77): цифра и звёзды, изменение за месяц, полосы 5…1 и все
// отзывы, новые первыми. Отзыв — текст, метки, звёзды, оценка, имя клиента и дата, как на кадре.
import { apiError, decimal, plural, REVIEW_TAGS, useApi, type EngineerRating, type Review } from '~/composables/useApi'

const props = defineProps<{ engineerId: string; rating: EngineerRating | null }>()
const emit = defineEmits<{ back: [] }>()

const api = useApi()
const reviews = ref<Review[] | null>(null)
const failure = ref('')
onMounted(() =>
  api.engineerReviews(props.engineerId).then(
    (list) => (reviews.value = list),
    (error) => (failure.value = apiError(error)),
  ),
)

// «↗ +0,2 за месяц» / «↘ −0,1 за месяц»; без отзывов старше месяца сравнивать не с чем.
const month = computed(() => {
  const change = props.rating?.monthChange
  if (change == null || !Number(change.toFixed(1))) return null
  return change > 0
    ? { text: `+${decimal(change, 1)} за месяц`, icon: 'trend-up-line' }
    : { text: `−${decimal(-change, 1)} за месяц`, icon: 'trend-down-line' }
})

const bars = computed(() => {
  const counts = props.rating?.distribution ?? [0, 0, 0, 0, 0]
  const total = Math.max(1, ...counts)
  return counts.map((count, index) => ({
    stars: 5 - index,
    share: count / total,
    text: count ? plural(count, ['отзыв', 'отзыва', 'отзывов']) : 'нет отзывов',
  }))
})

const date = (at: string) => new Date(at).toLocaleDateString('ru', { day: 'numeric', month: 'long', year: 'numeric' })
</script>

<template>
  <Modal title="Отзывы" wide @back="emit('back')">
    <!-- Сводка (кадр 1753): «4,5» 44 / 38, подпись 12 / 13 и звёзды 24 под ней, тренд 20 через 8;
         полосы — строки 16 через 10, звёздочка 12, полоса 4. До первого отзыва — 14. -->
    <div class="card mb-1.5 flex flex-col gap-2.5">
      <div class="flex items-center gap-2.5">
        <span class="text-[44px] leading-[38px]">{{ rating ? decimal(rating.stars, 1) : '—' }}</span>
        <span class="flex flex-col">
          <span class="muted small">{{ rating ? plural(rating.count, ['оценка', 'оценки', 'оценок']) : 'нет оценок' }}</span>
          <Stars :value="rating?.stars ?? 0" />
        </span>
        <span v-if="month" class="ml-auto flex items-center gap-2 px-2"><Icon :name="month.icon" /> {{ month.text }}</span>
      </div>
      <div v-for="bar in bars" :key="bar.stars" class="grid grid-cols-[auto_1fr_84px] items-center gap-2.5 leading-4">
        <span class="flex items-center gap-1">{{ bar.stars }} <Icon name="star" class="text-[#c0c7d2] [&_svg]:size-3" /></span>
        <span class="h-1 overflow-hidden rounded-full bg-[#c0c7d2]"><span class="block h-full rounded-full bg-accent" :style="{ width: `${bar.share * 100}%` }" /></span>
        <span>{{ bar.text }}</span>
      </div>
    </div>

    <p v-if="failure" class="error small">{{ failure }}</p>
    <template v-else-if="reviews === null">
      <div v-for="index in 3" :key="index" class="skeleton h-24 rounded-card" />
    </template>
    <p v-else-if="!reviews.length" class="card muted text-center">Отзывов пока нет</p>
    <div v-for="review in reviews ?? []" :key="review.requestId" class="card flex flex-col gap-2.5">
      <p v-if="review.comment" class="m-0">{{ review.comment }}</p>
      <!-- Метки отзыва (кадр 1753, обновлён 19.10): чипы 32 px, фон чуть темнее карточки, через 6. -->
      <div v-if="review.tags.length" class="flex flex-wrap gap-x-1.5 gap-y-2">
        <span v-for="tag in review.tags" :key="tag" class="inline-flex min-h-8 items-center rounded-full bg-[#e6eaee] px-4">{{ REVIEW_TAGS[tag] }}</span>
      </div>
      <!-- Звёзды, оценка, серая точка 4 px и имя — через 8 (кадр 1753); дата справа. -->
      <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
        <Stars :value="review.stars" />
        <span>{{ review.stars }}</span>
        <template v-if="review.clientName">
          <span class="size-1 rounded-full bg-muted" aria-hidden="true" />
          <span>{{ review.clientName }}</span>
        </template>
        <span class="muted ml-auto">{{ date(review.at) }}</span>
      </div>
    </div>

    <template #footer>
      <button class="btn primary wide" @click="emit('back')"><Icon name="check" /> Ок</button>
    </template>
  </Modal>
</template>
