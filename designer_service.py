"""
Serviço do "Crie sua jóia": jobs de geração assíncronos, persistência, limites e orçamentos.

Coleções Firestore:
    jewelry_designs/{id}   cada geração (ou ajuste) com escolhas, prompt, imagens, custo, timestamps
    designer_cache/{hash}  cache por hash das escolhas → reaproveita imagens já geradas
    designer_usage/{dia}   contador diário (gerações e custo estimado) para o teto de gastos
    quotes/{id}            pedidos de orçamento (aba "Orçamentos" do CRM)

Os jobs rodam em threads do próprio processo (Cloud Run precisa de CPU sempre alocada:
`--no-cpu-throttling`). O estado fica no Firestore, então o polling funciona em qualquer instância.
"""
import hashlib
import io
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from firebase_admin import firestore

import image_generation
import jewelry_designer as jd
from database import db, upload_bytes, download_public_url, utc_now, format_datetime_brt
from image_generation import ImageGenerationError, ReferenceImage

DESIGNS = 'jewelry_designs'
CACHE = 'designer_cache'
USAGE = 'designer_usage'
QUOTES = 'quotes'

STATUS_QUEUED = 'queued'
STATUS_RUNNING = 'running'
STATUS_COMPLETED = 'completed'
STATUS_PARTIAL = 'partial'
STATUS_FAILED = 'failed'
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)

QUOTE_STATUSES = [
    ('novo', 'Novo'),
    ('em_analise', 'Em análise'),
    ('orcamento_enviado', 'Orçamento enviado'),
    ('aprovado', 'Aprovado'),
    ('em_producao', 'Em produção'),
    ('concluido', 'Concluído'),
    ('recusado', 'Recusado'),
    ('cancelado', 'Cancelado'),
]
QUOTE_STATUS_LABELS = dict(QUOTE_STATUSES)
QUOTE_OPEN_STATUSES = ('novo', 'em_analise', 'orcamento_enviado')

_job_pool = ThreadPoolExecutor(max_workers=int(os.getenv('DESIGNER_JOB_WORKERS', 4)), thread_name_prefix='designer-job')
_image_pool = ThreadPoolExecutor(max_workers=int(os.getenv('DESIGNER_IMAGE_WORKERS', 8)), thread_name_prefix='designer-img')
_JOB_STALE_SECONDS = 8 * 60


class DesignerLimitError(Exception):
    pass


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def images_per_generation():
    return max(1, min(2, _env_int('DESIGNER_IMAGES_PER_GENERATION', 2)))


def _today_key():
    brt = timezone(timedelta(hours=-3))
    return datetime.now(brt).strftime('%Y-%m-%d')


