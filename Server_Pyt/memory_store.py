# memory_store.py
"""
Memory Store = "memória por room", persistida em JSON.

Objetivo:
- Manter estado incremental por sala (roomId) entre chunks
- Guardar:
  - resumo ao vivo (summary_live) para UI
  - buffer de chunks transcritos (chunks) para gate/LLM
  - feed de insights com dedupe (insights_feed)
  - sinais do deal (deal/signals) como memória estruturada
- Controlar cooldown de insights (pra não spammar a tela)
- Entregar um snapshot enxuto para prompts do LLM

Como é usado no app.py:
    state = load_state(room_id)
    state = add_chunk_text(state, text, seq=..., lang=..., ts=...)
    if can_emit_insights(state): ...
    state, accepted = add_insights(state, candidate_insights)
    save_state(room_id, state)
"""

import os
import json
import time
import hashlib
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

# ----------------------------
# Config (via env)
# ----------------------------
DEFAULT_DIR = os.getenv("MEMORY_DIR", "./memory")

# Janela de dedupe (quantos hashes manter)
DEFAULT_DEDUPE_WINDOW = int(os.getenv("DEDUPE_WINDOW", "200"))

# Cooldown mínimo entre insights (em segundos) por room
DEFAULT_COOLDOWN_SECONDS = int(os.getenv("INSIGHT_COOLDOWN_SECONDS", "10"))

# Buffer de chunks (quantos chunks guardar por room)
DEFAULT_MAX_CHUNKS = int(os.getenv("MAX_CHUNKS_PER_ROOM", "80"))

# Tamanho máximo do summary_live (pra UI)
DEFAULT_SUMMARY_MAX_CHARS = int(os.getenv("SUMMARY_LIVE_MAX_CHARS", "1200"))


# ----------------------------
# Utilitários internos
# ----------------------------
def _now_ts() -> float:
    """Timestamp em segundos (float)."""
    return time.time()


def _ensure_dir(path: str) -> None:
    """Garante que diretório exista."""
    os.makedirs(path, exist_ok=True)


def _safe_room_id(room_id: str) -> str:
    """
    Sanitiza room_id para virar nome de arquivo seguro.
    Evita path traversal e caracteres estranhos.
    """
    keep = []
    for ch in (room_id or ""):
        if ch.isalnum() or ch in ("-", "_"):
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep)[:120] or "global"


def _room_path(room_id: str, base_dir: str) -> str:
    """Path do arquivo JSON da room."""
    safe = _safe_room_id(room_id)
    return os.path.join(base_dir, f"{safe}.json")


def _normalize_text(s: str) -> str:
    """Normaliza texto para hashing/dedupe."""
    return " ".join((s or "").lower().strip().split())


# ----------------------------
# Classes (para deixar o módulo mais organizado)
# ----------------------------
class RoomStateSchema:
    """
    Define o "schema default" do estado por room.

    Esse estado é o que vai pro disco.
    Se o arquivo não existir (primeira vez), usamos esse default.
    """
    @staticmethod
    def default(room_id: str) -> Dict[str, Any]:
        now = _now_ts()
        return {
            "roomId": room_id,
            "deal": {
                "stage": "unknown",
                "intent_level": "unknown",
                "opportunity_score": None,
            },
            "signals": {
                "pain_points": [],
                "objections": [],
                "budget": {"has_budget": None, "evidence": []},
                "decision": {"decision_maker": "unknown", "stakeholders": [], "timeline": "unknown"},
            },

            # Texto “corrente” pra UI (não é o buffer completo)
            "summary_live": "",

            # Buffer de transcrições por chunk
            # item: { "seq": int|None, "ts": float, "text": str, "lang": str|None }
            "chunks": [],

            # Feed de insights (cards) + dedupe
            "insights_feed": [],
            "dedupe": {
                "recent_hashes": [],
                "window": DEFAULT_DEDUPE_WINDOW,
            },

            # Metadados gerais e cooldown
            "meta": {
                "created_at": now,
                "updated_at": now,
                "last_insight_at": 0.0,
            },
        }


