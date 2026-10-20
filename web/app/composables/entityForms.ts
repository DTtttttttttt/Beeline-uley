// Черновики заявки и бригады для форм (PLAN 6.19, 7.5, 7.7): одни и те же поля правятся в
// форме события и в карточках, поэтому черновик и его проверка живут здесь, в одном месте.
import {
  clockMinutes,
  EQUIPMENT_NAMES,
  LUNCH_MIN,
  LUNCH_WINDOW,
  TOOL_NAMES,
  type Engineer,
  type Equipment,
  type Home,
  type Request,
  type RequestObject,
  type Skill,
  type Tool,
  type Transport,
  type WorkPriority,
} from './useApi'

/** Заявка в форме: координаты ещё могут быть не найдены. */
export interface RequestDraft {
  id: string
  address: string
  lat: number | null
  lon: number | null
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
  hdType?: string | null // едет из заявки как есть: правка его не трогает
  // Объект и условия работы (PLAN 9.1). `null` — объекта нет вовсе: форма заводит его на первой
  // правке, иначе нетронутая заявка без объекта считалась бы изменённой.
  object: RequestObject | null
  gigabit: boolean
  repeatVisit: boolean
  cable: boolean
  drilling: boolean
  documentsMin: number
}

/** Бригада в форме. `address` — только ради поиска координат старта: в модели его нет. */
export interface EngineerDraft {
  id: string
  name: string
  address: string
  startLat: number | null
  startLon: number | null
  shiftStart: string
  shiftEnd: string
  lunchStart: string
  lunchEnd: string
  skills: Skill[]
  transport: Transport
  equipment: Equipment[]
  tools: Tool[]
  home?: Home | null
}

/** Черновик заявки: поля `from` поверх пустой обычной заявки. Списки копируются — правка
 * черновика не должна трогать заявку в плане. */
export function requestDraft(from: Partial<RequestDraft> = {}): RequestDraft {
  return {
    id: '',
    address: '',
    lat: null,
    lon: null,
    serviceDurationMin: 30,
    baseNormMin: 50,
    windowStart: '09:00',
    windowEnd: '18:00',
    workPriority: 'regular',
    skill: 'local',
    requiredTransport: null,
    urgent: false,
    gigabit: false,
    repeatVisit: false,
    cable: false,
    drilling: false,
    documentsMin: 0,
    ...from,
    object: from.object ? { ...from.object } : null,
    requiredEquipment: [...(from.requiredEquipment ?? [])],
    requiredTools: [...(from.requiredTools ?? [])],
  }
}

export function engineerDraft(from: Partial<EngineerDraft> = {}): EngineerDraft {
  return {
    id: '',
    name: '',
    address: '',
    startLat: null,
    startLon: null,
    shiftStart: '09:00',
    shiftEnd: '18:00',
    lunchStart: LUNCH_WINDOW.start,
    lunchEnd: LUNCH_WINDOW.end,
    transport: 'car',
    ...from,
    skills: [...(from.skills ?? [])],
    equipment: [...(from.equipment ?? [])],
    tools: [...(from.tools ?? [])],
  }
}

/**
 * Черновик строкой для вопроса «правили ли его»: «Сохранить изменения» в карточке видна, только
 * когда строка разошлась с исходной. Порядок списков не в счёт — снять и снова отметить пункт
 * не правка. Координаты заявки тоже: их задаёт адрес, правка адреса их сбрасывает, и адрес,
 * переписанный обратно, иначе считался бы правкой.
 */
export function draftKey(draft: RequestDraft | EngineerDraft): string {
  return JSON.stringify(
    Object.entries(draft)
      .filter(([key]) => key !== 'lat' && key !== 'lon')
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([key, value]) => [key, Array.isArray(value) ? [...value].sort() : value]),
  )
}

/** Общее поле «Оборудование» обратно в два списка модели: оборудование и инструменты. */
export function splitGear(codes: readonly string[]): { equipment: Equipment[]; tools: Tool[] } {
  return {
    equipment: codes.filter((code): code is Equipment => code in EQUIPMENT_NAMES),
    tools: codes.filter((code): code is Tool => code in TOOL_NAMES),
  }
}

/** Строка — время `HH:MM`, как его требуют модели (PLAN 4.2). */
export function isClock(value: string): boolean {
  return /^\d{2}:\d{2}$/.test(value)
}

