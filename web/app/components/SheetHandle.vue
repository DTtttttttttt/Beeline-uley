<template>
  <!-- Ручка листа мобильной версии: полоска 60×4 в верхнем отступе листа, поле касания — вся
       полоса над содержимым. На широком экране её нет. -->
  <button
    class="hidden max-md:absolute max-md:inset-x-0 max-md:top-0 max-md:z-[4] max-md:block max-md:h-[18px] max-md:cursor-grab max-md:touch-none max-md:border-0 max-md:bg-transparent max-md:p-0 max-md:pt-1.5 max-md:before:mx-auto max-md:before:block max-md:before:h-1 max-md:before:w-[60px] max-md:before:rounded-sm max-md:before:bg-[#e3e7ec] max-md:before:content-['']"
    type="button"
    title="Свернуть или развернуть"
  />
</template>
