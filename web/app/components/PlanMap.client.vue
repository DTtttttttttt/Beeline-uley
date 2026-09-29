<script setup lang="ts">
// Карта 2ГИС (docs/design.md, «Карта», кадр 1525): маршруты в цвет инженера, булавки заявок,
// офис, неназначенные. Компонент клиентский по имени файла: MapGL живёт только в браузере.
import { load } from '@2gis/mapgl'
import { Clusterer, type ClustererPointerEvent, type ClusterStyle, type ClusterTarget, type InputMarker } from '@2gis/mapgl-clusterer'
import { Ruler } from '@2gis/mapgl-ruler'
import { engineerColors, UNASSIGNED_COLOR, type Dataset, type Plan } from '~/composables/useApi'
import BUILDING from '~/assets/icons/building.svg?raw'
import CANCELED from '~/assets/icons/canceled.svg?raw'
import CLIPBOARD from '~/assets/icons/clipboard.svg?raw'

type MapGL = Awaited<ReturnType<typeof load>>
type MapInstance = InstanceType<MapGL['Map']>
type HtmlMarkerInstance = InstanceType<MapGL['HtmlMarker']>
type Drawn = { destroy: () => void }

const props = defineProps<{
  dataset: Dataset | null
  plan: Plan | null
  selected: string | null
  // Выбор из списка, карточки или гантта: карта приближается к заявке. `n` растёт на каждый выбор,
  // поэтому повторный выбор той же заявки приближает снова.
  focus: { id: string; n: number } | null
  // Сколько места поверх карты занимают шапка и панель (на мобильном — лист снизу): рамка набора
  // не должна уходить под них.
  inset: { top: number; left: number; bottom: number }
  mobile: boolean
}>()

const emit = defineEmits<{ select: [requestId: string] }>()

const NO_PLAN_COLOR = '#77849D'
// Кластеры (@2gis/mapgl-clusterer): круг булавки 42 px, при 40 px соседние кружки ещё задевали
// друг друга, поэтому 60 px (умолчание плагина).
// С зума 16 кластеров нет — соседние дома иначе не разлепить.
const CLUSTER_RADIUS_PX = 60
const CLUSTER_OFF_ZOOM = 16

/**
 * Булавка по макету (docs/design.md, «Карта»): круг 42 px с хвостиком, иконка 18 px по центру,
 * тень `0 2 10` цветом самой булавки на 25 %. Иконки — файлы, а не символы в подписи: шрифт
 * подписей MapGL не знает `✓` и `⚠` (блок 21). Хвостик 20×8 из макета стоит в `X 11, Y 39.4`
 * от круга, его остриё — точка адреса.
 *
 * Булавка — HTML-маркер, а не картинка: у картинки MapGL нет анимации, а наведение (110 % и
 * чуть темнее) должно меняться плавно. Выбранная (белая, 120 %) — отдельный маркер вне кластеров,
 * поэтому появляется сразу. Цвета задают CSS-переменные, переходы — стили в конце файла.
 */
const TIP = [20.8, 47.4]
const TAIL =
  'M9.79452 8L10.405 7.21034C12.0431 5.0917 14.0899 3.3236 16.4241 2.01092L20 0H0L3.21467 1.86216' +
  'C5.6477 3.27154 7.75621 5.17811 9.40261 7.45744L9.79452 8Z'

/** Иконки нарисованы одним цветом (белым или тёмным) — заливку берёт класс булавки. */
function glyph(icon: string): string {
  return icon.replace(/fill="(white|#28303F)"/g, 'class="pin-glyph"').replace('<svg ', '<svg x="12" y="12" ')
}

/**
 * Разметка булавки: `--c` — цвет, `--ch` — он же под курсором, `--cs` — тень. Больше одной
 * заявки (кластер или один адрес) — круг без хвостика и счётчик в правом верхнем углу: белый
 * кружок с цифрой цвета булавки, у белой булавки — тёмный с белой цифрой. Привязка у всех одна —
 * `TIP`: круг без хвостика стоит там же, где круг булавки, и кластеры не наезжают на соседей.
 */
