<script setup lang="ts">
// Метки, имя и комментарий оценки — общие у страницы клиента и окна диспетчера (блоки 70, 72).
import { REVIEW_TAGS, type ReviewInput, type ReviewTag } from '~/composables/useApi'

const review = defineModel<ReviewInput>({ required: true })

function toggle(tag: ReviewTag) {
  const tags = review.value.tags
  review.value = { ...review.value, tags: tags.includes(tag) ? tags.filter((item) => item !== tag) : [...tags, tag] }
}
</script>

<template>
  <!-- Кадры 1754 и 1779: вопрос 16 / 13, метки 32 px через 8, у полей значение со строкой `normal`. -->
  <div class="flex flex-col gap-2.5">
    <p class="m-0 leading-[13px]">Как была выполнена заявка?</p>
    <div class="flex flex-wrap gap-2">
      <button
        v-for="(name, tag) in REVIEW_TAGS"
        :key="tag"
        type="button"
        class="chip !min-h-8 !px-4"
        :class="{ 'bg-accent text-text': review.tags.includes(tag) }"
        :aria-pressed="review.tags.includes(tag)"
        @click="toggle(tag)"
      >
        {{ name }}
      </button>
    </div>
  </div>
  <label class="field leading-[normal] [&>:not(:first-child)]:!my-0 [&>input]:!h-[17px]">
    <span>Имя или псевдоним</span>
    <input v-model="review.clientName" maxlength="60" placeholder="Например, Мария" autocomplete="given-name" />
  </label>
  <label class="field leading-[normal] [&>:not(:first-child)]:!my-0">
    <span>Комментарий</span>
    <textarea v-model="review.comment" rows="2" maxlength="500" class="resize-none" placeholder="Что понравилось или что можно улучшить" />
  </label>
</template>
