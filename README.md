# VD Audio Stack (Windows) — Mediasoup + Egress + Whisper (Python) — Versão com **GStreamer**

Este documento explica (para devs juniores) como funciona nosso servidor **Node.js** com **mediasoup**, como o áudio sai do navegador via **WebRTC**, como fazemos **egress** (extração do áudio) para gerar arquivos **WAV 16k mono**, e como esses WAVs são enviados para o servidor **Python /process** (que faz transcrição + insights).

> **Importante:** sem **egress** (ou sem “chunks HTTP”), você pode estar enviando áudio via WebRTC, mas **nada vai virar arquivo/Whisper**, então não aparece transcrição — e muitas vezes não dá erro mesmo.

---

## 1) Glossário rápido (termos técnicos)

- **WebRTC**: tecnologia do browser para enviar áudio/vídeo em tempo real (baixa latência).
- **mediasoup**: servidor SFU (Selective Forwarding Unit). Ele recebe mídia WebRTC do browser e permite roteamento/controle (producers/consumers/transports).
- **Producer**: “publicador” de mídia. Ex.: o browser do vendedor publica áudio → isso vira um `producer` no servidor.
- **Consumer**: “assinante” de mídia. Ex.: seu egress cria um `consumer` server-side para receber o áudio daquele producer.
- **Transport**: canal de transporte de mídia. No browser normalmente é WebRTC; no egress usamos **PlainTransport** (RTP/RTCP “cru”, local).
- **RTP/RTCP**: protocolos do áudio “em pacotes”. RTP leva mídia; RTCP leva feedback/controle.
- **Egress**: “tirar” a mídia do mediasoup e transformar em algo útil (WAV) para mandar pro Whisper.
- **Diarização**: separar “quem falou o quê” (speaker separation). No futuro: vendedor vs lead.

---

## 2) Arquitetura do sistema (visão geral)

### Caminho A — **Chunks HTTP** (modo compat / atual)
1. Browser grava (MediaRecorder) e envia pequenos arquivos (`.webm/.ogg`) via `POST /upload-audio`.
2. Node encaminha o arquivo para o Python (`POST PYTHON_URL=/process`).
3. Python normaliza, transcreve, gera insights e retorna.
4. Node faz broadcast via WebSocket para a sala (room) atual.

✅ É simples e funciona mesmo sem mediasoup.  
❌ Latência e qualidade podem variar; não é “tempo real” de verdade.

### Caminho B — **WebRTC + mediasoup + Egress (GStreamer)** (tempo real de verdade)
1. Browser envia áudio por WebRTC para o mediasoup (via signaling WS).
2. Node cria um **PlainTransport** e um **Consumer server-side** ligado ao `Producer`.
3. O mediasoup manda RTP/Opus para uma porta UDP local.
4. O **GStreamer** escuta essa porta e converte para **WAV 16k mono** segmentado.
5. Cada WAV gerado é enviado ao Python `/process`.
6. Python retorna transcrição/insights e o Node faz broadcast via WS.

✅ É o caminho “certo” para tempo real com mediasoup.  
✅ Mantém WebRTC de verdade (baixa latência).  
⚠️ Exige configuração e pipeline estável no Windows.

---

## 3) O que significam estas ações do mediasoup (no WebSocket)

### `getRouterRtpCapabilities`
O browser pergunta: “quais codecs e capacidades de RTP esse servidor aceita?”  
O servidor responde com `router.rtpCapabilities`, e o cliente usa isso para “negociar” o WebRTC.

### `createWebRtcTransport`
O browser pede: “cria um transporte WebRTC pra eu conectar”.  
O servidor cria um `WebRtcTransport` (ICE/DTLS) e devolve parâmetros.

### `connectTransport`
O browser manda os `dtlsParameters` para finalizar o handshake DTLS do transporte.  
Sem isso, o transporte não fica pronto para enviar mídia.

### `produce`
O browser fala: “vou publicar uma track de áudio neste transporte”.  
O servidor cria um `Producer` e devolve `producer.id`.

> **É nesse ponto** que você pode auto-iniciar egress (AUTO_EGRESS) ou iniciar manualmente com `startEgress`.

---

## 4) Instalar **GStreamer no Windows** e colocar no PATH

### O que você precisa ter no sistema
Você precisa que o executável exista:
- `gst-launch-1.0.exe`

Baixe ele em: https://gstreamer.freedesktop.org/download/#windows

Na prática, ele costuma ficar numa pasta como:
- `C:\gstreamer\1.0\msvc_x86_64\bin`
ou
- `C:\Program Files\Gstreamer\1.0\msvc_x86_64\bin`

