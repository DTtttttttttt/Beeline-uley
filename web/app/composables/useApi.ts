// Контракт бэкенда (PLAN 5.1, 5.2) и обращения к нему (PLAN 5.4).
// Типы описаны здесь один раз: отдельного файла типов в структуре 4.1 нет.

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
}

export interface Input {
  office: Office
  requests: Request[]
  engineers: Engineer[]
}

// Факт того же дня из контрольной выборки участка (PLAN 2.1, 5.3): справка, не вход расчёта.
export interface Control {
  engineers: number
  total: number
  done: number
  cancelled: number
  overdue: number
  perEngineer: number
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
  crews: { id: string; name: string; count: number; skills: Skill[]; transports: Transport[] }[]
}

export interface Stop {
  requestId: string
  departure: string
  arrival: string
  start: string
  end: string
  travelKm: number
  travelMin: number
  committed: boolean
}

export interface Route {
  engineerId: string
  km: number
  geometry: number[][] // [lon, lat] — порядок карты 2ГИС
  stops: Stop[]
}

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
  freeSlots: Slot[]
}

export interface Explanation {
  text: string
  candidates: Candidate[]
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
}

export interface Validation {
  ok: boolean
  errors: string[]
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
}

// События диспетчера (PLAN 5.1): восемь видов, все создают новую версию плана.
export type PlanEvent =
  // `urgent_request` — прежнее имя того же события: форма его не шлёт, но план, посчитанный
  // из curl или сценария, хранит событие дословно, и показать его всё равно надо.
  // `urgent` необязателен: без него срочна только авария (PLAN 6.12).
  | { type: 'add_request' | 'urgent_request'; time: string; urgent?: boolean | null; request: Request }
  | { type: 'add_engineer'; time: string; engineer: Engineer }
  | { type: 'update_request'; time: string; request: Request }
  | { type: 'update_engineer'; time: string; engineer: Engineer }
  // Отмена и перенос — списком: «все заявки в Кашире» одно событие (блок 42).
  | { type: 'cancel_request'; time: string; requestIds: string[] }
  | { type: 'defer_request'; time: string; requestIds: string[] }
  | { type: 'close_request'; time: string; requestId: string; outcome: Outcome; actualEnd?: string | null }
  | { type: 'engineer_unavailable'; time: string; engineerId: string }

export type ReplanMode = 'from_event' | 'full' | 'insert'

/** Заявка из текста диспетчера (блок 42): тип, адрес, окно и оборудование; координат и норматива нет. */
export type AssistantRequestType = 'connection' | 'emergency' | 'order' | 'local'
export interface AssistantDraft {
  requestType: AssistantRequestType
  address: string
  windowStart: string | null
  windowEnd: string | null
  equipment: Equipment[]
}

/** Строка переписки в окне «Ассистент»: фраза диспетчера или ответ; у ответа — что он предлагает. */
export interface AssistantMessage {
  from: 'me' | 'bot'
  text: string
  reply?: AssistantReply
  // Предложение применено: кнопка под ответом становится серой «Применено», а результат —
  // версия плана и что в ней изменилось — остаётся под рукой, пока идёт переписка.
  applied?: boolean
  changes?: { plan: Plan; previous: Plan; phrase: string }
}

/**
 * Ответ ассистента: событие и черновик заявки — только предложение, применяет их диспетчер;
 * `answer` — ответ на вопрос по плану, `clarify` — не разобрали, `text` говорит, что уточнить.
 */
export interface AssistantReply {
  kind: 'event' | 'add_request' | 'answer' | 'clarify'
  text: string
  event: PlanEvent | null
  draft: AssistantDraft | null
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
  /** Заявка → где она на момент события. У неназначенной статуса нет (PLAN 6.19). */
  statuses: Record<string, RequestStatus>
  unassigned: Unassigned[]
  explanations: Record<string, Explanation>
  routeExplanations: Record<string, RouteExplanation>
  metrics: Metrics
  validation: Validation | null
  forecast: Forecast | null // прогноз опозданий (PLAN 6.18)
  approximate: boolean
  approximateNote: string | null
  fallbackToBaseline: boolean
  diff: Diff | null
  // Сколько секунд занял расчёт: столбец сводной таблицы вариантов (PLAN 6.14).
  computeSec: number
  createdAt: string
}

// Русские названия справочников — копия `backend/app/dictionaries.py`. Отдельного эндпоинта
// со справочниками нет, и заводить его ради четырёх словарей незачем; лежат здесь же, где
// типы контракта, по тому же решению, что и они (13.1).
export const SKILL_NAMES: Record<Skill, string> = {
  local: 'Локальные работы',
  connection: 'Работы на подключение и дозаказы',
  emergency: 'Аварийные работы',
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
    requiredTools: ['cable_tester'],
    requiredTransport: null,
  },
]

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
  max_requests: 'Максимум заявок',
  urgent_first: 'Срочные в приоритете',
  priority_first: 'Тип работ важнее количества',
  fast: 'Быстрый расчёт',
}

