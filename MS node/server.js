// server.js
// -----------------------------------------------------------------------------
// Servidor Node.js com:
// 1) HTTP API (Express)
//    - /health
//    - /upload-audio: recebe chunks via multipart e encaminha ao Python
//
// 2) WebSocket (ws)
//    - joinRoom(roomId) para broadcast
//    - signaling mediasoup (WebRTC)
//    - startEgress/stopEgress: egress RTP/Opus -> GStreamer -> WAV 16k mono -> Python
//
// 3) Mediasoup + EGRESS para Whisper
//    - WebRTC publish do browser (mediasoup-client)
//    - PlainTransport (RTP local) + GStreamer para WAV 16k mono segmentado
//    - forward para Python /process
//
// -----------------------------------------------------------------------------
// Requisitos:
// - GStreamer instalado (gst-launch-1.0 no PATH) OU GST_BIN apontando pro .exe
// - Python /process rodando (PYTHON_URL)
// -----------------------------------------------------------------------------
//
// ✅ Fixes Windows aplicados:
// - GST_BIN padrão apontando para "C:\Program Files\Gstreamer\1.0\msvc_x86_64\bin\gst-launch-1.0.exe"
// - Portas livres com UDP real (dgram), não TCP
// - udpsrc com address=127.0.0.1 (evita bind 0.0.0.0 que costuma dar erro de permissão no Windows)
//
// ✅ Correção principal nesta versão:
// - Retry robusto se o GStreamer falhar ao bind (porta “tomada”/bloqueada):
//   tenta novas portas automaticamente até MAX_EGRESS_PORT_RETRIES.
// - Logs mais úteis (sem -q) + imprime payloadType/clockRate/channels.
// - Pipeline conservador e estável (sem “inventar moda”):
//   udpsrc -> rtpjitterbuffer -> rtpopusdepay -> opusdec -> audioconvert -> audioresample
//   -> audio/x-raw,rate=16000,channels=1 -> queue -> splitmuxsink muxer=wavenc
//
// Observação:
// - Estamos ouvindo só RTP (rtpPort). RTCP (rtcpPort) é enviado pelo mediasoup, mas não é obrigatório
//   para decodificar o áudio. (Para robustez total: migrar para rtpbin no futuro.)

const express = require("express");
const http = require("http");
const { Server: WSServer } = require("ws");
const mediasoup = require("mediasoup");
const multer = require("multer");
const fs = require("fs");
const axios = require("axios");
const FormData = require("form-data");
const { v4: uuidv4 } = require("uuid");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");
const dgram = require("dgram");

/* =============================================================================
   0) Config e Bootstrap
============================================================================= */

const app = express();
const server = http.createServer(app);
const wss = new WSServer({ server });

// Upload fallback (modo atual: MediaRecorder -> /upload-audio)
fs.mkdirSync("tmp", { recursive: true });
const upload = multer({ dest: "tmp/" });

// Python processor (FastAPI)
const PYTHON_PROCESSOR_URL = process.env.PYTHON_URL || "http://localhost:8000/process";
const PYTHON_TIMEOUT_MS = Number(process.env.PYTHON_TIMEOUT_MS || 120000);

// ✅ GStreamer (Windows-safe)
const GST_BIN_DEFAULT = "C:\\Program Files\\Gstreamer\\1.0\\msvc_x86_64\\bin\\gst-launch-1.0.exe";
const GST_BIN = process.env.GST_BIN || GST_BIN_DEFAULT || "gst-launch-1.0";

// Egress: segmentar wav (cada arquivo ~ N segundos)
const EGRESS_CHUNK_SECONDS = Number(process.env.EGRESS_CHUNK_SECONDS || 5);

// pasta temporária de egress
const EGRESS_DIR = process.env.EGRESS_DIR || path.join(os.tmpdir(), "vd_egress");
fs.mkdirSync(EGRESS_DIR, { recursive: true });

// liga egress automaticamente ao produzir?
const AUTO_EGRESS = String(process.env.AUTO_EGRESS || "").toLowerCase() === "true" || process.env.AUTO_EGRESS === "1";

// limite de polling do watcher
const WATCH_POLL_MS = Number(process.env.WATCH_POLL_MS || 250);

