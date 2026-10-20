<script setup lang="ts">
// server: false — запрос идёт из браузера: apiBase указывает на хост, а не на сеть compose.
// Состояние сервисов в макете не показывается; видна только беда — бэкенд или Mongo недоступны.
const apiBase = useRuntimeConfig().public.apiBase
// Страница клиента (блок 70) — не панель диспетчера: адрес API клиенту ни к чему.
const route = useRoute()
const { data: health, status } = await useFetch<{ status: string }>(`${apiBase}/api/health`, {
  server: false,
})
</script>

<template>
  <template v-if="route.path.startsWith('/rate/')"></template>
  <p v-else-if="status === 'error'" class="fixed bottom-4 left-1/2 z-50 m-0 -translate-x-1/2 rounded-card bg-danger px-4 py-2 text-white">
    Бэкенд недоступен: {{ apiBase }} не отвечает
  </p>
  <p v-else-if="health && health.status !== 'ok'" class="fixed bottom-4 left-1/2 z-50 m-0 -translate-x-1/2 rounded-card bg-danger px-4 py-2 text-white">
    Mongo недоступна: наборы и планы не читаются
  </p>
  <NuxtPage />
</template>