function pinElement(kind: string, color: string, icon: string, count = 1): HTMLElement {
  const bundle = count > 1
  const height = bundle ? 42 : 50
  const element = document.createElement('div')
  element.className = `map-pin ${bundle ? 'bundle' : ''} ${kind}`
  element.style.setProperty('--c', color)
  element.style.setProperty('--ch', darker(color))
  element.style.setProperty('--cs', `${color}40`)
  element.innerHTML =
    `<svg width="42" height="${height}" viewBox="0 0 42 ${height}">` +
    '<circle class="pin-body" cx="21" cy="21" r="21"/>' +
    (bundle ? '' : `<path class="pin-body" transform="translate(11 39.4)" d="${TAIL}"/>`) +
    glyph(icon) +
    (bundle
      ? `<circle class="pin-badge" cx="37" cy="5" r="${count > 9 ? 9.5 : 8}"/>` +
        `<text class="pin-count" x="37" y="8.8" text-anchor="middle">${count}</text>`
      : '') +
    '</svg>'
  return element
}

/** Цвет чуть темнее — булавка под курсором. */
function darker(hex: string): string {
  const channel = (at: number) => Math.round(parseInt(hex.slice(at, at + 2), 16) * 0.9)
  return `rgb(${channel(1)},${channel(3)},${channel(5)})`
}

interface Group {
  ids: string[]
  coordinates: number[]
  color: string
  cross: boolean
  // Заявки адреса у разных бригад (неназначенная — тоже отдельная): булавка белая, как смешанный кластер.
  mixed: boolean
}

/** Разметка булавки группы; выбранная — белая и крупнее. */
function groupElement(group: Group, chosen: boolean): HTMLElement {
  return pinElement(
    `${group.cross ? 'cross' : ''} ${group.mixed ? 'white' : ''} ${chosen ? 'chosen' : ''}`,
    group.color,
    group.cross ? CANCELED : CLIPBOARD,
    group.ids.length,
  )
}


const container = useTemplateRef<HTMLElement>('container')
const failure = ref<string | null>(null)

let mapgl: MapGL | null = null
let map: MapInstance | null = null
let drawn: Drawn[] = []
// Линейка — штатный плагин 2ГИС: точки ставятся кликом, перетаскиваются и снимаются им самим.
let ruler: Ruler | null = null
// Булавки по адресу: ключ — координаты, округлённые до 5 знаков (docs/design.md).
let groups = new Map<string, Group>()
let chosenKey: string | null = null
// Выбранная булавка — отдельный маркер вне кластеров: выбор из списка всегда виден на карте.
let chosenMarker: HtmlMarkerInstance | null = null
let clusterer: Clusterer | null = null
let framed: string | null = null // набор, по которому уже подгоняли рамку
let alive = true
const measuring = ref(false)

onMounted(async () => {
  const key = useRuntimeConfig().public.dgisKey
  if (!key) {
    failure.value = 'Карта недоступна: не задан ключ 2ГИС (NUXT_PUBLIC_DGIS_KEY)'
    return
  }
  try {
    mapgl = await load()
  } catch (error) {
    failure.value = `Карта не загрузилась: ${error instanceof Error ? error.message : error}`
    return
  }
  // Пока грузился скрипт, компонент могли размонтировать: карту создавать уже некуда.
  if (!alive || !container.value) return

  const office = points().office
  map = new mapgl.Map(container.value, {
    key,
    center: office ? [office.lon, office.lat] : [37.62, 55.75],
    zoom: 9,
    zoomControl: false,
    lang: 'ru', // подписи карты и линейки: иначе язык браузера, и линейка пишет «Start»
  })
  clusterer = new Clusterer(map, {
    radius: CLUSTER_RADIUS_PX,
    disableClusteringAtZoom: CLUSTER_OFF_ZOOM,
    clusterStyle,
  })
  clusterer.on('click', clusterClick)
  draw()
})

function zoomBy(step: number) {
  if (!map) return
  map.setZoom(map.getZoom() + step, { duration: 200 })
}

function toggleMeasure() {
  measuring.value = !measuring.value
  ruler?.destroy()
  ruler = measuring.value && map ? new Ruler(map, { mode: 'polyline' }) : null
}

