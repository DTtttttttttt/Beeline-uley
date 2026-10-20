// Контракт бэкенда (PLAN 5.1, 5.2) и обращения к нему (PLAN 5.4).
// Типы описаны здесь один раз: отдельного файла типов в структуре 4.1 нет.
import type { InjectionKey, Ref } from 'vue'

export type Skill = 'local' | 'connection' | 'emergency'
export type Transport = 'car' | 'bicycle' | 'public_transport'
export type Equipment = 'router' | 'set_top_box' | 'alice'
/** Инструмент: нужен на месте, но не расходуется (PLAN 5.1). */
export type Tool = 'cable_tester' | 'crimping_tool' | 'laptop'
export type WorkPriority = 'emergency' | 'new_connection' | 'regular'
/** Чем кончилась работа по заявке — со слов бригады (PLAN 6.19, ответ 1). */
export type Outcome = 'done' | 'cancelled' | 'failed'
/** Где заявка на момент события: вычисляется по стопу и `T`, не хранится (PLAN 6.19). */
export type RequestStatus =
  | 'sent'
  | 'en_route'
  | 'in_progress'
  | 'done'
  | 'cancelled'
  | 'failed'
  | 'deferred'

export interface Office {
  address: string
  lat: number
  lon: number
}

/** Подсказка адреса (блок 34): координаты только у дома. */
export interface AddressSuggestion {
  address: string
  lat: number | null
  lon: number | null
}

/** Тип здания и подъезд к дому (PLAN 9.1) — копия `dictionaries.py`. */
export type BuildingType = 'apartment' | 'private' | 'office'
export type Access = 'normal' | 'far_parking' | 'closed'
export type DurationMode = 'norm' | 'complexity'
export type ComplexityLevel = 'simple' | 'medium' | 'complex'

/** Объект заявки: от него зависит длительность работы (PLAN 9.1). Пустое — «не знаем». */
export interface RequestObject {
  buildingType: BuildingType
  floor: number | null
  floors: number | null
  entrance: number | null
  flatNumber?: number | null // номер квартиры: куда идти инженеру, в расчёте не участвует (блок 78)
  flats: number | null // квартир в подъезде (блок 78); null — «?»
  elevator: boolean
  access: Access
}

export interface Request {
  id: string
  address: string
  lat: number
  lon: number
  serviceDurationMin: number
  baseNormMin: number
  windowStart: string
  windowEnd: string
  workPriority: WorkPriority
  skill: Skill
  requiredTransport: Transport | null
  requiredEquipment: Equipment[]
  requiredTools: Tool[]
  urgent: boolean
  hdType?: string | null // «Тип заявки HD» из выгрузки: подпись, в расчёте не участвует
  object?: RequestObject | null
  gigabit?: boolean
  repeatVisit?: boolean
  cable?: boolean // прокладка кабеля (блок 78)
  drilling?: boolean // сверление (блок 78)
  documentsMin?: number // документы норматива: на сложность не умножаются
}

export interface Engineer {
  id: string
  name: string
  startLat: number
  startLon: number
  shiftStart: string
  shiftEnd: string
  skills: Skill[]
  transport: Transport
  equipment: Equipment[]
  tools: Tool[]
  availableFrom: string | null
  unavailableFrom: string | null
  speed?: number // тысячные: 1120 — на 12 % медленнее нормы (PLAN 9.1)
  /** Снимок рейтинга на момент расчёта (PLAN 9.1); без отзывов — `null`. */
  rating?: Rating | null
  /** Взят из резерва варианта «Рейтинг в приоритете» (блок 71). */
  reserve?: boolean
  /** Дом (блок 73): «из дома» — старт в его точке, отдельного флага нет. */
  home?: Home | null
  /** Выезды на аварию за день (блок 74): состояние дня, ставит событие. */
  dispatched?: Dispatch[]
  // Окно обеда (блок 79): обед — час внутри него, между работами. Нет — общее `LUNCH_WINDOW`.
  lunchStart?: string
  lunchEnd?: string
}

/** Инженера отправили на аварию (блок 74); `lat/lon` — точка разворота, если развернули в пути. */
export interface Dispatch {
  time: string
  emergencyId: string
  lat: number | null
  lon: number | null
  km: number | null
  requestId: string | null
}

/** «Кто поедет на аварию» (блок 74): что делает сейчас, что будет и когда приедет. */
export interface EmergencyCandidate {
  engineerId: string
  now: 'en_route' | 'on_site' | 'free'
  requestId: string | null
  until: string | null
  action: 'divert' | 'finish' | 'go'
  arrival: string
  recommended: boolean
}

export interface Home {
  address: string
  lat: number
  lon: number
  equipment: Equipment[] // что лежит дома: его отмечает «Начало дня: из дома»
}

export interface Rating {
  stars: number
  count: number
}

export interface Input {
  office: Office
  requests: Request[]
  engineers: Engineer[]
  /** Длительность по нормативу или с учётом сложности (PLAN 9.1); у планов до блока 66 — norm. */
  durationMode?: DurationMode
}

// Факт того же дня из контрольной выборки участка (PLAN 2.1, 5.3): справка, не вход расчёта.
export interface Control {
  engineers: number
  total: number
  done: number
  cancelled: number
  overdue: number
  perEngineer: number
  // День людей нашим расписанием при мягких допущениях (PLAN 6.10, блок 51).
  run: { engineers: number; assigned: number; km: number; late: number; overShift: number }
}

export interface Dataset extends Input {
  id: string
  name: string
  builtIn: boolean
  createdAt: string
  updatedAt: string | null
  control: Control | null
}

/** Демо-наборы (PLAN 6.20): заявки участков и составы инженеров выбираются по отдельности. */
export interface DemoCatalog {
  requests: { id: string; name: string; count: number }[]
  crews: { id: string; name: string; count: number; skills: Skill[]; transports: Transport[]; training: boolean }[]
}

export interface Stop {
  requestId: string
  departure: string
  arrival: string
  start: string
  end: string
  travelKm: number
  travelMin: number
  /** Расчётная работа пары по скорости на начало дня (блок 69); у планов до него поля нет. */
  workMin?: number | null
  committed: boolean
}

export interface Route {
  engineerId: string
  km: number
  geometry: number[][] // [lon, lat] — порядок карты 2ГИС
  // Индекс точки `geometry`, с которой начинается переезд к каждому стопу (PLAN 6.11). Пусто у
  // планов до блока 63 и у плеч, склеенных Valhalla: проигрывание ведёт тогда по прямой.
  legStarts?: number[]
  stops: Stop[]
  // Обед бригады (блок 79): `after` — после какой заявки, `null` — до первой, в точке старта.
  lunch?: { start: string; end: string; after: string | null } | null
}

/** Окно обеда по умолчанию — копия `LUNCH_WINDOW` (`dictionaries.py`, блок 79). */
export const LUNCH_WINDOW = { start: '13:00', end: '16:00' } as const
export const LUNCH_MIN = 60

export interface Unassigned {
  requestId: string
  reasonCode: string
  reasonText: string
  deferNextDay: boolean
}