// (Opcional) Jitterbuffer latency (ms)
const GST_JITTER_LATENCY_MS = Number(process.env.GST_JITTER_LATENCY_MS || 50);

// Retry de egress (se porta der bind failed / erro)
const MAX_EGRESS_PORT_RETRIES = Number(process.env.MAX_EGRESS_PORT_RETRIES || 10);
const GST_STARTUP_GRACE_MS = Number(process.env.GST_STARTUP_GRACE_MS || 400);

/* =============================================================================
   1) CORS + health
============================================================================= */
app.use((req, res, next) => {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET,POST,OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization");

  if (req.method === "OPTIONS") return res.sendStatus(204);
  next();
});

app.get("/health", (req, res) => res.json({ ok: true }));

/* =============================================================================
   2) Mediasoup config (Opus)
============================================================================= */

const mediasoupConfig = {
  worker: {
    rtcMinPort: Number(process.env.RTC_MIN_PORT || 20000),
    rtcMaxPort: Number(process.env.RTC_MAX_PORT || 30000),
    logLevel: process.env.MEDIASOUP_LOG_LEVEL || "warn",
    logTags: ["info", "ice", "dtls", "rtp", "srtp"],
  },
  routerOptions: {
    mediaCodecs: [
      {
        kind: "audio",
        mimeType: "audio/opus",
        clockRate: 48000,
        channels: 2,
      },
    ],
  },
  webRtcTransportOptions: {
    listenIps: [{ ip: "0.0.0.0", announcedIp: process.env.ANNOUNCED_IP || null }],
    enableUdp: true,
    enableTcp: true,
    preferUdp: true,
    initialAvailableOutgoingBitrate: 1000000,
  },

  // PlainTransport para egress RTP local
  // - comedia: false => o server "empurra" RTP pro ip/porta que você conectar
  // - rtcpMux: false => duas portas (RTP + RTCP)
  plainTransportOptions: {
    listenIp: "127.0.0.1",
    rtcpMux: false,
    comedia: false,
  },
};

let worker;
let router;

/* =============================================================================
   3) Estruturas de estado
============================================================================= */

const peers = new Map();
const rooms = new Map();

/**
 * egressSessions: producerId -> {
 *   roomId,
 *   peerId,
 *   role,
 *   producerId,
 *   plainTransport,
 *   consumer,
 *   gstProc,
 *   wavPrefix,
 *   pollTimer,
 *   seenFiles,
 *   startedAt,
 *   rtpPort,
 *   rtcpPort
 * }
 */
const egressSessions = new Map();

/* =============================================================================
   4) Helpers: WS + Rooms
============================================================================= */

function send(ws, msg) {
  try {
    ws.send(JSON.stringify(msg));
  } catch (_) {}
}

function joinRoom(peerId, roomId) {
  const p = peers.get(peerId);
  if (!p) return;

  if (p.roomId) {
    const setOld = rooms.get(p.roomId);
    if (setOld) {
      setOld.delete(peerId);
      if (setOld.size === 0) rooms.delete(p.roomId);
    }
  }

  p.roomId = roomId;

  if (!rooms.has(roomId)) rooms.set(roomId, new Set());
  rooms.get(roomId).add(peerId);
}

function broadcastToRoom(roomId, payload) {
  const set = rooms.get(roomId);
  if (!set || set.size === 0) return;

  for (const peerId of set.values()) {
    const p = peers.get(peerId);
    if (!p?.ws) continue;
    send(p.ws, payload);
  }
}

/* =============================================================================
   5) Mediasoup init
============================================================================= */

async function initMediasoup() {
  // valida GST_BIN se for caminho absoluto
  try {
    if (GST_BIN.includes("\\") && !fs.existsSync(GST_BIN)) {
      console.warn(`[WARN] GST_BIN não encontrado em: ${GST_BIN}`);
      console.warn(`[WARN] Ajuste GST_BIN ou adicione o GStreamer ao PATH.`);
    }
  } catch (_) {}

  worker = await mediasoup.createWorker(mediasoupConfig.worker);

  worker.on("died", () => {
    console.error("mediasoup worker died, exiting in 2s...");
    setTimeout(() => process.exit(1), 2000);
  });

  router = await worker.createRouter({
    mediaCodecs: mediasoupConfig.routerOptions.mediaCodecs,
  });

  console.log("mediasoup worker + router criado");
}