export const VARIANT_NOTES: Record<string, string> = {
  baseline: 'Перебор из п. 2.3 ТЗ: первый подходящий инженер',
  emergency_first: 'Ни одна авария не уступает количеству, дальше больше заявок и меньше инженеров',
  max_requests: 'Больше выполненных заявок, затем меньше инженеров',
  urgent_first: 'Срочность и тип работ выше экономии инженеров',
  priority_first: 'Тип работ важнее количества выполненных заявок',
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
    loadDemo: (requests: string, crew: string) =>
      $fetch<Dataset>(`${base}/api/dataset/demo`, { method: 'POST', body: { requests, crew } }),
    // Событие даёт новую версию плана с `diff`; алгоритм наследуется от родителя (PLAN 11.1).
    // Без `mode` режим выбирает сервер (PLAN 6.12): обычная заявка встраивается, остальное —
    // с момента события. Фронт передаёт его только для «весь день заново».
    sendEvent: (planId: string, event: PlanEvent, mode?: ReplanMode) =>
      $fetch<Plan>(`${base}/api/plans/${planId}/events`, {
        method: 'POST',
        body: mode ? { event, mode } : { event },
      }),
    // Ассистент (блок 42): фраза → событие, черновик заявки или ответ по плану. План не меняет.
    // `history` — прежняя переписка: по ней ассистент понимает «он», «эта заявка».
    assist: (planId: string, text: string, time: string, history: AssistantMessage[]) =>
      $fetch<AssistantReply>(`${base}/api/plans/${planId}/assistant`, {
        method: 'POST',
        body: { text, time, history: history.map((message) => ({ role: message.from, text: message.text })) },
      }),
    // Включён ли ассистент (нет ключа — кнопки в шапке нет); Яндекс ради этого не спрашивается.
    assistantOn: () =>
      $fetch<{ assistant: string }>(`${base}/api/health`).then(
        (health) => health.assistant === 'ok',
        () => false,
      ),
    geocode: (address: string) =>
      $fetch<{ lat: number; lon: number }>(`${base}/api/geocode`, { method: 'POST', body: { address } }),
    suggest: (q: string) => suggestAddresses(base, q),
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
export function statusAt(plan: Plan, requestId: string, stop: Stop | null | undefined, time: string): RequestStatus | undefined {
  const closure = plan.closed[requestId]
  const deferred = plan.deferred[requestId]
  const fact = closure ? { status: closure.outcome, at: closure.time } : deferred ? { status: 'deferred' as const, at: deferred } : null
  if (fact && (fact.at <= time || !stop)) return fact.status
  if (!stop) return undefined
  if (stop.departure > time) return 'sent'
  if (time < stop.arrival) return 'en_route'
  if (time < stop.end) return 'in_progress'
  return 'done'
}

/**
 * Работы дня инженера для гантта и шкалы в карточке — одна копия правил подписи: заголовок —
 * тип BK, то есть что бригада делает (тип HD вроде «Информация» этого не говорит, W); время — работы по плану, а не
 * окно заявки: таймлайн показывает, когда бригада работает; переезд — к этой работе.
 */
export function dayWorks(plan: Plan, engineerId: string) {
  const requests = new Map(plan.input.requests.map((request) => [request.id, request]))
  const risk = plan.forecast?.requests ?? {}
  const stops = plan.routes.find((route) => route.engineerId === engineerId)?.stops ?? []
  return stops.map((stop) => {
    const request = requests.get(stop.requestId)
    const forecast = risk[stop.requestId]
    return {
      id: stop.requestId,
      start: clockMinutes(stop.start),
      end: clockMinutes(stop.end),
      title: request ? requestTitle(request) : `№${stop.requestId}`,
      time: `${stop.start}–${stop.end}`,
      address: request?.address ?? '',
      // Риск выше низкого — ⚠ и вероятность (PLAN 6.18); у закреплённого прогноза нет.
      risk: forecast && forecast.risk !== 'low' ? `${Math.round(forecast.lateProbability * 100)}%` : null,
      // Для подсказки к ⚠ (RiskBadge).
      forecast: forecast && forecast.risk !== 'low' ? forecast : null,
      request,
      plannedStart: stop.start,
      committed: stop.committed,
      travel: `${duration(stop.travelMin)} · ${km(stop.travelKm)}`,
      // Минуты для графика окна метрик. Работа — норматив без дороги: у стопа он ровно
      // `end − start`, а часы стопа усечены до минуты. Ожидание — по усечённым часам.
      workMin: request?.serviceDurationMin ?? 0,
      waitMin: clockMinutes(stop.start) - clockMinutes(stop.arrival),
      travelMin: stop.travelMin,
    }
  })
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
  const detail = (error as { data?: { detail?: unknown } })?.data?.detail
  if (typeof detail === 'string') return detail
  return error instanceof Error ? error.message : String(error)
}