onBeforeUnmount(() => {
  alive = false
  clearTimeout(focusTimer)
  clear()
  ruler?.destroy()
  clusterer?.destroy()
  clusterer = null
  map?.destroy()
  map = null
})

// Полная перерисовка — только по смене данных; выбор перекладывает одни булавки (`highlight`),
// иначе каждый клик пересоздавал бы и линии маршрутов, и вкладка подвисала.
watch([() => props.dataset, () => props.plan], draw)
watch(() => props.selected, highlight)
watch(() => props.focus, (focus) => focus && zoomTo(focus.id))

/** Вход, по которому рисуем: у плана он свой — в нём есть заявка из события (PLAN 6.12). */
function points() {
  const input = props.plan?.input ?? props.dataset
  return { office: input?.office ?? null, requests: input?.requests ?? [], engineers: input?.engineers ?? [] }
}

function clear() {
  drawn.forEach((object) => object.destroy())
  drawn = []
  chosenMarker?.destroy()
  chosenMarker = null
  clusterer?.load([])
  groups = new Map()
  chosenKey = null
}

/** Повторный клик по той же булавке листает заявки адреса: иначе до второй не добраться. */
function pick(group: Group) {
  if (measuring.value) return
  const at = props.selected ? group.ids.indexOf(props.selected) : -1
  emit('select', group.ids[(at + 1) % group.ids.length]!)
}

/**
 * Булавки на карту: выбранная — своим маркером поверх соседних, остальные — в кластеризатор.
 * `load` пересоздаёт видимые маркеры кластеризатора, поэтому зовётся только по смене данных
 * или выбора, а не на каждый кадр.
 */
function place() {
  if (!map || !mapgl || !clusterer) return
  chosenMarker?.destroy()
  chosenMarker = null
  const chosen = chosenKey ? groups.get(chosenKey) : null
  if (chosen) {
    const element = groupElement(chosen, true)
    chosenMarker = new mapgl.HtmlMarker(map, { coordinates: chosen.coordinates, html: element, anchor: TIP, zIndex: 4, interactive: true, preventMapInteractions: false })
    element.addEventListener('click', () => pick(chosen))
  }
  const markers: InputMarker[] = []
  for (const [key, group] of groups) {
    if (key !== chosenKey) {
      markers.push({ type: 'html', coordinates: group.coordinates, html: groupElement(group, false), anchor: TIP, zIndex: 2, userData: key, preventMapInteractions: false })
    }
  }
  clusterer.load(markers)
}

/**
 * Кластер — булавка без хвостика со счётчиком заявок (а не адресов). Все заявки у одной
 * бригады — её цвет; у разных (неназначенная тоже отдельная) — белая с тёмным счётчиком.
 */
function clusterStyle(_count: number, target: ClusterTarget): ClusterStyle {
  const inside = target.data.map((marker) => groups.get(marker.userData as string)).filter((group) => !!group)
  const colors = new Set(inside.map((group) => group.color))
  const mixed = colors.size > 1 || inside.some((group) => group.mixed)
  const cross = inside.every((group) => group.cross)
  const requests = inside.reduce((sum, group) => sum + group.ids.length, 0)
  return {
    type: 'html',
    html: pinElement(
      `${mixed ? 'white' : ''} ${cross ? 'cross' : ''}`,
      [...colors][0] ?? NO_PLAN_COLOR,
      cross ? CANCELED : CLIPBOARD,
      requests,
    ),
    anchor: TIP,
    zIndex: 3,
    preventMapInteractions: false,
  }
}

/** Клик по кластеру приближает к его булавкам (не дальше зума, где кластеров нет); по булавке — выбор заявки. */
function clusterClick(event: ClustererPointerEvent) {
  if (measuring.value || !map || !clusterer) return
  const target = event.target
  if (target.type === 'marker') {
    const group = groups.get(target.data.userData as string)
    if (group) pick(group)
    return
  }
  const lons = target.data.map((marker) => marker.coordinates[0]!)
  const lats = target.data.map((marker) => marker.coordinates[1]!)
  map.fitBounds(
    { northEast: [Math.max(...lons), Math.max(...lats)], southWest: [Math.min(...lons), Math.min(...lats)] },
    { padding: padding(), maxZoom: CLUSTER_OFF_ZOOM },
  )
}