/* =============================================================================
   6) HTTP /upload-audio (fallback / compat com MediaRecorder)
============================================================================= */

app.post("/upload-audio", upload.single("audio"), async (req, res) => {
  if (!req.file) return res.status(400).json({ error: "no_audio" });

  const roomId = (req.body.roomId ? String(req.body.roomId) : "").trim() || "global";

  try {
    const filePath = req.file.path;
    const originalName = req.file.originalname || "chunk.webm";

    console.log("Received chunk", originalName, "size", req.file.size, "roomId", roomId);

    const form = new FormData();
    form.append("audio", fs.createReadStream(filePath), { filename: originalName });

    if (req.body.seq) form.append("seq", req.body.seq);
    if (req.body.timestamp) form.append("timestamp", req.body.timestamp);
    if (req.body.roomId) form.append("roomId", req.body.roomId);
    if (req.body.clientId) form.append("clientId", req.body.clientId);
    if (req.body.context_hint) form.append("context_hint", req.body.context_hint);

    const pyResp = await axios.post(PYTHON_PROCESSOR_URL, form, {
      headers: form.getHeaders(),
      timeout: PYTHON_TIMEOUT_MS,
    });

    fs.unlink(filePath, () => {});
    const payload = pyResp.data || {};

    if (Array.isArray(payload.new_insights) && payload.new_insights.length > 0) {
      broadcastToRoom(roomId, {
        type: "insights",
        roomId,
        chunk_id: payload.chunk_id,
        gate: payload.gate || null,
        new_insights: payload.new_insights,
        memory_state: payload.memory_state || null,
        received_at: payload?.meta?.received_at || null,
      });
    } else {
      broadcastToRoom(roomId, {
        type: "gate",
        roomId,
        chunk_id: payload.chunk_id,
        gate: payload.gate || null,
        memory_state: payload.memory_state || null,
        received_at: payload?.meta?.received_at || null,
      });
    }

    return res.json(payload);
  } catch (err) {
    const status = err?.response?.status;
    const data = err?.response?.data;

    console.error(
      "error forwarding to python",
      status ? `(status ${status})` : "",
      err.message || err,
      data ? `| body: ${JSON.stringify(data).slice(0, 1000)}` : ""
    );

    if (req.file?.path) {
      try {
        fs.unlinkSync(req.file.path);
      } catch (_) {}
    }

    return res.status(500).json({
      error: "forward_failed",
      detail: err.message || String(err),
      python_status: status || null,
      python_body: data || null,
    });
  }
});

/* =============================================================================
   7) EGRESS (real): Producer(Opus RTP) -> PlainTransport -> GStreamer -> WAV -> Python
============================================================================= */

async function forwardWavToPython({ wavPath, roomId, role, peerId, producerId }) {
  const form = new FormData();
  const filename = path.basename(wavPath);

  form.append("audio", fs.createReadStream(wavPath), { filename });
  form.append("roomId", roomId);
  form.append("timestamp", String(Date.now()));
  form.append("seq", String(Date.now()));
  form.append("context_hint", `egress peer=${peerId} producer=${producerId} role=${role || "unknown"}`);

  const pyResp = await axios.post(PYTHON_PROCESSOR_URL, form, {
    headers: form.getHeaders(),
    timeout: PYTHON_TIMEOUT_MS,
  });

  const payload = pyResp.data || {};

  if (Array.isArray(payload.new_insights) && payload.new_insights.length > 0) {
    broadcastToRoom(roomId, {
      type: "insights",
      roomId,
      chunk_id: payload.chunk_id,
      gate: payload.gate || null,
      new_insights: payload.new_insights,
      memory_state: payload.memory_state || null,
      received_at: payload?.meta?.received_at || null,
    });
  } else {
    broadcastToRoom(roomId, {
      type: "gate",
      roomId,
      chunk_id: payload.chunk_id,
      gate: payload.gate || null,
      memory_state: payload.memory_state || null,
      received_at: payload?.meta?.received_at || null,
    });
  }

  return payload;
}