export interface Slot {
  start: string
  end: string
}

export interface Candidate {
  engineerId: string
  skillOk: boolean
  transportOk: boolean
  equipmentOk: boolean
  timeOk: boolean
  extraKm: number | null
  /** Сколько заявка займёт у этой бригады — по скорости на начало дня (блок 69). */
  durationMin?: number | null
  freeSlots: Slot[]
  /** Когда приедет к заявке (блок 80): у исполнителя — по плану, у других — после лучшей вставки. */
  arrival?: string | null
}

/** Что поможет неназначенной (PLAN 6.8, блок 55): одно ослабленное условие, проверенное расчётом. */
export interface Remedy {
  kind: 'window' | 'equipment' | 'transport' | 'engineer' | 'shift'
  text: string
  engineerId: string | null
  windowStart: string | null
  windowEnd: string | null
  engineer: Engineer | null
}

export interface Explanation {
  text: string
  candidates: Candidate[]
  remedies: Remedy[]
}

/** Факт по заявке: чем кончилась, когда и у кого (PLAN 6.19, ответ 1). */
export interface Closure {
  outcome: Outcome
  time: string
  engineerId: string
}

export interface Metrics {
  engineersUsed: number
  totalKm: number
  kmByEngineer: Record<string, number>
  assignedCount: number
  unassignedCount: number
  assignedByWorkPriority: Record<WorkPriority, number>
  unassignedByWorkPriority: Record<WorkPriority, number>
  assignedUrgent: number
  unassignedUrgent: number
  deferredCount: number
  /** Сколько заявок закрыто фактом и с каким исходом (PLAN 6.10, 6.19). */
  closedByOutcome: Record<Outcome, number>
  travelMin: number
  waitMin: number
  workMin: number
  utilizationByEngineer: Record<string, number>
  expectedLate: number
  /** Сколько ждут назначенные аварии, в сумме (PLAN 6.10, блок 49); у планов до блока — нет. */
  emergencyWaitMin?: number | null
  /** Меньше стольких инженеров назначенные заявки не выполнить — оценка снизу (PLAN 6.10, блок 50). */
  engineersLowerBound?: number | null
  /** Сколько заявок план мог взять вообще: назначенные и помещающиеся в чей-то пустой день (блок 50). */
  requestsCeiling?: number | null
  /** Сколько минут сложность и скорость добавили к нормативу (PLAN 9.1). */
  complexityExtraMin?: number
  objectsByLevel?: Partial<Record<ComplexityLevel, number>>
  /** Средний рейтинг исполнителя по назначенным заявкам и заявок у инженеров 4,5+ (блок 71). */
  ratingAvg?: number | null
  topRatedCount?: number
  /** Дорога до первой заявки в среднем по инженерам с работой, мин (блок 73). */
  firstLegMin?: number | null
}

/** Основной вариант того же дня по нормативу — у вариантов сравнения со сложностью (PLAN 9.1). */
export interface NormMetrics {
  assignedCount: number
  engineersUsed: number
  totalKm: number
}

/** Ответ `POST /api/requests/duration`: длительность по объекту для формы (PLAN 9.1). */
export interface DurationAnswer {
  normMin: number
  minutes: number
  workMin: number
  documentsMin: number
  accessMin: number
  complexity: number
  level: ComplexityLevel
  // «+10 мин — гигабитное подключение» с пояснением серым (блок 78); сумма — `minutes − normMin`.
  reasons: { minutes: number; text: string; hint: string }[]
}

export interface Validation {
  ok: boolean
  errors: string[]
}

/** Как шёл поиск OR-Tools (PLAN 6.5): есть только у оптимизированного расчёта. */
export interface SearchStats {
  objective: number
  solutions: number
  stoppedBy: 'solutions' | 'time'
  /** Прочитано ли стартовое решение — прошлый план при перепланировании. */
  warmStarted: boolean
  sec: number
  /** Секунда от старта поиска → лучшая цель, не больше 50 точек. */
  trace: [number, number][]
}

/** Уровень риска опоздания: < 10 %, 10-30 %, > 30 % (PLAN 6.18). */
export type Risk = 'low' | 'medium' | 'high'

export interface RequestForecast {
  lateProbability: number
  /** Время, позже которого начинают только 10 % прогонов; у прогона оно бывает и за полночь. */
  p90Start: string
  risk: Risk
}

export interface EngineerForecast {
  overShiftProbability: number
}

/** Модельная оценка: отклонения заданы нами, а не получены из истории (PLAN 6.18). */
export interface Forecast {
  requests: Record<string, RequestForecast>
  engineers: Record<string, EngineerForecast>
}

export type Change =
  | 'reassigned'
  | 'time_changed'
  | 'order_changed'
  | 'added'
  | 'cancelled'
  | 'deferred'
  | 'became_unassigned'

export interface RequestChange {
  requestId: string
  change: Change
  from: string | null
  to: string | null
  oldStart: string | null
  newStart: string | null
  engineerId: string | null
  oldPosition: number | null
  newPosition: number | null
}

export interface EngineerChange {
  engineerId: string
  oldKm: number
  newKm: number
  oldCount: number
  newCount: number
}

export interface Diff {
  requests: RequestChange[]
  engineers: EngineerChange[]
  metrics: {
    engineersUsed: number[]
    totalKm: number[]
    unassignedCount: number[]
  }
  /** Доля заявок, назначенных в обеих версиях, не сдвинутых событием (PLAN 6.13, блок 50). */
  stability?: number | null
  /** Кто поехал на аварию события и что бросил (блок 74, «Разворот»). */
  dispatch?: EmergencyCandidate | null
}

// События диспетчера (PLAN 5.1): восемь видов, все создают новую версию плана.
export type PlanEvent =
  // `urgent_request` — прежнее имя того же события: форма его не шлёт, но план, посчитанный
  // из curl или сценария, хранит событие дословно, и показать его всё равно надо.
  // `urgent` необязателен: без него срочна только авария (PLAN 6.12).
  // `engineerId` — кого диспетчер отправил на аварию (блок 74); без него — кто раньше приедет.
  | { type: 'add_request' | 'urgent_request'; time: string; urgent?: boolean | null; request: Request; engineerId?: string | null }
  | { type: 'add_engineer'; time: string; engineer: Engineer }
  | { type: 'update_request'; time: string; request: Request }
  | { type: 'update_engineer'; time: string; engineer: Engineer }
  // Отмена и перенос — списком: «все заявки в Кашире» одно событие (блок 42).
  | { type: 'cancel_request'; time: string; requestIds: string[] }
  | { type: 'defer_request'; time: string; requestIds: string[] }
  | { type: 'close_request'; time: string; requestId: string; outcome: Outcome; actualEnd?: string | null }
  | { type: 'engineer_unavailable'; time: string; engineerId: string }
  // Часы дня (блок 68): событие собирает сервер по скрытым фактам, форма его не шлёт.
  // `running` — затянувшиеся работы, из-за которых перестроен остаток дня.
  | { type: 'clock'; time: string; closed: { requestId: string; actualEnd: string }[]; running: string[] }