class PatchMerger:
    """
    Merge incremental (patch) no estado.

    Regras:
    - dict: merge recursivo
    - list:
        * se patch for {"__append__": [...]} => append
        * senão, substitui
    - outros tipos: substitui
    """
    @staticmethod
    def apply(state: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
        if patch is None:
            return state

        if not isinstance(state, dict) or not isinstance(patch, dict):
            return deepcopy(patch)

        out = deepcopy(state)

        for k, v in patch.items():
            if k not in out:
                out[k] = deepcopy(v)
                continue

            if isinstance(out[k], dict) and isinstance(v, dict):
                out[k] = PatchMerger.apply(out[k], v)

            elif isinstance(out[k], list) and isinstance(v, dict) and "__append__" in v:
                add = v.get("__append__", [])
                if isinstance(add, list):
                    out[k] = out[k] + deepcopy(add)

            else:
                out[k] = deepcopy(v)

        return out


class InsightDeduper:
    """
    Dedupe de insights.

    Cria um hash estável por insight e evita duplicar no feed.
    Mantém janela deslizante de hashes recentes.
    """
    @staticmethod
    def insight_hash(insight: Dict[str, Any]) -> str:
        t = _normalize_text(str(insight.get("type", "")))
        title = _normalize_text(str(insight.get("title", "")))
        why = _normalize_text(str(insight.get("why", insight.get("evidence", ""))))
        action = _normalize_text(str(insight.get("next_action", insight.get("action", ""))))
        key = f"{t}|{title}|{why}|{action}"
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def add_insights(
        state: Dict[str, Any],
        new_insights: List[Dict[str, Any]],
        dedupe_window: int = DEFAULT_DEDUPE_WINDOW,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """
        Adiciona insights com dedupe por hash.
        Retorna:
          (state_atualizado, insights_aceitos)
        """
        state = deepcopy(state)

        dedupe = state.setdefault("dedupe", {})
        recent = dedupe.setdefault("recent_hashes", [])
        window = int(dedupe.get("window", dedupe_window) or dedupe_window)

        accepted: List[Dict[str, Any]] = []
        feed = state.setdefault("insights_feed", [])

        for ins in new_insights or []:
            if not isinstance(ins, dict):
                continue

            h = ins.get("id_hash") or InsightDeduper.insight_hash(ins)
            if h in recent:
                continue

            ins2 = deepcopy(ins)
            ins2["id_hash"] = h
            ins2.setdefault("created_at", _now_ts())

            feed.append(ins2)
            accepted.append(ins2)

            recent.append(h)
            # mantém janela
            if len(recent) > window:
                recent[:] = recent[-window:]

        dedupe["recent_hashes"] = recent
        dedupe["window"] = window
        state["insights_feed"] = feed
        state["dedupe"] = dedupe

        return state, accepted


class ChunkBuffer:
    """
    Buffer de chunks de transcrição.

    Responsabilidades:
    - guardar últimos N chunks
    - manter summary_live "corrente" pra UI
    - devolver texto acumulado (últimos N chunks / últimos X segundos)
    """
    @staticmethod
    def add_chunk_text(
        state: Dict[str, Any],
        text: str,
        seq: Optional[int] = None,
        lang: Optional[str] = None,
        ts: Optional[float] = None,
        max_chunks: int = DEFAULT_MAX_CHUNKS,
    ) -> Dict[str, Any]:
        """
        Adiciona 1 chunk transcrito ao buffer + atualiza summary_live.

        Importante:
        - se vier texto vazio, não adiciona (evita poluir buffer e derrubar gate)
        """
        state = deepcopy(state)
        text = (text or "").strip()
        if not text:
            return state

        ts = float(ts if ts is not None else _now_ts())
        item = {"seq": seq, "ts": ts, "text": text, "lang": lang}

        chunks = state.setdefault("chunks", [])
        if not isinstance(chunks, list):
            chunks = []
            state["chunks"] = chunks

        chunks.append(item)

        # mantém só os últimos N
        if len(chunks) > max_chunks:
            chunks[:] = chunks[-max_chunks:]

        # summary_live: concatena e corta
        prev = (state.get("summary_live") or "").strip()
        combined = (prev + " " + text).strip() if prev else text
        if len(combined) > DEFAULT_SUMMARY_MAX_CHARS:
            combined = combined[-DEFAULT_SUMMARY_MAX_CHARS:]

        state["chunks"] = chunks
        state["summary_live"] = combined
        return state

    @staticmethod
    def get_buffer_text(
        state: Dict[str, Any],
        max_chunks: int = 5,
        max_seconds: Optional[int] = None,
    ) -> str:
        """
        Retorna texto acumulado do buffer.

        - max_chunks: pega os últimos N chunks (em ordem)
        - max_seconds: opcional; limita por janela temporal (ex: últimos 30s)
        """
        chunks = state.get("chunks") or []
        if not isinstance(chunks, list) or not chunks:
            return ""

        if max_seconds is not None:
            now = _now_ts()
            cutoff = now - float(max_seconds)
            filtered = [c for c in chunks if float(c.get("ts", 0.0) or 0.0) >= cutoff]
            use = filtered[-max_chunks:] if max_chunks else filtered
        else:
            use = chunks[-max_chunks:] if max_chunks else chunks

        parts: List[str] = []
        for c in use:
            t = (c.get("text") or "").strip()
            if t:
                parts.append(t)

        return " ".join(parts).strip()


class InsightCooldown:
    """
    Cooldown de insights.

    Responsabilidade:
    - evitar mostrar insights a cada poucos segundos (anti-spam na UI)
    """
    @staticmethod
    def can_emit(state: Dict[str, Any], cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS) -> bool:
        last = float(state.get("meta", {}).get("last_insight_at", 0.0) or 0.0)
        return (_now_ts() - last) >= cooldown_seconds

    @staticmethod
    def mark_emitted(state: Dict[str, Any]) -> None:
        state.setdefault("meta", {})
        state["meta"]["last_insight_at"] = _now_ts()


class MemorySnapshot:
    """
    Cria um snapshot pequeno do estado para mandar ao LLM.

    Responsabilidades:
    - evitar enviar o estado inteiro (que pode crescer)
    - enviar apenas:
      * deal + signals
      * summary_live
      * buffer_text curto
      * últimos insights
    """
    @staticmethod
    def summarize_state_for_prompt(state: Dict[str, Any]) -> Dict[str, Any]:
        deal = state.get("deal", {}) or {}
        signals = state.get("signals", {}) or {}

        last_insights = (state.get("insights_feed") or [])[-5:]
        slim_insights = []
        for ins in last_insights:
            slim_insights.append({
                "type": ins.get("type"),
                "title": ins.get("title"),
                "why": ins.get("why") or ins.get("evidence"),
                "next_action": ins.get("next_action") or ins.get("action"),
                "confidence": ins.get("confidence"),
            })

        buffer_text = ChunkBuffer.get_buffer_text(state, max_chunks=5)

        return {
            "deal": {
                "stage": deal.get("stage"),
                "intent_level": deal.get("intent_level"),
                "opportunity_score": deal.get("opportunity_score"),
            },
            "signals": signals,
            "summary_live": state.get("summary_live", ""),
            "buffer_text": buffer_text,
            "recent_insights": slim_insights,
        }


class JsonRoomPersistence:
    """
    Persistência em disco: 1 JSON por room.

    Responsabilidades:
    - load_state(roomId): lê o JSON e faz merge com defaults
    - save_state(roomId): escreve JSON atualizado
    """
    @staticmethod
    def load_state(room_id: str, base_dir: str = DEFAULT_DIR) -> Dict[str, Any]:
        _ensure_dir(base_dir)
        fp = _room_path(room_id, base_dir)

        if not os.path.exists(fp):
            return RoomStateSchema.default(room_id)

        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)

            base = RoomStateSchema.default(room_id)
            merged = PatchMerger.apply(base, data)

            # garante campos novos (compat com versões antigas)
            merged.setdefault("chunks", [])
            merged.setdefault("summary_live", "")
            merged.setdefault("insights_feed", [])
            merged.setdefault("dedupe", {"recent_hashes": [], "window": DEFAULT_DEDUPE_WINDOW})
            merged.setdefault("meta", {"created_at": _now_ts(), "updated_at": _now_ts(), "last_insight_at": 0.0})

            return merged

        except Exception:
            # Se o JSON estiver corrompido, recomeça
            return RoomStateSchema.default(room_id)

    @staticmethod
    def save_state(room_id: str, state: Dict[str, Any], base_dir: str = DEFAULT_DIR) -> None:
        _ensure_dir(base_dir)
        fp = _room_path(room_id, base_dir)

        state = deepcopy(state)
        state.setdefault("meta", {})
        state["meta"]["updated_at"] = _now_ts()

        with open(fp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)


# ----------------------------
# API pública do módulo (mantém compatibilidade com seu app.py)
# ----------------------------
def load_state(room_id: str, base_dir: str = DEFAULT_DIR) -> Dict[str, Any]:
    return JsonRoomPersistence.load_state(room_id, base_dir)


def save_state(room_id: str, state: Dict[str, Any], base_dir: str = DEFAULT_DIR) -> None:
    JsonRoomPersistence.save_state(room_id, state, base_dir)


def apply_patch(state: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    return PatchMerger.apply(state, patch)


def insight_hash(insight: Dict[str, Any]) -> str:
    return InsightDeduper.insight_hash(insight)


def add_insights(
    state: Dict[str, Any],
    new_insights: List[Dict[str, Any]],
    dedupe_window: int = DEFAULT_DEDUPE_WINDOW,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    return InsightDeduper.add_insights(state, new_insights, dedupe_window)


def can_emit_insights(state: Dict[str, Any], cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS) -> bool:
    return InsightCooldown.can_emit(state, cooldown_seconds)


def mark_insight_emitted(state: Dict[str, Any]) -> None:
    InsightCooldown.mark_emitted(state)


def add_chunk_text(
    state: Dict[str, Any],
    text: str,
    seq: Optional[int] = None,
    lang: Optional[str] = None,
    ts: Optional[float] = None,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
) -> Dict[str, Any]:
    return ChunkBuffer.add_chunk_text(state, text, seq=seq, lang=lang, ts=ts, max_chunks=max_chunks)


def get_buffer_text(
    state: Dict[str, Any],
    max_chunks: int = 5,
    max_seconds: Optional[int] = None,
) -> str:
    return ChunkBuffer.get_buffer_text(state, max_chunks=max_chunks, max_seconds=max_seconds)


def summarize_state_for_prompt(state: Dict[str, Any]) -> Dict[str, Any]:
    return MemorySnapshot.summarize_state_for_prompt(state)