function startWavPolling({ roomId, peerId, producerId, role, wavPrefix }) {
  const seen = new Set();

  const timer = setInterval(async () => {
    try {
      const files = fs.readdirSync(EGRESS_DIR).filter((f) => f.startsWith(wavPrefix) && f.endsWith(".wav"));
      files.sort();

      for (const f of files) {
        if (seen.has(f)) continue;
        const full = path.join(EGRESS_DIR, f);

        // aguarda “assentar” o arquivo
        let lastSize = -1;
        let stable = 0;
        for (let i = 0; i < 10; i++) {
          await new Promise((r) => setTimeout(r, 120));
          if (!fs.existsSync(full)) break;
          const st = fs.statSync(full);
          if (st.size > 4096 && st.size === lastSize) stable += 1;
          else stable = 0;
          lastSize = st.size;
          if (stable >= 1) break;
        }

        if (!fs.existsSync(full)) {
          seen.add(f);
          continue;
        }

        seen.add(f);

        await forwardWavToPython({ wavPath: full, roomId, role, peerId, producerId });

        fs.unlink(full, () => {});
      }
    } catch (e) {
      console.error("polling error:", e?.message || e);
    }
  }, WATCH_POLL_MS);

  return { timer, seen };
}

/**
 * ✅ Porta livre REAL para UDP (Windows-safe)
 * - bind UDP em port=0 => o SO escolhe uma porta permitida
 */
async function getFreeUdpPort(host = "127.0.0.1") {
  return await new Promise((resolve, reject) => {
    const sock = dgram.createSocket("udp4");

    sock.once("error", (err) => {
      try {
        sock.close();
      } catch (_) {}
      reject(err);
    });

    sock.bind({ address: host, port: 0, exclusive: true }, () => {
      const { port } = sock.address();
      try {
        sock.close();
      } catch (_) {}
      resolve(port);
    });
  });
}

/**
 * ✅ Pipeline Windows-safe (conservador e estável)
 * Observação importante:
 * - NÃO usamos "-q" aqui para conseguir ver erros/avisos reais no stderr.
 * - Se quiser “silenciar” depois, volte o "-q" quando estiver 100% estável.
 */
function spawnGStreamerEgress({ producerId, rtpPort, payloadType, clockRate, channels, wavPattern }) {
  const maxSizeTimeNs = BigInt(EGRESS_CHUNK_SECONDS) * 1000000000n;

  const caps = `application/x-rtp,media=audio,encoding-name=OPUS,payload=${payloadType},clock-rate=${clockRate},channels=${channels}`;

  const args = [
    // sem -q (debug útil)
    "udpsrc",
    "address=127.0.0.1",
    `port=${rtpPort}`,
    `caps=${caps}`,

    "!",
    "rtpjitterbuffer",
    `latency=${GST_JITTER_LATENCY_MS}`,
    "drop-on-latency=true",

    "!",
    "rtpopusdepay",
    "!",
    "opusdec",
    "!",
    "audioconvert",
    "!",
    "audioresample",
    "!",
    "audio/x-raw,rate=16000,channels=1",

    "!",
    "queue", // evita travamento se o sink demorar

    "!",
    "splitmuxsink",
    "muxer=wavenc",
    `location=${wavPattern}`,
    `max-size-time=${maxSizeTimeNs.toString()}`,
  ];

  console.log(`EGRESS gst cmd: ${GST_BIN} ${args.map((x) => JSON.stringify(x)).join(" ")}`);

  const proc = spawn(GST_BIN, args, {
    stdio: ["ignore", "ignore", "pipe"],
    windowsHide: true,
  });

  proc.stderr.on("data", (d) => {
    const line = String(d).trim();
    if (line) console.log(`[gst egress ${producerId}]`, line);
  });

  proc.on("exit", (code, sig) => {
    console.log(`gst exit producer=${producerId} code=${code} sig=${sig}`);
  });

  return proc;
}

/**
 * Aguarda um “startup grace period” para detectar falhas imediatas (bind failed etc.)
 */