export type ReplanMode = 'from_event' | 'full' | 'insert' | 'delay'

/** «Итоги дня» (блок 68): работа по плану и по факту — суммы выполненных заявок, без дороги. */
export interface DaySummary {
  engineers: {
    engineerId: string
    name: string
    planMin: number
    factMin: number
    speedStart: number
    speedNow: number
    requests: { requestId: string; planMin: number; factMin: number }[]
  }[]
  cancelled: string[]
  deferred: string[]
  failed: string[]
  unassigned: string[]
}

/** Заявка из текста диспетчера (блок 42): тип, адрес, окно и оборудование; координат и норматива нет. */
export type AssistantRequestType = 'connection' | 'emergency' | 'order' | 'local'
export interface AssistantDraft {
  requestType: AssistantRequestType
  address: string
  windowStart: string | null
  windowEnd: string | null
  equipment: Equipment[]
}

/** Новый инженер из текста диспетчера (блок 59): то, что названо; остальное заполнит форма. */
export interface AssistantEngineerDraft {
  name: string | null
  shiftStart: string | null
  shiftEnd: string | null
  skills: Skill[]
  transport: Transport | null
  equipment: Equipment[]
  tools: Tool[]
}

/**
 * Правка заявки, которую доделывает её карточка (блок 59): `request` — заявка с разобранными
 * правками, поверх них карточка кладёт новый тип (`REQUEST_TYPES`) и адрес.
 */
export interface AssistantEdit {
  request: Request
  requestType: AssistantRequestType | null
  address: string | null
}

/** Принятое вложение ассистента (блок 60): чип в чате; в историю уходит только `id`. */
export interface AssistantAttachment {
  id: string
  name: string
  kind: 'csv' | 'json' | 'txt'
  summary: string
}

/** Строка переписки в окне «Ассистент»: фраза диспетчера или ответ; у ответа — что он предлагает. */
export interface AssistantMessage {
  from: 'me' | 'bot'
  text: string
  reply?: AssistantReply
  // Файлы, приложенные к фразе диспетчера (блок 60).
  files?: AssistantAttachment[]
  // План, о котором ответ (блок 61): черновик «что будет, если» посчитан от него и применим, только
  // пока день — та же версия.
  planId?: string
  // Предложение применено: кнопка под ответом становится серой «Применено», а результат —
  // версия плана и что в ней изменилось — остаётся под рукой, пока идёт переписка.
  applied?: boolean
  changes?: { plan: Plan; previous: Plan; phrase: string }
}

/**
 * Ответ ассистента: событие, черновики и правка — только предложение, применяет их диспетчер;
 * `time` — переход ко времени дня (`null` — к текущему), `answer` — ответ на вопрос по плану,
 * `clarify` — не разобрали, `text` говорит, что уточнить.
 */
export interface AssistantReply {
  kind: 'event' | 'add_request' | 'add_engineer' | 'edit_request' | 'time' | 'reassign' | 'answer' | 'clarify'
  text: string
  event: PlanEvent | null
  draft: AssistantDraft | null
  engineer: AssistantEngineerDraft | null
  edit: AssistantEdit | null
  time: string | null
  // Переназначение (блок 61): в лучшее место маршрута, как «Переназначить».
  reassign: { requestId: string; engineerId: string } | null
  // «Что будет, если…» (блок 61): посчитано черновиком `draftPlanId`, «Применить» делает его версией.
  whatIf: boolean
  draftPlanId: string | null
  // `full` — «пересчитать весь день с начала» («с утра», «с начала дня»); null — режим выбирает сервер.
  mode: 'full' | null
}

export type Algorithm = 'baseline' | 'optimized' | 'manual'

/** Два ответа про бригаду (PLAN 6.9): почему у неё эти заявки и почему в таком порядке. */
export interface RouteExplanation {
  assignment: string
  order: string
}

export interface Plan {
  id: string
  parentPlanId: string | null
  datasetId: string
  algorithm: Algorithm
  variant: string | null
  compareGroupId: string | null
  event: PlanEvent | null
  replanMode: ReplanMode | null
  input: Input
  routes: Route[]
  assignments: Record<string, string | null>
  pinned: Record<string, string>
  /** Заявка → время, когда диспетчер перенёс её на следующий день (PLAN 6.19). */
  deferred: Record<string, string>
  /** Заявка → факт от диспетчера: чем кончилась, когда и у кого (PLAN 6.19). */
  closed: Record<string, Closure>
  /** Затянувшаяся работа → когда она ещё шла (блок 68): её стоп кончается не раньше. */
  running?: Record<string, string>
  /** Заявка → где она на момент события. У неназначенной статуса нет (PLAN 6.19). */
  statuses: Record<string, RequestStatus>
  unassigned: Unassigned[]
  explanations: Record<string, Explanation>
  routeExplanations: Record<string, RouteExplanation>
  metrics: Metrics
  validation: Validation | null
  forecast: Forecast | null // прогноз опозданий (PLAN 6.18)
  /** Риск тех же маршрутов по нормативу — «было» в подсказке риска (блок 67). */
  normForecast?: Record<string, RequestForecast>
  /** Сложность каждой заявки входа: коэффициент и уровень (блок 67). */
  complexity?: Record<string, { complexity: number; level: ComplexityLevel }>
  normMetrics?: NormMetrics | null
  approximate: boolean
  approximateNote: string | null
  fallbackToBaseline: boolean
  search: SearchStats | null
  diff: Diff | null
  // Сколько секунд занял расчёт: столбец сводной таблицы вариантов (PLAN 6.14).
  computeSec: number
  createdAt: string
  /** Вариант предпросмотра, ещё не применённый: в цепочку не входит (PLAN 6.12, блок 53). */
  draft: boolean
  draftExpiresAt: string | null
}

/** Сводка варианта предпросмотра — её пишет сервер по двум версиям плана (блок 53). */
export interface PreviewSummary {
  /** Кто взял заявку события; `arrivalMin: null` — прибытие раньше события (весь день заново). */
  taken: { engineerId: string; arrivalMin: number | null; start: string } | null
  reassigned: number
  shifted: number
  unassigned: number
}

export interface PreviewOption {
  mode: ReplanMode
  plan: Plan
  summary: PreviewSummary
}

/** Варианты реакции на событие: черновики, рекомендация и режимы, не принявшие событие. */
export interface Preview {
  options: PreviewOption[]
  recommended: ReplanMode
  refused: Partial<Record<ReplanMode, string>>
  /** Авария днём (блок 74): кто может поехать, по времени прибытия; пусто — некому. */
  emergencyCandidates: EmergencyCandidate[]
}

// Русские названия справочников — копия `backend/app/dictionaries.py`. Отдельного эндпоинта
// со справочниками нет, и заводить его ради четырёх словарей незачем; лежат здесь же, где
// типы контракта, по тому же решению, что и они (13.1).
export const SKILL_NAMES: Record<Skill, string> = {
  local: 'Локальные работы',
  connection: 'Работы на подключение и дозаказы',
  emergency: 'Аварийные работы',
}

