<script setup lang="ts">
defineProps<{ measuring?: boolean; mobile?: boolean }>()

const emit = defineEmits<{
  measure: []
  zoomIn: []
  zoomOut: []
}>()
</script>

<template>
  <div
    class="absolute right-4 z-10 flex flex-col gap-2.5"
    :class="mobile ? 'top-5' : 'top-1/2 -translate-y-1/2'"
    aria-label="Управление картой"
  >
    <button
      class="pressable flex size-[42px] cursor-pointer items-center justify-center rounded-full border-0 text-text shadow-panel"
      :class="measuring ? 'bg-accent' : 'bg-white'"
      type="button"
      :aria-pressed="measuring"
      title="Измерить расстояние"
      @click="emit('measure')"
    >
      <Icon name="map-measure" class="[&_svg]:size-[18px]" />
    </button>
    <button
      class="pressable flex size-[42px] cursor-pointer items-center justify-center rounded-full border-0 bg-white text-text shadow-panel"
      type="button"
      title="Приблизить"
      @click="emit('zoomIn')"
    >
      <Icon name="map-zoom-in" class="[&_svg]:size-[18px]" />
    </button>
    <button
      class="pressable flex size-[42px] cursor-pointer items-center justify-center rounded-full border-0 bg-white text-text shadow-panel"
      type="button"
      title="Отдалить"
      @click="emit('zoomOut')"
    >
      <Icon name="map-zoom-out" class="[&_svg]:size-[18px]" />
    </button>
  </div>
</template>
