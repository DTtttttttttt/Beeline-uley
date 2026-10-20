<script setup lang="ts">
// Карта 2ГИС (docs/design.md, «Карта», кадр 1525): маршруты в цвет инженера, булавки заявок,
// офис, неназначенные. Компонент клиентский по имени файла: MapGL живёт только в браузере.
import { load } from '@2gis/mapgl'
import { Clusterer, type ClustererPointerEvent, type ClusterStyle, type ClusterTarget, type InputMarker } from '@2gis/mapgl-clusterer'
import { Ruler } from '@2gis/mapgl-ruler'
import { engineerColors, UNASSIGNED_COLOR, type Dataset, type Plan } from '~/composables/useApi'
import { between, lunchShare, PLAY_MOMENT, positionAt, workShare, type Doing, type Track } from '~/composables/playback'
import BUILDING from '~/assets/icons/building.svg?raw'
import HOME from '~/assets/icons/home.svg?raw'
// Иконки маркера инженера — из Figma (кадр 1787, «Frame 1553 / 1557 / 1558»).
import CANCELED from '~/assets/icons/canceled.svg?raw'
import CLIPBOARD from '~/assets/icons/clipboard.svg?raw'
import ALARM from '~/assets/icons/alarm.svg?raw'
import DONE from '~/assets/icons/pin-done.svg?raw'
import LUNCH from '~/assets/icons/lunch.svg?raw'

type MapGL = Awaited<ReturnType<typeof load>>
type MapInstance = InstanceType<MapGL['Map']>
type HtmlMarkerInstance = InstanceType<MapGL['HtmlMarker']>
type Drawn = { destroy: () => void }

const props = defineProps<{
  dataset: Dataset | null
  plan: Plan | null
  selected: string | null
  // Выбор из списка, карточки или гантта: карта приближается к заявке, а у инженера — вписывает его
  // маршрут. `n` растёт на каждый выбор, поэтому повторный выбор того же приближает снова.
  focus: { id: string; n: number; engineer?: boolean } | null
  // Чей маршрут выделен: инженер под курсором в списке или с открытой карточкой.
  highlighted: string | null
  // Заявки, чьи булавки мигают, пока идёт пересчёт.
  pulsing: string[]
  // Заявка под курсором в списке: её булавка крупнее, как выбранная (правки 81.4).
  hoveredRequest: string | null
  // Сколько места поверх карты занимают шапка и панель (на мобильном — лист снизу): рамка набора
  // не должна уходить под них.
  inset: { top: number; left: number; bottom: number }
  // Сколько раз загружали набор (правки 82): у демо-наборов id постоянные, и рамка по id не
  // подгонялась бы при повторной загрузке того же набора.
  loads: number
  mobile: boolean
  // Проигрывание дня (блок 63): участки маршрутов показанного плана — инженеры едут по ним; `null` —
  // полоса проигрывания закрыта, маркеров инженеров нет. Сам момент приходит через `PLAY_MOMENT`:
  // он меняется каждый кадр, и пропом перерисовывал бы страницу.
  crew: Track[] | null
}>()

const moment = inject(PLAY_MOMENT, shallowRef(null))

const emit = defineEmits<{ select: [requestId: string]; engineer: [engineerId: string] }>()

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

/** Иконки нарисованы одним цветом (белым, тёмным или `currentColor`) — заливку берёт класс булавки. */
function glyph(icon: string): string {
  return icon
    .replace(/fill="(white|#28303F|currentColor)"/g, 'class="pin-glyph"')
    .replace('<svg ', '<svg x="12" y="12" ')
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
    // Круговой прогресс идущей работы и обеда (кадр 1787): дорожка по всему кругу — белый 20 %, по
    // ней белая дуга от верха по часовой. Пока работы нет, кольца нет совсем (правки 82): дуга
    // нулевой длины с круглым концом давала бы точку.
    (bundle
      ? ''
      : '<circle class="pin-track" cx="21" cy="21" r="18"/>' +
        '<circle class="pin-ring" cx="21" cy="21" r="18" pathLength="100" transform="rotate(-90 21 21)" stroke-dasharray="0 100"/>') +
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
  crews: Set<string | null> // чьи заявки по адресу; `null` — неназначенная
  // Значок одной заявки (кадр 1787): ✓ выполнена, «!» авария (блоки 74, 77), иначе планшет.
  icon: string
}

