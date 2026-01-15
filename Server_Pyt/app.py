# app.py
import os
import re
import json
import uuid
import shutil
import asyncio
import logging
import tempfile
import subprocess
from datetime import datetime
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse

# Carrega .env automaticamente (opcional, mas recomendado)
# pip install python-dotenv
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

from groq import Groq
from faster_whisper import WhisperModel

# ------------------------
# Memória (persistida em JSON por room)
# ------------------------
from memory_store import (
    load_state,
    save_state,
    add_insights,
    apply_patch,
    can_emit_insights,
    mark_insight_emitted,
    summarize_state_for_prompt,
)

# ------------------------
# Gate (decide quando "vale a pena" chamar o LLM)
# ------------------------
from signal_gate import SignalGate, GateConfig

LOG = logging.getLogger("uvicorn.error")

# ============================================================
#  CONFIG (env vars)
# ============================================================

# --- Groq ---
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
# Se quiser OBRIGAR Groq (derrubar server se não tiver chave): REQUIRE_GROQ=1
REQUIRE_GROQ = os.getenv("REQUIRE_GROQ", "0").strip() in ("1", "true", "True", "yes", "YES")

# --- Whisper ---
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "base")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")

# --- Queue/Timeout ---
MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "200"))
JOB_TIMEOUT_SECONDS = int(os.getenv("JOB_TIMEOUT_SECONDS", "120"))

# --- Buffer ---
BUFFER_MAX_CHUNKS = int(os.getenv("BUFFER_MAX_CHUNKS", "8"))
BUFFER_MAX_CHARS = int(os.getenv("BUFFER_MAX_CHARS", "2200"))

# --- Gate knobs ---
GATE_MIN_WORDS = int(os.getenv("GATE_MIN_WORDS", "18"))
GATE_MIN_CHARS = int(os.getenv("GATE_MIN_CHARS", "60"))
GATE_COOLDOWN_SECONDS = int(os.getenv("GATE_COOLDOWN_SECONDS", "8"))
GATE_MIN_SCORE = int(os.getenv("GATE_MIN_SCORE", "3"))
GATE_KEYWORD_SCORE = int(os.getenv("GATE_KEYWORD_SCORE", "2"))

# --- FFMPEG ---
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")

# ============================================================
#  PROMPT
# ============================================================
SALES_INTELLIGENCE_PROMPT = """
Você é um analista comercial sênior especializado em vendas consultivas, comportamento do cliente e inteligência de receita.
Você está analisando a transcrição de uma conversa real entre um vendedor e um potencial cliente.

Objetivo:
Gerar insights acionáveis que ajudem o vendedor a fechar a venda, entender intenção real do cliente, identificar riscos e definir os próximos passos.

Regras:
- Não invente fatos — use apenas o que estiver explicitamente ou implicitamente na transcrição
- Quando algo for inferência, indique isso em "rationale" ou "evidence"
- Quando faltar informação, registre em "unknowns"
- Seja direto, profissional e orientado a vendas
- Responda SOMENTE JSON válido, sem markdown e sem ```.

Produza APENAS JSON válido no formato:

{
  "deal_stage": "discovery | interest | evaluation | negotiation | closing | lost | unknown",
  "customer_intent": {
    "level": "low | medium | high",
    "evidence": ["..."]
  },
  "pain_points": [
    { "topic": "string", "evidence": "..." }
  ],
  "budget_signals": {
    "has_budget": true | false | null,
    "evidence": ["..."]
  },
  "decision_process": {
    "decision_maker": "string | unknown",
    "stakeholders": ["..."] | null,
    "timeline": "string | unknown"
  },
  "objections": [
    { "type": "price | trust | timing | feature | authority | other", "detail": "..." }
  ],
  "opportunity_score": {
    "score": 0-100,
    "rationale": "..."
  },
  "next_best_actions": [
    {
      "priority": "high | medium | low",
      "action": "o que o vendedor deve fazer",
      "reason": "por que isso aumenta chance de fechamento"
    }
  ],
  "upsell_or_cross_sell_opportunities": [
    {
      "product_or_feature": "string",
      "reason": "..."
    }
  ],
  "risks": [
    { "risk": "string", "impact": "high | medium | low" }
  ],
  "unknowns": [
    "..."
  ],
  "seller_coaching": {
    "what_went_well": ["..."],
    "what_to_improve": ["..."]
  }
}
""".strip()

