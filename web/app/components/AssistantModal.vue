<script setup lang="ts">
// «Ассистент» (блок 42): диспетчер пишет фразу обычным текстом — «Соколов заболел с 13:00» — или
// задаёт вопрос по плану. Модель ничего не решает: событие и черновик заявки — только
// предложение, кнопка под ответом отправляет его в обычный путь («Пересчёт дня» или форма
// добавления заявки). Кнопка есть только у последнего ответа: под старым она предлагала бы
// действие над днём, который с тех пор мог уйти вперёд. Запрос делает страница — так работает
// блокировка кнопок; переписка тоже её, чтобы пережить закрытие окна.
// Окно — чат справа внизу, как на сайте Билайна (Modal `dock`): поле ввода многострочное и растёт
// по тексту, Enter отправляет, Shift+Enter — новая строка; пока ждём ответ, на его месте скелетон.
import { HOLD_MS, PHRASES } from '~/composables/phrases'
import type { Bar } from '~/composables/progress'
import type { AssistantAttachment, AssistantMessage, AssistantReply, Plan } from '~/composables/useApi'

const props = defineProps<{
  plan: Plan
  log: AssistantMessage[]
  // Файлы к ещё не отправленной фразе и полоса их загрузки (блок 60).
  pending: AssistantAttachment[]
  uploading: boolean
  progress: Bar | null
  busy: string | null
  error: string | null
}>()
const emit = defineEmits<{
  back: []
  send: [text: string]
  reset: []
  attach: [files: File[]]
  detach: [id: string]
  apply: [message: AssistantMessage]
  changes: [changes: NonNullable<AssistantMessage['changes']>]
}>()

const text = ref('')
const area = useTemplateRef<HTMLTextAreaElement>('area')
const end = useTemplateRef<HTMLElement>('end')

/** Примеры на настоящих именах и номерах плана: нажатие подставляет фразу в поле, а не отправляет. */
const examples = computed(() => {
  const engineer = props.plan.input.engineers[0]
  const request = props.plan.input.requests[0]
  return [
    engineer && `${engineer.name} недоступен`,
    engineer && `${engineer.name}: смена до 20:00`,
    engineer && `Что будет, если ${engineer.name} заболеет в 13:00?`,
    request && `Отмени заявку ${request.id}`,
    request && `Почему заявка ${request.id} назначена именно так?`,
    'Кто из инженеров свободен?',
    'Покажи план на 15:00',
  ].filter((example): example is string => !!example)
})

/** Высота поля — по тексту, но не выше шести строк: дальше внутри поля прокрутка. */
const MAX_HEIGHT = 120
function fit() {
  const field = area.value
  if (!field) return
  field.style.height = 'auto'
  field.style.height = `${Math.min(field.scrollHeight, MAX_HEIGHT)}px`
  field.style.overflowY = field.scrollHeight > MAX_HEIGHT ? 'auto' : 'hidden'
}

function fill(example: string) {
  text.value = example
  nextTick(() => {
    fit()
    area.value?.focus()
  })
}

// Что принимает сервер (`attachments.py`): проверяем до загрузки, чтобы не гонять файл зря.
const ACCEPT = ['.csv', '.json', '.txt']
const MAX_BYTES = 1024 * 1024
const MAX_FILES = 3
const picker = useTemplateRef<HTMLInputElement>('picker')
const problem = ref<string | null>(null)

/** Файлы из скрепки или перетаскивания: подходящие уходят на загрузку, иначе — строка почему. */
function take(list: FileList | null | undefined) {
  const files = [...(list ?? [])]
  if (picker.value) picker.value.value = ''
  if (!files.length || props.busy || props.uploading) return
  const odd = files.find((file) => !ACCEPT.some((suffix) => file.name.toLowerCase().endsWith(suffix)))
  const big = files.find((file) => file.size > MAX_BYTES)
  problem.value = odd
    ? `«${odd.name}»: ассистент читает только CSV, JSON и TXT`
    : big
      ? `«${big.name}» больше 1 МБ — большой набор загружают в «Новый датасет»`
      : props.pending.length + files.length > MAX_FILES
        ? `К одному сообщению — не больше ${MAX_FILES} файлов`
        : null
  if (!problem.value) emit('attach', files)
}

function send() {
  // Без текста, но с файлом — тоже вопрос: сервер требует фразу, и она по умолчанию про файл.
  const phrase = text.value.trim() || (props.pending.length ? 'Что в файле?' : '')
  if (!phrase || props.busy || props.uploading) return
  problem.value = null
  text.value = ''
  nextTick(fit)
  emit('send', phrase)
}