// Графики смен (ответ организаторов 29.09, PLAN 2.4) — копия `SHIFT_PRESETS` бэкенда: 2/2 —
// 10:00–22:00, допустимо 09:30–23:00; 5/2 — 09:00–18:00. Пресет заполняет начало и конец смены.
export const SHIFT_PRESETS: Record<string, { name: string; start: string; end: string }> = {
  '2-2': { name: '2/2 (10:00 – 22:00)', start: '10:00', end: '22:00' },
  '2-2-long': { name: '2/2 (09:30 – 23:00)', start: '09:30', end: '23:00' },
  '5-2': { name: '5/2 (09:00 – 18:00)', start: '09:00', end: '18:00' },
}

export const TRANSPORT_NAMES: Record<Transport, string> = {
  car: 'Автомобиль',
  bicycle: 'Велосипед',
  public_transport: 'Пешеход / общественный транспорт',
}

export const EQUIPMENT_NAMES: Record<Equipment, string> = {
  router: 'Роутер',
  set_top_box: 'Приставка',
  alice: 'Алиса',
}

export const TOOL_NAMES: Record<Tool, string> = {
  cable_tester: 'Кабельный тестер',
  crimping_tool: 'Обжимной инструмент',
  laptop: 'Ноутбук',
}

/**
 * Общее поле «Оборудование» в формах (docs/design.md, L): расходуемое оборудование и
 * инструменты одним списком; при отправке раскладывается обратно (`splitGear`).
 */
export const GEAR_NAMES: Record<Equipment | Tool, string> = { ...EQUIPMENT_NAMES, ...TOOL_NAMES }

export const WORK_PRIORITY_NAMES: Record<WorkPriority, string> = {
  emergency: 'Авария',
  new_connection: 'Новое подключение',
  regular: 'Обычная',
}

/** Объект заявки (PLAN 9.1) — та же копия справочника. */
export const BUILDING_TYPE_NAMES: Record<BuildingType, string> = {
  apartment: 'Многоквартирный дом',
  private: 'Частный дом',
  office: 'Офис',
}
export const ACCESS_NAMES: Record<Access, string> = {
  normal: 'Можно подъехать близко',
  far_parking: 'Парковка далеко',
  closed: 'Закрытая территория или пропуск',
}
/** Условия работы окна «Условия работы» (блок 78, кадр 1785) — копия `DURATION_FACTOR_NAMES`; пояснения — кадра. */
export const CONDITION_NAMES = {
  gigabit: { title: 'Гигабитное подключение', hint: 'Требуется подключение на скорости 1 Гбит/с' },
  repeatVisit: { title: 'Повторный выезд', hint: 'По этой проблеме инженер уже приезжал' },
  cable: { title: 'Нужна прокладка кабеля', hint: 'Требуется проложить новый кабель до оборудования' },
  drilling: { title: 'Нужно сверление', hint: 'Потребуется сделать новые отверстия для кабеля' },
} as const
export const COMPLEXITY_LEVEL_NAMES: Record<ComplexityLevel, string> = {
  simple: 'простые',
  medium: 'средние',
  complex: 'сложные',
}
/** Метки отзыва (блок 70, кадр 1779) — копия `REVIEW_TAG_NAMES`: только похвала. */
export const REVIEW_TAGS = {
  fast: 'быстро',
  neat: 'аккуратно',
  explained: 'всё объяснил',
  friendly: 'доброжелательный',
  polite: 'вежливый',
  punctual: 'пунктуальный',
} as const
export type ReviewTag = keyof typeof REVIEW_TAGS

/** Оценка заявки (блок 70): звёзды 1–5, метки, комментарий и имя клиента. */
export interface ReviewInput {
  stars: number
  tags: ReviewTag[]
  comment: string
  clientName: string
}

/** Сохранённая оценка заявки: кому ушла, кто поставил и когда. */
export interface Review extends ReviewInput {
  requestId: string
  engineerId: string
  source: 'dispatcher' | 'client' | 'demo'
  at: string
}

/** Живой рейтинг (`GET /api/ratings`): распределение — число оценок 5…1. */
export interface EngineerRating extends Rating {
  engineerId: string
  distribution: number[]
  monthChange: number | null
  last: Review[]
}

/** Оценка выполненной заявки в карточке: исполнитель, оценка и ссылка для клиента. */
export interface RequestReview {
  engineerId: string
  engineerName: string
  review: Review | null
  link: string
}

/** Порог «рейтинг 4.5+» — копия `TOP_RATING` бэкенда. */
export const TOP_RATING = 4.5

/** «★ 4,8 · 23 отзыва»; без отзывов — `null`, строку пишет «нет отзывов». */
export function ratingText(rating: Rating | null | undefined): string | null {
  return rating ? `★ ${decimal(rating.stars, 1)} · ${plural(rating.count, ['отзыв', 'отзыва', 'отзывов'])}` : null
}

/** Форма оценки по ссылке клиента (`GET /api/rate/{token}`). */
export interface RateForm {
  engineerName: string
  date: string
  connection: boolean
  rated: boolean
}

/** Русские исходы и статусы — та же копия справочника, что и у остальных (PLAN 6.19). */
export const OUTCOME_NAMES: Record<Outcome, string> = {
  done: 'Выполнена',
  cancelled: 'Отменена',
  failed: 'Не выполнена',
}

export const REQUEST_STATUS_NAMES: Record<RequestStatus, string> = {
  sent: 'Отправлено',
  en_route: 'В пути',
  in_progress: 'Выполняется',
  done: 'Завершено',
  cancelled: 'Отменена',
  failed: 'Не выполнена',
  deferred: 'Перенесена на следующий день',
}

// Вердикт кандидата (docs/design.md, «Вердикт кандидата»): чего не хватает — в родительном
// падеже («Нет роутера»), какой нужен транспорт — фразой. Та же копия справочника, что выше.
export const MISSING_NAMES: Record<Equipment | Tool, string> = {
  router: 'роутера',
  set_top_box: 'приставки',
  alice: 'Алисы',
  cable_tester: 'кабельного тестера',
  crimping_tool: 'обжимного инструмента',
  laptop: 'ноутбука',
}

export const TRANSPORT_NEEDED: Record<Transport, string> = {
  car: 'Нужен автомобиль',
  bicycle: 'Нужен велосипед',
  public_transport: 'Нужен общественный транспорт',
}

/** Бейдж статуса в списке и карточке: у неназначенной заявки статуса нет (PLAN 6.19). */
export const STATUS_BADGES: Record<RequestStatus, string> = { ...REQUEST_STATUS_NAMES, deferred: 'Перенесена' }
export const NO_STATUS_BADGE = 'Не назначена'

/**
 * Тип заявки BK (docs/design.md, «Тип заявки»): выводится из полей заявки, а в форме однозначно
 * их задаёт. `forms` — склонения для сводки инженера «4 подключения · 1 дозаказ».
 */