/** Разметка булавки группы; выбранная — белая и крупнее. */
function groupElement(group: Group, chosen: boolean): HTMLElement {
  return mark(
    pinElement(
      `${group.cross ? 'cross' : ''} ${group.mixed ? 'white' : ''} ${chosen ? 'chosen' : ''}`,
      group.color,
      group.cross ? CANCELED : group.icon,
      group.ids.length,
    ),
    [group],
  )
}

/**
 * Булавки, кроме офиса, помнят свои группы: наведение на инженера и пересчёт меняют им классы
 * на месте (`restyle`), а не пересоздают кластеры. Кластер, созданный во время наведения (зум),
 * получает классы сразу.
 */
const marks = new WeakMap<HTMLElement, Group[]>()
let pulsing = new Set<string>()
function mark(element: HTMLElement, inside: Group[]): HTMLElement {
  marks.set(element, inside)
  element.classList.toggle('faded', !!lit && !inside.some((group) => group.crews.has(lit)))
  element.classList.toggle('pulse', inside.some((group) => group.ids.some((id) => pulsing.has(id))))
  element.classList.toggle('lifted', !!props.hoveredRequest && inside.some((group) => group.ids.includes(props.hoveredRequest!)))
  return element
}

/** Доля дуги прогресса булавки: `null` — работы сейчас нет, кольцо скрыто. */
function ring(element: HTMLElement, share: number | null) {
  element.classList.toggle('ringing', share !== null)
  const length = share === null ? 0 : Math.min(100, Math.max(0, share * 100))
  element.querySelector('.pin-ring')?.setAttribute('stroke-dasharray', `${length} 100`)
}

/**
 * Булавки адресов берутся из `pins`, а не из страницы: булавку внутри кластера кластеризатор
 * держит вне DOM и при зуме вставляет тот же элемент — с прежними классами. Кластеры — со страницы:
 * их кластеризатор пересоздаёт сам.
 */
function restyle() {
  const shown = container.value?.querySelectorAll<HTMLElement>('.map-pin') ?? []
  for (const element of new Set([...pins, ...shown])) {
    const inside = marks.get(element)
    if (inside) mark(element, inside)
    else if (element.dataset.engineer) owned(element)
  }
}

/** Дом и обед — булавки одного инженера: при выделенном чужом маршруте гаснут, как его заявки. */
function owned(element: HTMLElement): HTMLElement {
  element.classList.toggle('faded', !!lit && element.dataset.engineer !== lit)
  return element
}


const container = useTemplateRef<HTMLElement>('container')
const failure = ref<string | null>(null)

let mapgl: MapGL | null = null
let map: MapInstance | null = null
let drawn: Drawn[] = []
// Линии маршрутов по инженерам: пересоздаётся только та, у которой сменился вид (`key`).
let lines = new Map<string, { key: string; objects: Drawn[] }>()
// Чей маршрут выделен — `highlighted`, если у него есть заявки в плане.
let lit: string | null = null
// Планы, которые карта уже показывала: перемотка времени и возврат к ним линии заново не чертит.
const seen = new Set<string>()
// Прорисовка нового плана: `tick` — сколько шагов прошло.
let growth: { started: number; tick: number; frame: number } | null = null
// Линейка — штатный плагин 2ГИС: точки ставятся кликом, перетаскиваются и снимаются им самим.
let ruler: Ruler | null = null
// Булавки по адресу: ключ — координаты, округлённые до 5 знаков (docs/design.md).
let groups = new Map<string, Group>()
let chosenKey: string | null = null
// Разметка булавок адресов из последнего `place`, и в кластерах тоже.
let pins: HTMLElement[] = []
// Булавки одной заявки и обеда (`lunch:<инженер>`) по id: им рисуется круговой прогресс.
let ringed = new Map<string, HTMLElement>()
// У какой заявки обедает инженер: пока идёт обед, его булавка заменяет булавку этой заявки.
let lunchAt = new Map<string, string>()
// Что уже нарисовано (правки 82): новая версия дня — часы закрывают работы поминутно — меняет на
// карте только изменившееся. Иначе все булавки и линии пересоздавались, и карта мигала.
let staticLook = '' // офис, дома и обеды
let pinLooks = new Map<string, string>() // булавка адреса без значка: цвет, заявки, исполнители
let pinViews = new Map<string, { icon: string; elements: HTMLElement[] }>() // значок и разметка булавки
// Выбранная булавка — отдельный маркер вне кластеров: выбор из списка всегда виден на карте.
let chosenMarker: HtmlMarkerInstance | null = null
let clusterer: Clusterer | null = null
let framed: string | null = null // набор, по которому уже подгоняли рамку
// Маркеры инженеров при проигрывании: участки маршрутов считаются раз на план, маркер двигается
// `setCoordinates`, а не пересоздаётся.
let crewMarkers: { track: Track; marker: HtmlMarkerInstance; element: HTMLElement }[] = []
let moveTimer: ReturnType<typeof setTimeout> | undefined
let movedAt = 0
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
  placeCrew()
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
  clearCrew()
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
watch(() => props.hoveredRequest, restyle)
watch(() => props.focus, (focus) => focus && zoomTo(focus))
watch(() => props.highlighted, () => {
  lit = litEngineer()
  drawLines()
  restyle()
  crewMarkers.forEach(({ track, element }) => element.classList.toggle('faded', !!lit && track.engineer.id !== lit))
})
watch(() => props.crew, placeCrew)
watch(moment, schedule)
watch(() => props.pulsing, (ids) => {
  pulsing = new Set(ids)
  restyle()
}, { immediate: true })

