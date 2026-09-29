<script setup lang="ts">
// Поле времени `HH:MM` с клавиатуры — одно на весь интерфейс вместо `input type="time"`:
// без системной иконки часов и без разных у браузеров всплывающих выборщиков. Формат ставится
// сразу при наборе, и невалидное время ввести нельзя: «9» → «09», после «2» час не больше 23,
// «7» в минутах → «07». Наружу уходит только полное время; незаконченное при уходе с поля
// дополняется нулями («9» → «09:00»), пустое возвращает прежнее значение.
const model = defineModel<string>({ required: true })

defineProps<{ disabled?: boolean }>()

/** Цифры → «HH:MM» по мере набора. Лишние и недопустимые цифры отбрасываются. */
function format(raw: string): { text: string; hours: string; minutes: string } {
  let hours = ''
  let minutes = ''
  for (const digit of raw.replace(/\D/g, '')) {
    if (hours.length < 2) {
      if (!hours) hours = digit > '2' ? `0${digit}` : digit
      else if (!(hours === '2' && digit > '3')) hours += digit
    } else if (minutes.length < 2) {
      minutes += !minutes && digit > '5' ? `0${digit}` : digit
    }
  }
  // Двоеточие — только когда начались минуты: иначе Backspace упирался бы в него.
  return { text: minutes ? `${hours}:${minutes}` : hours, hours, minutes }
}

const text = ref(model.value)
// Пока поле в фокусе, набор не затирается: время дня в шапке меняется само раз в минуту (блок 33).
const field = useTemplateRef<HTMLInputElement>('field')
watch(model, (value) => {
  if (document.activeElement !== field.value) text.value = value
})

function input(event: Event) {
  const field = event.target as HTMLInputElement
  const next = format(field.value)
  text.value = next.text
  field.value = next.text // отброшенная цифра не должна мелькнуть в поле
  if (next.minutes.length === 2) model.value = next.text
}

function blur() {
  const { hours, minutes } = format(text.value)
  if (!hours) {
    text.value = model.value
    return
  }
  const whole = `${hours.padStart(2, '0')}:${minutes.padEnd(2, '0')}`
  model.value = whole
  text.value = whole
}
</script>

<template>
  <input
    ref="field"
    :value="text"
    type="text"
    inputmode="numeric"
    autocomplete="off"
    placeholder="чч:мм"
    maxlength="5"
    size="5"
    :disabled="disabled"
    @input="input"
    @blur="blur"
  >
</template>