# ============================================================
#  APP INIT (FastAPI + clients + modelos)
# ============================================================
app = FastAPI(title="Audio Analysis Service (Queue + Faster-Whisper + Groq + Memory + Gate)")

# --- Groq client (opcional) ---
groq_client: Optional[Groq] = None
if GROQ_API_KEY:
    groq_client = Groq(api_key=GROQ_API_KEY)
    LOG.info("Groq client initialized")
else:
    msg = "GROQ_API_KEY não definido. Rodando em modo STT-only (sem LLM)."
    if REQUIRE_GROQ:
        raise RuntimeError(msg + " Defina GROQ_API_KEY ou desative REQUIRE_GROQ.")
    LOG.warning(msg)

# --- Whisper model (carrega 1x) ---
whisper_model = WhisperModel(
    WHISPER_MODEL_SIZE,
    device=WHISPER_DEVICE,
    compute_type=WHISPER_COMPUTE_TYPE,
)
LOG.info(f"Faster-Whisper model loaded: {WHISPER_MODEL_SIZE} ({WHISPER_COMPUTE_TYPE})")

# --- Gate ---
signal_gate = SignalGate(
    cfg=GateConfig(
        min_words_buffer=GATE_MIN_WORDS,
        min_chars_buffer=GATE_MIN_CHARS,
        cooldown_seconds=GATE_COOLDOWN_SECONDS,
        keyword_score=GATE_KEYWORD_SCORE,
        min_score_to_pass=GATE_MIN_SCORE,
    )
)

# --- Queue ---
job_queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
worker_task: Optional[asyncio.Task] = None


# ============================================================
#  CLASSES
# ============================================================
class AudioNormalizer:
    """
    Normaliza qualquer áudio (webm/ogg/etc) para WAV mono PCM16 16kHz.
    Requer ffmpeg no PATH (ou FFMPEG_BIN configurado).
    """
    @staticmethod
    def to_wav16k(src_path: str, dst_path: str) -> None:
        cmd = [
            FFMPEG_BIN, "-y",
            "-i", src_path,
            "-ac", "1",
            "-ar", "16000",
            "-sample_fmt", "s16",
            "-f", "wav",
            dst_path
        ]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode != 0:
            raise RuntimeError("ffmpeg failed: " + p.stderr.decode("utf-8", errors="ignore"))


class SpeechToTextService:
    """
    Encapsula Faster-Whisper para STT.
    """
    def __init__(self, model: WhisperModel):
        self.model = model

    async def transcribe(self, wav_path: str) -> Dict[str, Any]:
        def _sync_transcribe():
            segments, info = self.model.transcribe(
                wav_path,
                beam_size=1,
                vad_filter=True,
                temperature=0.0,
                language="pt",
            )
            segs = []
            full = []
            for s in segments:
                t = (s.text or "").strip()
                if t:
                    segs.append({"start": float(s.start), "end": float(s.end), "text": t})
                    full.append(t)
            return {
                "language": getattr(info, "language", None),
                "text": " ".join(full).strip(),
                "segments": segs
            }

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _sync_transcribe)


