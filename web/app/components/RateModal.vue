<script setup lang="ts">
// «Оценка выполнения заявки» (кадр 1754, раздел 1.3 задания): оценка диспетчера. У заявки одна
// общая оценка — есть уже, форма открывается с ней, и «Оценить» её правит. Уходит тому, кто закрыл
// работу (`Closure.engineerId`). Кнопка-ссылка в шапке — QR для клиента.
import { apiError, useApi, type RequestReview, type ReviewInput } from '~/composables/useApi'

// `draft` — ввод, с которым ушли в окно ссылки: вернувшись, форма его не теряет.
const props = defineProps<{ planId: string; requestId: string; draft: ReviewInput | null }>()
const emit = defineEmits<{ back: []; link: [link: string, draft: ReviewInput]; saved: [] }>()

const api = useApi()
const card = ref<RequestReview | null>(null)
const review = ref<ReviewInput>(props.draft ?? { stars: 0, tags: [], comment: '', clientName: '' })
const failure = ref('')
const saving = ref(false)

onMounted(() =>
  api.requestReview(props.planId, props.requestId).then(
    (answer) => {
      card.value = answer
      if (answer.review && !props.draft) {
        const { stars, tags, comment, clientName } = answer.review
        review.value = { stars, tags: [...tags], comment, clientName }
      }
    },
    (error) => (failure.value = apiError(error)),
  ),
)

async function save() {
  if (!review.value.stars || saving.value) return
  saving.value = true
  failure.value = ''
  try {
    const { comment, clientName } = review.value
    await api.saveReview(props.planId, props.requestId, { ...review.value, comment: comment.trim(), clientName: clientName.trim() })
    emit('saved')
  } catch (error) {
    failure.value = apiError(error)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Modal title="Оценка выполнения заявки" wide @back="emit('back')">
    <template v-if="card" #action>
      <button class="round" title="Ссылка для клиента" @click="emit('link', card.link, review)"><Icon name="link" /></button>
    </template>
    <template v-if="card">
      <!-- Кадр 1754: звёзды 44 через 6; над ними 11 px от шапки, под ними 31 до вопроса. -->
      <StarPicker v-model="review.stars" class="gap-1.5 pt-[11px] pb-[23px] [&_button]:flex [&_button]:p-0 [&_svg]:size-11" />
      <ReviewFields v-model="review" />
    </template>
    <div v-else-if="!failure" class="skeleton h-40 rounded-card" />
    <template #footer>
      <p v-if="failure" class="error small">{{ failure }}</p>
      <div class="grid grid-cols-2 gap-2">
        <button class="btn" @click="emit('back')"><Icon name="arrow-left" /> Назад</button>
        <button class="btn primary" :disabled="!card || !review.stars || saving" @click="save">
          <Icon name="check" /> {{ saving ? 'Сохраняем…' : 'Оценить' }}
        </button>
      </div>
    </template>
  </Modal>
</template>
