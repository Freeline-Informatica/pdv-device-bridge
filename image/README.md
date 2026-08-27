# Imagens Raspberry Pi OS Lite arm64

As imagens usam o gerador oficial `raspberrypi/rpi-image-gen` e são produzidas separadamente para Pi 3, Pi 4 e Pi 5. Execute em um host Raspberry Pi OS 64-bit atualizado:

```sh
sudo ./image/build-images.sh \
  https://devices.freeline.example \
  https://downloads.freeline.example/pdv-device-bridge-0.2.0-arm64.tar.gz \
  SHA256_DO_BUNDLE \
  https://downloads.freeline.example/release-public-key.pem
```

Antes de ligar o aparelho, grave o JSON individual emitido pelo dashboard como `pdv-device-bridge.json` na partição de boot. Ethernet usa DHCP. Wi-Fi e a chave pública emergencial devem ser configurados pelo Raspberry Pi Imager; autenticação SSH por senha e login root ficam desabilitados.

No primeiro boot, o serviço instala o bundle verificado, consome e apaga o provisionamento e ativa bridge, agente e Avahi. O serviço operacional não abre nenhuma porta de administração na internet.