/** Что предлагает ответ: `null` — предлагать нечего (ответ на вопрос или просьба уточнить). */
function action(reply: AssistantReply | undefined): string | null {
  if (reply?.kind === 'event' || reply?.whatIf) return 'Применить'
  if (reply?.kind === 'reassign') return 'Переназначить'
  if (reply?.kind === 'add_request') return 'Открыть форму заявки'
  if (reply?.kind === 'add_engineer') return 'Открыть форму инженера'
  if (reply?.kind === 'edit_request') return 'Открыть карточку'
  if (reply?.kind === 'time') return reply.time ? `Перейти к ${reply.time}` : 'Вернуться к текущему времени'
  return null
}

/** Ждём ответ ассистента: последняя реплика — диспетчера, и на месте ответа скелетон. */
const waiting = computed(() => !!props.busy && props.log.at(-1)?.from === 'me')

// Подпись под скелетоном (блок 58): оценки времени у ответа нет, поэтому фразы идут по одной
// в HOLD_MS и стоят на последней, пока ответ не пришёл.
const thought = ref(0)
let thinking: ReturnType<typeof setInterval> | undefined
watch(
  waiting,
  (on) => {
    clearInterval(thinking)
    thought.value = 0
    if (on) thinking = setInterval(() => (thought.value = Math.min(thought.value + 1, PHRASES.assistant.length - 1)), HOLD_MS)
  },
  { immediate: true },
)
onBeforeUnmount(() => clearInterval(thinking))

/**
 * К концу переписки. Прокручивается само окно, а не последняя строка в поле зрения: липкий подвал
 * с полем ввода закрыл бы её. Лист окна — ближайший `section` (Modal).
 */
function toBottom(smooth = true) {
  const box = end.value?.closest('section')
  box?.scrollTo({ top: box.scrollHeight, behavior: smooth ? 'smooth' : 'instant' })
}

// Новая реплика и появление скелетона — вниз; ответ занимает место скелетона и снова прокручивает.
watch(
  () => [props.log.length, waiting.value],
  () => nextTick(() => toBottom()),
)
onMounted(() => {
  nextTick(() => toBottom(false))
  // На телефоне фокус открыл бы клавиатуру раньше, чем диспетчер увидел ответ.
  if (window.matchMedia('(min-width: 768px)').matches) area.value?.focus()
})
</script>