function keyOf(point: { lat: number; lon: number }): string {
  return `${point.lat.toFixed(5)},${point.lon.toFixed(5)}`
}

function draw() {
  if (!map || !mapgl) return
  clear()

  const { office, requests, engineers } = points()
  if (!office) return
  const plan = props.plan
  const color = engineerColors(engineers)

  // Как маршрут в 2ГИС: белая кайма, в центре цвет инженера.
  for (const route of plan?.routes ?? []) {
    if (route.geometry.length < 2) continue
    const coordinates = route.geometry // уже [lon, lat] — бэкенд перевернул в geometry.py
    drawn.push(new mapgl.Polyline(map, { coordinates, color: '#FFFFFF', width: 9, zIndex: 1 }))
    drawn.push(new mapgl.Polyline(map, {
      coordinates,
      color: color.get(route.engineerId) ?? NO_PLAN_COLOR,
      width: 5,
      zIndex: 2,
    }))
  }

  drawn.push(new mapgl.HtmlMarker(map, {
    coordinates: [office.lon, office.lat],
    html: pinElement('office', '#FFFFFF', BUILDING),
    anchor: TIP,
    zIndex: 3,
    interactive: false,
  }))

  // Заявки по одному адресу — одна булавка со счётчиком; цвет — первой назначенной заявки.
  const byKey = new Map<string, { ids: string[]; lat: number; lon: number; engineerId: string | null; crews: Set<string | null> }>()
  for (const request of requests) {
    const key = keyOf(request)
    const entry = byKey.get(key) ?? { ids: [], lat: request.lat, lon: request.lon, engineerId: null, crews: new Set() }
    const engineerId = plan?.assignments[request.id] ?? null
    entry.ids.push(request.id)
    entry.engineerId ??= engineerId
    entry.crews.add(engineerId)
    byKey.set(key, entry)
  }

  for (const [key, entry] of byKey) {
    const cross = !!plan && !entry.engineerId
    const mixed = !!plan && entry.crews.size > 1
    const group: Group = {
      ids: entry.ids,
      coordinates: [entry.lon, entry.lat],
      color: !plan ? NO_PLAN_COLOR : cross ? UNASSIGNED_COLOR : (color.get(entry.engineerId!) ?? NO_PLAN_COLOR),
      cross,
      mixed,
    }
    groups.set(key, group)
    if (props.selected && entry.ids.includes(props.selected)) chosenKey = key
  }
  place()

  fit(office, requests)
}

/** Выбор сменился: новая булавка выходит из кластеров выбранной, прежняя возвращается в них. */
function highlight() {
  const selected = props.selected
  const next = selected ? ([...groups].find(([, group]) => group.ids.includes(selected))?.[0] ?? null) : null
  if (next === chosenKey) return
  chosenKey = next
  place()
}

// Лист мобильной версии едет 200 мс, пока он не встал, отступы карты устарели.
const SHEET_SETTLE_MS = 250
// Половина стороны рамки вокруг булавки, градусы (~20 м): в рамку нулевого размера fitBounds не вписывается.
const FOCUS_BOX = 0.0002
let focusTimer: ReturnType<typeof setTimeout> | undefined

/**
 * Заявку выбрали в списке, карточке или гантте — карта приближается к её булавке, как к кластеру
 * (`fitBounds` с отступами панели, не дальше зума, где кластеров нет; ближе — не отдаляем).
 * Клик по самой булавке карту не двигает: в `focus` он не попадает.
 */
function zoomTo(requestId: string) {
  clearTimeout(focusTimer)
  focusTimer = setTimeout(() => {
    const group = [...groups.values()].find((entry) => entry.ids.includes(requestId))
    if (!alive || !map || !group) return
    const [lon = 0, lat = 0] = group.coordinates
    map.fitBounds(
      { northEast: [lon + FOCUS_BOX, lat + FOCUS_BOX], southWest: [lon - FOCUS_BOX, lat - FOCUS_BOX] },
      { padding: padding(), maxZoom: Math.max(map.getZoom(), CLUSTER_OFF_ZOOM) },
    )
  }, props.mobile ? SHEET_SETTLE_MS : 0)
}