> Muitos guias recomendam baixar e instalar **dois MSI**: runtime + devel, e depois ajustar o PATH para o `...\bin`. :contentReference[oaicite:0]{index=0}

### Como colocar no PATH (3 jeitos)

#### Jeito 1) PATH permanente (Windows GUI)
1. Abra **System Properties** → **Environment Variables**
2. Em “System variables” (ou “User variables”), encontre `Path`
3. **Add**:
   - `C:\Program Files\Gstreamer\1.0\msvc_x86_64\bin`
4. Feche e reabra o terminal.

#### Jeito 2) PATH temporário (somente nesse terminal)
PowerShell:
$env:Path = "C:\Program Files\Gstreamer\1.0\msvc_x86_64\bin;" + $env:Path
gst-launch-1.0 --version

Teste rápido
No terminal:

Rode:
gst-launch-1.0 --version
Se isso funcionar, o Node consegue spawnar o GStreamer.

5) Por que “sem chunks HTTP” não transcreveu nada (e não deu erro)?
Porque WebRTC por si só não gera WAV.

Se você desmarca “chunks HTTP” no front, você para de enviar arquivos via /upload-audio.

Se você não iniciar o egress, o áudio fica “preso” no mediasoup (como fluxo RTP), sem virar arquivo, sem ir para o Python.

Resultado: nenhum WAV, nenhum POST para o Python, portanto nenhuma transcrição — e o servidor pode ficar “silencioso”, sem erro.

✅ Conclusão prática:

Ou você usa chunks HTTP,

ou você usa mediasoup + egress,

ou você usa os dois (por compat/debug), mas não é obrigatório.

6) Como o Egress com GStreamer funciona (pipeline)
Pipeline que estamos usando (simples e estável):

udpsrc (RTP/Opus) → rtpjitterbuffer → rtpopusdepay → opusdec → audioconvert → audioresample
→ audio/x-raw,rate=16000,channels=1
→ splitmuxsink muxer=wavenc max-size-time=...

udpsrc: abre uma porta UDP e recebe RTP.

caps: diz “isso aqui é RTP de áudio Opus”.

rtpjitterbuffer: reduz problemas de reordenação/variação de rede.

rtpopusdepay: remove cabeçalho RTP e extrai Opus “puro”.

opusdec: decodifica Opus → PCM (raw audio).

audioresample: converte 48k → 16k.

splitmuxsink: segmenta em arquivos; com muxer=wavenc ele escreve WAV.

7) O que cada arquivo do projeto faz
server.js (Node)
HTTP:

GET /health

POST /upload-audio (modo chunks HTTP)

WebSocket:

rooms (joinRoom)

signaling mediasoup (getRouterRtpCapabilities, createWebRtcTransport, connectTransport, produce)

egress control (startEgress, stopEgress)

Egress:

cria PlainTransport e Consumer

spawn do GStreamer e geração de WAV segmentado

polling no diretório para detectar WAVs e enviar ao Python

index.html (Front)
Conecta no WS

Entra em uma sala (joinRoom)

Captura microfone

Duas formas de envio:

MediaRecorder → chunks HTTP /upload-audio

WebRTC → mediasoup produce

Botões:

“Enviar chunks HTTP (compat Whisper atual)”

“Auto iniciar Egress (após produzir)”

“Iniciar Egress / Parar Egress”

python server (/process) (FastAPI)
Recebe áudio (webm/wav etc)

Normaliza para WAV 16k

Transcreve

Mantém contexto/memória/insights

Retorna JSON para o Node (que faz broadcast)

8) Checklist de inicialização (Windows)

Python
cd Server_Pyt
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn audio_analysis_server:app --host 0.0.0.0 --port 8000

Node
cd 'MS node'
npm install
node server.js

Front:
Para rodar o index.html rode:
python -m http.server 5173.

Clique para conectar no WS.

Faça joinRoom.

Publique áudio (produce).

Inicie Egress (ou ligue AUTO_EGRESS).

DESAFIO (para o time) — Suportar Egress com GStreamer OU FFmpeg no mesmo servidor
Objetivo: o server.js poder usar um dos dois engines de egress dependendo de variável de ambiente.

Requisitos do desafio
Criar um EGRESS_ENGINE no .env com valores:

gst (GStreamer)

ffmpeg (FFmpeg)

Fazer um “wrapper”:

spawnEgress({ engine, ... }) decide qual pipeline usar

Manter o mesmo contrato:

gerar WAV 16k mono segmentado em EGRESS_DIR

