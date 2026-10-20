<script setup lang="ts">
// «Информация о здании» (блок 78, кадры 1783, 1784, 1781): адрес; тип здания; где работа — этаж,
// подъезд, квартира; этажей в доме и квартир в подъезде с подсказками; подъезд к зданию; лифт.
// Правит копию: «Сохранить» отдаёт её полям заявки, «Назад» — без изменений. Ниже — тот же блок
// «Почему будет выполняться дольше?», что в карточке, по вводу в окне.
import type { RequestDraft } from '~/composables/entityForms'
import { ACCESS_NAMES, BUILDING_TYPE_NAMES, type RequestObject } from '~/composables/useApi'

const props = defineProps<{ draft: RequestDraft; speed?: number; invalid?: boolean }>()
const emit = defineEmits<{ back: []; save: [patch: Pick<RequestDraft, 'address' | 'lat' | 'lon' | 'object'>] }>()

const EMPTY: RequestObject = {
  buildingType: 'apartment',
  floor: null,
  floors: null,
  entrance: null,
  flatNumber: null,
  flats: null,
  elevator: false,
  access: 'normal',
}
const place = ref({ address: props.draft.address, lat: props.draft.lat, lon: props.draft.lon })
const item = ref<RequestObject>({ ...EMPTY, ...props.draft.object })
// У частного дома этаж, подъезд, квартиры и лифт не нужны: сложность их и не учитывает.
const house = computed(() => item.value.buildingType === 'private')

/** Пустое поле числа — «не знаем» (`null`), а не ноль. */
function whole(event: Event): number | null {
  const value = (event.target as HTMLInputElement).value.trim()
  return value === '' ? null : Math.trunc(Number(value))
}
const WHERE = [
  { key: 'floor', label: 'Этаж' },
  { key: 'entrance', label: 'Подъезд' },
  { key: 'flatNumber', label: 'Квартира' },
] as const
const HOUSE = [
  { key: 'floors', label: 'Этажей в доме', tip: 'Этажность дома: на верхних этажах без лифта работа идёт дольше' },
  { key: 'flats', label: 'Квартир в подъезде', tip: 'Чем больше квартир в подъезде, тем дольше искать нужный порт в щитке и тянуть кабель' },
] as const

const fields = computed(() => ({ ...props.draft, object: item.value }))

function save() {
  // Нетронутое окно у заявки без объекта объекта не заводит: иначе она считалась бы изменённой.
  const same = !props.draft.object && JSON.stringify(item.value) === JSON.stringify(EMPTY)
  emit('save', { ...place.value, object: same ? null : { ...item.value } })
}
</script>

<template>
  <Teleport to="#sheet-vars">
    <Modal title="Информация о здании" wide @back="emit('back')">
      <p class="muted m-0">Характеристики здания помогут точнее рассчитать время выезда и работ</p>
      <AddressField v-model="place" label="Адрес" :invalid="invalid" />
      <!-- Тип здания — выше этажа, подъезда и квартиры (правки 81.6). -->
      <SelectField v-model="item.buildingType" label="Тип здания" :options="BUILDING_TYPE_NAMES" />
      <div v-if="!house" class="grid grid-cols-3 gap-2">
        <label v-for="number in WHERE" :key="number.key" class="field">
          <span>{{ number.label }}</span>
          <input type="number" min="1" step="1" :value="item[number.key] ?? ''" placeholder="—" @input="item[number.key] = whole($event)">
        </label>
      </div>
      <div v-if="!house" class="grid grid-cols-2 gap-2">
        <label v-for="number in HOUSE" :key="number.key" class="field relative pr-11">
          <span>{{ number.label }}</span>
          <input type="number" min="1" step="1" :value="item[number.key] ?? ''" placeholder=" " @input="item[number.key] = whole($event)">
          <Tip class="absolute top-1/2 right-[21px] flex -translate-y-1/2 text-muted" :label="number.label">
            <Icon name="help" />
            <template #tip>{{ number.tip }}</template>
          </Tip>
        </label>
      </div>
      <SelectField v-model="item.access" label="Подъезд к зданию" :options="ACCESS_NAMES" />
      <label v-if="!house" class="option">
        <input v-model="item.elevator" type="checkbox">
        <CheckMark :checked="item.elevator" :size="28" />
        Есть лифт
      </label>
      <!-- Перед блоком — 14 px, а не 8 (кадры 1783, 1785). -->
      <DurationWhy class="mt-1.5" :fields="fields" :speed="speed" />
      <template #footer>
        <div class="grid grid-cols-2 gap-2">
          <button class="btn" @click="emit('back')"><Icon name="arrow-left" /> Назад</button>
          <button class="btn primary" @click="save"><Icon name="check" /> Сохранить</button>
        </div>
      </template>
    </Modal>
  </Teleport>
</template>
