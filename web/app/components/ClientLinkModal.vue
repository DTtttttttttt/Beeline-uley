<script setup lang="ts">
// «Ссылка для клиента» (раздел 1.3 задания, кадр 1793): QR — клиент наводит телефон и попадает на
// страницу оценки; под ним — подсказка, внизу «Скопировать ссылку». Ссылка подписана сервером и
// ведёт на `PUBLIC_BASE_URL`: с `localhost` телефон её не откроет. Сама ссылка строкой видна, только
// если буфер обмена закрыт (не https и не localhost) — тогда её можно выделить.
import QRCode from 'qrcode'

const props = defineProps<{ link: string }>()
const emit = defineEmits<{ back: [] }>()

const image = ref('')
watch(
  () => props.link,
  (link) =>
    QRCode.toDataURL(link, { margin: 0, width: 590, color: { dark: '#28303f', light: '#ffffff' } }).then(
      (url) => (image.value = url),
      () => (image.value = ''),
    ),
  { immediate: true },
)

const copied = ref(false)
const refused = ref(false)
async function copy() {
  try {
    await navigator.clipboard.writeText(props.link)
    copied.value = true
  } catch {
    refused.value = true
  }
}
</script>

<template>
  <!-- Кадр 1793: окно 426 px, QR 295, подсказка 16 серым по центру. -->
  <Modal title="Ссылка для клиента" :width="426" @back="emit('back')">
    <img v-if="image" :src="image" alt="QR-код ссылки на оценку" class="mx-auto mt-4 size-[295px]" />
    <div v-else class="skeleton mx-auto mt-4 size-[295px] rounded-card" />
    <p class="muted m-0 mt-3.5 text-center">Покажите клиенту QR-код или отправьте ему ссылку. По ней он сможет оценить работу инженера</p>
    <p v-if="refused" class="muted small m-0 text-center break-all select-all">{{ link }}</p>
    <template #footer>
      <button class="btn primary wide mt-7" @click="copy">
        <Icon :name="copied ? 'check' : 'copy'" /> {{ copied ? 'Скопировано' : 'Скопировать ссылку' }}
      </button>
    </template>
  </Modal>
</template>
