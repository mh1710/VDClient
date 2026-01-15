# utils.py
"""
Utils = utilitários pequenos e reutilizáveis do servidor.

Objetivo:
- Centralizar funções "infra" que aparecem em vários arquivos
  (ex.: conversão de áudio com ffmpeg, remoção segura de arquivos)
- Deixar o app.py mais limpo e focado em "fluxo de negócio"

Conteúdo:
1) AudioNormalizer
   - Converte qualquer áudio de entrada (webm/ogg/wav/opus) para WAV 16kHz mono PCM16
   - Usa ffmpeg (precisa estar no PATH)

2) FileOps
   - safe_remove: remove arquivo de forma best-effort (sem quebrar o pipeline)
"""

import os
import logging
import subprocess
from dataclasses import dataclass
from typing import Optional

LOG = logging.getLogger("audio_utils")


# ----------------------------
# Exceptions específicas
# ----------------------------
class FfmpegError(RuntimeError):
    """Erro lançado quando o ffmpeg falha na conversão."""
    pass


# ----------------------------
# Classes utilitárias
# ----------------------------
@dataclass(frozen=True)
class AudioNormalizeConfig:
    """
    Config da normalização de áudio.

    - sample_rate: taxa alvo (ex: 16000)
    - channels: mono = 1
    - sample_fmt: PCM 16-bit
    """
    sample_rate: int = 16000
    channels: int = 1
    sample_fmt: str = "s16"
    output_format: str = "wav"  # mantém wav para whisper


class AudioNormalizer:
    """
    Responsável por normalizar/converter áudio para um formato padrão do STT.

    Por que existe:
    - O MediaRecorder costuma gerar WebM/Opus (ou OGG/Opus)
    - O whisper (e pipeline em geral) fica mais estável em WAV mono 16k PCM16
    - Isso evita erros como "EBML header parsing failed" e reduz variância no decode
    """

    def __init__(self, cfg: Optional[AudioNormalizeConfig] = None):
        self.cfg = cfg or AudioNormalizeConfig()

    def normalize_to_wav16k(self, src_path: str, dst_path: str) -> None:
        """
        Converte qualquer input para WAV PCM16LE mono 16k.
        Requer ffmpeg disponível no PATH.

        Levanta:
          - FileNotFoundError se src_path não existir
          - FfmpegError se o ffmpeg falhar
        """
        if not src_path or not os.path.exists(src_path):
            raise FileNotFoundError(f"Arquivo de entrada não encontrado: {src_path}")

        # Observação:
        # -ffmpeg -y: sobrescreve destino
        # -ac 1: mono
        # -ar 16000: sample rate
        # -sample_fmt s16: PCM16
        # -f wav: saída wav
        cmd = [
            "ffmpeg", "-y",
            "-i", src_path,
            "-ac", str(self.cfg.channels),
            "-ar", str(self.cfg.sample_rate),
            "-sample_fmt", self.cfg.sample_fmt,
            "-f", self.cfg.output_format,
            dst_path,
        ]

        LOG.debug("Running ffmpeg: %s", " ".join(cmd))

        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode != 0:
            stderr = p.stderr.decode("utf-8", errors="ignore")
            # ajuda a debugar sem jogar stdout gigante
            raise FfmpegError(f"ffmpeg failed (code={p.returncode}): {stderr}")


class FileOps:
    """
    Operações simples com arquivo (best-effort).

    Por que existe:
    - Em pipelines de upload/chunk, arquivo pode já ter sido deletado,
      estar travado, ou ter sumido por race condition.
    - Aqui a regra é: "tentar limpar sem quebrar o servidor".
    """

    @staticmethod
    def safe_remove(path: str) -> None:
        """
        Remove um arquivo se existir, sem levantar erro.
        """
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            # best-effort extra
            try:
                os.remove(path)
            except Exception:
                pass


# ----------------------------
# Compatibilidade com seu código atual (API funcional)
# ----------------------------
# Se você já usa normalize_audio_to_wav16k(...) no app.py,
# essa função continua existindo, só que agora delega para a classe.
_default_audio_normalizer = AudioNormalizer()


def normalize_audio_to_wav16k(src_path: str, dst_path: str) -> None:
    """
    Wrapper compatível com versão anterior.
    """
    _default_audio_normalizer.normalize_to_wav16k(src_path, dst_path)


def safe_remove(path: str) -> None:
    """
    Wrapper compatível com versão anterior.
    """
    FileOps.safe_remove(path)