export interface RequestType {
  title: string
  forms: readonly [string, string, string]
  skill: Skill
  workPriority: WorkPriority
  serviceDurationMin: number
  baseNormMin: number
  documentsMin: number // столбец «Документы» норматива (PLAN 2.2, 9.1)
  requiredTools: Tool[]
  requiredTransport: Transport | null
}

export const REQUEST_TYPES: RequestType[] = [
  {
    title: 'Подключение клиентов',
    forms: ['подключение', 'подключения', 'подключений'],
    skill: 'connection',
    workPriority: 'new_connection',
    serviceDurationMin: 70,
    baseNormMin: 90,
    documentsMin: 10,
    requiredTools: ['cable_tester', 'crimping_tool'],
    requiredTransport: null,
  },
  {
    title: 'Авария на ТКД',
    forms: ['авария', 'аварии', 'аварий'],
    skill: 'emergency',
    workPriority: 'emergency',
    serviceDurationMin: 80,
    baseNormMin: 100,
    documentsMin: 0,
    requiredTools: ['cable_tester', 'laptop'],
    requiredTransport: 'car',
  },
  {
    title: 'Дозаказ оборудования',
    forms: ['дозаказ', 'дозаказа', 'дозаказов'],
    skill: 'connection',
    workPriority: 'regular',
    serviceDurationMin: 20,
    baseNormMin: 40,
    documentsMin: 10,
    requiredTools: [],
    requiredTransport: null,
  },
  {
    title: 'Локальная заявка',
    forms: ['локальная заявка', 'локальные заявки', 'локальных заявок'],
    skill: 'local',
    workPriority: 'regular',
    serviceDurationMin: 30,
    baseNormMin: 50,
    documentsMin: 0,
    requiredTools: ['cable_tester'],
    requiredTransport: null,
  },
]

/** Поля заявки, которые задаёт её тип: один раз для формы, карточки и правки ассистента. */
export function typeFields(type: RequestType) {
  return {
    skill: type.skill,
    workPriority: type.workPriority,
    serviceDurationMin: type.serviceDurationMin,
    baseNormMin: type.baseNormMin,
    documentsMin: type.documentsMin,
    requiredTransport: type.requiredTransport,
    requiredTools: [...type.requiredTools],
  }
}

/** Тип заявки из черновика ассистента → строка `REQUEST_TYPES` (те же четыре, в том же порядке). */
export const ASSISTANT_TYPES: Record<AssistantRequestType, RequestType> = {
  connection: REQUEST_TYPES[0]!,
  emergency: REQUEST_TYPES[1]!,
  order: REQUEST_TYPES[2]!,
  local: REQUEST_TYPES[3]!,
}

/** Тип BK заявки; у заявки из чужого файла, не подходящей ни под одну строку, — `null`. */
export function requestTypeOf(request: Pick<Request, 'skill' | 'workPriority'>): RequestType | null {
  return REQUEST_TYPES.find((type) => type.skill === request.skill && type.workPriority === request.workPriority) ?? null
}

/** Заголовок заявки: тип BK, а для «чужой» комбинации полей — тип работы. */
export function requestTitle(request: Pick<Request, 'skill' | 'workPriority'>): string {
  return requestTypeOf(request)?.title ?? WORK_PRIORITY_NAMES[request.workPriority]
}

/** «Срочная» — авария или заявка, появившаяся в течение дня (PLAN 7.4). */
export function isUrgent(request: Pick<Request, 'urgent' | 'workPriority'>): boolean {
  return request.urgent || request.workPriority === 'emergency'
}

/**
 * Цвета маршрутов (docs/design.md, «Палитра»): восемь из палитры и четыре подобранных в тон.
 * Цвет бригады — по её индексу во входе плана, одна палитра у карты, списка и гантта.
 * Малиновый — только неназначенная заявка.
 */
export const ROUTE_COLORS = [
  '#92C9F7', '#9EA8DE', '#7FA66A', '#B59DDA', '#CD94DC',
  '#F58EB1', '#80CBC5', '#80C781', '#EB9C97', '#F0D36B', '#F19899',
  '#F2CC89', '#FEAD94', '#5F7FCB',
]
export const UNASSIGNED_COLOR = '#F4504F'

export function engineerColors(engineers: readonly { id: string }[]): Map<string, string> {
  return new Map(engineers.map((engineer, index) => [engineer.id, ROUTE_COLORS[index % ROUTE_COLORS.length]!]))
}

// Варианты расчёта (PLAN 6.14). Имена — для сводной таблицы: код варианта диспетчеру
// ничего не говорит, а описание объясняет, чем этот порядок критериев отличается.
// Названия и порядок — как в окне «Варианты плана» (docs/design.md): базового там строки нет,
// он приходит в том же ответе и открывается переключателем «базовый».
export const VARIANT_NAMES: Record<string, string> = {
  baseline: 'Базовый (по ТЗ)',
  emergency_first: 'Аварии в приоритете',
  emergency_asap: 'Аварии сразу',
  max_requests: 'Максимум заявок',
  urgent_first: 'Срочные в приоритете',
  priority_first: 'Тип работ важнее количества',
  rating_first: 'Рейтинг в приоритете',
  fast: 'Быстрый расчёт',
}

export const VARIANT_NOTES: Record<string, string> = {
  baseline: 'Перебор из п. 2.3 ТЗ: первый подходящий инженер',
  emergency_first: 'Ни одна авария не уступает количеству, дальше больше заявок и меньше инженеров',
  emergency_asap: 'Сначала аварии как можно раньше, затем остальной день вокруг них',
  max_requests: 'Больше выполненных заявок, затем меньше инженеров',
  urgent_first: 'Срочность и тип работ выше экономии инженеров',
  priority_first: 'Тип работ важнее количества выполненных заявок',
  rating_first: 'Работа выполняется лучшими по рейтингу, с инженерами из резерва',
  fast: 'Основная модель, первое решение вставкой без поиска, лимит 2 секунды',
}

// Чем посчитан план: подпись набора на графике и в легенде (PLAN 6.15).
export const ALGORITHM_NAMES: Record<Algorithm, string> = {
  baseline: 'Базовый',
  optimized: 'Оптимизированный',
  manual: 'Ручная правка',
}

// 503 от подсказок значит «выключены» или «2ГИС отказал ключу», и до перезапуска бэкенда
// это не изменится: дальше не спрашиваем до перезагрузки страницы.
let suggestOff = false
// Минутный лимит исчерпан — до этого момента (мс) запросов не шлём вовсе. Узнаём заранее:
// бэкенд в каждом ответе пишет остаток (`X-Suggest-Remaining`) и когда освободится место
// (`X-Suggest-Reset`); на нуле форма молчит, не дожидаясь 429. 429 остаётся на случай, когда
// лимит съели другие окна или диспетчеры.
let suggestPausedUntil = 0

/**
 * Подсказки 2ГИС (блок 34): точка есть только у дома, улица подставляется текстом. Не бросает:
 * без подсказки адрес найдёт геокодер при сохранении. 502 (сбой связи) — разовый, спросим на
 * следующей паузе.
 */
