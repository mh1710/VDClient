# signal_gate.py
"""
Signal Gate = "porteiro" de análise.

Objetivo:
- Evitar chamar o Groq a cada chunk (caro e desnecessário)
- Só liberar análise quando o *buffer acumulado* tiver contexto suficiente
- Reduzir falsos positivos quando o Whisper retorna vazio/ruído
- Aplicar cooldown por room depois de uma análise (não por chunk)

Uso típico (no app.py):
    gate = SignalGate(GateConfig(...))
    ok, payload = gate.should_analyze(room_id, buffer_text, just_transcribed_text)
    if ok:
        ... chama LLM ...
        gate.mark_analyzed(room_id)
"""

import re
from dataclasses import dataclass
from typing import Dict, Any, List, Tuple, Optional
from datetime import datetime, timedelta

# Regex simples pra contar palavras em pt-BR (inclui acentos)
_WORD_RE = re.compile(r"\b[\wáéíóúâêîôûãõçÁÉÍÓÚÂÊÎÔÛÃÕÇ]+\b", re.UNICODE)

# Palavras/fragmentos que indicam "sinais de venda" (ajuste livremente)
DEFAULT_KEYWORDS = {
    "pain": [
        "dor", "problema", "dificuldade", "complicado", "ruim", "frustr",
        "não funciona", "quebr", "lento"
    ],
    "budget": ["preço", "caro", "barato", "orçamento", "budget", "custo", "valor", "pagamento"],
    "timing": ["prazo", "quando", "até", "urgente", "semana", "mês", "hoje", "amanhã", "data"],
    "authority": ["decisor", "aprovação", "diretor", "sócio", "gestor", "equipe", "comitê"],
    "risk": ["concorr", "já tenho", "já uso", "não preciso", "depois", "não agora", "talvez"],
}


@dataclass
class GateConfig:
    """
    Configurações do gate.

    - min_words_buffer / min_chars_buffer:
      Mínimos do TEXTO ACUMULADO (buffer), não do chunk individual.

    - cooldown_seconds:
      Depois que uma análise roda, bloqueia novas análises por X segundos (por room),
      evitando "spam" de chamadas ao LLM.

    - keyword_score / min_score_to_pass:
      Scoring simples por hits de keywords.

    - length_fallback_words:
      Se o cliente estiver falando bastante mas sem keywords explícitas, ainda assim passa.

    - min_alpha_ratio:
      Proteção contra ruído: se o texto tiver muita pontuação/lixo e poucas letras/números,
      não vale analisar.
    """
    min_words_buffer: int = 18
    min_chars_buffer: int = 60

    cooldown_seconds: int = 8

    keyword_score: int = 2
    min_score_to_pass: int = 3

    length_fallback_words: int = 36

    min_alpha_ratio: float = 0.55


class KeywordSignalDetector:
    """
    Detector de sinais por keywords.

    O trabalho dele é:
    - achar ocorrências simples (substring) de keywords no texto
    - retornar lista de "hits" com tipo e match

    Observação:
    - Isso é propositalmente simples e rápido.
    - Se quiser evoluir depois: stemming, regex por palavra inteira, embeddings etc.
    """
    def __init__(self, keywords: Optional[Dict[str, List[str]]] = None):
        self.keywords = keywords or DEFAULT_KEYWORDS

    def hits(self, text: str) -> List[Dict[str, str]]:
        hits: List[Dict[str, str]] = []
        t = (text or "").lower()
        for ktype, words in self.keywords.items():
            for w in words:
                if w and w in t:
                    hits.append({"type": ktype, "match": w})
        return hits


class TextQualityHeuristics:
    """
    Heurísticas de qualidade do texto.

    O trabalho dele é:
    - contar palavras
    - medir "alpha_ratio" pra filtrar ruído

    Isso ajuda muito quando o Whisper retorna:
    - texto vazio
    - muitos símbolos/pontuação
    - coisas aleatórias que não são fala
    """
    @staticmethod
    def count_words(text: str) -> int:
        return len(_WORD_RE.findall(text or ""))

    @staticmethod
    def alpha_ratio(text: str) -> float:
        """
        Proporção de caracteres alfanuméricos no texto.
        Ex:
          - "oi tudo bem?" -> alto
          - ".... ----- ???" -> baixo
        """
        t = (text or "").strip()
        if not t:
            return 0.0
        alpha = sum(ch.isalnum() for ch in t)
        return alpha / max(1, len(t))


