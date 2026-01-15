# context_manager.py
"""
Context Manager (RAG leve por room) — versão com classes e comentários.

O que este módulo faz:
- Mantém um "contexto" por room em um arquivo JSON (persistência simples).
- Cada chunk pode guardar:
  - resumo curto, resumo longo, transcrição
  - embedding (opcional, se sentence-transformers estiver instalado)
- Permite recuperar chunks relevantes:
  - por similaridade (cosine) quando embeddings existem
  - ou por recência (fallback)

Por que isso existe:
- Seu pipeline recebe muitos chunks pequenos
- Você quer "memória de contexto" para:
  - ajudar o LLM a entender a conversa até agora
  - buscar rapidamente trechos relacionados ao que acabou de ser dito

Uso:
    from context_manager import ContextManager

    ctx = ContextManager()
    ctx.add_chunk_analysis(room_id, chunk_id, short_summary, full_summary, transcript_text)

    relevant = ctx.retrieve_relevant(room_id, query_text, k=5)
"""

from __future__ import annotations

import os
import json
import time
import math
import logging
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple

LOG = logging.getLogger("context_manager")


# =========================================================
# 1) Embeddings (opcional)
# =========================================================
# Se sentence-transformers estiver instalado, usamos embeddings.
# Caso contrário, o módulo funciona somente com recência.
try:
    from sentence_transformers import SentenceTransformer  # type: ignore

    _EMBED_MODEL = SentenceTransformer("all-mpnet-base-v2")
except Exception:
    _EMBED_MODEL = None


# =========================================================
# 2) Config / Paths
# =========================================================
STORE_DIR = os.getenv("CONTEXT_STORE_DIR", "./context_store")
os.makedirs(STORE_DIR, exist_ok=True)


def _now_ts() -> float:
    """Timestamp em segundos (epoch)."""
    return time.time()


def _safe_room_id(room_id: str) -> str:
    """
    Sanitiza o roomId para usar em nome de arquivo.
    Evita path traversal e caracteres estranhos.
    """
    room_id = (room_id or "global").strip() or "global"
    # Bem simples: troca / e \\ e espaços
    safe = room_id.replace("/", "_").replace("\\", "_").replace(" ", "_")
    return safe[:160]


def _room_path(room_id: str) -> str:
    """Caminho do arquivo JSON da room."""
    safe_id = _safe_room_id(room_id)
    return os.path.join(STORE_DIR, f"room_{safe_id}.json")


# =========================================================
# 3) Matemática — Cosine Similarity
# =========================================================
def _cosine_sim(a: List[float], b: List[float]) -> float:
    """
    Similaridade cosseno entre dois vetores.
    - 1.0: muito semelhante
    - 0.0: ortogonal
    """
    da = math.sqrt(sum(x * x for x in a))
    db = math.sqrt(sum(x * x for x in b))
    if da == 0 or db == 0:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (da * db)


# =========================================================
# 4) Schemas (dataclasses) — estrutura dos dados
# =========================================================
@dataclass
class ContextSettings:
    """
    Configurações da memória por room.

    - k_retrieval: quantos chunks retornar por consulta
    - retention_chunks: quantos chunks manter no arquivo
      (evita crescer infinito)
    """
    k_retrieval: int = 5
    retention_chunks: int = 500


@dataclass
class GlobalSummary:
    """
    Resumo global acumulado da room.
    Útil quando você quer um "resumo do que já aconteceu".
    """
    text: str = ""
    updated_at: Optional[float] = None


@dataclass
class ChunkRecord:
    """
    Representa um chunk persistido no JSON.

    Campos:
    - chunk_id: identificador do trecho (UUID do seu pipeline)
    - short_summary/full_summary: opcional (se você gerar)
    - transcript: texto transcrito
    - embedding: lista de floats (opcional)
    - created_at: timestamp
    """
    chunk_id: str
    short_summary: str
    full_summary: str
    transcript: str
    embedding: Optional[List[float]]
    created_at: float


