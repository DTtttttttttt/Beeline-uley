// Атрибут `title` показывается не системной подсказкой браузера, а такой же карточкой, как у значков
// (Tip.vue): снизу, по правому краю элемента, у нижнего края экрана — сверху. Только при наведении
// мышью — на телефоне системной подсказки и так нет. Пока подсказка открыта, у элемента `title=""`:
// пустой атрибут гасит системную подсказку, а смену текста Vue (`:title`) ловит наблюдатель.
// Пустой `title` (Tip.vue) по-прежнему значит «подсказки нет» — и у родителя тоже.
export default defineNuxtPlugin(() => {
  const tip = document.createElement('div')
  tip.setAttribute('role', 'tooltip')
  tip.className =
    'pointer-events-none fixed z-50 w-max max-w-65 rounded-card bg-panel px-3.5 py-2.5 text-sm font-normal text-text shadow-panel transition duration-150 opacity-0 scale-95'
  document.body.append(tip)

  let owner: HTMLElement | null = null
  let text = ''
  const watcher = new MutationObserver(() => {
    const now = owner?.getAttribute('title')
    if (now === '') return
    if (now == null) return hide()
    text = now
    owner!.setAttribute('title', '')
    place()
  })

  function place() {
    tip.textContent = text
    const rect = owner!.getBoundingClientRect()
    const { offsetWidth: width, offsetHeight: height } = tip
    const left = Math.max(8, Math.min(rect.right < width + 8 ? rect.left : rect.right - width, window.innerWidth - width - 8))
    const below = rect.bottom + 6 + height <= window.innerHeight - 8
    tip.style.left = `${left}px`
    tip.style.top = `${below ? rect.bottom + 6 : rect.top - 6 - height}px`
    tip.style.transformOrigin = `${left < rect.left ? 'left' : 'right'} ${below ? 'top' : 'bottom'}`
  }

  function show(element: HTMLElement) {
    owner = element
    text = element.getAttribute('title')!
    element.setAttribute('title', '')
    watcher.observe(element, { attributes: true, attributeFilter: ['title'] })
    place()
    tip.classList.remove('opacity-0', 'scale-95')
  }

  function hide() {
    if (!owner) return
    watcher.disconnect()
    if (owner.getAttribute('title') === '') owner.setAttribute('title', text)
    owner = null
    tip.classList.add('opacity-0', 'scale-95')
  }

  document.addEventListener('pointerover', (event) => {
    if (event.pointerType !== 'mouse') return
    const element = (event.target as Element).closest<HTMLElement>('[title]')
    if (element === owner) return
    hide()
    if (element?.getAttribute('title')) show(element)
  })
  document.addEventListener('pointerout', (event) => {
    if (owner && !owner.contains(event.relatedTarget as Node | null)) hide()
  })
  // Нажатие часто открывает окно или прячет элемент, а уход курсора со спрятанного не придёт.
  document.addEventListener('pointerdown', hide)
  window.addEventListener('scroll', hide, true)
})