class CooldownTracker:
    """
    Controla cooldown por room.

    O trabalho dele é:
    - lembrar quando foi a última análise por room
    - informar se ainda está em cooldown
    - registrar o momento de análise
    """
    def __init__(self):
        self._last_analysis_at_by_room: Dict[str, datetime] = {}

    def is_in_cooldown(self, room_id: str, cooldown_seconds: int) -> Tuple[bool, Optional[float]]:
        room_id = room_id or "global"
        now = datetime.utcnow()
        last = self._last_analysis_at_by_room.get(room_id)
        if not last:
            return False, None
        delta = now - last
        if delta < timedelta(seconds=cooldown_seconds):
            remaining = max(0.0, cooldown_seconds - delta.total_seconds())
            return True, remaining
        return False, None

    def mark(self, room_id: str) -> None:
        room_id = room_id or "global"
        self._last_analysis_at_by_room[room_id] = datetime.utcnow()


class SignalGate:
    """
    Orquestrador do Gate.

    Fluxo do should_analyze:
    1) aplica cooldown por room
    2) valida buffer vazio
    3) valida mínimos de tamanho (palavras/chars)
    4) valida qualidade (alpha_ratio)
    5) calcula hits/score por keywords
    6) aplica fallback por tamanho (fala longa)
    7) retorna (ok, payload)
    """
    def __init__(self, cfg: Optional[GateConfig] = None, keywords: Optional[Dict[str, List[str]]] = None):
        self.cfg = cfg or GateConfig()
        self.detector = KeywordSignalDetector(keywords)
        self.quality = TextQualityHeuristics()
        self.cooldown = CooldownTracker()

    def should_analyze(
        self,
        room_id: str,
        buffer_text: str,
        just_transcribed_text: str = "",
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Decide se deve chamar o LLM agora.

        IMPORTANTÍSSIMO:
        - a decisão é baseada no TEXTO ACUMULADO (buffer_text)
        - just_transcribed_text é apenas informativo (debug/telemetria)

        Retorno:
          (ok, payload)
        payload sempre inclui:
          ok, score, hits
        """
        room_id = room_id or "global"
        buf = (buffer_text or "").strip()

        # 1) Cooldown (depois de análise)
        in_cd, remaining = self.cooldown.is_in_cooldown(room_id, self.cfg.cooldown_seconds)
        if in_cd:
            return False, {
                "ok": False,
                "score": 0,
                "hits": [{"type": "cooldown", "match": f"remaining={remaining:.1f}s"}],
            }

        # 2) Buffer vazio
        if not buf:
            return False, {
                "ok": False,
                "score": 0,
                "hits": [{"type": "empty_buffer", "match": "no_text"}],
            }

        # 3) Requisitos mínimos (no acumulado)
        word_count = self.quality.count_words(buf)
        char_count = len(buf)

        if word_count < self.cfg.min_words_buffer and char_count < self.cfg.min_chars_buffer:
            return False, {
                "ok": False,
                "score": 0,
                "hits": [{"type": "too_short_buffer", "match": f"{word_count}w/{char_count}c"}],
            }

        # 4) Proteção contra ruído (texto muito "não fala")
        alpha_ratio = self.quality.alpha_ratio(buf)
        if alpha_ratio < self.cfg.min_alpha_ratio:
            return False, {
                "ok": False,
                "score": 0,
                "hits": [{"type": "noisy_buffer", "match": f"alpha_ratio={alpha_ratio:.2f}"}],
            }

        # 5) Keyword scoring
        hits = self.detector.hits(buf)
        score = len(hits) * self.cfg.keyword_score

        # 6) Fallback por tamanho
        if score < self.cfg.min_score_to_pass:
            if word_count >= self.cfg.length_fallback_words:
                score = self.cfg.min_score_to_pass
                hits.append({"type": "length_fallback", "match": f">={self.cfg.length_fallback_words}w"})
            else:
                return False, {
                    "ok": False,
                    "score": score,
                    "hits": (hits[:12] if hits else [{"type": "no_signal", "match": "no_keywords"}]),
                }

        return True, {"ok": True, "score": score, "hits": hits[:12]}

    def mark_analyzed(self, room_id: str) -> None:
        """
        Deve ser chamado imediatamente após rodar uma análise no LLM.
        Isso ativa o cooldown e evita análises em sequência.
        """
        self.cooldown.mark(room_id or "global")