/** Рамка по всем точкам — один раз на набор: иначе Кашира и Ступино остаются за экраном. */
function fit(office: { lat: number; lon: number }, requests: { lat: number; lon: number }[]) {
  const id = props.dataset?.id ?? props.plan?.datasetId ?? null
  if (!map || !id || id === framed) return
  framed = id

  const lons = [office.lon, ...requests.map((request) => request.lon)]
  const lats = [office.lat, ...requests.map((request) => request.lat)]
  map.fitBounds(
    {
      northEast: [Math.max(...lons), Math.max(...lats)],
      southWest: [Math.min(...lons), Math.min(...lats)],
    },
    { padding: padding() },
  )
}

/** Шапка и левая панель лежат поверх карты: точки под ними были бы не видны. */
function padding() {
  return { top: props.inset.top + 30, right: 70, bottom: props.inset.bottom + 40, left: props.inset.left + 40 }
}
</script>

<template>
  <div class="h-full min-h-[400px]">
    <p v-if="failure" class="absolute top-1/2 left-1/2 m-0 -translate-x-1/2 -translate-y-1/2 text-danger">{{ failure }}</p>
    <!-- isolate: z-index HTML-маркеров MapGL остаётся внутри карты, а не спорит с панелью и шапкой -->
    <div v-show="!failure" ref="container" class="isolate h-full min-h-[400px]" :class="{ 'measuring cursor-crosshair': measuring }" />
    <MapControls
      v-if="!failure"
      :measuring="measuring"
      :mobile="mobile"
      @measure="toggleMeasure"
      @zoom-in="zoomBy(1)"
      @zoom-out="zoomBy(-1)"
    />
  </div>
</template>

<!--
  Булавки создаются разметкой в скрипте, а не шаблоном, поэтому стили не scoped. Переходы —
  200 мс: масштаб от острия (оно остаётся в точке адреса), заливка, иконка, счётчик и тень.
-->
<style>
.map-pin {
  --fill: var(--c);
  --fill-hover: var(--ch);
  --glyph: #fff;
  --badge: #fff;
  --count: var(--c);
  --shadow: var(--cs);
  cursor: pointer;
  /* Щипок и двойной тап над булавкой — дело карты, а не страницы: `none` отдаёт их MapGL (см. `preventMapInteractions`). */
  touch-action: none;
  transform-origin: 20.8px 47.4px;
  transition: transform 200ms ease;
}

.map-pin svg {
  display: block;
  overflow: visible;
  filter: drop-shadow(0 2px 10px var(--shadow));
  transition: filter 200ms ease;
}

.map-pin .pin-body {
  fill: var(--fill);
  transition: fill 200ms ease;
}

.map-pin .pin-glyph {
  fill: var(--glyph);
  transition: fill 200ms ease;
}

.map-pin .pin-badge {
  fill: var(--badge);
  transition: fill 200ms ease;
}

.map-pin .pin-count {
  fill: var(--count);
  font: 11px "Beeline Sans", Arial, sans-serif;
  transition: fill 200ms ease;
}

.map-pin:hover {
  transform: scale(1.1);
}

.map-pin:hover .pin-body {
  fill: var(--fill-hover);
}

/* Кластер и несколько заявок по адресу — круг без хвостика: масштаб от центра. */
.map-pin.bundle {
  transform-origin: 21px 21px;
}

/* Белая: заявки разных бригад (кластер или адрес) и выбранная — тёмная иконка и счётчик. */
.map-pin.white,
.map-pin.chosen {
  --fill: #fff;
  --fill-hover: var(--color-field);
  --glyph: var(--color-text);
  --badge: var(--color-text);
  --count: #fff;
  --shadow: rgb(40 48 63 / 25%);
}

/* Выбранная — ещё и 120 %, под курсором тоже. */
.map-pin.chosen {
  transform: scale(1.2);
}

.map-pin.chosen.cross {
  --glyph: var(--color-danger);
}

/* Пока включена линейка, клик по булавке ставит точку, а не выбирает заявку. */
.measuring .map-pin {
  pointer-events: none;
}

.map-pin.office {
  --fill: #fff;
  --glyph: var(--color-text);
  --shadow: rgb(40 48 63 / 25%);
  cursor: default;
}
</style>
