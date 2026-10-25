"""
Google Gemini (Nano Banana) — geração e edição via generateContent.

Modelo padrão: `gemini-nano-banana-2.1` (GEMINI_IMAGE_MODEL).
"""
import base64
import os
import time

import requests

from .base import GeneratedImage, ImageGenerationError, ImageGenerationProvider

API_BASE = 'https://generativelanguage.googleapis.com/v1/models'


class GoogleImageProvider(ImageGenerationProvider):
    name = 'google'

    def __init__(self):
        self.api_key = (os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY') or '').strip()
        self._model = (os.getenv('GEMINI_IMAGE_MODEL') or 'gemini-nano-banana-2.1').strip()
        try:
            self.timeout = float(os.getenv('GEMINI_IMAGE_TIMEOUT', 180))
        except (TypeError, ValueError):
            self.timeout = 180.0

    def is_configured(self):
        return bool(self.api_key)

    @property
    def model(self):
        return self._model

    def estimated_cost_usd(self):
        try:
            return float(os.getenv('GEMINI_COST_PER_IMAGE_USD', 0.04))
        except (TypeError, ValueError):
            return 0.04

    def _call(self, parts, started):
        if not self.is_configured():
            raise ImageGenerationError('GEMINI_API_KEY não configurada', provider=self.name, code='not_configured')
        payload = {'contents': [{'parts': parts}]}
        try:
            resp = requests.post(
                f'{API_BASE}/{self._model}:generateContent',
                headers={'x-goog-api-key': self.api_key, 'Content-Type': 'application/json'},
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ImageGenerationError(f'Gemini: erro de rede ({exc})', provider=self.name, code='network', retryable=True)

        if resp.status_code >= 400:
            try:
                err = (resp.json() or {}).get('error') or {}
            except ValueError:
                err = {}
            retryable = resp.status_code in (408, 429) or resp.status_code >= 500
            raise ImageGenerationError(
                f"Gemini {resp.status_code}: {err.get('message') or resp.text[:300]}",
                provider=self.name, code=str(err.get('status') or resp.status_code),
                retryable=retryable, status=resp.status_code,
            )

        body = resp.json()
        candidates = body.get('candidates') or []
        for cand in candidates:
            for part in (cand.get('content') or {}).get('parts') or []:
                inline = part.get('inlineData') or part.get('inline_data')
                if inline and inline.get('data'):
                    return GeneratedImage(
                        data=base64.b64decode(inline['data']),
                        mime_type=inline.get('mimeType') or inline.get('mime_type') or 'image/png',
                        provider=self.name,
                        model=self._model,
                        response_id=body.get('responseId', ''),
                        cost_usd=self.estimated_cost_usd(),
                        usage=body.get('usageMetadata') or {},
                        latency_ms=int((time.time() - started) * 1000),
                    )
        finish = (candidates[0].get('finishReason') if candidates else '') or ''
        blocked = finish in ('SAFETY', 'PROHIBITED_CONTENT', 'IMAGE_SAFETY') or bool(body.get('promptFeedback', {}).get('blockReason'))
        raise ImageGenerationError(
            f'Gemini: resposta sem imagem ({finish or "vazia"})', provider=self.name,
            code='moderation_blocked' if blocked else 'empty', retryable=not blocked, user_error=blocked,
        )

    def generate(self, prompt, *, size='1024x1024', reference_images=None):
        started = time.time()
        parts = [{'text': prompt}]
        for ref in (reference_images or [])[:4]:
            parts.append({'inline_data': {'mime_type': ref.mime_type, 'data': base64.b64encode(ref.data).decode()}})
        return self._call(parts, started)

    def edit(self, image, prompt, *, size='1024x1024', mime_type='image/png'):
        started = time.time()
        parts = [
            {'text': prompt},
            {'inline_data': {'mime_type': mime_type, 'data': base64.b64encode(image).decode()}},
        ]
        return self._call(parts, started)
