<script setup lang="ts">
// Модальное окно (docs/design.md, «Модальные окна»): поверх затемнённой карты, круглая «‹»,
// заголовок. Ответ сервера окно печатает у своей кнопки само — внизу, в слоте `footer`.
// `closable: false` — окно не уходит ни по «‹», ни по клику мимо: так живут «Новый датасет» и
// «Варианты плана», пока день не посчитан и вариант не выбран (блок 36).
// На телефоне окно — тот же лист, что и список: три положения, тянется из любого места
// (composables/sheet.ts).
import { useSheet, useSheetDrag } from '~/composables/sheet'

// `dock` — окно-чат справа внизу, рядом с круглой кнопкой (ассистент, блок 42): на широком экране
// оно не по центру, а у правого нижнего края, фон затемнён, как у остальных окон; на телефоне — тот же лист.
const props = withDefaults(defineProps<{ title: string; wide?: boolean; closable?: boolean; dock?: boolean }>(), {
  closable: true,
})
const emit = defineEmits<{ back: [] }>()
const back = () => props.closable && emit('back')

const { state, dragged } = useSheet()
const { startMouse, cycle } = useSheetDrag(useTemplateRef<HTMLElement>('sheet'))
</script>

<template>
  <div
    class="fixed inset-0 z-20 flex items-start justify-center bg-[rgb(40_48_63_/_25%)] px-4 pt-[100px] pb-4 max-md:bottom-[var(--tabs-h,64px)] max-md:items-end max-md:bg-transparent max-md:p-0"
    :class="{ 'md:!items-end md:!justify-end md:!pr-[68px] md:!pb-10': dock }"
    @click.self="back"
  >
    <section
      ref="sheet"
      class="no-scrollbar flex max-h-[calc(100vh-116px)] max-w-full flex-col overflow-y-auto rounded-[36px] bg-panel px-3.5 shadow-panel max-md:h-[var(--sheet-h)] max-md:max-h-none max-md:w-full max-md:rounded-b-none max-md:rounded-t-[36px] max-md:shadow-[0_-4px_24px_rgb(40_48_63_/_12%)] max-md:duration-200"
      :class="[
        dock ? 'w-[420px] md:h-[min(760px,calc(100vh-140px))]' : wide ? 'w-[500px]' : 'w-[400px]',
        dragged === null ? 'max-md:transition-[height]' : 'max-md:transition-none',
        { 'max-md:[&>footer]:hidden': state === 'collapsed' && dragged === null },
      ]"
      role="dialog"
      :aria-label="title"
    >
      <header class="fade-header flex items-center gap-2">
        <SheetHandle @pointerdown="startMouse" @click.stop="cycle" />
        <button v-if="closable" class="round" title="Назад" @click="back"><Icon name="back" /></button>
        <!-- Без «‹» строка заголовка держит её высоту (44 px): окно не прыгает, когда замок снимается. -->
        <h2 class="m-0 flex-1 text-center text-lg font-medium" :class="closable ? ($slots.action ? '' : 'mr-[52px]') : 'leading-11'">{{ title }}</h2>
        <!-- Действие справа (например, «Новый чат»): вместо пустого отступа, который уравновешивал «‹». -->
        <slot name="action" />
      </header>
      <!-- Окно — это и есть прокрутка: шапка и подвал липкие, содержимое уходит под их фэйд. -->
      <div class="flex flex-col gap-2.5" :class="{ 'pb-3.5': !$slots.footer, 'grow justify-end': dock }">
        <slot />
      </div>
      <footer v-if="$slots.footer" class="fade-footer flex flex-col gap-2">
        <slot name="footer" />
      </footer>
    </section>
  </div>
</template>
