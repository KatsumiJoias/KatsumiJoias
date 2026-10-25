"""
Adaptador de geração de imagem com provedor primário + fallback configuráveis.

    IMAGE_PROVIDER_PRIMARY   openai | google | mock     (padrão: openai se houver chave, senão mock)
    IMAGE_PROVIDER_FALLBACK  google | openai | mock | none  (padrão: google se houver chave)

Uso:
    from image_generation import generate_with_fallback
    result, attempts = generate_with_fallback(prompt, reference_images=refs)
"""
import os
import time

from .base import GeneratedImage, ImageGenerationError, ImageGenerationProvider, ReferenceImage
from .google_provider import GoogleImageProvider
from .mock_provider import MockImageProvider
from .openai_provider import OpenAIImageProvider

_REGISTRY = {
    'openai': OpenAIImageProvider,
    'google': GoogleImageProvider,
    'gemini': GoogleImageProvider,
    'mock': MockImageProvider,
}

__all__ = [
    'GeneratedImage', 'ImageGenerationError', 'ImageGenerationProvider', 'ReferenceImage',
    'get_provider', 'primary_provider', 'fallback_provider', 'generate_with_fallback', 'edit_with_fallback',
    'providers_status',
]


def get_provider(name):
    cls = _REGISTRY.get((name or '').strip().lower())
    return cls() if cls else None


def primary_provider():
    name = (os.getenv('IMAGE_PROVIDER_PRIMARY') or '').strip().lower()
    if not name:
        name = 'openai' if os.getenv('OPENAI_API_KEY') else ('google' if (os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY')) else 'mock')
    provider = get_provider(name) or MockImageProvider()
    if not provider.is_configured():
        print(f"[image_generation] Provedor primário '{name}' sem chave — usando mock.")
        return MockImageProvider()
    return provider


def fallback_provider(primary=None):
    name = (os.getenv('IMAGE_PROVIDER_FALLBACK') or '').strip().lower()
    if name in ('none', 'off', '0'):
        return None
    if not name:
        name = 'google' if (os.getenv('GEMINI_API_KEY') or os.getenv('GOOGLE_API_KEY')) else ''
    provider = get_provider(name)
    if not provider or not provider.is_configured():
        return None
    if primary is not None and provider.name == primary.name:
        return None
    return provider


def providers_status():
    primary = primary_provider()
    fallback = fallback_provider(primary)
    return {
        'primary': {'name': primary.name, 'model': primary.model},
        'fallback': {'name': fallback.name, 'model': fallback.model} if fallback else None,
    }


def _run_with_retries(call, provider, max_attempts, attempts_log):
    last_error = None
    for attempt in range(1, max_attempts + 1):
        started = time.time()
        try:
            result = call(provider)
            attempts_log.append({'provider': provider.name, 'model': provider.model, 'attempt': attempt,
                                 'ok': True, 'ms': int((time.time() - started) * 1000)})
            return result
        except ImageGenerationError as exc:
            last_error = exc
            attempts_log.append({'provider': provider.name, 'model': provider.model, 'attempt': attempt,
                                 'ok': False, 'code': exc.code, 'error': str(exc)[:300],
                                 'ms': int((time.time() - started) * 1000)})
            if exc.user_error or not exc.retryable:
                break
            time.sleep(min(8, 1.5 * (2 ** (attempt - 1))))
        except Exception as exc:  # erro inesperado do provedor
            last_error = ImageGenerationError(str(exc), provider=provider.name, code='unexpected', retryable=True)
            attempts_log.append({'provider': provider.name, 'attempt': attempt, 'ok': False,
                                 'code': 'unexpected', 'error': str(exc)[:300]})
    raise last_error


def _with_fallback(call):
    attempts = []
    primary = primary_provider()
    try:
        max_attempts = max(1, int(os.getenv('IMAGE_PROVIDER_MAX_RETRIES', 2)))
    except (TypeError, ValueError):
        max_attempts = 2
    try:
        return _run_with_retries(call, primary, max_attempts, attempts), attempts
    except ImageGenerationError as exc:
        if exc.user_error:
            raise
        fallback = fallback_provider(primary)
        if not fallback:
            raise
        print(f"[image_generation] {primary.name} falhou ({exc.code}); usando fallback {fallback.name}")
        return _run_with_retries(call, fallback, 1, attempts), attempts


def generate_with_fallback(prompt, *, size='1024x1024', reference_images=None):
    """Retorna (GeneratedImage, attempts_log). Lança ImageGenerationError se tudo falhar."""
    return _with_fallback(lambda p: p.generate(prompt, size=size, reference_images=reference_images))


def edit_with_fallback(image, prompt, *, size='1024x1024', mime_type='image/png'):
    return _with_fallback(lambda p: p.edit(image, prompt, size=size, mime_type=mime_type))