<template>
  <!-- Файл можно бросить на окно (блок 60): перетаскивание идёт тем же путём, что и скрепка. -->
  <Modal title="Ассистент" dock @back="emit('back')" @dragover.prevent @drop.prevent="take($event.dataTransfer?.files)">
    <!-- «Новый чат»: переписка живёт на странице, и без кнопки её не начать заново. Место держит и
         пустая переписка, чтобы заголовок не смещался. -->
    <template #action>
      <button class="round !bg-accent" :class="{ invisible: !log.length }" type="button" title="Новый чат" aria-label="Новый чат" :disabled="!!busy" @click="emit('reset')">
        <Icon name="edit" />
      </button>
    </template>
    <!-- Подсказки пустого чата — сверху: тело окна прижимает реплики ко дну, а `mb-auto` забирает
         свободное место под подсказками. -->
    <div v-if="!log.length" class="mb-auto flex flex-col gap-2.5">
      <p class="muted m-0">
        Напишите, что случилось в течение дня, или спросите про план. Ассистент предложит действие — применяете его вы.
      </p>
      <div class="flex flex-wrap gap-2">
        <button v-for="example in examples" :key="example" class="chip !h-auto !whitespace-normal py-2 text-left [overflow-wrap:anywhere]" @click="fill(example)">
          {{ example }}
        </button>
      </div>
    </div>
    <div v-for="(message, index) in log" :key="index" class="flex flex-col gap-1.5" :class="message.from === 'me' ? 'items-end' : 'items-start'">
      <div class="max-w-[88%] min-w-0 rounded-[18px] px-3.5 py-2 whitespace-pre-line [overflow-wrap:anywhere]" :class="message.from === 'me' ? 'bg-accent' : 'bg-field'">
        {{ message.text }}
      </div>
      <div v-if="message.files?.length" class="flex max-w-[88%] flex-wrap justify-end gap-1.5">
        <span v-for="file in message.files" :key="file.id" class="chip !h-auto py-1.5 [overflow-wrap:anywhere]" :title="file.summary">
          {{ file.name }}
        </span>
      </div>
      <!-- Кнопка — у последнего ответа и у уже применённых: применённая остаётся серой «Применено»
           и не даёт применить то же второй раз, а рядом — что в плане изменилось. -->
      <div v-if="action(message.reply) && (message.applied || index === log.length - 1)" class="flex flex-wrap items-center gap-2">
        <button v-if="message.applied" class="btn" type="button" disabled>
          <Icon name="check" /> Применено
        </button>
        <button v-else class="btn primary" type="button" :disabled="!!busy" @click="emit('apply', message)">
          <Icon name="check" /> {{ busy ?? action(message.reply) }}
        </button>
        <button v-if="message.changes" class="btn" type="button" @click="message.changes && emit('changes', message.changes)">
          Что изменилось
        </button>
      </div>
    </div>
    <!-- Скелетон ответа нейросети: три строки разной длины, пока ждём YandexGPT. -->
    <div v-if="waiting" class="flex w-[72%] animate-pulse flex-col gap-2 rounded-[18px] bg-field px-3.5 py-3.5" role="status" aria-label="Ассистент отвечает">
      <span class="h-3 w-full rounded-full bg-[#dde2e8]" />
      <span class="h-3 w-4/5 rounded-full bg-[#dde2e8]" />
      <span class="h-3 w-3/5 rounded-full bg-[#dde2e8]" />
    </div>
    <Transition v-if="waiting" name="appear" mode="out-in">
      <p :key="thought" class="muted small m-0" aria-live="off">{{ PHRASES.assistant[thought] }}</p>
    </Transition>
    <div ref="end" />
    <template #footer>
      <p v-if="problem || error" class="error small m-0">{{ problem ?? error }}</p>
      <ProgressBar v-if="progress" :bar="progress" />
      <!-- Файлы к фразе (блок 60): имя и что в нём нашёл сервер, × убирает файл до отправки. -->
      <div v-if="pending.length" class="flex flex-wrap gap-1.5">
        <span v-for="file in pending" :key="file.id" class="chip !h-auto gap-1.5 py-1.5 [overflow-wrap:anywhere]">
          <span>{{ file.name }} · {{ file.summary }}</span>
          <button class="cursor-pointer border-0 bg-transparent p-0 text-muted" type="button" :title="`Убрать ${file.name}`" :aria-label="`Убрать ${file.name}`" @click="emit('detach', file.id)">
            <Icon name="close" />
          </button>
        </span>
      </div>
      <!-- Поле и кнопка — как в чате поддержки на сайте: поле с радиусом 26 px, растёт по тексту,
           справа от него круглая кнопка 44 px, отступ между ними 4 px. -->
      <form class="flex items-end gap-1" @submit.prevent="send">
        <button
          class="pressable flex size-11 shrink-0 cursor-pointer items-center justify-center rounded-full border-0 bg-field p-0 text-text disabled:cursor-default disabled:text-[#afb7c4]"
          type="button"
          title="Приложить файл CSV, JSON или TXT"
          aria-label="Приложить файл"
          :disabled="!!busy || uploading"
          @click="picker?.click()"
        >
          <Icon name="attach" />
        </button>
        <input ref="picker" class="hidden" type="file" multiple :accept="ACCEPT.join(',')" @change="take(($event.target as HTMLInputElement).files)">
        <div class="flex min-w-0 flex-1 items-end rounded-[26px] bg-field py-3 px-4">
          <textarea
            ref="area"
            v-model="text"
            class="no-scrollbar block max-h-[120px] min-h-5 min-w-0 flex-1 resize-none overflow-y-hidden border-0 bg-transparent p-0 text-base/5 outline-0 placeholder:text-muted"
            rows="1"
            maxlength="500"
            placeholder="Напишите, чем помочь"
            aria-label="Ваш запрос"
            @input="fit"
            @keydown.enter.exact.prevent="send"
          />
        </div>
        <button
          class="pressable flex size-11 shrink-0 cursor-pointer items-center justify-center rounded-full border-0 p-0 disabled:cursor-default"
          :class="(text.trim() || pending.length) && !busy && !uploading ? 'bg-accent text-text' : 'bg-field text-[#afb7c4]'"
          type="submit"
          title="Отправить"
          aria-label="Отправить"
          :disabled="!!busy || uploading || !(text.trim() || pending.length)"
        >
          <Icon name="send" />
        </button>
      </form>
    </template>
  </Modal>
</template>