class GroqAnalysisService:
    """
    Encapsula chamada ao Groq para retornar JSON puro.
    """
    def __init__(self, client: Groq, model_name: str):
        self.client = client
        self.model_name = model_name

    @staticmethod
    def _strip_code_fences(s: str) -> str:
        if not isinstance(s, str):
            return s
        s = s.strip()
        if s.startswith("```"):
            s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
            s = re.sub(r"\s*```$", "", s)
        return s.strip()

    @staticmethod
    def _system_message() -> str:
        return (
            "Responda SOMENTE com JSON válido, sem markdown e sem ```.\n"
            "Siga exatamente o schema solicitado. Se não houver evidência suficiente, use 'unknown' ou null.\n"
            "Idioma: pt-BR."
        )

    async def analyze_sales(self, prompt_text: str) -> Dict[str, Any]:
        messages = [
            {"role": "system", "content": self._system_message()},
            {"role": "user", "content": prompt_text},
        ]

        def _sync_call():
            return self.client.chat.completions.create(
                messages=messages,
                model=self.model_name,
            )

        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(None, _sync_call)

        content = resp.choices[0].message.content
        content = self._strip_code_fences(content)

        try:
            return json.loads(content)
        except Exception:
            return {"raw": content, "errors": "Groq não retornou JSON válido"}


class MemoryBufferManager:
    """
    Mantém um buffer curto de transcrição recente por room.
    """
    def __init__(self, max_chunks: int, max_chars: int):
        self.max_chunks = max_chunks
        self.max_chars = max_chars

    def append(self, state: Dict[str, Any], text: str, seq: Optional[int] = None) -> None:
        if not text:
            return

        buf = state.setdefault("buffer", [])
        if not isinstance(buf, list):
            buf = []
            state["buffer"] = buf

        buf.append({
            "t": text,
            "seq": seq,
            "at": datetime.utcnow().isoformat() + "Z",
        })

        if len(buf) > self.max_chunks:
            buf[:] = buf[-self.max_chunks:]

        while buf and len(self.join_text({"buffer": buf})) > self.max_chars:
            buf.pop(0)

    @staticmethod
    def join_text(state: Dict[str, Any]) -> str:
        buf = state.get("buffer") or []
        if not isinstance(buf, list):
            return ""
        return " ".join((x.get("t") or "") for x in buf).strip()