async function suggestAddresses(base: string, q: string): Promise<AddressSuggestion[]> {
  if (suggestOff || Date.now() < suggestPausedUntil) return []
  try {
    const answer = await $fetch.raw<AddressSuggestion[]>(`${base}/api/geocode/suggest`, { query: { q } })
    if (answer.headers.get('X-Suggest-Remaining') === '0') {
      suggestPausedUntil = Date.now() + (Number(answer.headers.get('X-Suggest-Reset')) || 60) * 1000
    }
    return answer._data ?? []
  } catch (problem) {
    const { statusCode, response } = problem as { statusCode?: number; response?: Response }
    if (statusCode === 503) suggestOff = true
    if (statusCode === 429) {
      // Заголовка нет (чужой прокси срезал) — пережидаем всю минуту: так точно не ошибёмся.
      const seconds = Number(response?.headers.get('Retry-After')) || 60
      suggestPausedUntil = Date.now() + seconds * 1000
    }
    return []
  }
}

export function useApi() {
  const base = useRuntimeConfig().public.apiBase
  return {
    getDataset: () => $fetch<Dataset>(`${base}/api/dataset`),
    // FormData: один JSON («file») либо два CSV («requests», «engineers»); офис — последняя
    // строка CSV заявок — имена полей обработчика (PLAN 5.4). Тип запроса ставит сам fetch.
    importDataset: (form: FormData) =>
      $fetch<Dataset>(`${base}/api/dataset/import`, { method: 'POST', body: form }),
    demos: () => $fetch<DemoCatalog>(`${base}/api/dataset/demos`),
    // Пара «заявки + состав» становится текущим набором; «Восток» со штатным — встроенный.
    loadDemo: (requests: string, crew: string, durationMode: DurationMode) =>
      $fetch<Dataset>(`${base}/api/dataset/demo`, { method: 'POST', body: { requests, crew, durationMode } }),
    // Событие даёт новую версию плана с `diff`; алгоритм наследуется от родителя (PLAN 11.1).
    // Без `mode` режим выбирает сервер (PLAN 6.12): обычная заявка встраивается, остальное —
    // с момента события. Фронт передаёт его только для «весь день заново».
    sendEvent: (planId: string, event: PlanEvent, mode?: ReplanMode) =>
      $fetch<Plan>(`${base}/api/plans/${planId}/events`, {
        method: 'POST',
        body: mode ? { event, mode } : { event },
      }),
    // Часы дня (блоки 68, 82): сервер закрывает работы по скрытым фактам до `time` — новые версии
    // по порядку; 204 — нового нет, `[]`.
    clock: (planId: string, time: string) =>
      $fetch<Plan[] | undefined>(`${base}/api/plans/${planId}/clock`, { method: 'POST', body: { time } }).then((plans) => plans || []),
    daySummary: (planId: string) => $fetch<DaySummary>(`${base}/api/plans/${planId}/day-summary`),
    // Отзывы (блок 70): форма клиента по подписанной ссылке, рейтинг, оценки заявок.
    rate: (token: string, review: ReviewInput) => $fetch(`${base}/api/rate/${token}`, { method: 'POST', body: review }),
    ratings: () => $fetch<EngineerRating[]>(`${base}/api/ratings`),
    engineerReviews: (engineerId: string) =>
      $fetch<Review[]>(`${base}/api/engineers/${encodeURIComponent(engineerId)}/reviews`),
    planReviews: (planId: string) => $fetch<Review[]>(`${base}/api/plans/${planId}/reviews`),
    requestReview: (planId: string, requestId: string) =>
      $fetch<RequestReview>(`${base}/api/plans/${planId}/requests/${encodeURIComponent(requestId)}/review`),
    saveReview: (planId: string, requestId: string, review: ReviewInput) =>
      $fetch<Review>(`${base}/api/plans/${planId}/requests/${encodeURIComponent(requestId)}/review`, {
        method: 'PUT',
        body: review,
      }),
    // Предпросмотр события (PLAN 6.12, блок 53): варианты черновиками, версии ещё нет.
    previewEvent: (planId: string, event: PlanEvent) =>
      $fetch<Preview>(`${base}/api/plans/${planId}/events/preview`, { method: 'POST', body: { event } }),
    // Применить вариант: черновик становится версией цепочки, остальные удаляются.
    commitPlan: (planId: string) => $fetch<Plan>(`${base}/api/plans/${planId}/commit`, { method: 'POST' }),
    // Ассистент (блок 42): фраза → событие, черновик заявки или ответ по плану. План не меняет.
    // `history` — прежняя переписка: по ней ассистент понимает «он», «эта заявка».
    // Вложения — id: этой фразы в `attachments`, прежних — в репликах истории (блок 60).
    assist: (planId: string, text: string, time: string, history: AssistantMessage[], attachments: string[] = []) =>
      $fetch<AssistantReply>(`${base}/api/plans/${planId}/assistant`, {
        method: 'POST',
        body: {
          text,
          time,
          attachments,
          history: history.map((message) => ({
            role: message.from,
            text: message.text,
            attachments: (message.files ?? []).map((file) => file.id),
          })),
        },
      }),
    attach: (file: File) => {
      const form = new FormData()
      form.append('file', file)
      return $fetch<AssistantAttachment>(`${base}/api/assistant/attachments`, { method: 'POST', body: form })
    },
    // Включён ли ассистент (нет ключа — кнопки в шапке нет); Яндекс ради этого не спрашивается.
    assistantOn: () =>
      $fetch<{ assistant: string }>(`${base}/api/health`).then(
        (health) => health.assistant === 'ok',
        () => false,
      ),
    geocode: (address: string) =>
      $fetch<{ lat: number; lon: number }>(`${base}/api/geocode`, { method: 'POST', body: { address } }),
    suggest: (q: string) => suggestAddresses(base, q),
    // Длительность по объекту (PLAN 9.1): формула одна, на сервере; форма её только спрашивает.
    requestDuration: (body: Record<string, unknown>) =>
      $fetch<DurationAnswer>(`${base}/api/requests/duration`, { method: 'POST', body }),
    // Ручное переназначение (PLAN 6.16): новая версия плана либо 409 с причиной.
    // `engineerId: null` — снять назначение, `position: null` — лучшее место.
    manual: (planId: string, requestId: string, engineerId: string | null, position: number | null) =>
      $fetch<Plan>(`${base}/api/plans/${planId}/manual`, {
        method: 'POST',
        body: { requestId, engineerId, position },
      }),
    // Сравнение вариантов (PLAN 6.14): каждый вариант считается своим лимитом поиска,
    // поэтому ответа ждать минуты. Пустое тело означает «все варианты».
    compare: () => $fetch<Plan[]>(`${base}/api/plans/compare`, { method: 'POST', body: {} }),
    // Планы неизменяемы и лежат в базе: по id их берёт восстановление дня после обновления страницы.
    getPlan: (id: string) => $fetch<Plan>(`${base}/api/plans/${id}`),
    // Файл плана для Excel (блок 56): имя задаёт сервер (`filename*` в Content-Disposition).
    // Не переходом по адресу: ответ с ошибкой заменил бы страницу голым JSON.
    exportPlan: async (id: string) => {
      const answer = await $fetch.raw<Blob>(`${base}/api/plans/${id}/export.csv`, { responseType: 'blob' })
      const name = /filename\*=UTF-8''([^;]+)/.exec(answer.headers.get('Content-Disposition') ?? '')?.[1]
      return { blob: answer._data!, name: name ? decodeURIComponent(name) : 'plan.csv' }
    },
  }
}