/** Вход, по которому рисуем: у плана он свой — в нём есть заявка из события (PLAN 6.12). */
function points() {
  const input = props.plan?.input ?? props.dataset
  return { office: input?.office ?? null, requests: input?.requests ?? [], engineers: input?.engineers ?? [] }
}

function clear() {
  stopGrowth()
  ringed = new Map()
  drawn.forEach((object) => object.destroy())
  drawn = []
  lines.forEach((line) => line.objects.forEach((object) => object.destroy()))
  lines = new Map()
  chosenMarker?.destroy()
  chosenMarker = null
  clusterer?.load([])
  groups = new Map()
  chosenKey = null
  staticLook = ''
  pinLooks = new Map()
  pinViews = new Map()
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
  pins = []
  for (const id of [...ringed.keys()]) if (!id.startsWith('lunch:')) ringed.delete(id)
  // Неизменившаяся булавка уходит в кластеризатор прежним элементом (правки 82): после смены
  // назначений у части заявок остальные не мигают.
  const before = { views: pinViews, looks: pinLooks }
  const kept = (key: string, group: Group) =>
    before.looks.get(key) === pinLook(group) && before.views.get(key)?.icon === group.icon
      ? before.views.get(key)!.elements.find((element) => !element.classList.contains('chosen'))
      : undefined
  pinViews = new Map([...groups].map(([key, group]) => [key, { icon: group.icon, elements: [] as HTMLElement[] }]))
  pinLooks = new Map([...groups].map(([key, group]) => [key, pinLook(group)]))
  const chosen = chosenKey ? groups.get(chosenKey) : null
  if (chosen) {
    const element = groupElement(chosen, true)
    pinViews.get(chosenKey!)!.elements.push(element)
    if (chosen.ids.length === 1) ringed.set(chosen.ids[0]!, element)
    chosenMarker = new mapgl.HtmlMarker(map, { coordinates: chosen.coordinates, html: element, anchor: TIP, zIndex: 4, interactive: true, preventMapInteractions: false })
    element.addEventListener('click', () => pick(chosen))
  }
  const markers: InputMarker[] = []
  for (const [key, group] of groups) {
    if (key !== chosenKey) {
      const old = kept(key, group)
      const html = old ? mark(old, [group]) : groupElement(group, false)
      pinViews.get(key)!.elements.push(html)
      pins.push(html)
      if (group.ids.length === 1) ringed.set(group.ids[0]!, html)
      markers.push({ type: 'html', coordinates: group.coordinates, html, anchor: TIP, zIndex: 2, userData: key, preventMapInteractions: false })
    }
  }
  clusterer.load(markers)
  move()
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
    html: mark(
      pinElement(
        `${mixed ? 'white' : ''} ${cross ? 'cross' : ''}`,
        [...colors][0] ?? NO_PLAN_COLOR,
        cross ? CANCELED : CLIPBOARD,
        requests,
      ),
      inside,
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
  fitPoints(target.data.map((marker) => marker.coordinates), CLUSTER_OFF_ZOOM)
}

function keyOf(point: { lat: number; lon: number }): string {
  return `${point.lat.toFixed(5)},${point.lon.toFixed(5)}`
}

/** Вид булавки адреса без значка: если он тот же, булавку не пересоздают, а меняют ей значок. */
function pinLook(group: Group): string {
  return [group.color, group.cross, group.mixed, group.ids.join(','), [...group.crews].join(',')].join('|')
}

function draw() {
  if (!map || !mapgl) return

  const { office, requests, engineers } = points()
  if (!office) return clear()
  const plan = props.plan
  const color = engineerColors(engineers)
  lit = litEngineer()

  // Прорастает только утренний план нового дня (правки 81.4): версии дня по событиям и часам
  // сменяют друг друга по времени, и каждая отрастала бы с нуля — после обновления страницы часы
  // дописывают их одну за другой, и прорисовка шла бы по кругу. Черновик «Пересчёта дня» тоже не
  // чертится: варианты сравнивают, переключаясь между ними.
  if (plan && !plan.parentPlanId && !seen.has(plan.id) && !plan.draft) grow()
  else drawLines()
  if (plan) seen.add(plan.id)

  // Офис, дома и обеды пересоздаются, только если поменялись.
  const located = new Map(requests.map((request) => [request.id, [request.lon, request.lat]]))
  const lunches = (plan?.routes ?? []).flatMap((route) => {
    const engineer = engineers.find((item) => item.id === route.engineerId)
    if (!route.lunch || !engineer) return []
    const where = (route.lunch.after && located.get(route.lunch.after)) || [engineer.startLon, engineer.startLat]
    return [{ engineer, lunch: route.lunch, where }]
  })
  const look = JSON.stringify([
    [office.lat, office.lon],
    engineers.filter((engineer) => engineer.home).map((engineer) => [engineer.id, engineer.name, engineer.home!.lat, engineer.home!.lon, color.get(engineer.id)]),
    lunches.map(({ engineer, lunch, where }) => [engineer.id, engineer.name, lunch, where, color.get(engineer.id)]),
  ])
  if (look !== staticLook) {
    staticLook = look
    drawn.forEach((object) => object.destroy())
    drawn = []
    for (const id of [...ringed.keys()]) if (id.startsWith('lunch:')) ringed.delete(id)

    drawn.push(new mapgl.HtmlMarker(map, {
      coordinates: [office.lon, office.lat],
      html: pinElement('office', '#FFFFFF', BUILDING),
      anchor: TIP,
      zIndex: 3,
      interactive: false,
    }))

    // Дом каждого инженера (блоки 73, 77) — булавка в его цвете с домиком (кадр 1787); линия того,
    // кто стартует из дома, от него и идёт: её начало — стартовая точка маршрута с бэкенда.
    for (const engineer of engineers) {
      if (!engineer.home) continue
      const element = pinElement('home', color.get(engineer.id) ?? NO_PLAN_COLOR, HOME)
      element.title = `Дом: ${engineer.name}`
      element.dataset.engineer = engineer.id
      owned(element)
      drawn.push(new mapgl.HtmlMarker(map, {
        coordinates: [engineer.home.lon, engineer.home.lat],
        html: element,
        anchor: TIP,
        zIndex: 1,
        interactive: false,
      }))
    }

    // Обед (блок 79, кадр 1527) — булавка цвета инженера с вилкой и ножом там, где он обедает: у
    // заявки, после которой. Видна только пока обед идёт и на это время заменяет булавку заявки;
    // нажатие выбирает эту заявку, а выбранная заявка выделяет и её обед (правки 82).
    lunchAt = new Map()
    for (const { engineer, lunch, where } of lunches) {
      const element = pinElement('lunch', color.get(engineer.id) ?? NO_PLAN_COLOR, LUNCH)
      element.title = `Обед ${lunch.start}–${lunch.end}: ${engineer.name}`
      element.dataset.engineer = engineer.id
      owned(element)
      ringed.set(`lunch:${engineer.id}`, element)
      const after = lunch.after
      if (after) {
        lunchAt.set(engineer.id, after)
        element.addEventListener('click', () => !measuring.value && emit('select', after))
      }
      drawn.push(new mapgl.HtmlMarker(map, { coordinates: where, html: element, anchor: TIP, zIndex: 5, interactive: !!after, preventMapInteractions: false }))
    }
  }

  // Заявки по одному адресу — одна булавка со счётчиком; цвет — первой назначенной заявки.
  const byKey = new Map<string, { ids: string[]; lat: number; lon: number; engineerId: string | null; crews: Set<string | null>; alarm: boolean }>()
  for (const request of requests) {
    const key = keyOf(request)
    const entry = byKey.get(key) ?? { ids: [], lat: request.lat, lon: request.lon, engineerId: null, crews: new Set(), alarm: false }
    entry.alarm ||= request.workPriority === 'emergency'
    const engineerId = plan?.assignments[request.id] ?? null
    entry.ids.push(request.id)
    entry.engineerId ??= engineerId
    entry.crews.add(engineerId)
    byKey.set(key, entry)
  }

  const next = new Map<string, Group>()
  let chosen: string | null = null
  for (const [key, entry] of byKey) {
    const cross = !!plan && !entry.engineerId
    const mixed = !!plan && entry.crews.size > 1
    next.set(key, {
      ids: entry.ids,
      coordinates: [entry.lon, entry.lat],
      color: !plan ? NO_PLAN_COLOR : cross ? UNASSIGNED_COLOR : (color.get(entry.engineerId!) ?? NO_PLAN_COLOR),
      cross,
      mixed,
      crews: entry.crews,
      icon: entry.ids.length > 1 ? CLIPBOARD : plan?.closed[entry.ids[0]!]?.outcome === 'done' ? DONE : entry.alarm ? ALARM : CLIPBOARD,
    })
    if (props.selected && entry.ids.includes(props.selected)) chosen = key
  }
  // Те же булавки (адреса, цвета, заявки, выбранная) — меняется только значок, на месте; иначе —
  // кластеризатор получает их заново.
  const same = chosen === chosenKey && next.size === pinLooks.size && [...next].every(([key, group]) => pinLooks.get(key) === pinLook(group))
  groups = next
  chosenKey = chosen
  if (same) {
    for (const [key, group] of groups) {
      const view = pinViews.get(key)!
      if (view.icon === group.icon) continue
      view.icon = group.icon
      for (const element of view.elements) {
        element.innerHTML = groupElement(group, element.classList.contains('chosen')).innerHTML
        marks.set(element, [group])
      }
    }
    move()
  } else place()

  fit(office, requests)
}

/**
 * Инженер на карте (кадры 1527, 1787, узел «Frame 1739», правки 82): круг 20 px его цвета в белой
 * обводке 2 px и белый треугольник курса — он смотрит, куда инженер едет. Центр круга — место
 * инженера. Треугольник из макета перенесён к центру круга.
 */
const CREW =
  '<svg width="40" height="40" viewBox="-20 -20 40 40" aria-hidden="true">' +
  '<path class="crew-arrow" d="M0.02 -15.4L8.24 -5.69C9.34 -4.39 8.41 -2.4 6.71 -2.4L-6.67 -2.4C-8.37 -2.4 -9.29 -4.39 -8.19 -5.69Z"/>' +
  '<circle r="12" fill="#fff"/><circle class="crew-body" r="10"/>' +
  '</svg>'
// Не чаще 30 раз в секунду: глазу хватает, а `setCoordinates` на каждый кадр нагружал бы карту.
const MOVE_MS = 33

function clearCrew() {
  clearTimeout(moveTimer)
  moveTimer = undefined
  crewMarkers.forEach(({ marker }) => marker.destroy())
  crewMarkers = []
}

/**
 * Маркеры инженеров, у которых есть заявки: круг цвета инженера со стрелкой курса, поверх булавок.
 * Новая версия дня (часы закрывают работы поминутно) маркеры не пересоздаёт, а отдаёт им новые
 * участки: иначе инженеры мигали бы на месте (правки 81.7).
 */
function placeCrew() {
  if (!map || !mapgl || !props.crew) return clearCrew()
  const color = engineerColors(points().engineers)
  const kept = new Map(crewMarkers.map((item) => [item.track.engineer.id, item]))
  const next: typeof crewMarkers = []
  for (const track of props.crew) {
    if (!track.route?.stops.length) continue
    const look = color.get(track.engineer.id) ?? NO_PLAN_COLOR
    const old = kept.get(track.engineer.id)
    if (old && old.element.dataset.look === look) {
      kept.delete(track.engineer.id)
      next.push({ ...old, track })
      continue
    }
    const element = document.createElement('div')
    element.className = 'crew-marker'
    element.dataset.look = look
    element.style.setProperty('--c', color.get(track.engineer.id) ?? NO_PLAN_COLOR)
    element.title = track.engineer.name
    element.innerHTML = CREW
    element.classList.toggle('faded', !!lit && track.engineer.id !== lit)
    const marker = new mapgl.HtmlMarker(map, { coordinates: track.start, html: element, anchor: [20, 20], zIndex: 5, interactive: false, preventMapInteractions: false })
    next.push({ track, marker, element })
  }
  kept.forEach(({ marker }) => marker.destroy())
  crewMarkers = next
  move()
}

function schedule() {
  if (moveTimer) return
  moveTimer = setTimeout(move, Math.max(0, MOVE_MS - (performance.now() - movedAt)))
}

const DOINGS: Doing[] = ['en_route', 'on_site', 'free', 'off', 'unavailable']

function move() {
  moveTimer = undefined
  movedAt = performance.now()
  const t = moment.value
  if (t === null) return
  // Круговой прогресс: работа, которая идёт в момент `t`, и обед.
  const shares = new Map<string, number>()
  for (const track of props.crew ?? []) {
    const work = workShare(track, t)
    if (work) shares.set(work.requestId, work.share)
    const lunch = lunchShare(track, t)
    if (lunch !== null) shares.set(`lunch:${track.engineer.id}`, lunch)
  }
  for (const [id, element] of ringed) ring(element, shares.get(id) ?? null)
  // Идущий обед виден и закрывает булавку заявки, у которой он идёт.
  const eating = new Set([...lunchAt].filter(([engineer]) => shares.has(`lunch:${engineer}`)).map(([, request]) => request))
  for (const [id, element] of ringed) {
    if (id.startsWith('lunch:')) {
      element.classList.toggle('eating', shares.has(id))
      // Обед выделен вместе со своей заявкой: на время обеда он и есть её булавка (правки 82).
      element.classList.toggle('chosen', !!props.selected && lunchAt.get(id.slice('lunch:'.length)) === props.selected)
    } else element.classList.toggle('under-lunch', eating.has(id))
  }
  for (const { track, marker, element } of crewMarkers) {
    const { at, doing: where, heading } = positionAt(track, t)
    // Обедающий скрыт, как на заявке: о нём говорит булавка обеда с кольцом (правки 82).
    const doing = shares.has(`lunch:${track.engineer.id}`) ? 'on_site' : where
    marker.setCoordinates(at)
    // Стоящий инженер смотрит туда, куда ехал.
    if (heading !== null) element.querySelector('.crew-arrow')?.setAttribute('transform', `rotate(${heading.toFixed(1)})`)
    if (!element.classList.contains(doing)) {
      element.classList.remove(...DOINGS)
      element.classList.add(doing)
    }
  }
}

/** Выделять есть что, только если у инженера есть заявки: иначе погасли бы все линии и ни одна не выделилась. */
function litEngineer(): string | null {
  const id = props.highlighted
  return id && props.plan?.routes.some((route) => route.engineerId === id && route.stops.length) ? id : null
}

// Прорисовка: линия растёт за 15 шагов по 80 мс (1,2 с), соседняя бригада стартует на шаг позже.
const GROW_STEPS = 15
const GROW_STEP_MS = 80

/**
 * Линии маршрутов, как в 2ГИС: белая кайма, в центре цвет инженера. Выделенный маршрут толще и
 * поверх остальных, чужие при выделении — прозрачные и без каймы. Во время прорисовки линия —
 * префикс своих координат.
 */
function drawLines() {
  const gl = mapgl
  const on = map
  if (!gl || !on) return
  // Нажатие на линию открывает карточку инженера (правки 81.4); с линейкой клик ставит точку.
  const line = (coordinates: number[][], color: string, width: number, zIndex: number, engineerId: string) => {
    const polyline = new gl.Polyline(on, { coordinates, color, width, zIndex })
    polyline.on('click', () => !measuring.value && emit('engineer', engineerId))
    return polyline
  }
  const color = engineerColors(points().engineers)
  const next = new Map<string, { key: string; objects: Drawn[] }>()
  drawable().forEach((route, index) => {
    const share = growth ? Math.min(1, Math.max(0, (growth.tick - index) / GROW_STEPS)) : 1
    if (!share) return
    const look = !lit ? 'plain' : route.engineerId === lit ? 'lit' : 'dim'
    // Линия пересоздаётся, только если сменился её путь, доля прорисовки или вид.
    const key = `${share} ${look} ${color.get(route.engineerId)} ${route.geometry.length} ${route.geometry[0]} ${route.geometry.at(-1)} ${route.legStarts?.join(',')}`
    const old = lines.get(route.engineerId)
    if (old?.key === key) {
      next.set(route.engineerId, old)
      lines.delete(route.engineerId)
      return
    }
    const coordinates = prefix(route.geometry, share) // уже [lon, lat] — бэкенд перевернул в geometry.py
    const own = color.get(route.engineerId) ?? NO_PLAN_COLOR
    const objects =
      look === 'dim'
        ? [line(coordinates, `${own}26`, 5, 1, route.engineerId)] // 0x26 — прозрачность 0,15
        : look === 'lit'
          ? [line(coordinates, '#FFFFFF', 11, 3, route.engineerId), line(coordinates, own, 7, 4, route.engineerId)]
          : [line(coordinates, '#FFFFFF', 9, 1, route.engineerId), line(coordinates, own, 5, 2, route.engineerId)]
    next.set(route.engineerId, { key, objects })
  })
  // Прежние линии снимаются после того, как встали новые: между ними нет пустого кадра.
  lines.forEach((line) => line.objects.forEach((object) => object.destroy()))
  lines = next
}

/** Маршруты, у которых есть линия; порядок задаёт очередь прорисовки. */
function drawable() {
  return (props.plan?.routes ?? []).filter((route) => route.geometry.length >= 2)
}

/**
 * Начало линии на долю `share` её точек; последняя точка — между соседними. Без Valhalla линия —
 * прямые между стопами, пять-десять точек, и целыми точками она росла бы скачками.
 */
function prefix(geometry: number[][], share: number): number[][] {
  if (share >= 1) return geometry
  const at = share * (geometry.length - 1)
  const whole = Math.floor(at)
  return [...geometry.slice(0, whole + 1), between(geometry[whole]!, geometry[whole + 1]!, at - whole)]
}

/** Новый план чертится по шагам; при «уменьшить движение» — сразу целиком. */
function grow() {
  stopGrowth()
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return drawLines()
  const total = GROW_STEPS + drawable().length - 1
  growth = { started: performance.now(), tick: -1, frame: 0 }
  const frame = () => {
    if (!growth) return
    const tick = Math.floor((performance.now() - growth.started) / GROW_STEP_MS)
    if (tick >= total) {
      growth = null
      return drawLines()
    }
    if (tick !== growth.tick) {
      growth.tick = tick
      drawLines()
    }
    growth.frame = requestAnimationFrame(frame)
  }
  frame()
}

function stopGrowth() {
  if (growth) cancelAnimationFrame(growth.frame)
  growth = null
}

/** Выбор сменился: новая булавка выходит из кластеров выбранной, прежняя возвращается в них. */
function highlight() {
  const selected = props.selected
  const next = selected ? ([...groups].find(([, group]) => group.ids.includes(selected))?.[0] ?? null) : null
  // Та же булавка — выбор мог перейти на заявку с обедом: его выделение обновит `move`.
  if (next === chosenKey) return move()
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
 * Клик по самой булавке карту не двигает: в `focus` он не попадает. Инженера — карта вписывает
 * линию его маршрута (без маршрута — не двигается).
 */
function zoomTo(focus: { id: string; engineer?: boolean }) {
  clearTimeout(focusTimer)
  focusTimer = setTimeout(() => {
    if (!alive || !map) return
    if (focus.engineer) {
      const geometry = props.plan?.routes.find((route) => route.engineerId === focus.id)?.geometry ?? []
      if (geometry.length >= 2) fitPoints(geometry, CLUSTER_OFF_ZOOM)
      return
    }
    const group = [...groups.values()].find((entry) => entry.ids.includes(focus.id))
    if (!group) return
    const [lon = 0, lat = 0] = group.coordinates
    map.fitBounds(
      { northEast: [lon + FOCUS_BOX, lat + FOCUS_BOX], southWest: [lon - FOCUS_BOX, lat - FOCUS_BOX] },
      { padding: padding(), maxZoom: Math.max(map.getZoom(), CLUSTER_OFF_ZOOM) },
    )
  }, props.mobile ? SHEET_SETTLE_MS : 0)
}

/**
 * Рамка по всем точкам — один раз на загрузку набора, как при открытии страницы: иначе Кашира и
 * Ступино остаются за экраном.
 */
function fit(office: { lat: number; lon: number }, requests: { lat: number; lon: number }[]) {
  const id = props.dataset?.id ?? props.plan?.datasetId ?? null
  if (!map || !id || `${id}:${props.loads}` === framed) return
  framed = `${id}:${props.loads}`
  fitPoints([office, ...requests].map((point) => [point.lon, point.lat]))
}

/** Рамка по точкам `[lon, lat]` с отступами панели; `maxZoom` — не ближе этого зума. */
function fitPoints(points: number[][], maxZoom?: number) {
  if (!map) return
  const lons = points.map(([lon = 0]) => lon)
  const lats = points.map(([, lat = 0]) => lat)
  map.fitBounds(
    { northEast: [Math.max(...lons), Math.max(...lats)], southWest: [Math.min(...lons), Math.min(...lats)] },
    { padding: padding(), ...(maxZoom === undefined ? {} : { maxZoom }) },
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
  transition:
    transform 200ms ease,
    opacity 200ms ease;
}

/* Выделен чужой маршрут — булавка гаснет; выбранная остаётся видна. */
.map-pin.faded:not(.chosen) {
  opacity: 0.25;
}

/* Заявки пересчёта мигают, пока он идёт. */
.map-pin.pulse svg {
  animation: pin-pulse 0.8s ease-in-out infinite alternate;
}

@keyframes pin-pulse {
  to {
    opacity: 0.35;
  }
}

/* «Уменьшить движение»: глобальное правило ускорило бы мигание до мерцания — его просто нет. */
@media (prefers-reduced-motion: reduce) {
  .map-pin.pulse svg {
    animation: none;
  }
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

/* Галочка выполненной (кадр 1787) — цветом самой булавки по белому кружку. */
.map-pin .pin-check {
  stroke: var(--fill);
  transition: stroke 200ms ease;
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

/* Инженер при проигрывании дня (кадры 1527, 1787, «Frame 1739»; правки 82): круг 20 px его цвета в
   белой обводке со стрелкой курса, тень его цветом. Вне смены и выбывший — серый. */
.crew-marker {
  width: 40px;
  height: 40px;
  filter: drop-shadow(0 2px 10px color-mix(in srgb, var(--c) 25%, transparent));
  /* Щипок над маркером — дело карты, как у булавок (блок 38.7). */
  touch-action: none;
  transition: opacity 400ms ease;
}

.crew-marker svg {
  display: block;
  overflow: visible;
}

.crew-marker .crew-arrow {
  fill: #fff;
  stroke: #fff;
  stroke-width: 4;
  stroke-linejoin: round;
}

.crew-marker .crew-body {
  fill: var(--c);
  transition: fill 200ms ease;
}

.crew-marker.off .crew-body,
.crew-marker.unavailable .crew-body {
  fill: #a0a8b8;
}

.crew-marker.faded {
  opacity: 0.3;
}

/* Инженер виден только в пути (правки 82): плавно появляется, когда выезжает к заявке, и плавно
   гаснет, приехав, — о работе говорит круговой прогресс булавки (правки 81.4); до первого выезда и
   после последней работы его нет. */
.crew-marker:not(.en_route) {
  opacity: 0;
}

/* Круговой прогресс работы и обеда (кадр 1787): белая дуга по дорожке белым 20 %. Пока работы
   нет, кольца нет (правки 82). */
.map-pin .pin-ring,
.map-pin .pin-track {
  display: none;
  fill: none;
  stroke: #fff;
  stroke-width: 2;
  stroke-linecap: round;
}

.map-pin .pin-track {
  stroke-opacity: 0.2;
}

.map-pin.ringing .pin-ring,
.map-pin.ringing .pin-track {
  display: inline;
}

.map-pin.white .pin-ring,
.map-pin.chosen .pin-ring,
.map-pin.white .pin-track,
.map-pin.chosen .pin-track {
  stroke: var(--c);
}

/* Обед (правки 82): булавка обеда видна, только пока он идёт, и на это время закрывает булавку
   заявки, у которой инженер обедает. */
.map-pin.lunch:not(.eating),
.map-pin.under-lunch {
  visibility: hidden;
}

/* Заявка под курсором в списке — крупнее, как выбранная (правки 81.4). */
.map-pin.lifted {
  transform: scale(1.2);
}

/* Дом инженера (блоки 73, 77) — булавка его цвета, не нажимается; обед нажимается, как его заявка. */
.map-pin.home {
  cursor: default;
}

.map-pin.office {
  --fill: #fff;
  --glyph: var(--color-text);
  --shadow: rgb(40 48 63 / 25%);
  cursor: default;
}
</style>
