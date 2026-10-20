<script setup lang="ts">
// Страница клиента (блоки 70 и 77, кадры 1779 и 1780): оценка выполненной заявки по ссылке из QR.
// Отдельная простая страница — без карты и панелей. Ссылка подписана сервером, по ней он знает
// заявку и исполнителя; у заявки одна общая оценка, поэтому уже оценённая форма не открывается,
// а оценка, появившаяся, пока клиент заполнял форму, не перезаписывается (409).
import { apiError, useApi, type RateForm, type ReviewInput } from '~/composables/useApi'

useHead({ title: 'Оценка выполнения заявки — билайн' })

const token = String(useRoute().params.token)
const api = useApi()

// server: false — как и остальные запросы: адрес API — адрес хоста, а не сети compose.
const { data: form, status } = await useFetch<RateForm>(() => `${useRuntimeConfig().public.apiBase}/api/rate/${token}`, {
  server: false,
})

const review = ref<ReviewInput>({ stars: 0, tags: [], comment: '', clientName: '' })
const sending = ref(false)
const sent = ref(false)
const failure = ref('')

async function send() {
  if (!review.value.stars || sending.value) return
  sending.value = true
  failure.value = ''
  try {
    const { comment, clientName } = review.value
    await api.rate(token, { ...review.value, comment: comment.trim(), clientName: clientName.trim() })
    sent.value = true
  } catch (error) {
    failure.value = apiError(error)
  } finally {
    sending.value = false
  }
}
</script>

<template>
  <main class="flex min-h-dvh flex-col bg-panel px-4 pt-6 pb-10 text-text md:px-5 md:pt-5">
    <Icon name="logo" class="self-start" />
    <div class="mx-auto flex w-full max-w-[736px] grow flex-col items-center gap-[30px] pt-10 text-center md:pt-[144px]" :class="{ 'justify-center !gap-5 md:!pt-0': !form || form.rated || sent }">
      <template v-if="status === 'pending' || status === 'idle'">
        <div class="skeleton h-9 w-3/4 rounded-card" />
        <div class="skeleton h-12 w-2/3 rounded-card" />
      </template>

      <template v-else-if="status === 'error' || !form">
        <h1 class="m-0 text-2xl font-medium md:text-[40px] md:leading-10">Не удалось открыть форму оценки</h1>
        <p class="m-0 text-muted">Ссылка недействительна или устарела. Попробуйте открыть её ещё раз.</p>
      </template>

      <template v-else-if="sent">
        <h1 class="m-0 text-2xl font-medium md:text-[40px] md:leading-10">Спасибо за оценку!</h1>
        <p class="m-0 text-muted">Ваш отзыв о работе инженера сохранён</p>
      </template>

      <template v-else-if="form.rated">
        <h1 class="m-0 text-2xl font-medium md:text-[40px] md:leading-10">Вы уже оценили, спасибо</h1>
        <p class="m-0 text-muted">Отзыв о работе инженера сохранён</p>
      </template>

      <!-- Кадр 1779: заголовок в 208 px от верха (20 + логотип 44 + 144), звёзды 44 через 6, между
           блоками 30; «Оценить» — после формы. -->
      <template v-else>
        <h1 class="m-0 text-2xl font-medium md:text-[40px] md:leading-10">Оцените качество выполнения заявки</h1>
        <StarPicker v-model="review.stars" class="gap-1.5 [&_button]:flex [&_button]:p-0 [&_svg]:size-11" />
        <form class="flex w-full flex-col gap-3.5 text-left" @submit.prevent="send">
          <ReviewFields v-model="review" />
          <p v-if="failure" class="error small m-0">{{ failure }}</p>
          <button class="btn primary wide mt-4" :disabled="sending || !review.stars">
            <Icon name="check" /> {{ sending ? 'Отправляем…' : 'Оценить' }}
          </button>
        </form>
      </template>
    </div>
  </main>
</template>