def _as_datetime(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def hash_ip(ip):
    salt = os.getenv('FLASK_SECRET_KEY', '')[:16]
    return hashlib.sha256(f'{salt}:{ip or ""}'.encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# Limites e custos
# ---------------------------------------------------------------------------

def _user_designs(user_id, limit=200):
    docs = db.collection(DESIGNS).where('user_id', '==', user_id).limit(limit).stream()
    out = []
    for doc in docs:
        data = doc.to_dict() or {}
        data['id'] = doc.id
        out.append(data)
    return out


def check_limits(user_id, kind='generate'):
    """Lança DesignerLimitError com mensagem amigável quando algum limite estoura."""
    designs = _user_designs(user_id)
    now = datetime.now(timezone.utc)

    active = [d for d in designs if d.get('status') in ACTIVE_STATUSES
              and (_as_datetime(d.get('updated_at')) or now) > now - timedelta(seconds=_JOB_STALE_SECONDS)]
    if len(active) >= _env_int('DESIGNER_MAX_CONCURRENT_PER_USER', 1):
        raise DesignerLimitError('Sua criação anterior ainda está sendo gerada. Aguarde ela terminar para criar outra.')

    since = now - timedelta(hours=24)
    recent = [d for d in designs if (_as_datetime(d.get('created_at')) or now) > since and not d.get('cached_from')]
    daily_limit = _env_int('DESIGNER_DAILY_LIMIT_PER_USER', 10)
    if len(recent) >= daily_limit:
        raise DesignerLimitError(
            f'Você atingiu o limite de {daily_limit} criações nas últimas 24 horas. '
            'Volte amanhã ou fale com a nossa equipe pelo WhatsApp para seguir com seu projeto.'
        )

    budget = _env_float('DESIGNER_DAILY_BUDGET_USD', 25.0)
    if budget > 0:
        usage = db.collection(USAGE).document(_today_key()).get()
        spent = float((usage.to_dict() or {}).get('cost_usd', 0)) if usage.exists else 0.0
        if spent >= budget:
            raise DesignerLimitError(
                'O ateliê virtual atingiu o volume máximo de criações de hoje. '
                'Tente novamente amanhã — ou fale com a nossa equipe pelo WhatsApp.'
            )


def _record_usage(cost_usd, images):
    try:
        db.collection(USAGE).document(_today_key()).set({
            'cost_usd': firestore.Increment(float(cost_usd or 0)),
            'images': firestore.Increment(int(images or 0)),
            'updated_at': utc_now(),
        }, merge=True)
    except Exception as exc:
        print(f'[designer] usage log error: {exc}')


# ---------------------------------------------------------------------------
# Leitura / serialização
# ---------------------------------------------------------------------------

def get_design(design_id):
    doc = db.collection(DESIGNS).document(design_id).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    data['id'] = doc.id
    return data


def _mark_stale_if_needed(design):
    if design.get('status') not in ACTIVE_STATUSES:
        return design
    updated = _as_datetime(design.get('updated_at'))
    if updated and updated < datetime.now(timezone.utc) - timedelta(seconds=_JOB_STALE_SECONDS):
        db.collection(DESIGNS).document(design['id']).update({
            'status': STATUS_FAILED,
            'error_message': 'A geração demorou mais que o esperado. Tente novamente.',
            'updated_at': utc_now(),
        })
        design['status'] = STATUS_FAILED
        design['error_message'] = 'A geração demorou mais que o esperado. Tente novamente.'
    return design


def serialize_design(design, include_internal=False):
    design = _mark_stale_if_needed(design)
    images = []
    for img in design.get('images') or []:
        item = {
            'index': img.get('index'),
            'status': img.get('status'),
            'url': img.get('url') or '',
        }
        if include_internal:
            item.update({k: img.get(k) for k in ('provider', 'model', 'response_id', 'seed', 'cost_usd',
                                                 'fallback_used', 'attempts', 'error', 'latency_ms')})
        images.append(item)
    out = {
        'id': design.get('id'),
        'kind': design.get('kind', 'generate'),
        'status': design.get('status'),
        'error_message': design.get('error_message') or '',
        'choices': design.get('choices') or {},
        'labels': design.get('labels') or {},
        'details_text': design.get('details_text') or '',
        'edit_instruction': design.get('edit_instruction') or '',
        'parent_design_id': design.get('parent_design_id') or '',
        'warnings': design.get('warnings') or [],
        'images': images,
        'quote_id': design.get('quote_id') or '',
        'cached': bool(design.get('cached_from')),
        'created_at': format_datetime_brt(design.get('created_at')) if design.get('created_at') else '',
    }
    if include_internal:
        out.update({
            'prompt': design.get('prompt') or '',
            'prompt_version': design.get('prompt_version') or '',
            'provider_primary': design.get('provider_primary') or '',
            'model_primary': design.get('model_primary') or '',
            'actual_cost_usd': design.get('actual_cost_usd') or 0,
            'estimated_cost_usd': design.get('estimated_cost_usd') or 0,
            'user_email': design.get('user_email') or '',
            'cache_key': design.get('cache_key') or '',
        })
    return out


def list_user_designs(user_id, limit=40):
    designs = _user_designs(user_id, limit=300)
    designs.sort(key=lambda d: _as_datetime(d.get('created_at')) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return [serialize_design(d) for d in designs[:limit]]


# ---------------------------------------------------------------------------
# Criação de jobs
# ---------------------------------------------------------------------------

def _cache_lookup(key):
    if os.getenv('DESIGNER_CACHE', '1') == '0':
        return None
    doc = db.collection(CACHE).document(key).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    ttl_days = _env_int('DESIGNER_CACHE_TTL_DAYS', 30)
    created = _as_datetime(data.get('created_at'))
    if created and created < datetime.now(timezone.utc) - timedelta(days=ttl_days):
        return None
    images = [i for i in (data.get('images') or []) if i.get('url')]
    return {**data, 'images': images} if images else None


def create_generation(user_id, user_email, choices, details_text, warnings, ip_hash=''):
    """Cria o documento do design e agenda o job. Retorna o design serializado."""
    primary = image_generation.primary_provider()
    model = primary.model
    key = jd.cache_key(choices, details_text, model)
    count = images_per_generation()
    refs = jd.reference_image_paths(choices)
    prompts = [jd.build_prompt(choices, details_text, variation='a' if i == 0 else 'b', has_references=bool(refs))
               for i in range(count)]
    now = utc_now()
    base_doc = {
        'user_id': user_id,
        'user_email': user_email or '',
        'kind': 'generate',
        'choices': choices,
        'labels': jd.choice_labels(choices),
        'details_text': details_text or '',
        'warnings': warnings or [],
        'prompt': prompts[0],
        'prompts': prompts,
        'prompt_version': jd.PROMPT_VERSION,
        'reference_images': [os.path.relpath(p, jd.REFS_DIR).replace('\\', '/') for _, p in refs],
        'cache_key': key,
        'provider_primary': primary.name,
        'model_primary': model,
        'requested_count': count,
        'estimated_cost_usd': round(primary.estimated_cost_usd() * count, 4),
        'actual_cost_usd': 0.0,
        'ip_hash': ip_hash,
        'created_at': now,
        'updated_at': now,
    }

    cached = _cache_lookup(key)
    ref = db.collection(DESIGNS).document()
    if cached:
        base_doc.update({
            'status': STATUS_COMPLETED,
            'cached_from': cached.get('design_id', ''),
            'images': [
                {**img, 'index': idx, 'status': 'done', 'cost_usd': 0.0}
                for idx, img in enumerate(cached['images'][:count])
            ],
            'completed_at': now,
        })
        ref.set(base_doc)
        base_doc['id'] = ref.id
        return serialize_design(base_doc)

    base_doc.update({
        'status': STATUS_QUEUED,
        'images': [{'index': i, 'status': 'pending'} for i in range(count)],
    })
    ref.set(base_doc)
    base_doc['id'] = ref.id
    _job_pool.submit(_run_generation_job, ref.id)
    return serialize_design(base_doc)


def create_edit(user_id, user_email, parent, image_index, instruction, ip_hash=''):
    images = parent.get('images') or []
    source = next((i for i in images if i.get('index') == image_index and i.get('url')), None)
    if not source:
        raise ValueError('Imagem de origem não encontrada.')
    primary = image_generation.primary_provider()
    prompt = jd.build_edit_prompt(instruction)
    now = utc_now()
    doc = {
        'user_id': user_id,
        'user_email': user_email or '',
        'kind': 'edit',
        'parent_design_id': parent['id'],
        'source_image_index': image_index,
        'source_image_url': source['url'],
        'choices': parent.get('choices') or {},
        'labels': parent.get('labels') or {},
        'details_text': parent.get('details_text') or '',
        'edit_instruction': instruction,
        'prompt': prompt,
        'prompt_version': jd.PROMPT_VERSION,
        'provider_primary': primary.name,
        'model_primary': getattr(primary, 'edit_model', primary.model),
        'requested_count': 1,
        'estimated_cost_usd': round(primary.estimated_cost_usd(), 4),
        'actual_cost_usd': 0.0,
        'status': STATUS_QUEUED,
        'images': [{'index': 0, 'status': 'pending'}],
        'ip_hash': ip_hash,
        'created_at': now,
        'updated_at': now,
    }
    ref = db.collection(DESIGNS).document()
    ref.set(doc)
    doc['id'] = ref.id
    _job_pool.submit(_run_edit_job, ref.id)
    return serialize_design(doc)


def retry_design(design):
    """Reenfileira apenas as variações que falharam."""
    images = design.get('images') or []
    for img in images:
        if img.get('status') != 'done':
            img.update({'status': 'pending', 'error': ''})
    db.collection(DESIGNS).document(design['id']).update({
        'status': STATUS_QUEUED, 'images': images, 'error_message': '', 'updated_at': utc_now(),
        'retries': firestore.Increment(1),
    })
    if design.get('kind') == 'edit':
        _job_pool.submit(_run_edit_job, design['id'])
    else:
        _job_pool.submit(_run_generation_job, design['id'])


# ---------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------

_doc_locks = {}
_doc_locks_guard = threading.Lock()


def _doc_lock(design_id):
    with _doc_locks_guard:
        return _doc_locks.setdefault(design_id, threading.Lock())


def _update_image(design_id, index, data):
    """Atualiza uma variação dentro do array `images` (lock por documento)."""
    with _doc_lock(design_id):
        ref = db.collection(DESIGNS).document(design_id)
        snap = ref.get()
        if not snap.exists:
            return
        images = (snap.to_dict() or {}).get('images') or []
        for img in images:
            if img.get('index') == index:
                img.update(data)
        ref.update({'images': images, 'updated_at': utc_now()})


def _store_result(user_id, design_id, index, result):
    ext = {'image/jpeg': 'jpg', 'image/webp': 'webp'}.get(result.mime_type, 'png')
    path = f'designs/{user_id}/{design_id}/{index}-{uuid.uuid4().hex[:8]}.{ext}'
    url = upload_bytes(result.data, path, content_type=result.mime_type)
    if not url:
        # Sem Storage disponível (ambiente local sem bucket): grava em static/ para desenvolvimento
        local_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'designer', 'generated', design_id)
        os.makedirs(local_dir, exist_ok=True)
        filename = f'{index}.{ext}'
        with open(os.path.join(local_dir, filename), 'wb') as fh:
            fh.write(result.data)
        url = f'/static/designer/generated/{design_id}/{filename}'
        path = url
    return url, path


def _load_references(design):
    refs = []
    for rel in design.get('reference_images') or []:
        full = os.path.join(jd.REFS_DIR, rel)
        try:
            with open(full, 'rb') as fh:
                data = fh.read()
            mime = 'image/png' if full.lower().endswith('.png') else ('image/webp' if full.lower().endswith('.webp') else 'image/jpeg')
            refs.append(ReferenceImage(data=data, mime_type=mime, filename=os.path.basename(full), role=rel.split('/')[0]))
        except OSError as exc:
            print(f'[designer] reference not readable {rel}: {exc}')
    return refs


def _generate_one(design, index, prompt, refs):
    design_id = design['id']
    _update_image(design_id, index, {'status': 'running', 'started_at': utc_now()})
    try:
        result, attempts = image_generation.generate_with_fallback(prompt, reference_images=refs or None)
        url, path = _store_result(design['user_id'], design_id, index, result)
        payload = {
            'status': 'done',
            'url': url,
            'storage_path': path,
            'provider': result.provider,
            'model': result.model,
            'response_id': result.response_id,
            'seed': result.seed,
            'revised_prompt': result.revised_prompt,
            'cost_usd': result.cost_usd,
            'usage': result.usage,
            'latency_ms': result.latency_ms,
            'attempts': attempts,
            'fallback_used': result.provider != design.get('provider_primary'),
            'finished_at': utc_now(),
            'error': '',
        }
        _update_image(design_id, index, payload)
        return payload
    except ImageGenerationError as exc:
        message = ('Não foi possível gerar esta imagem com essa descrição. Ajuste os detalhes e tente de novo.'
                   if exc.user_error else 'Falha temporária ao gerar esta variação.')
        _update_image(design_id, index, {'status': 'failed', 'error': message, 'error_code': exc.code,
                                         'error_detail': str(exc)[:500], 'finished_at': utc_now()})
        return {'status': 'failed', 'user_error': exc.user_error}
    except Exception as exc:
        print(f'[designer] unexpected error on {design_id}/{index}: {exc}')
        _update_image(design_id, index, {'status': 'failed', 'error': 'Falha inesperada ao gerar esta variação.',
                                         'error_detail': str(exc)[:500], 'finished_at': utc_now()})
        return {'status': 'failed'}


def _finish(design_id, results, total, cache=False):
    done = [r for r in results if r.get('status') == 'done']
    cost = round(sum(float(r.get('cost_usd') or 0) for r in done), 5)
    if len(done) == total:
        status, message = STATUS_COMPLETED, ''
    elif done:
        status, message = STATUS_PARTIAL, 'Uma das variações não pôde ser gerada. Você pode tentar novamente.'
    else:
        user_error = any(r.get('user_error') for r in results)
        status = STATUS_FAILED
        message = ('Não conseguimos gerar a imagem com essa combinação/descrição. Ajuste os detalhes e tente de novo.'
                   if user_error else 'O serviço de imagens está instável no momento. Tente novamente em instantes.')
    ref = db.collection(DESIGNS).document(design_id)
    ref.update({
        'status': status,
        'error_message': message,
        'actual_cost_usd': firestore.Increment(cost),
        'completed_at': utc_now(),
        'updated_at': utc_now(),
    })
    _record_usage(cost, len(done))
    if cache and status == STATUS_COMPLETED:
        design = get_design(design_id)
        if design and design.get('cache_key'):
            db.collection(CACHE).document(design['cache_key']).set({
                'design_id': design_id,
                'model': design.get('model_primary'),
                'images': [{'url': i.get('url'), 'storage_path': i.get('storage_path'), 'provider': i.get('provider'),
                            'model': i.get('model'), 'response_id': i.get('response_id')}
                           for i in design.get('images') or []],
                'created_at': utc_now(),
            })
    print(f'[designer] {design_id}: {status} ({len(done)}/{total}) custo≈US$ {cost:.4f}')


def _run_generation_job(design_id):
    try:
        design = get_design(design_id)
        if not design:
            return
        db.collection(DESIGNS).document(design_id).update({'status': STATUS_RUNNING, 'updated_at': utc_now()})
        prompts = design.get('prompts') or [design.get('prompt')]
        refs = _load_references(design)
        pending = [img for img in design.get('images') or [] if img.get('status') != 'done']
        futures = [
            _image_pool.submit(_generate_one, design, img['index'], prompts[min(img['index'], len(prompts) - 1)], refs)
            for img in pending
        ]
        results = [f.result() for f in futures]
        already_done = [img for img in design.get('images') or [] if img.get('status') == 'done']
        _finish(design_id, results + [{'status': 'done', 'cost_usd': 0} for _ in already_done],
                design.get('requested_count') or len(results), cache=True)
    except Exception as exc:
        print(f'[designer] job {design_id} crashed: {exc}')
        db.collection(DESIGNS).document(design_id).update({
            'status': STATUS_FAILED, 'error_message': 'Erro inesperado na geração. Tente novamente.', 'updated_at': utc_now(),
        })


def _run_edit_job(design_id):
    try:
        design = get_design(design_id)
        if not design:
            return
        db.collection(DESIGNS).document(design_id).update({'status': STATUS_RUNNING, 'updated_at': utc_now()})
        source = download_public_url(design.get('source_image_url'))
        if not source:
            raise RuntimeError('source image unavailable')
        mime = _sniff_mime(source)
        _update_image(design_id, 0, {'status': 'running', 'started_at': utc_now()})
        try:
            result, attempts = image_generation.edit_with_fallback(source, design['prompt'], mime_type=mime)
            url, path = _store_result(design['user_id'], design_id, 0, result)
            payload = {
                'status': 'done', 'url': url, 'storage_path': path, 'provider': result.provider,
                'model': result.model, 'response_id': result.response_id, 'seed': result.seed,
                'cost_usd': result.cost_usd, 'usage': result.usage, 'latency_ms': result.latency_ms,
                'attempts': attempts, 'fallback_used': result.provider != design.get('provider_primary'),
                'finished_at': utc_now(), 'error': '',
            }
            _update_image(design_id, 0, payload)
            _finish(design_id, [payload], 1)
        except ImageGenerationError as exc:
            _update_image(design_id, 0, {'status': 'failed', 'error': 'Falha ao ajustar a imagem.',
                                         'error_code': exc.code, 'error_detail': str(exc)[:500]})
            _finish(design_id, [{'status': 'failed', 'user_error': exc.user_error}], 1)
    except Exception as exc:
        print(f'[designer] edit job {design_id} crashed: {exc}')
        db.collection(DESIGNS).document(design_id).update({
            'status': STATUS_FAILED, 'error_message': 'Erro inesperado ao ajustar a imagem. Tente novamente.',
            'updated_at': utc_now(),
        })


def _sniff_mime(data):
    if data[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return 'image/png'


# ---------------------------------------------------------------------------
# Orçamentos
# ---------------------------------------------------------------------------

def create_quote(design, image_index, user_id, user_email, customer, notes='', phone='', budget=''):
    images = [i for i in (design.get('images') or []) if i.get('url')]
    selected = next((i for i in images if i.get('index') == image_index), images[0] if images else None)
    if not selected:
        raise ValueError('Esta criação ainda não tem imagens prontas.')

    # Junta toda a cadeia (design original + ajustes) para o ateliê ver o histórico
    chain = []
    cursor = design
    while cursor and len(chain) < 10:
        chain.append(cursor)
        parent_id = cursor.get('parent_design_id')
        cursor = get_design(parent_id) if parent_id else None
    root = chain[-1]

    now = utc_now()
    doc = {
        'user_id': user_id,
        'customer_email': (user_email or '').lower(),
        'customer_name': (customer or {}).get('name') or (user_email or '').split('@')[0],
        'customer_phone': phone or (customer or {}).get('phone') or '',
        'firestore_customer_id': (customer or {}).get('id') or '',
        'design_id': design['id'],
        'root_design_id': root['id'],
        'selected_image_index': selected.get('index'),
        'selected_image_url': selected.get('url'),
        'choices': design.get('choices') or {},
        'labels': design.get('labels') or {},
        'details_text': root.get('details_text') or '',
        'edit_instructions': [d.get('edit_instruction') for d in reversed(chain) if d.get('edit_instruction')],
        'designs': [
            {
                'design_id': d['id'],
                'kind': d.get('kind', 'generate'),
                'prompt': d.get('prompt') or '',
                'prompt_version': d.get('prompt_version') or '',
                'edit_instruction': d.get('edit_instruction') or '',
                'images': [
                    {k: img.get(k) for k in ('index', 'url', 'storage_path', 'provider', 'model', 'response_id',
                                             'seed', 'cost_usd', 'fallback_used')}
                    for img in (d.get('images') or []) if img.get('url')
                ],
                'actual_cost_usd': d.get('actual_cost_usd') or 0,
                'created_at': d.get('created_at'),
                'completed_at': d.get('completed_at'),
            }
            for d in reversed(chain)
        ],
        'total_generation_cost_usd': round(sum(float(d.get('actual_cost_usd') or 0) for d in chain), 5),
        'notes': (notes or '')[:1500],
        'budget_range': (budget or '')[:60],
        'status': 'novo',
        'quoted_price_brl': None,
        'admin_notes': '',
        'history': [{'status': 'novo', 'at': now, 'by': user_email or 'cliente', 'note': 'Solicitação enviada pelo site'}],
        'created_at': now,
        'updated_at': now,
    }
    ref = db.collection(QUOTES).document()
    ref.set(doc)
    for d in chain:
        db.collection(DESIGNS).document(d['id']).update({'quote_id': ref.id, 'updated_at': now})
    doc['id'] = ref.id
    return doc


def get_quote(quote_id):
    doc = db.collection(QUOTES).document(quote_id).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    data['id'] = doc.id
    return data


def list_quotes(status=None, limit=500):
    query = db.collection(QUOTES)
    if status:
        query = query.where('status', '==', status)
    out = []
    for doc in query.limit(limit).stream():
        data = doc.to_dict() or {}
        data['id'] = doc.id
        out.append(data)
    out.sort(key=lambda q: _as_datetime(q.get('created_at')) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out


def list_user_quotes(user_id, email=None):
    seen, out = set(), []
    queries = [db.collection(QUOTES).where('user_id', '==', user_id)] if user_id else []
    if email:
        queries.append(db.collection(QUOTES).where('customer_email', '==', email.lower()))
    for query in queries:
        for doc in query.limit(100).stream():
            if doc.id in seen:
                continue
            seen.add(doc.id)
            data = doc.to_dict() or {}
            data['id'] = doc.id
            out.append(data)
    out.sort(key=lambda q: _as_datetime(q.get('created_at')) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out


def serialize_quote_public(quote):
    return {
        'id': quote['id'],
        'number': quote['id'][:8].upper(),
        'status': quote.get('status'),
        'status_label': QUOTE_STATUS_LABELS.get(quote.get('status'), quote.get('status')),
        'selected_image_url': quote.get('selected_image_url') or '',
        'labels': quote.get('labels') or {},
        'details_text': quote.get('details_text') or '',
        'notes': quote.get('notes') or '',
        'quoted_price_brl': quote.get('quoted_price_brl'),
        'customer_message': quote.get('customer_message') or '',
        'design_id': quote.get('design_id'),
        'created_at': format_datetime_brt(quote.get('created_at')) if quote.get('created_at') else '',
    }


def update_quote(quote_id, data, actor, note=''):
    quote = get_quote(quote_id)
    if not quote:
        return None
    update = {k: v for k, v in data.items() if v is not None}
    update['updated_at'] = utc_now()
    status_changed = data.get('status') and data['status'] != quote.get('status')
    if status_changed or note:
        history = list(quote.get('history') or [])
        history.append({'status': data.get('status') or quote.get('status'), 'at': utc_now(), 'by': actor, 'note': note})
        update['history'] = history
    db.collection(QUOTES).document(quote_id).update(update)
    return get_quote(quote_id)


def count_open_quotes():
    total = 0
    for status in QUOTE_OPEN_STATUSES:
        total += sum(1 for _ in db.collection(QUOTES).where('status', '==', status).select(['status']).stream())
    return total


def delete_design_images(design):
    """Usado pelo seed --wipe: remove os arquivos de um design do Storage."""
    from database import bucket
    for img in design.get('images') or []:
        path = img.get('storage_path') or ''
        if path and not path.startswith('/static/'):
            try:
                bucket.blob(path).delete()
            except Exception:
                pass
