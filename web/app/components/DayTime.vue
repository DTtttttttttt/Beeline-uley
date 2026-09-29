<script setup lang="ts">
// Время дня (docs/design.md, кадр 1525) — машина времени (блок 33): на этот момент показываются план
// и статусы заявок, с ним же уходят действия. Пилюля с часами как в макете; когда время поставлено
// руками, внутри неё «сейчас» — вернуть его к часам. Стоит в шапке, в мобильной версии — над картой.
const time = defineModel<string>({ required: true })
defineProps<{ pinned: boolean; disabled: boolean }>()
const emit = defineEmits<{ now: [] }>()
</script>

<template>
  <label
    class="btn min-w-fit cursor-text [&_input]:w-[46px] [&_input]:border-0 [&_input]:bg-transparent [&_input]:p-0 [&_input]:font-[inherit] [&_input]:text-inherit [&_input]:outline-0 [&_input:disabled]:text-muted"
    title="Время дня: план и статусы на этот момент, с ним же уходят действия"
  >
    <Icon name="clock" />
    <TimeInput v-model="time" :disabled="disabled" />
    <button
      v-if="pinned && !disabled"
      class="small -mr-1.5 cursor-pointer rounded-full border-0 bg-panel px-2 py-0.5 text-muted"
      title="Вернуть время к часам"
      @click.prevent="emit('now')"
    >
      сейчас
    </button>
  </label>
</template>