polling detecta e envia pro Python

Permitir fallback:

se gst falhar (exit != 0), opcionalmente tentar ffmpeg

Log claro:

printar engine, rtpPort, payloadType, caminho do bin.

Dicas práticas (sem “inventar moda”)
Não misture lógica de polling e lógica de engine; engine só gera arquivos.

Centralize a parte “mediasoup → PlainTransport → Consumer” (isso é comum).

Separe os detalhes:

FFmpeg: SDP/pipes/args

GStreamer: udpsrc caps + pipeline

Critério de sucesso
Com EGRESS_ENGINE=gst: gera WAV e transcreve.

Com EGRESS_ENGINE=ffmpeg: gera WAV e transcreve.

Sem chunks HTTP: continua transcrevendo usando só egress.

Observação final (o “mais importante”)
Sim, você ainda está usando mediasoup quando está no caminho WebRTC:

O áudio chega como Producer via WebRTC

O egress cria um Consumer server-side

O GStreamer/FFmpeg é só a “ponte” para transformar RTP em WAV para o Whisper.

Sem egress, mediasoup funciona… mas você não “materializa” áudio para o Whisper.

## 9) Espaço para a extensão (o que o time precisa saber antes de implementar)

A ideia aqui é deixar **100% claro**:
1) **onde o áudio nasce**
2) **por onde ele passa**
3) **como ele chega no Python**
4) **qual formato o Python espera**
5) **quais endpoints existem**
6) **como deve ser a requisição (contrato HTTP)**

---

### 9.1 Arquitetura dos servidores (quem faz o quê)

#### **Front (browser)**
- Captura microfone.
- Envia áudio por **dois caminhos possíveis**:
  1) **Chunks HTTP** (MediaRecorder) → `POST /upload-audio` (no Node)
  2) **WebRTC** (mediasoup-client) → WS signaling + RTP (no mediasoup) → **Egress** (GStreamer/FFmpeg) → WAV → Python

#### **Node.js (server.js)**
- É o **orquestrador**:
  - Recebe chunks via HTTP e encaminha ao Python
  - Faz signaling do mediasoup via WebSocket
  - Inicia/para o egress (GStreamer/FFmpeg)
  - Faz broadcast dos resultados (insights/transcrição) via WS pra sala

> **Node NÃO transcreve.** Ele apenas encaminha áudio para o Python e repassa resultados para o front.

#### **Python (FastAPI)**
- É o **processador**:
  - Recebe áudio como upload (multipart)
  - Normaliza (para WAV 16k mono)
  - Transcreve (Whisper / pipeline)
  - Atualiza memória/insights
  - Responde JSON com `chunk_id`, `gate`, `new_insights`, etc.

---

### 9.2 Fluxos de áudio (de ponta a ponta)

#### Fluxo A — Chunks HTTP (compat atual)
1. Browser grava um pedacinho (chunk) em `.webm` (ou `.ogg`)
2. Browser faz `POST http://NODE_HOST:3000/upload-audio` com `multipart/form-data`
3. Node recebe e encaminha para `POST http://PY_HOST:8000/process`
4. Python responde JSON
5. Node faz broadcast via WS para a `roomId`

✅ Aqui o áudio **chega primeiro no Node**, depois vai pro Python.

---

#### Fluxo B — WebRTC + mediasoup + Egress (GStreamer/FFmpeg)
1. Browser publica áudio via mediasoup-client (WebRTC)
2. Node cria `Producer` (do lado servidor)
3. Egress é iniciado (manual ou AUTO_EGRESS)
4. Node cria `PlainTransport` + `Consumer`
5. mediasoup envia RTP/Opus para `127.0.0.1:rtpPort`
6. GStreamer/FFmpeg converte RTP → WAV 16k mono segmentado (ex.: 5s)
7. Node detecta cada WAV, e faz `POST http://PY_HOST:8000/process`
8. Python responde JSON
9. Node faz broadcast via WS para a `roomId`

✅ Aqui o áudio **não chega como arquivo no Node**.  
Ele chega como **RTP via mediasoup**, e só vira arquivo após o egress.

---

### 9.3 Como o áudio deve chegar no Python (formatos aceitos)

O endpoint Python (`/process`) deve aceitar **upload de arquivo** via `multipart/form-data`:

- Preferencial: `.wav` PCM (16 kHz, mono) — **melhor para Whisper**
- Também pode aceitar (no modo chunks HTTP):
  - `.webm` / `.ogg` (normalização no Python com ffmpeg/soundfile/etc)