# =========================================================
# 5) Storage — leitura/gravação do JSON
# =========================================================
class JsonRoomStore:
    """
    Camada responsável por:
    - carregar o JSON da room
    - criar estrutura default se não existir
    - salvar alterações

    Separar isso do ContextManager deixa o código mais limpo e testável.
    """

    def __init__(self, base_dir: str = STORE_DIR, default_settings: Optional[ContextSettings] = None):
        self.base_dir = base_dir
        self.default_settings = default_settings or ContextSettings()
        os.makedirs(self.base_dir, exist_ok=True)

    def _default_room_data(self, room_id: str, settings: Optional[ContextSettings] = None) -> Dict[str, Any]:
        """
        Estrutura padrão do arquivo JSON da room.
        """
        s = settings or self.default_settings
        return {
            "roomId": room_id,
            "created_at": _now_ts(),
            "settings": {"k_retrieval": s.k_retrieval, "retention_chunks": s.retention_chunks},
            "global_summary": {"text": "", "updated_at": None},
            "chunks": [],  # lista de chunks (newest first)
        }

    def load(self, room_id: str) -> Dict[str, Any]:
        """
        Lê o JSON da room.
        Se não existir, cria.
        """
        room_id = room_id or "global"
        path = _room_path(room_id)

        if not os.path.exists(path):
            data = self._default_room_data(room_id)
            self.save(room_id, data)
            return data

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            # Se o arquivo corromper, recomeça para não travar o serviço
            LOG.exception("Room JSON corrompido. Recriando: %s", path)
            data = self._default_room_data(room_id)
            self.save(room_id, data)
            return data

        # Garantir campos essenciais (migração simples)
        data.setdefault("roomId", room_id)
        data.setdefault("created_at", _now_ts())
        data.setdefault("settings", {"k_retrieval": self.default_settings.k_retrieval,
                                     "retention_chunks": self.default_settings.retention_chunks})
        data.setdefault("global_summary", {"text": "", "updated_at": None})
        data.setdefault("chunks", [])
        return data

    def save(self, room_id: str, data: Dict[str, Any]) -> None:
        """
        Persiste JSON da room.
        """
        room_id = room_id or "global"
        path = _room_path(room_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


# =========================================================
# 6) Embedding service — encapsula encode
# =========================================================
class EmbeddingService:
    """
    Encapsula o uso do sentence-transformers.
    Assim, o resto do sistema não depende diretamente da lib.

    Comportamento:
    - available = False quando não há modelo
    - encode(text): retorna embedding ou None
    """

    def __init__(self):
        self.model = _EMBED_MODEL

    @property
    def available(self) -> bool:
        return self.model is not None

    def encode(self, text: str) -> Optional[List[float]]:
        """
        Gera embedding normalizado.
        Retorna None se falhar.
        """
        if not self.model:
            return None
        try:
            v = self.model.encode(text or "", normalize_embeddings=True)
            return [float(x) for x in v]
        except Exception:
            LOG.exception("Falha ao gerar embedding")
            return None


# =========================================================
# 7) ContextManager — API principal usada pelo app
# =========================================================
class ContextManager:
    """
    API principal de contexto por room.

    Funções:
    - create_room: cria do zero
    - add_chunk_analysis: adiciona chunk (com embedding opcional)
    - retrieve_relevant: recupera chunks relevantes (embedding) ou recentes (fallback)
    - update_global_summary / get_global_summary
    - list_chunks
    """

    def __init__(
        self,
        store: Optional[JsonRoomStore] = None,
        embedder: Optional[EmbeddingService] = None,
        default_settings: Optional[ContextSettings] = None,
    ):
        self.settings = default_settings or ContextSettings()
        self.store = store or JsonRoomStore(default_settings=self.settings)
        self.embedder = embedder or EmbeddingService()

    # ---------- criação ----------
    def create_room(self, room_id: str, settings: Optional[Dict[str, Any]] = None) -> None:
        """
        Cria (ou sobrescreve) uma room.

        settings: dict opcional com:
          - k_retrieval
          - retention_chunks
        """
        room_id = room_id or "global"

        if settings:
            s = ContextSettings(
                k_retrieval=int(settings.get("k_retrieval", self.settings.k_retrieval)),
                retention_chunks=int(settings.get("retention_chunks", self.settings.retention_chunks)),
            )
        else:
            s = self.settings

        data = self.store._default_room_data(room_id, settings=s)
        self.store.save(room_id, data)

    # ---------- escrita ----------
    def add_chunk_analysis(
        self,
        room_id: str,
        chunk_id: str,
        short_summary: str,
        full_summary: str,
        transcript_text: str,
    ) -> None:
        """
        Adiciona um chunk ao JSON.

        Observações:
        - embedding é calculado em cima do short_summary (ou transcript fallback)
          para melhorar qualidade na busca.
        - chunks são inseridos no topo (newest first)
        - aplica retenção para não crescer infinito
        """
        room_id = room_id or "global"
        data = self.store.load(room_id)

        # Decide o texto base do embedding
        emb_text = (short_summary or "").strip() or (transcript_text or "").strip()
        emb = self.embedder.encode(emb_text) if emb_text else None

        chunk = {
            "chunk_id": chunk_id,
            "short_summary": short_summary or "",
            "full_summary": full_summary or "",
            "transcript": transcript_text or "",
            "embedding": emb,
            "created_at": _now_ts(),
        }

        # newest first
        data["chunks"].insert(0, chunk)

        # retenção
        settings = data.get("settings") or {}
        retention = int(settings.get("retention_chunks", self.settings.retention_chunks))
        if len(data["chunks"]) > retention:
            data["chunks"] = data["chunks"][:retention]

        self.store.save(room_id, data)

    # ---------- leitura / retrieval ----------
    def retrieve_relevant(self, room_id: str, query_text: str, k: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Retorna até k chunks relevantes.

        - Com embeddings:
            - calcula embedding da query
            - rankeia por cosine similarity
        - Sem embeddings:
            - devolve os k mais recentes (fallback)

        Retorno:
            [{"score": 0.92, "chunk": {...}}, ...]
        """
        room_id = room_id or "global"
        data = self.store.load(room_id)

        settings = data.get("settings") or {}
        k = int(k or settings.get("k_retrieval", self.settings.k_retrieval))

        chunks = data.get("chunks", []) or []
        if not chunks or k <= 0:
            return []

        # Se temos embeddings armazenados + embedder disponível, tenta busca semântica
        has_any_embedding = any(bool(c.get("embedding")) for c in chunks)
        if self.embedder.available and has_any_embedding:
            q_emb = self.embedder.encode(query_text or "")
            if q_emb:
                scored: List[Tuple[float, Dict[str, Any]]] = []
                for c in chunks:
                    emb = c.get("embedding")
                    if not emb:
                        continue
                    score = _cosine_sim(q_emb, emb)
                    scored.append((float(score), c))

                scored.sort(key=lambda x: x[0], reverse=True)
                return [{"score": s, "chunk": c} for s, c in scored[:k]]

        # Fallback: recência
        return [{"score": None, "chunk": c} for c in chunks[:k]]

    # ---------- global summary ----------
    def update_global_summary(self, room_id: str, new_summary_text: str) -> None:
        """
        Atualiza o summary global da room.
        """
        room_id = room_id or "global"
        data = self.store.load(room_id)
        data["global_summary"] = {"text": new_summary_text or "", "updated_at": _now_ts()}
        self.store.save(room_id, data)

    def get_global_summary(self, room_id: str) -> Optional[Dict[str, Any]]:
        """
        Retorna o summary global.
        """
        room_id = room_id or "global"
        data = self.store.load(room_id)
        return data.get("global_summary")

    # ---------- debug / util ----------
    def list_chunks(self, room_id: str) -> List[Dict[str, Any]]:
        """
        Lista os chunks (newest first).
        """
        room_id = room_id or "global"
        data = self.store.load(room_id)
        return data.get("chunks", []) or []
