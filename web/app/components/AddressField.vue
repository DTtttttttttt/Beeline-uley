<script setup lang="ts">
// Поле адреса с подсказками 2ГИС (блок 34; вынесено из полей заявки в блоке 78 — оно живёт и в
// окне «Информация о здании»). Правка адреса сбрасывает прежнюю точку: иначе заявка с адресом B
// уехала бы в точку A. Подсказка с домом ставит точку сразу, и геокодер при сохранении не нужен.
// Запрос — по вводу, после паузы 800 мс и от трёх символов: в минуту у ключа их всего пять.
import { useApi, type AddressSuggestion } from '~/composables/useApi'

const place = defineModel<{ address: string; lat: number | null; lon: number | null }>({ required: true })
const props = defineProps<{ label: string; disabled?: boolean; invalid?: boolean }>()

const SUGGEST_DEBOUNCE_MS = 800
const api = useApi()
const hints = ref<AddressSuggestion[]>([])
const active = ref(-1)
let timer: ReturnType<typeof setTimeout> | undefined
// Номер попытки: ушли из поля или нажали Esc — ответ, который ещё летит, уже не показываем.
let round = 0
onBeforeUnmount(close)

function close() {
  clearTimeout(timer)
  round += 1
  hints.value = []
}

function onType() {
  place.value.lat = null
  place.value.lon = null
  close()
  const query = place.value.address.trim()
  if (props.disabled || query.length < 3) return
  const mine = round
  timer = setTimeout(async () => {
    const found = await api.suggest(query)
    // Пока ждали, адрес дописали или ушли из поля — ответ не про них.
    if (mine !== round || place.value.address.trim() !== query) return
    hints.value = found
    active.value = -1
  }, SUGGEST_DEBOUNCE_MS)
}

function pick(hint: AddressSuggestion) {
  close()
  Object.assign(place.value, { address: hint.address, lat: hint.lat, lon: hint.lon })
}

function onKey(event: KeyboardEvent) {
  if (!hints.value.length) return
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    const count = hints.value.length
    if (active.value < 0) active.value = event.key === 'ArrowDown' ? 0 : count - 1
    else active.value = (active.value + (event.key === 'ArrowDown' ? 1 : -1) + count) % count
  } else if (event.key === 'Enter' && hints.value[active.value]) {
    event.preventDefault()
    pick(hints.value[active.value]!)
  } else if (event.key === 'Escape') {
    event.stopPropagation()
    close()
  }
}
</script>

<template>
  <!-- Подсказки — слоем поверх полей ниже, а не строками между ними (правки 81.6). -->
  <div class="relative">
    <label class="field" :class="{ invalid }">
      <span>{{ label }}</span>
      <input
        v-model="place.address"
        type="text"
        :disabled="disabled"
        placeholder="Москва, улица, дом"
        autocomplete="off"
        @input="onType"
        @keydown="onKey"
        @blur="close"
      >
    </label>
    <!-- mousedown.prevent: иначе поле теряет фокус раньше клика, и список исчезает под рукой -->
    <div v-if="hints.length" class="dropdown absolute inset-x-0 top-[calc(100%+4px)] z-30">
      <button
        v-for="(hint, index) in hints"
        :key="index"
        type="button"
        class="dropdown-item"
        :class="{ 'bg-text/5': index === active }"
        @mousedown.prevent="pick(hint)"
      >
        {{ hint.address }}
      </button>
    </div>
  </div>
</template>