class InsightExtractor:
    """
    Converte análise Groq (schema de vendas) em "cards" de insights.
    """
    @staticmethod
    def extract(analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
        insights: List[Dict[str, Any]] = []
        if not isinstance(analysis, dict):
            return insights

        for p in (analysis.get("pain_points") or []):
            if isinstance(p, dict) and p.get("topic"):
                insights.append({
                    "type": "pain_point",
                    "title": f"Dor: {p.get('topic')}",
                    "why": p.get("evidence") or "",
                    "next_action": "Explorar impacto e priorização dessa dor; quantificar custo/tempo.",
                    "confidence": 0.82
                })

        for o in (analysis.get("objections") or []):
            if isinstance(o, dict) and o.get("detail"):
                t = o.get("type") or "other"
                insights.append({
                    "type": f"objection_{t}",
                    "title": f"Objeção detectada ({t})",
                    "why": o.get("detail") or "",
                    "next_action": "Validar objeção e aprofundar; responder com prova/ROI.",
                    "confidence": 0.80
                })

        for r in (analysis.get("risks") or []):
            if isinstance(r, dict) and r.get("risk"):
                insights.append({
                    "type": "risk",
                    "title": "Risco no deal",
                    "why": r.get("risk") or "",
                    "next_action": "Mitigar risco com alinhamento de critérios e próximos passos claros.",
                    "confidence": 0.78
                })

        for a in (analysis.get("next_best_actions") or []):
            if isinstance(a, dict) and a.get("action"):
                insights.append({
                    "type": "next_action",
                    "title": f"Ação recomendada ({a.get('priority','medium')})",
                    "why": a.get("reason") or "",
                    "next_action": a.get("action"),
                    "confidence": 0.85
                })

        return insights

    @staticmethod
    def memory_patch(analysis: Dict[str, Any]) -> Dict[str, Any]:
        patch: Dict[str, Any] = {}
        if not isinstance(analysis, dict):
            return patch

        stage = analysis.get("deal_stage")
        if stage:
            patch.setdefault("deal", {})
            patch["deal"]["stage"] = stage

        op = analysis.get("opportunity_score")
        if isinstance(op, dict) and isinstance(op.get("score"), (int, float)):
            patch.setdefault("deal", {})
            patch["deal"]["opportunity_score"] = int(op["score"])

        intent = analysis.get("customer_intent")
        if isinstance(intent, dict) and intent.get("level"):
            patch.setdefault("deal", {})
            patch["deal"]["intent_level"] = intent["level"]

        return patch


class AudioAnalysisPipeline:
    """
    Pipeline por chunk:
      1) ffmpeg -> wav
      2) STT
      3) atualiza memória/buffer
      4) gate decide se chama Groq com base no buffer
      5) Groq -> JSON
      6) insights + dedupe + persistência
    """
    def __init__(
        self,
        stt: SpeechToTextService,
        llm: Optional[GroqAnalysisService],
        buffer_mgr: MemoryBufferManager,
        gate: SignalGate,
    ):
        self.stt = stt
        self.llm = llm
        self.buffer_mgr = buffer_mgr
        self.gate = gate

    @staticmethod
    def _room_key(room_id: Optional[str]) -> str:
        return (room_id or "").strip() or "global"

    @staticmethod
    def diarize_stub(duration_s: float) -> List[Dict[str, Any]]:
        return [{"speaker": "SPEAKER_0", "start": 0.0, "end": float(duration_s), "confidence": 0.9}]

    async def run(self, job: Dict[str, Any]) -> Dict[str, Any]:
        # 1) Normaliza áudio
        AudioNormalizer.to_wav16k(job["in_path"], job["out_wav"])

        # 2) STT
        stt = await self.stt.transcribe(job["out_wav"])
        transcript_text = (stt.get("text") or "").strip()

        duration = 0.0
        if stt.get("segments"):
            duration = max([seg["end"] for seg in stt["segments"]])
        diarization = self.diarize_stub(duration)

        transcript_by_speaker = [{
            "speaker": "SPEAKER_0",
            "segments": stt.get("segments", []) or [{"start": 0.0, "end": duration, "text": transcript_text}]
        }]

        # 3) Memória + Buffer
        room_key = self._room_key(job.get("roomId"))
        state = load_state(room_key)

        if transcript_text:
            self.buffer_mgr.append(state, transcript_text, seq=job.get("seq"))
            state["summary_live"] = (state.get("summary_live", "") + " " + transcript_text).strip()[-1200:]

        buf_text = self.buffer_mgr.join_text(state)

        # 4) Gate
        ok_gate, gate_payload = self.gate.should_analyze(
            room_id=room_key,
            buffer_text=buf_text,
            just_transcribed_text=transcript_text
        )

        # 5) LLM (opcional)
        analysis: Dict[str, Any] = {"skipped": True, "reason": "gate_or_cooldown_or_no_llm"}
        new_insights_accepted: List[Dict[str, Any]] = []

        if ok_gate and can_emit_insights(state) and self.llm is not None:
            snapshot = summarize_state_for_prompt(state)

            prompt = (
                f"{SALES_INTELLIGENCE_PROMPT}\n\n"
                f"Contexto adicional:\n{(job.get('context_hint') or '').strip()}\n\n"
                f"Memória atual (snapshot):\n{json.dumps(snapshot, ensure_ascii=False)}\n\n"
                f"Transcrição acumulada (buffer recente):\n{buf_text}\n\n"
                f"Último trecho (mais recente):\n{transcript_text}\n\n"
                f"Metadados:\n"
                f"- roomId: {job.get('roomId')}\n"
                f"- seq: {job.get('seq')}\n"
                f"- timestamp: {job.get('timestamp')}\n"
            )

            analysis = await self.llm.analyze_sales(prompt)
            self.gate.mark_analyzed(room_key)

            patch = InsightExtractor.memory_patch(analysis)
            if patch:
                state = apply_patch(state, patch)

            candidate_insights = InsightExtractor.extract(analysis)
            state, new_insights_accepted = add_insights(state, candidate_insights)

            if new_insights_accepted:
                mark_insight_emitted(state)

        # 6) Persistência
        save_state(room_key, state)
        memory_state = summarize_state_for_prompt(state)

        return {
            "chunk_id": job["chunk_id"],
            "seq": job.get("seq"),
            "meta": {
                "roomId": job.get("roomId"),
                "seq": job.get("seq"),
                "clientId": job.get("clientId"),
                "chunk_id": job["chunk_id"],
                "timestamp": job.get("timestamp"),
                "received_at": datetime.utcnow().isoformat() + "Z",
            },
            "gate": gate_payload,
            "new_insights": new_insights_accepted,
            "memory_state": memory_state,
            "diarization": diarization,
            "transcript": stt,
            "transcript_by_speaker": transcript_by_speaker,
            "analysis": analysis,
            "llm_enabled": bool(self.llm is not None),
        }


class JobQueueRunner:
    def __init__(self, queue: asyncio.Queue, pipeline: AudioAnalysisPipeline):
        self.queue = queue
        self.pipeline = pipeline

    async def worker(self):
        LOG.info("Queue worker started")
        while True:
            job = await self.queue.get()
            fut: asyncio.Future = job["future"]
            try:
                result = await self.pipeline.run(job)
                if not fut.done():
                    fut.set_result(result)
            except Exception as e:
                LOG.exception("Job failed")
                if not fut.done():
                    fut.set_exception(e)
            finally:
                try:
                    shutil.rmtree(job["temp_dir"], ignore_errors=True)
                except Exception:
                    pass
                self.queue.task_done()


# ============================================================
#  PIPELINE SINGLETONS
# ============================================================
stt_service = SpeechToTextService(whisper_model)
llm_service: Optional[GroqAnalysisService] = GroqAnalysisService(groq_client, GROQ_MODEL) if groq_client else None
buffer_manager = MemoryBufferManager(BUFFER_MAX_CHUNKS, BUFFER_MAX_CHARS)
pipeline = AudioAnalysisPipeline(stt_service, llm_service, buffer_manager, signal_gate)
queue_runner = JobQueueRunner(job_queue, pipeline)


# ============================================================
#  FASTAPI LIFECYCLE
# ============================================================
@app.on_event("startup")
async def on_startup():
    global worker_task
    if worker_task is None:
        worker_task = asyncio.create_task(queue_runner.worker())
        LOG.info("startup: worker task created")


# ============================================================
#  ENDPOINT
# ============================================================
@app.post("/process")
async def process_audio(
    audio: UploadFile = File(...),
    seq: Optional[int] = Form(None),
    timestamp: Optional[float] = Form(None),
    clientId: Optional[str] = Form(None),
    roomId: Optional[str] = Form(None),
    context_hint: Optional[str] = Form(""),
):
    if job_queue.full():
        return JSONResponse({"error": "queue_full", "detail": "Fila cheia, tente novamente."}, status_code=429)

    chunk_id = str(uuid.uuid4())
    temp_dir = tempfile.mkdtemp(prefix="audioproc_")
    in_path = os.path.join(temp_dir, audio.filename or f"{chunk_id}.webm")
    out_wav = os.path.join(temp_dir, f"{chunk_id}.wav")

    content = await audio.read()
    with open(in_path, "wb") as f:
        f.write(content)

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    job = {
        "future": future,
        "chunk_id": chunk_id,
        "temp_dir": temp_dir,
        "in_path": in_path,
        "out_wav": out_wav,
        "seq": seq,
        "timestamp": timestamp,
        "clientId": clientId,
        "roomId": roomId,
        "context_hint": context_hint,
    }

    await job_queue.put(job)

    try:
        result = await asyncio.wait_for(future, timeout=JOB_TIMEOUT_SECONDS)
        return JSONResponse(result)
    except asyncio.TimeoutError:
        return JSONResponse({"error": "timeout", "detail": "Processamento demorou demais."}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": "processing_failed", "detail": str(e)}, status_code=500)