async function waitGstHealthy(proc, producerId) {
  await new Promise((r) => setTimeout(r, GST_STARTUP_GRACE_MS));

  if (!proc || proc.killed) throw new Error("gst process not running");
  if (proc.exitCode !== null && proc.exitCode !== undefined) {
    throw new Error(`gst exited early (code=${proc.exitCode})`);
  }

  // sem um “ready signal” oficial no gst-launch, este é o melhor check simples:
  return true;
}

async function startEgressForProducer({ roomId, peerId, producerId }) {
  if (egressSessions.has(producerId)) {
    return { ok: true, alreadyRunning: true, producerId };
  }

  const peer = peers.get(peerId);
  if (!peer) throw new Error("peer not found");

  const producer = peer.producers.get(producerId);
  if (!producer) throw new Error("producer not found");

  if (producer.kind !== "audio") {
    throw new Error("egress supports only audio producers");
  }

  // 1) PlainTransport (cria uma vez; se der problema, fecha e recria no retry)
  let plainTransport = null;
  let consumer = null;
  let gstProc = null;

  // 5) Arquivos wav segmentados
  const wavPrefix = `room_${roomId}_prod_${producerId}_`;
  const wavPattern = path.join(EGRESS_DIR, `${wavPrefix}%05d.wav`);

  // 7) polling dos wavs (cria depois de gst subir)
  const role = peer.role || "unknown";

  for (let attempt = 1; attempt <= MAX_EGRESS_PORT_RETRIES; attempt++) {
    try {
      // garante limpeza de tentativa anterior
      try {
        gstProc?.kill("SIGKILL");
      } catch (_) {}
      gstProc = null;

      try {
        consumer?.close?.();
      } catch (_) {}
      consumer = null;

      try {
        plainTransport?.close?.();
      } catch (_) {}
      plainTransport = null;

      plainTransport = await router.createPlainTransport(mediasoupConfig.plainTransportOptions);

      // 2) Portas UDP reais (Windows-safe)
      const rtpPort = await getFreeUdpPort("127.0.0.1");
      const rtcpPort = await getFreeUdpPort("127.0.0.1");

      // comedia=false => precisa conectar explicitamente para onde vamos mandar RTP/RTCP
      await plainTransport.connect({
        ip: "127.0.0.1",
        port: rtpPort,
        rtcpPort: rtcpPort,
      });

      // 3) rtpCapabilities mínimas para criar um consumer compatível
      const receiverRtpCapabilities = {
        codecs: [
          {
            kind: "audio",
            mimeType: "audio/opus",
            clockRate: 48000,
            channels: 2,
          },
        ],
        headerExtensions: [],
      };

      // 4) Consumer (server-side)
      consumer = await plainTransport.consume({
        producerId,
        rtpCapabilities: receiverRtpCapabilities,
        paused: false,
      });

      const codec = consumer.rtpParameters?.codecs?.[0] || {};
      const payloadType = codec.payloadType ?? 111;
      const clockRate = codec.clockRate ?? 48000;
      const channels = codec.channels ?? 2;

      console.log(
        `[egress] producer=${producerId} attempt=${attempt}/${MAX_EGRESS_PORT_RETRIES} RTP=127.0.0.1:${rtpPort} RTCP=${rtcpPort} payloadType=${payloadType} clockRate=${clockRate} channels=${channels}`
      );

      // 6) Spawn GStreamer
      gstProc = spawnGStreamerEgress({
        producerId,
        rtpPort,
        payloadType,
        clockRate,
        channels,
        wavPattern,
      });

      // se falhar ao bind, geralmente cai muito rápido
      await waitGstHealthy(gstProc, producerId);

      // 7) Polling dos wavs (só depois que gst ficou “de pé”)
      const polling = startWavPolling({ roomId, peerId, producerId, role, wavPrefix });

      // 8) Estado da sessão
      const session = {
        roomId,
        peerId,
        role,
        producerId,
        plainTransport,
        consumer,
        gstProc,
        wavPrefix,
        pollTimer: polling.timer,
        seenFiles: polling.seen,
        startedAt: Date.now(),
        rtpPort,
        rtcpPort,
      };

      egressSessions.set(producerId, session);

      // Cleanup automático
      producer.on("close", () => stopEgressForProducer(producerId).catch(() => {}));
      consumer.on("transportclose", () => stopEgressForProducer(producerId).catch(() => {}));

      console.log(
        `EGRESS(GST) started producer=${producerId} room=${roomId} rtpPort=${rtpPort} rtcpPort=${rtcpPort} chunk=${EGRESS_CHUNK_SECONDS}s`
      );

      return {
        ok: true,
        producerId,
        roomId,
        rtpPort,
        rtcpPort,
        wavPrefix,
        chunkSeconds: EGRESS_CHUNK_SECONDS,
        engine: "gstreamer",
        payloadType,
        gstBin: GST_BIN,
        attempt,
      };
    } catch (e) {
      const msg = e?.message || String(e);
      console.error(`[egress] attempt ${attempt} failed:`, msg);

      // se é a última tentativa, propaga erro
      if (attempt === MAX_EGRESS_PORT_RETRIES) {
        // cleanup final
        try {
          gstProc?.kill("SIGKILL");
        } catch (_) {}
        try {
          consumer?.close?.();
        } catch (_) {}
        try {
          plainTransport?.close?.();
        } catch (_) {}

        throw new Error(`egress failed after ${MAX_EGRESS_PORT_RETRIES} attempts: ${msg}`);
      }
    }
  }

  throw new Error("unreachable");
}

