"""
OpenAI GPT Image 2.5 (Images API).

Modelos: `gpt-image-2.5-sunburst` (qualidade, edição precisa) e `gpt-image-2.5-flare`
(mais rápido). Escolha via OPENAI_IMAGE_MODEL / OPENAI_IMAGE_EDIT_MODEL.
"""
import base64
import os
import time

import requests

from .base import GeneratedImage, ImageGenerationError, ImageGenerationProvider

API_BASE = 'https://api.openai.com/v1'

# US$ por 1M de tokens (GPT Image 2.5 — mesma tabela para Sunburst e Flare)
_RATE_TEXT_INPUT = 5.0
_RATE_IMAGE_INPUT = 8.0
_RATE_IMAGE_OUTPUT = 30.0


def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


class OpenAIImageProvider(ImageGenerationProvider):
    name = 'openai'

    def __init__(self):
        self.api_key = (os.getenv('OPENAI_API_KEY') or '').strip()
        self._model = (os.getenv('OPENAI_IMAGE_MODEL') or 'gpt-image-2.5-sunburst').strip()
        self.edit_model = (os.getenv('OPENAI_IMAGE_EDIT_MODEL') or self._model).strip()
        self.quality = (os.getenv('OPENAI_IMAGE_QUALITY') or 'high').strip()
        self.output_format = (os.getenv('OPENAI_IMAGE_FORMAT') or 'jpeg').strip()
        self.timeout = _env_float('OPENAI_IMAGE_TIMEOUT', 180)
        self.organization = (os.getenv('OPENAI_ORG_ID') or '').strip()

    def is_configured(self):
        return bool(self.api_key)

    @property
    def model(self):
        return self._model

    def estimated_cost_usd(self):
        return _env_float('DESIGNER_COST_PER_IMAGE_USD', 0.17)

    # ------------------------------------------------------------------
    def _headers(self, json_body=True):
        headers = {'Authorization': f'Bearer {self.api_key}'}
        if json_body:
            headers['Content-Type'] = 'application/json'
        if self.organization:
            headers['OpenAI-Organization'] = self.organization
        return headers

    def _mime(self):
        return {'jpeg': 'image/jpeg', 'webp': 'image/webp'}.get(self.output_format, 'image/png')

    def _cost_from_usage(self, usage):
        if not usage:
            return self.estimated_cost_usd()
        details = usage.get('input_tokens_details') or {}
        text_in = details.get('text_tokens', usage.get('input_tokens', 0) if not details else 0) or 0
        image_in = details.get('image_tokens', 0) or 0
        out = usage.get('output_tokens', 0) or 0
        return round(
            text_in / 1e6 * _RATE_TEXT_INPUT
            + image_in / 1e6 * _RATE_IMAGE_INPUT
            + out / 1e6 * _RATE_IMAGE_OUTPUT,
            5,
        )

    def _raise_for_response(self, resp):
        request_id = resp.headers.get('x-request-id', '')
        try:
            body = resp.json()
        except ValueError:
            body = {}
        err = (body or {}).get('error') or {}
        code = err.get('code') or ''
        message = err.get('message') or resp.text[:300]
        retryable = resp.status_code in (408, 409, 429) or resp.status_code >= 500
        if code == 'insufficient_quota' or code == 'billing_hard_limit_reached':
            retryable = False
        user_error = code == 'moderation_blocked' or err.get('type') == 'image_generation_user_error'
        raise ImageGenerationError(
            f'OpenAI {resp.status_code} {code}: {message} (request_id={request_id})',
            provider=self.name, code=code or str(resp.status_code),
            retryable=retryable and not user_error, user_error=user_error, status=resp.status_code,
        )

    def _parse(self, resp, model, started):
        if resp.status_code >= 400:
            self._raise_for_response(resp)
        body = resp.json()
        data = (body.get('data') or [{}])[0]
        b64 = data.get('b64_json')
        if not b64:
            raise ImageGenerationError('OpenAI: resposta sem imagem', provider=self.name, code='empty', retryable=True)
        usage = body.get('usage') or {}
        return GeneratedImage(
            data=base64.b64decode(b64),
            mime_type=self._mime(),
            provider=self.name,
            model=model,
            response_id=resp.headers.get('x-request-id', '') or str(body.get('created', '')),
            revised_prompt=data.get('revised_prompt') or '',
            cost_usd=self._cost_from_usage(usage),
            usage=usage,
            latency_ms=int((time.time() - started) * 1000),
        )

    # ------------------------------------------------------------------
    def generate(self, prompt, *, size='1024x1024', reference_images=None):
        if not self.is_configured():
            raise ImageGenerationError('OPENAI_API_KEY não configurada', provider=self.name, code='not_configured')
        started = time.time()
        try:
            if reference_images:
                # Com referências reais (pedras/metais/peça base) usamos o endpoint de edição
                files = [
                    ('image[]', (ref.filename, ref.data, ref.mime_type)) for ref in reference_images[:4]
                ]
                form = {
                    'model': self._model,
                    'prompt': prompt,
                    'size': size,
                    'quality': self.quality,
                    'output_format': self.output_format,
                    'n': '1',
                }
                resp = requests.post(f'{API_BASE}/images/edits', headers=self._headers(json_body=False),
                                     data=form, files=files, timeout=self.timeout)
            else:
                payload = {
                    'model': self._model,
                    'prompt': prompt,
                    'size': size,
                    'quality': self.quality,
                    'output_format': self.output_format,
                    'background': 'opaque',
                    'n': 1,
                }
                resp = requests.post(f'{API_BASE}/images/generations', headers=self._headers(),
                                     json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ImageGenerationError(f'OpenAI: erro de rede ({exc})', provider=self.name, code='network', retryable=True)
        return self._parse(resp, self._model, started)

    def edit(self, image, prompt, *, size='1024x1024', mime_type='image/png'):
        if not self.is_configured():
            raise ImageGenerationError('OPENAI_API_KEY não configurada', provider=self.name, code='not_configured')
        started = time.time()
        ext = {'image/jpeg': 'jpg', 'image/webp': 'webp'}.get(mime_type, 'png')
        try:
            resp = requests.post(
                f'{API_BASE}/images/edits',
                headers=self._headers(json_body=False),
                data={
                    'model': self.edit_model,
                    'prompt': prompt,
                    'size': size,
                    'quality': self.quality,
                    'output_format': self.output_format,
                    'n': '1',
                },
                files=[('image[]', (f'source.{ext}', image, mime_type))],
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ImageGenerationError(f'OpenAI: erro de rede ({exc})', provider=self.name, code='network', retryable=True)
        return self._parse(resp, self.edit_model, started)