/** «10:30» → 630. Времена контракта — минуты от полуночи одного дня (PLAN 2.6). */
export function clockMinutes(clock: string): number {
  const [hours, rest] = clock.split(':')
  return Number(hours) * 60 + Number(rest)
}

/** Число с запятой: `decimal(4.25, 1)` → «4,3». Одно правило на километры, проценты и секунды. */
export function decimal(value: number, digits: number): string {
  return value.toFixed(digits).replace('.', ',')
}

/** Километры с запятой: «4,2 км»; со знаком — «+7,8 км», «−2,5 км». */
export function km(value: number, signed = false): string {
  const sign = signed ? (value < 0 ? '−' : '+') : ''
  return `${sign}${decimal(signed ? Math.abs(value) : value, 1)} км`
}

/** Минуты от полуночи → «HH:MM» — обратное к `clockMinutes`. */
export function clockOf(minutes: number): string {
  return `${String(Math.floor(minutes / 60)).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}`
}

/** Длительность работы: «20 мин», «1 ч», «1 ч 20 мин». */
export function duration(minutes: number): string {
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return [hours ? `${hours} ч` : '', rest || !hours ? `${rest} мин` : ''].filter(Boolean).join(' ')
}

/** «1 заявка», «2 заявки», «5 заявок» — то же правило, что `_plural` в объяснениях бэкенда. */
export function plural(count: number, forms: readonly [string, string, string]): string {
  const tail = count % 10
  const hundred = count % 100
  const form =
    tail === 1 && hundred !== 11
      ? forms[0]
      : tail >= 2 && tail <= 4 && (hundred < 12 || hundred > 14)
        ? forms[1]
        : forms[2]
  return `${count} ${form}`
}

/** Часы машины `HH:MM` — то же, что подставляет кнопка «текущее время» (PLAN 7.7). */
export function clockNow(): string {
  const at = new Date()
  return clockOf(at.getHours() * 60 + at.getMinutes())
}

/**
 * Время дня по умолчанию — пока его не поставили в шапке руками. Не просто часы машины: в цепочке
 * событий время идёт только вперёд (6.17), а события обычно вводят руками («13:00»), и
 * отставшие часы дали бы 400. Поэтому берётся более позднее из двух.
 */
export function eventTime(plan: Plan | null): string {
  const last = plan?.event?.time
  const now = clockNow()
  return last && last > now ? last : now
}

/**
 * Где версия стоит на шкале дня (блок 33), в минутах: с момента своего события. Первичный план
 * и «весь день заново» — с 00:00: второй считается так, будто событие было известно с утра.
 * Ручная правка наследует событие и режим родителя и встаёт в ту же точку, после него.
 */
function placed(plan: Plan): number {
  return plan.event && plan.replanMode !== 'full' ? clockMinutes(plan.event.time) : 0
}

/**
 * Версия цепочки, которая действовала в момент `time` (блок 33): последняя из вставших на шкалу
 * не позже него. После «весь день заново» прежние версии так и не выбираются никогда: он стоит
 * с 00:00 и позже них в цепочке.
 */
export function versionAt(chain: Plan[], time: string): Plan {
  const at = clockMinutes(time)
  return chain.findLast((plan) => placed(plan) <= at) ?? chain[0]!
}

/**
 * Статус заявки в момент `time` (блок 33): та же таблица, что `planning/status.py`, только время —
 * из шапки, а не момент события версии, поэтому сервер его не посчитает. Факт и перенос — со
 * своего времени: версия «весь день заново» стоит с 00:00, и закрытая в 13:00 заявка до 13:00
 * идёт по стопу. Без стопа (невыполненная ушла из маршрута) показать больше нечего — сразу пометка.
 */
export function statusAt(plan: Plan, requestId: string, stop: Stop | null | undefined, time: string, live = false): RequestStatus | undefined {
  const closure = plan.closed[requestId]
  const deferred = plan.deferred[requestId]
  const fact = closure ? { status: closure.outcome, at: closure.time } : deferred ? { status: 'deferred' as const, at: deferred } : null
  if (fact && (fact.at <= time || !stop)) return fact.status
  if (!stop) return undefined
  if (stop.departure > time) return 'sent'
  if (time < stop.arrival) return 'en_route'
  if (time < stop.end || (live && !closure)) return 'in_progress'
  return 'done'
}

/**
 * Живой край дня (блок 69): показана последняя версия цепочки, и часы дня (`POST /clock`) уже
 * догнали время в шапке. Только здесь незакрытая работа после планового конца ещё идёт: закрывает
 * её факт. В прошлых версиях фактов будущего нет — там работа кончается по плану, как раньше.
 */
export const LIVE_EDGE: InjectionKey<Readonly<Ref<boolean>>> = Symbol('liveEdge')

/** Плановый конец работы в минутах: начало + расчётная работа пары; у планов до блока 69 — `end`. */
export function plannedEnd(stop: Stop): number {
  return stop.workMin == null ? clockMinutes(stop.end) : clockMinutes(stop.start) + stop.workMin
}

/** Сколько минут работа идёт сверх плана в момент `time` (только на живом краю); 0 — не идёт. */
export function overrun(plan: Plan, stop: Stop, time: string, live: boolean): number {
  if (!live || plan.closed[stop.requestId] || stop.start > time) return 0
  return Math.max(0, clockMinutes(time) - plannedEnd(stop))
}

/**
 * Скорость выполнения инженера словами (раздел 4 задания): «на 12 % медленнее нормы» — красноватым,
 * «по норме» — серым, «на 8 % быстрее нормы» — зелёным. Скорость — в тысячных, 1120 — медленнее.
 */
export function speedText(speed: number): { text: string; tone: 'text-danger' | 'text-muted' | 'text-ok' } {
  const percent = Math.round((speed - 1000) / 10)
  if (!percent) return { text: 'по норме', tone: 'text-muted' }
  return percent > 0
    ? { text: `на ${percent} % медленнее нормы`, tone: 'text-danger' }
    : { text: `на ${-percent} % быстрее нормы`, tone: 'text-ok' }
}

/**
 * Работы дня инженера для гантта и шкалы в карточке — одна копия правил подписи: заголовок —
 * тип BK, то есть что бригада делает (тип HD вроде «Информация» этого не говорит, W); время — работы по плану, а не
 * окно заявки: таймлайн показывает, когда бригада работает; переезд — к этой работе.
 */
