"""Contrato comum dos provedores de geração de imagem."""
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ReferenceImage:
    data: bytes
    mime_type: str = 'image/png'
    filename: str = 'reference.png'
    role: str = ''          # 'metal' | 'stone' | 'base'


@dataclass
class GeneratedImage:
    data: bytes
    mime_type: str
    provider: str
    model: str
    response_id: str = ''
    seed: Optional[int] = None
    revised_prompt: str = ''
    cost_usd: float = 0.0
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0


class ImageGenerationError(Exception):
    """
    retryable: falha transitória (rede, 429, 5xx) — vale tentar de novo/fallback.
    user_error: pedido recusado (moderação, prompt inválido) — não adianta repetir.
    """

    def __init__(self, message, *, provider='', code='', retryable=False, user_error=False, status=None):
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.retryable = retryable
        self.user_error = user_error
        self.status = status


class ImageGenerationProvider:
    """Interface: todo provedor implementa generate() e edit()."""

    name = 'base'

    def is_configured(self) -> bool:
        return False

    @property
    def model(self) -> str:
        return ''

    def generate(self, prompt: str, *, size: str = '1024x1024',
                 reference_images: Optional[List[ReferenceImage]] = None) -> GeneratedImage:
        raise NotImplementedError

    def edit(self, image: bytes, prompt: str, *, size: str = '1024x1024',
             mime_type: str = 'image/png') -> GeneratedImage:
        raise NotImplementedError

    def estimated_cost_usd(self) -> float:
        return 0.0
