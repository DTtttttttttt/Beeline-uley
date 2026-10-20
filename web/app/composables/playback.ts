// Проигрывание дня (PLAN 6.11, блок 63): где инженер и что делает в момент `t` — минуты от полуночи
// дробью. Только чтение плана: времена стопов посчитал бэкенд, линии нарисовала Valhalla, здесь
// маркер лишь ставится на линию по доле прошедшего времени.
import type { InjectionKey, Ref } from 'vue'
import { clockMinutes, type Engineer, type Plan, type Route } from './useApi'

/** Момент проигрывания в минутах дробью; `null` — полоса закрыта. Страница отдаёт его карте мимо шаблона. */
export const PLAY_MOMENT: InjectionKey<Readonly<Ref<number | null>>> = Symbol('playMoment')

/** Что делает инженер: едет, на заявке (ждёт окна или работает), свободен, вне смены, выбыл. */
export type Doing = 'en_route' | 'on_site' | 'free' | 'off' | 'unavailable'

/** Участок линии к стопу: точки `[lon, lat]` и пройденная длина у каждой. */
interface Leg {
  points: number[][]
  lengths: number[]
}

export interface Track {
  engineer: Engineer
  route: Route | null
  start: number[]
  legs: Leg[]
  // Факт закрытия заявки — с его времени работа кончена, как в статусах (`statusAt`).
  closed: Map<string, number>
  // С какой минуты инженер выбыл. Выбывший новой работы не берёт с начала дня (`free_from`), и в
  // версии «весь день заново» (она стоит на 00:00) он выбыл на весь день.
  outFrom: number | null
  // Живой край дня (`LIVE_EDGE`): незакрытая работа после планового конца ещё идёт (блок 69).
  live: boolean
}

/**
 * Участки маршрутов один раз на план. По `legStarts` — куски линии Valhalla; у планов до блока 63 и
 * у плеч, которые Valhalla склеила, — прямые между стартом инженера и его заявками.
 */
export function tracks(plan: Plan, live = false): Track[] {
  const where = new Map(plan.input.requests.map((request) => [request.id, [request.lon, request.lat]]))
  const routes = new Map(plan.routes.map((route) => [route.engineerId, route]))
  const closed = new Map(Object.entries(plan.closed).map(([id, closure]) => [id, clockMinutes(closure.time)]))
  return plan.input.engineers.map((engineer) => {
    const route = routes.get(engineer.id) ?? null
    const stops = route?.stops ?? []
    const geometry = route?.geometry ?? []
    const starts = route?.legStarts ?? []
    const start = geometry[0] ?? [engineer.startLon, engineer.startLat]
    const legs =
      starts.length === stops.length && geometry.length >= 2
        ? starts.map((from, k) => geometry.slice(from, (starts[k + 1] ?? geometry.length - 1) + 1))
        : stops.map((stop, k) => [k ? where.get(stops[k - 1]!.requestId)! : start, where.get(stop.requestId)!])
    const outFrom = engineer.unavailableFrom ? (plan.replanMode === 'full' ? 0 : clockMinutes(engineer.unavailableFrom)) : null
    return { engineer, route, start, legs: legs.map(measure), closed, outFrom, live }
  })
}

/** Длина по плоскости с поправкой долготы на широту: для доли пути на участке в городе её хватает. */
function measure(points: number[][]): Leg {
  const lengths = [0]
  for (let i = 1; i < points.length; i++) {
    const [lon0 = 0, lat0 = 0] = points[i - 1]!
    const [lon1 = 0, lat1 = 0] = points[i]!
    const dx = (lon1 - lon0) * Math.cos((((lat0 + lat1) / 2) * Math.PI) / 180)
    lengths.push(lengths[i - 1]! + Math.hypot(dx, lat1 - lat0))
  }
  return { points, lengths }
}

/** Точка между соседними `[lon, lat]` на доле `part` отрезка; ею же карта дорисовывает растущую линию. */
export function between(a: number[], b: number[], part: number): number[] {
  const [lon0 = 0, lat0 = 0] = a
  const [lon1 = 0, lat1 = 0] = b
  return [lon0 + (lon1 - lon0) * part, lat0 + (lat1 - lat0) * part]
}

/** Точка на доле `share` длины участка и курс отрезка, по которому инженер едет, — градусы от севера по часовой. */
function along(leg: Leg, share: number): { at: number[]; heading: number | null } {
  const target = share * leg.lengths.at(-1)!
  const i = Math.max(1, leg.lengths.findIndex((length) => length >= target))
  const span = leg.lengths[i]! - leg.lengths[i - 1]!
  const part = span ? (target - leg.lengths[i - 1]!) / span : 0
  const [a, b] = [leg.points[i - 1]!, leg.points[i] ?? leg.points[i - 1]!]
  return { at: between(a, b, part), heading: course(a, b) }
}