async function stopEgressForProducer(producerId) {
  const s = egressSessions.get(producerId);
  if (!s) return { ok: true, alreadyStopped: true, producerId };

  egressSessions.delete(producerId);

  try {
    if (s.pollTimer) clearInterval(s.pollTimer);
  } catch (_) {}

  try {
    s.gstProc?.kill("SIGKILL");
  } catch (_) {}

  try {
    s.consumer?.close?.();
  } catch (_) {}

  try {
    s.plainTransport?.close?.();
  } catch (_) {}

  console.log(`EGRESS(GST) stopped producer=${producerId}`);
  return { ok: true, producerId };
}

/* =============================================================================
   8) WebSocket: rooms + mediasoup signaling + egress control
============================================================================= */

wss.on("connection", (ws) => {
  const peerId = uuidv4();
  console.log("ws connected", peerId);

  peers.set(peerId, {
    ws,
    roomId: null,
    transports: new Map(),
    producers: new Map(),
    consumers: new Map(),
    role: null,
  });

  send(ws, { type: "welcome", id: peerId });

  ws.on("message", async (raw) => {
    let msg;
    try {
      msg = JSON.parse(raw.toString());
    } catch (_) {
      return send(ws, { ok: false, error: "invalid_json" });
    }

    const { action, data, requestId } = msg;

    try {
      const peer = peers.get(peerId);
      if (!peer) throw new Error("peer not found");

      if (action === "joinRoom") {
        const roomId = (data?.roomId ? String(data.roomId) : "").trim();
        if (!roomId) throw new Error("roomId required");
        joinRoom(peerId, roomId);
        return send(ws, { requestId, ok: true, data: { roomId } });
      }

      if (action === "setRole") {
        const role = (data?.role ? String(data.role) : "").trim();
        peer.role = role || null;
        return send(ws, { requestId, ok: true, data: { role: peer.role } });
      }

      if (action === "getRouterRtpCapabilities") {
        return send(ws, { requestId, ok: true, data: router.rtpCapabilities });
      }

      if (action === "createWebRtcTransport") {
        const transport = await router.createWebRtcTransport(mediasoupConfig.webRtcTransportOptions);
        peer.transports.set(transport.id, transport);

        transport.on("dtlsstatechange", (state) => {
          if (state === "closed") {
            try {
              transport.close();
            } catch (_) {}
            peer.transports.delete(transport.id);
          }
        });

        transport.on("close", () => {
          peer.transports.delete(transport.id);
        });

        const payload = {
          id: transport.id,
          iceParameters: transport.iceParameters,
          iceCandidates: transport.iceCandidates,
          dtlsParameters: transport.dtlsParameters,
          sctpParameters: transport.sctpParameters,
        };

        return send(ws, { requestId, ok: true, data: payload });
      }

      if (action === "connectTransport") {
        const transportId = data?.transportId;
        const dtlsParameters = data?.dtlsParameters;
        if (!transportId || !dtlsParameters) throw new Error("transportId/dtlsParameters required");

        const transport = peer.transports.get(transportId);
        if (!transport) throw new Error("transport not found");

        await transport.connect({ dtlsParameters });
        return send(ws, { requestId, ok: true });
      }

      if (action === "produce") {
        const transportId = data?.transportId;
        const kind = data?.kind;
        const rtpParameters = data?.rtpParameters;

        if (!transportId || !kind || !rtpParameters) throw new Error("transportId/kind/rtpParameters required");

        const transport = peer.transports.get(transportId);
        if (!transport) throw new Error("transport not found");

        const producer = await transport.produce({ kind, rtpParameters });
        peer.producers.set(producer.id, producer);

        producer.on("transportclose", () => {
          peer.producers.delete(producer.id);
          stopEgressForProducer(producer.id).catch(() => {});
        });

        producer.on("close", () => {
          peer.producers.delete(producer.id);
          stopEgressForProducer(producer.id).catch(() => {});
        });

        console.log(`peer ${peerId} produced ${kind} (producerId=${producer.id})`);

        if (AUTO_EGRESS) {
          const roomId = peer.roomId || "global";
          startEgressForProducer({ roomId, peerId, producerId: producer.id }).catch((e) => {
            console.error("auto egress failed", e?.message || e);
          });
        }

        return send(ws, { requestId, ok: true, data: { id: producer.id } });
      }

      if (action === "startEgress") {
        const roomId = peer.roomId || "global";
        const producerId = (data?.producerId ? String(data.producerId) : "").trim();
        if (!producerId) throw new Error("producerId required");

        const resp = await startEgressForProducer({ roomId, peerId, producerId });
        return send(ws, { requestId, ok: true, data: resp });
      }

      if (action === "stopEgress") {
        const producerId = (data?.producerId ? String(data.producerId) : "").trim();
        if (!producerId) throw new Error("producerId required");

        const resp = await stopEgressForProducer(producerId);
        return send(ws, { requestId, ok: true, data: resp });
      }

      return send(ws, { requestId, ok: false, error: "unknown_action" });
    } catch (err) {
      console.error("action error", action, err);
      return send(ws, { requestId, ok: false, error: err.message || String(err) });
    }
  });

  ws.on("close", () => {
    console.log("ws closed", peerId);

    const peer = peers.get(peerId);
    if (peer) {
      for (const producerId of peer.producers.keys()) {
        stopEgressForProducer(producerId).catch(() => {});
      }

      try {
        for (const t of peer.transports.values())
          try {
            t.close();
          } catch (_) {}
        for (const p of peer.producers.values())
          try {
            p.close();
          } catch (_) {}
        for (const c of peer.consumers.values())
          try {
            c.close();
          } catch (_) {}
      } catch (_) {}

      if (peer.roomId) {
        const set = rooms.get(peer.roomId);
        if (set) {
          set.delete(peerId);
          if (set.size === 0) rooms.delete(peer.roomId);
        }
      }
    }

    peers.delete(peerId);
  });
});

