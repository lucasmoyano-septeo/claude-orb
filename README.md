# claude-orb

Un botón flotante en tu escritorio Ubuntu para hablar con Claude Code por voz,
tipo walkie-talkie: mantén pulsado, habla, suelta. Claude escucha, mira una
captura de tu pantalla en ese instante, y te contesta hablando.

<p align="center">🎙️ → 👀 → 🧠 → 🔊</p>

## Qué hace en cada turno

1. Mantienes pulsado el orbe → graba tu voz.
2. Sueltas → transcribe con Whisper (local, sin internet) y toma una captura
   de pantalla al mismo tiempo.
3. Un marco rojo parpadea medio segundo alrededor de toda la pantalla justo
   después de capturarla, para que sepas en qué momento "miró". El marco se
   dibuja **después** de la captura, así nunca aparece dentro de la imagen
   que Claude realmente ve.
4. Se lo manda todo a una sesión de Claude Code (modelo Sonnet, sin memoria
   ni MCP de más, para que sea rápido) con permiso libre para usar `xdotool`
   (ratón/teclado) e `import` (capturas) por su cuenta.
5. Lee la respuesta en voz alta con `edge-tts`, y el orbe se mueve al ritmo
   real del audio (no es una animación decorativa: lee la envolvente de
   volumen del propio mp3).
6. Puedes interrumpirlo en cualquier momento — mientras piensa o mientras
   habla — con solo volver a pulsar el botón.

## ⚠️ Antes de instalarlo, lee esto

Este asistente puede **mover tu ratón, escribir con tu teclado y hacer clic
por su cuenta, sin pedirte confirmación**. Es una decisión de diseño
deliberada (acción libre en vez de confirmar cada paso), no un descuido.
Si prefieres que pida permiso antes de tocar algo, quita la frase de
"libertad total... sin pedir confirmación" en `SYSTEM_PROMPT` dentro de
`button.py` y dile en su lugar que confirme antes de actuar.

También manda una captura de tu pantalla completa a Claude en cada turno.
Si tienes algo sensible abierto, no lo uses en ese momento — el marco rojo
existe justamente para que sepas cuándo está mirando.

## Requisitos

- Ubuntu (o similar) con sesión **X11** (no Wayland — usa `xdotool`, que no
  funciona en Wayland sin capas adicionales).
- Paquetes de sistema: `xdotool`, `imagemagick` (para `import`), `ffmpeg`,
  `alsa-utils` (para `arecord`), `python3-gi` + `gir1.2-gtk-3.0` (GTK3 desde
  Python).
- Python 3.10+.
- La CLI `claude` (Claude Code) instalada y con sesión iniciada.

```bash
sudo apt install xdotool imagemagick ffmpeg alsa-utils python3-gi gir1.2-gtk-3.0
```

## Instalación

```bash
git clone https://github.com/lucasmoyano-septeo/claude-orb.git
cd claude-orb
python3 -m venv venv --system-site-packages
./venv/bin/pip install faster-whisper edge-tts
chmod +x run.sh button.py
```

## Uso

```bash
bash run.sh
```

Aparece un orbe circular flotante abajo a la derecha de la pantalla.

| Estado | Color | Qué significa |
|---|---|---|
| Reposo | gris, respira despacio | listo para escuchar |
| Grabando | rojo, con ondas expandiéndose | mantén pulsado y habla |
| Pensando | ámbar, puntos rebotando | esperando la respuesta de Claude |
| Hablando | verde, barras que siguen el audio real | reproduciendo la respuesta |

Para parar la app: `pkill -f button.py`, o cierra la ventana desde tu barra
de tareas.

## Por qué es rápido (o por qué no lo es)

El turno usa `--model sonnet` y varios flags que quitan lo que no hace falta
para una pregunta rápida de voz (memoria/CLAUDE.md, servidores MCP, listado
de skills, integración de Chrome). Con eso, una pregunta simple sin captura
tarda entre 3 y 6 segundos en tener respuesta. Con captura de pantalla, algo
más, porque el modelo tiene que leer la imagen. Y si Claude decide usar sus
herramientas por su cuenta (mirar algo con más cuidado, mover el ratón),
tarda lo que tarde en hacerlo de verdad — eso no es un cuello de botella que
se arregle con flags, es el precio de darle libertad de acción.

## Estructura

```
button.py   -- la app entera: ventana flotante, dibujo del orbe, grabación,
               transcripción, invocación de Claude Code, texto a voz.
run.sh      -- lanzador (evita procesos duplicados).
```

## Licencia

MIT. Ver `LICENSE`.