/** Курс от `a` к `b` (`[lon, lat]`) с поправкой долготы на широту; на месте — `null`. */
function course(a: number[], b: number[]): number | null {
  const [lon0 = 0, lat0 = 0] = a
  const [lon1 = 0, lat1 = 0] = b
  const dx = (lon1 - lon0) * Math.cos((((lat0 + lat1) / 2) * Math.PI) / 180)
  const dy = lat1 - lat0
  return dx || dy ? (Math.atan2(dx, dy) * 180) / Math.PI : null
}

/**
 * Где инженер в момент `t`: между выездом и прибытием — на участке по доле времени, от прибытия до
 * конца работы — у заявки, до первого выезда — в точке старта, между заявками и после последней — у
 * той, где закончил; закрытую фактом заявку — с времени факта, а на живом краю дня незакрытая работа
 * идёт и после планового конца, пока её не закроет факт (блок 69). Выбывший, доделав закреплённое, стоит
 * там же, но серым.
 */
export function positionAt(track: Track, t: number): { at: number[]; doing: Doing; heading: number | null } {
  const { engineer } = track
  let at = track.start
  let doing: Doing = 'free'
  // Курс — только в пути (маркер со стрелкой, кадр 1787); стоящий смотрит, куда ехал.
  let heading: number | null = null
  for (const [k, stop] of (track.route?.stops ?? []).entries()) {
    const departure = clockMinutes(stop.departure)
    const arrival = clockMinutes(stop.arrival)
    if (t < departure) break
    const leg = track.legs[k]!
    if (t >= (track.closed.get(stop.requestId) ?? Infinity)) {
      at = leg.points.at(-1)!
      continue
    }
    if (t < arrival) {
      ;({ at, heading } = along(leg, (t - departure) / (arrival - departure)))
      doing = 'en_route'
      break
    }
    at = leg.points.at(-1)!
    if (t < clockMinutes(stop.end) || track.live) {
      doing = 'on_site'
      break
    }
  }
  // Закреплённую работу выбывший доделывает (PLAN 6.12): пока он едет к ней или на ней — он в работе.
  if (doing !== 'free') return { at, doing, heading }
  if (track.outFrom !== null && t >= track.outFrom) doing = 'unavailable'
  else if (!onShift(engineer, t)) doing = 'off'
  return { at, doing, heading }
}

/**
 * Какая работа инженера идёт в момент `t` и какая её доля прошла — круговой прогресс булавки
 * (правки 81.4). Ожидание окна — не работа; закрытая фактом кончилась на факте; на живом краю
 * незакрытая работа после планового конца ещё идёт — дуга полная.
 */
export function workShare(track: Track, t: number): { requestId: string; share: number } | null {
  for (const stop of track.route?.stops ?? []) {
    const start = clockMinutes(stop.start)
    const end = clockMinutes(stop.end)
    if (t < start || t >= (track.closed.get(stop.requestId) ?? Infinity)) continue
    if (t < end) return { requestId: stop.requestId, share: (t - start) / Math.max(1, end - start) }
    if (track.live) return { requestId: stop.requestId, share: 1 }
  }
  return null
}

/** Доля прошедшего обеда в момент `t`; `null` — обед не идёт. */
export function lunchShare(track: Track, t: number): number | null {
  const lunch = track.route?.lunch
  if (!lunch) return null
  const [start, end] = [clockMinutes(lunch.start), clockMinutes(lunch.end)]
  return t >= start && t < end ? (t - start) / (end - start) : null
}

function onShift(engineer: Engineer, t: number): boolean {
  const from = Math.max(clockMinutes(engineer.shiftStart), engineer.availableFrom ? clockMinutes(engineer.availableFrom) : 0)
  return t >= from && t < clockMinutes(engineer.shiftEnd)
}

/** Шкала ползунка: от самого раннего начала смены до самого позднего конца. */
export function dayRange(plan: Plan): [number, number] {
  const engineers = plan.input.engineers
  if (!engineers.length) return [0, 24 * 60]
  return [
    Math.min(...engineers.map((engineer) => clockMinutes(engineer.shiftStart))),
    Math.max(...engineers.map((engineer) => clockMinutes(engineer.shiftEnd))),
  ]
}