/* =============================================================================
   9) Start
============================================================================= */

const PORT = Number(process.env.PORT || 3000);

(async () => {
  try {
    await initMediasoup();

    server.listen(PORT, () => {
      console.log(`Server running on http://0.0.0.0:${PORT}`);
      console.log("WebSocket signaling ready");
      console.log(`Upload endpoint POST http://0.0.0.0:${PORT}/upload-audio`);
      console.log(`Python processor: ${PYTHON_PROCESSOR_URL}`);
      console.log(`EGRESS_DIR: ${EGRESS_DIR}`);
      console.log(`EGRESS_CHUNK_SECONDS: ${EGRESS_CHUNK_SECONDS}`);
      console.log(`AUTO_EGRESS: ${AUTO_EGRESS ? "on" : "off"}`);
      console.log(`GST_BIN: ${GST_BIN}`);
      console.log(`GST_JITTER_LATENCY_MS: ${GST_JITTER_LATENCY_MS}`);
      console.log(`MAX_EGRESS_PORT_RETRIES: ${MAX_EGRESS_PORT_RETRIES}`);
      console.log(`GST_STARTUP_GRACE_MS: ${GST_STARTUP_GRACE_MS}`);
    });
  } catch (err) {
    console.error("failed to start", err);
    process.exit(1);
  }
})();
