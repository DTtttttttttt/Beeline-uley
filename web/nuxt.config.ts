import tailwindcss from '@tailwindcss/vite'

// https://nuxt.com/docs/api/configuration/nuxt-config
export default defineNuxtConfig({
  compatibilityDate: '2025-07-15',
  devtools: { enabled: false },
  app: {
    head: {
      htmlAttrs: { lang: 'ru' },
      title: 'билайн улей',
      meta: [
        { name: 'description', content: 'билайн улей — панель диспетчера: распределение заявок по бригадам на день, маршруты на карте, объяснения решений и перепланирование по событиям.' },
        { property: 'og:type', content: 'website' },
        { property: 'og:url', content: 'https://beeline-uley.ru/' },
        { property: 'og:image', content: 'https://beeline-uley.ru/og-image.png' },
        { property: 'og:image:width', content: '1200' },
        { property: 'og:image:height', content: '630' },
        { property: 'og:site_name', content: 'билайн улей' },
        { property: 'og:title', content: 'билайн улей — планировщик выездов инженеров' },
        { property: 'og:description', content: 'Распределение заявок по бригадам, маршруты на карте и перепланирование в течение дня.' }
      ],
      link: [
        { rel: 'icon', type: 'image/png', href: '/favicon.png' },
        { rel: 'icon', href: '/favicon.ico', sizes: 'any' }
      ]
    }
  },
  css: ['~/assets/css/main.css'],
  vite: {
    plugins: [tailwindcss()],
  },
  runtimeConfig: {
    public: {
      // перекрываются переменными NUXT_PUBLIC_API_BASE и NUXT_PUBLIC_DGIS_KEY
      apiBase: 'http://localhost:8000',
      dgisKey: ''
    }
  }
})