/** `HH:MM` строками сравниваются как числа — тот же порядок, что у моделей. */
function before(start: string, end: string): boolean {
  return isClock(start) && isClock(end) && start < end
}

/**
 * Почему форма не отправилась: фраза и поля черновика, которые подсвечиваются красным. Значения
 * полей запоминаются, чтобы подсветка снималась с поля, как только его поправили (`invalidFields`).
 */
export interface FormProblem {
  text: string
  fields: Record<string, string>
}

export function formProblem<Draft extends object>(text: string, draft: Draft, ...fields: (keyof Draft & string)[]): FormProblem {
  return { text, fields: Object.fromEntries(fields.map((field) => [field, JSON.stringify(draft[field])])) }
}

/** Поля, которые всё ещё красные: с тех пор как форма не отправилась, их не меняли. */
export function invalidFields(problem: FormProblem | null, draft: object): string[] {
  if (!problem) return []
  const values = draft as Record<string, unknown>
  return Object.keys(problem.fields).filter((field) => JSON.stringify(values[field]) === problem.fields[field])
}

/**
 * Готовое тело заявки или то, чего не хватает. Проверки — те, что иначе вернулись
 * бы от сервера 422 без понятной причины: очищенное число, окно, координаты.
 */
export function requestBody(draft: RequestDraft): Request | FormProblem {
  if (!draft.id.trim() || !draft.address.trim()) return formProblem('Нужны номер заявки и адрес', draft, 'address')
  // Очищенное поле `type="number"` даёт пустую строку, а не ноль.
  if (!(draft.serviceDurationMin > 0) || !(draft.baseNormMin > 0)) {
    return formProblem('Длительность работы и норматив — целые минуты больше нуля', draft)
  }
  if (draft.object && (['floor', 'floors', 'entrance', 'flatNumber', 'flats'] as const).some((key) => (draft.object![key] ?? null) !== null && !(draft.object![key]! >= 1))) {
    return formProblem('Этаж, этажность, подъезд, квартира и квартиры в подъезде — целые числа от единицы или пусто', draft, 'object')
  }
  if (draft.object?.floor && draft.object.floors && draft.object.floor > draft.object.floors) {
    return formProblem('Этаж выше, чем этажей в доме', draft, 'object')
  }
  if (!before(draft.windowStart, draft.windowEnd)) {
    return formProblem('Окно заявки: начало должно быть раньше конца', draft, 'windowStart', 'windowEnd')
  }
  if (draft.lat === null || draft.lon === null) {
    return formProblem('Нужны координаты: «Найти по адресу» или точка на карте', draft, 'address')
  }
  return {
    ...draft,
    id: draft.id.trim(),
    address: draft.address.trim(),
    lat: draft.lat,
    lon: draft.lon,
    requiredEquipment: [...draft.requiredEquipment],
    requiredTools: [...draft.requiredTools],
  }
}

export function engineerBody(draft: EngineerDraft): Engineer | FormProblem {
  if (!draft.id.trim() || !draft.name.trim()) return formProblem('Нужна фамилия инженера', draft, 'name')
  if (!draft.skills.length) return formProblem('У инженера должен быть хотя бы один навык', draft, 'skills')
  if (!before(draft.shiftStart, draft.shiftEnd)) {
    return formProblem('Смена инженера: начало должно быть раньше конца', draft, 'shiftStart', 'shiftEnd')
  }
  if (!before(draft.lunchStart, draft.lunchEnd) || clockMinutes(draft.lunchEnd) - clockMinutes(draft.lunchStart) < LUNCH_MIN) {
    return formProblem(`Окно обеда: не короче ${LUNCH_MIN} минут`, draft, 'lunchStart', 'lunchEnd')
  }
  if (draft.startLat === null || draft.startLon === null) {
    return formProblem('Нужны координаты старта: «Найти по адресу» или точка на карте', draft)
  }
  return {
    id: draft.id.trim(),
    name: draft.name.trim(),
    startLat: draft.startLat,
    startLon: draft.startLon,
    shiftStart: draft.shiftStart,
    shiftEnd: draft.shiftEnd,
    lunchStart: draft.lunchStart,
    lunchEnd: draft.lunchEnd,
    skills: [...draft.skills],
    transport: draft.transport,
    equipment: [...draft.equipment],
    tools: [...draft.tools],
    home: draft.home ?? null,
    // Состояние дня, а не свойство бригады: его ставит пересчёт по событию (PLAN 6.12).
    availableFrom: null,
    unavailableFrom: null,
  }
}