export function dayWorks(plan: Plan, engineerId: string, time?: string, live = false) {
  const requests = new Map(plan.input.requests.map((request) => [request.id, request]))
  const risk = plan.forecast?.requests ?? {}
  const stops = plan.routes.find((route) => route.engineerId === engineerId)?.stops ?? []
  return stops.map((stop) => {
    const request = requests.get(stop.requestId)
    const forecast = risk[stop.requestId]
    // Затянувшаяся работа растёт до текущей минуты, пока её не закроет факт (блок 69).
    const over = time ? overrun(plan, stop, time, live) : 0
    return {
      id: stop.requestId,
      start: clockMinutes(stop.start),
      end: over ? Math.max(clockMinutes(time!), clockMinutes(stop.end)) : clockMinutes(stop.end),
      title: request ? requestTitle(request) : `№${stop.requestId}`,
      time: `${stop.start}–${stop.end}`,
      address: request?.address ?? '',
      // Риск выше низкого — ⚠ и вероятность (PLAN 6.18); у закреплённого прогноза нет.
      risk: forecast && forecast.risk !== 'low' ? `${Math.round(forecast.lateProbability * 100)}%` : null,
      // Для подсказки к ⚠ (RiskBadge): риск и, с учётом сложности, он же по нормативу (блок 67).
      forecast: forecast && forecast.risk !== 'low' ? forecast : null,
      normForecast: plan.normForecast?.[stop.requestId] ?? null,
      request,
      plannedStart: stop.start,
      committed: stop.committed,
      travel: `${duration(stop.travelMin)} · ${km(stop.travelKm)}`,
      // Минуты для графика окна метрик — по усечённым часам стопа. Работа — `end − start`:
      // с учётом сложности она длиннее норматива (PLAN 9.1). `extraMin` — добавка сложности и
      // скорости к нормативу: её называет подсказка блока.
      workMin: (over ? Math.max(clockMinutes(time!), clockMinutes(stop.end)) : clockMinutes(stop.end)) - clockMinutes(stop.start),
      // Расчётная работа пары и что с ней сейчас — для подсказки блока; факт — только после закрытия.
      planMin: stop.workMin ?? clockMinutes(stop.end) - clockMinutes(stop.start),
      state: time ? workState(plan, stop, time, over) : null,
      // Исход закрытой к этому времени работы — значок блока (блок 77): ✓ выполнена, ✕ отменена.
      outcome: plan.closed[stop.requestId] && (!time || plan.closed[stop.requestId]!.time <= time) ? plan.closed[stop.requestId]!.outcome : null,
      extraMin: request ? extraMinutes(stop, request) : 0,
      waitMin: clockMinutes(stop.start) - clockMinutes(stop.arrival),
      travelMin: stop.travelMin,
    }
  })
}

/** Что с работой в момент `time`: подсказка блока гантта и шкалы инженера (блок 69). */
function workState(plan: Plan, stop: Stop, time: string, over: number): string {
  const closure = plan.closed[stop.requestId]
  if (closure && closure.time <= time) {
    return closure.outcome === 'done'
      ? `выполнена за ${Math.max(0, clockMinutes(closure.time) - clockMinutes(stop.start))} мин`
      : OUTCOME_NAMES[closure.outcome].toLowerCase()
  }
  if (over) return `выполняется, +${over} мин сверх плана`
  if (stop.start <= time && time < stop.end) return 'выполняется'
  return stop.end <= time ? 'завершена по плану' : 'запланирована'
}

/**
 * Сколько минут работа стопа длиннее норматива — добавка сложности объекта и скорости инженера
 * (PLAN 9.1). Одна формула на подсказки гантта и риска; меньше норматива — ноль.
 */
export function extraMinutes(stop: Pick<Stop, 'start' | 'end' | 'workMin'>, request: Pick<Request, 'serviceDurationMin'>): number {
  // От расчётной работы, а не от конца: у закрытой фактом и затянувшейся конец — факт (блок 69).
  const work = stop.workMin ?? clockMinutes(stop.end) - clockMinutes(stop.start)
  return Math.max(0, work - request.serviceDurationMin)
}

/** Событие одной строкой: «Отмена заявки №47670». Имя бригады — по входу плана, где она есть. */
export function eventName(event: PlanEvent, engineers: readonly Engineer[]): string {
  const name = (id: string) => engineers.find((engineer) => engineer.id === id)?.name ?? id
  switch (event.type) {
    case 'add_request':
    case 'urgent_request':
      return `Новая заявка №${event.request.id}`
    case 'add_engineer':
      return `Новый инженер: ${event.engineer.name}`
    case 'update_request':
      return `Правка заявки №${event.request.id}`
    case 'update_engineer':
      return `Правка инженера: ${event.engineer.name}`
    case 'cancel_request':
      return event.requestIds.length > 1 ? `Отмена заявок: ${event.requestIds.length}` : `Отмена заявки №${event.requestIds[0]}`
    case 'defer_request':
      return event.requestIds.length > 1 ? `Перенос заявок: ${event.requestIds.length}` : `Перенос заявки №${event.requestIds[0]}`
    case 'close_request':
      return `${OUTCOME_NAMES[event.outcome]}: заявка №${event.requestId}`
    case 'engineer_unavailable':
      return `Недоступен: ${name(event.engineerId)}`
    case 'clock':
      return event.running.length
        ? `Задержка заявки №${event.running.join(', №')}`
        : `Выполнено: ${event.closed.map((item) => `№${item.requestId}`).join(', ')}`
  }
}

/** Начало работы по каждой заявке маршрутов плана. */
export function starts(plan: Plan): Map<string, string> {
  return new Map(plan.routes.flatMap((route) => route.stops.map((stop) => [stop.requestId, stop.start] as const)))
}

/**
 * Мобильная версия — уже 768 px (docs/design.md). Раскладку меняют responsive-классы Tailwind с тем же
 * порогом (`@media (max-width: 767px)` в index.vue, Modal.vue, SideLists.vue), поэтому телефон
 * получает её сразу, ещё с сервера; JS нужен только поведению — гантту, листу, зуму карты.
 */
export const MOBILE_QUERY = '(max-width: 767px)'

export function useMobile() {
  const mobile = ref(false)
  let query: MediaQueryList | null = null
  const update = () => (mobile.value = !!query?.matches)
  onMounted(() => {
    query = window.matchMedia(MOBILE_QUERY)
    update()
    query.addEventListener('change', update)
  })
  onBeforeUnmount(() => query?.removeEventListener('change', update))
  return mobile
}

/** Текст ошибки из ответа FastAPI: в `detail` лежит готовая фраза для диспетчера. */
export function apiError(error: unknown): string {
  const { data, statusCode } = error as { data?: { detail?: unknown }; statusCode?: number }
  const detail = data?.detail
  if (typeof detail === 'string') return detail
  // 422 несёт `detail` списком на английском, а прокси (502–504) — свою HTML-страницу: сырое
  // «[POST] https://…: 422» диспетчеру ничего не говорит.
  if (statusCode === 422) return 'Сервер не принял запрос (код 422) — проверьте введённые данные'
  if (statusCode && statusCode >= 500) return `Сервер не ответил (код ${statusCode}) — попробуйте ещё раз`
  return error instanceof Error ? error.message : String(error)
}