**Recomendação para estabilidade:**
- Egress sempre gerar WAV 16k mono.
- Chunks HTTP podem continuar mandando webm/ogg enquanto o egress não estiver 100%.

---

### 9.4 Endpoints do Node (contratos)

#### `GET /health`
- Uso: healthcheck
- Resposta:
```json
{ "ok": true }
POST /upload-audio (chunks HTTP)
Uso: fallback/compat

Content-Type: multipart/form-data

Campos esperados:

audio (arquivo): chunk webm/ogg/wav

roomId (string): sala

seq (string|int opcional): sequência do chunk

timestamp (string|int opcional)

clientId (string opcional)

context_hint (string opcional)

Exemplo (curl):


curl -X POST http://localhost:3000/upload-audio \
  -F "audio=@chunk.webm" \
  -F "roomId=room-1" \
  -F "seq=1" \
  -F "timestamp=1700000000000" \
  -F "clientId=browser-abc" \
  -F "context_hint=seller"
O Node:

recebe o arquivo

encaminha ao Python /process

retorna o JSON do Python

9.5 Endpoints do Python (contratos)
POST /process
Uso: processar áudio e retornar insights/transcrição

Content-Type: multipart/form-data

Campos mínimos:

audio (arquivo): chunk (webm/ogg/wav)

roomId (string)

timestamp (string|int)

seq (string|int)

Campos úteis (recomendados para evolução):

clientId (string)

context_hint (string) → ex.: seller, lead, egress peer=... producer=...

(futuro) track (string): seller / lead

(futuro) channel (int): 0 / 1 (para áudio estéreo separado)

Exemplo (curl):


curl -X POST http://localhost:8000/process \
  -F "audio=@segment_00001.wav" \
  -F "roomId=room-1" \
  -F "seq=1700000000001" \
  -F "timestamp=1700000000001" \
  -F "context_hint=egress role=seller"
Resposta típica (exemplo):


{
  "chunk_id": "room-1_1700000000001",
  "gate": "pass",
  "new_insights": [
    { "type": "insight", "text": "Lead demonstrou interesse em preço." }
  ],
  "memory_state": { "summary": "..." },
  "meta": { "received_at": "..." }
}
Observação: os nomes exatos podem variar conforme seu Python atual, mas a estrutura geral é essa (chunk_id, gate, insights, memory_state).

9.6 O que chega antes: Node ou Python?
Depende do modo:

Chunks HTTP (MediaRecorder):

Áudio chega primeiro no Node (/upload-audio)

Node encaminha para Python (/process)

Egress (GStreamer/FFmpeg):

Áudio chega primeiro no mediasoup como WebRTC (não é arquivo)

Depois vira WAV no egress

Aí o Node manda WAV para o Python (/process)

9.7 Como deve ser a requisição “padrão” para o futuro (contrato recomendado)
Para preparar diarização / 2 faixas (seller + lead) no futuro, padronize estes campos SEM mudar nada agora:

Sempre enviar para o Python:

roomId

seq

timestamp

context_hint

E adicionar já (mesmo que por enquanto seja “unknown”):

role: seller | lead | unknown

source: http_chunks | egress_gst | egress_ffmpeg

producerId: id do producer no mediasoup (quando vier de egress)

(futuro) trackId: seller/lead (quando houver 2 tracks reais)

Exemplo de context_hint recomendado:

source=egress_gst role=seller peer=<peerId> producer=<producerId>

Isso permite:

o Python saber quem é quem

o Python saber de onde veio

log/troubleshooting melhor

diarização e atribuição por speaker no futuro

9.8 Onde implementar a extensão (checklist de tarefas)
No Node:
Adicionar EGRESS_ENGINE (gst ou ffmpeg)

Fazer spawnEgress(engine, ...)

Garantir que o egress sempre gere WAV em EGRESS_DIR

Garantir que cada WAV é enviado via forwardWavToPython()

Garantir logs: engine, rtpPort, payloadType, wavPattern

No Python:
Confirmar que /process aceita .wav 16k mono

Confirmar normalização quando receber .webm

Preparar estrutura para diarização:

se receber role=seller/lead em separado → armazenar transcrições por canal

se receber estéreo → separar canais

9.9 Critério de sucesso para a extensão
Sem marcar “chunks HTTP” e com egress ligado:

deve gerar WAV no EGRESS_DIR

deve fazer POST no Python /process

deve aparecer transcrição/insights no WS do front

Com “chunks HTTP” marcado e sem egress:

deve continuar funcionando como fallback

Com os dois ligados:

deve funcionar, mas é esperado duplicar (a menos que você faça dedupe por source)
