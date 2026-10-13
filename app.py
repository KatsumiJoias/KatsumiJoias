from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, Response
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import os
import time
import re
import uuid
import threading
import requests
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from functools import wraps
import firebase_admin
from firebase_admin import auth as firebase_auth
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from database import (
    db,
    save_login_token,
    get_and_delete_login_token,
    get_all_products,
    get_product_by_id,
    create_product,
    update_product,
    delete_product,
    record_product_sale,
    upload_image,
    delete_image,
    get_all_customers,
    get_customer_by_id,
    get_customer_by_firebase_uid,
    get_customer_by_email,
    create_customer,
    update_customer,
    delete_customer,
    get_setting,
    get_all_settings,
    update_setting,
    get_exchange_rate,
    update_exchange_rate,
    create_order_db,
    get_order_by_id,
    get_order_by_pagarme_id,
    update_order_status,
    update_order_fields,
    get_all_orders,
    get_orders_by_firebase_uid,
    get_orders_by_customer_email,
    get_shipping_settings,
    save_shipping_settings,
    create_solicitacao,
    get_all_solicitacoes,
    get_solicitacoes_by_email,
    get_solicitacao_by_id,
    add_solicitacao_reply,
    calculate_shipping,
    format_datetime_brt,
    utc_now,
    _order_created_at_sort_key,
    SOURCE_WEBSITE,
    SOURCE_INTERNAL,
    get_next_numeric_sku,
    find_products_by_sku,
    parse_carat_value,
    apply_product_stock_sale,
    get_all_sellers,
    get_seller_by_id,
    create_seller,
    update_seller,
    delete_seller,
    create_activity_log,
    diff_for_log,
    get_activity_logs,
    get_distinct_log_actors,
    get_orders_by_customer_id,
    serialize_order_for_display,
    LOG_ACTION_CREATE,
    LOG_ACTION_UPDATE,
    LOG_ACTION_DELETE,
)
import pagarme as pagarme_client
import email_service
import whatsapp_service
import seo

# Load environment variables
load_dotenv()

app = Flask(__name__)

_secret = os.getenv("FLASK_SECRET_KEY")
if not _secret:
    raise RuntimeError("FLASK_SECRET_KEY environment variable is not set. Refusing to start.")
app.secret_key = _secret

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
app.config['SESSION_COOKIE_NAME']     = '__session'
app.config['SESSION_COOKIE_SECURE']   = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],           # no global limit — applied per-route
    storage_uri="memory://",
)

# ---------------------------------------------------------------------------
# Exchange rate: daily auto-update (scheduler + first request of the day)
# ---------------------------------------------------------------------------
_sao_paulo_tz = pytz.timezone('America/Sao_Paulo')
_exchange_rate_cache = {'data': None, 'ts': 0}
_EXCHANGE_RATE_TTL = 300  # 5 minutes
_exchange_refresh_lock = threading.RLock()
_exchange_daily_check_date = None  # last BRT date we checked/attempted refresh


def _invalidate_exchange_rate_cache():
    _exchange_rate_cache['data'] = None
    _exchange_rate_cache['ts'] = 0


def _parse_exchange_updated_at(updated_at_str):
    if not updated_at_str:
        return None
    try:
        dt = datetime.fromisoformat(str(updated_at_str).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = _sao_paulo_tz.localize(dt)
        return dt.astimezone(_sao_paulo_tz)
    except (ValueError, TypeError):
        return None


def _exchange_rate_stale_for_today():
    """True when the stored rate was not refreshed today (horário de Brasília)."""
    rate_setting = get_exchange_rate()
    if not rate_setting or 'value' not in rate_setting:
        return True
    updated_at = _parse_exchange_updated_at((rate_setting.get('value') or {}).get('updated_at'))
    if not updated_at:
        return True
    return updated_at.date() < datetime.now(_sao_paulo_tz).date()


def refresh_exchange_rate_from_api(source='manual', force=False):
    """Fetch USD→BRL from Frankfurter and persist to Firestore."""
    if not force and not _exchange_rate_stale_for_today():
        current = get_exchange_rate()
        if current and 'value' in current:
            return current['value']

    with _exchange_refresh_lock:
        if not force and not _exchange_rate_stale_for_today():
            current = get_exchange_rate()
            if current and 'value' in current:
                return current['value']

        response = requests.get('https://api.frankfurter.app/latest?from=USD&to=BRL', timeout=10)
        response.raise_for_status()
        data = response.json()
        market_rate = float(data['rates']['BRL'])

        increase_setting = get_setting('exchange_rate_increase')
        rate_increase = float(increase_setting['value']) if increase_setting and 'value' in increase_setting else 0.0
        final_rate = market_rate + rate_increase

        rate_data = {
            'rate': final_rate,
            'market_rate': market_rate,
            'increase': rate_increase,
            'date': data.get('date', ''),
            'updated_at': datetime.now(_sao_paulo_tz).isoformat(),
        }
        update_exchange_rate(rate_data)
        _invalidate_exchange_rate_cache()
        print(f"[ExchangeRate/{source}] Updated automatically: {final_rate:.4f} BRL/USD")
        return rate_data


def _scheduled_exchange_rate_update():
    """Called by APScheduler every day at 08:00 BRT."""
    try:
        refresh_exchange_rate_from_api(source='scheduler', force=True)
    except Exception as e:
        print(f"[Scheduler] Error updating exchange rate: {e}")


def _maybe_refresh_exchange_rate_on_request():
    """Cloud Run-safe fallback: refresh once per day on the first request."""
    global _exchange_daily_check_date
    path = request.path or ''
    if path.startswith(('/static/', '/favicon', '/robots.txt', '/sitemap.xml', '/internal/cron/')):
        return

    today = datetime.now(_sao_paulo_tz).date()
    if _exchange_daily_check_date == today:
        return

    with _exchange_refresh_lock:
        if _exchange_daily_check_date == today:
            return
        try:
            if _exchange_rate_stale_for_today():
                refresh_exchange_rate_from_api(source='daily_request', force=True)
        except Exception as e:
            print(f"[ExchangeRate/daily_request] Error: {e}")
        finally:
            _exchange_daily_check_date = today


_scheduler = BackgroundScheduler(timezone=_sao_paulo_tz)
_scheduler.add_job(
    _scheduled_exchange_rate_update,
    trigger=CronTrigger(hour=8, minute=0, timezone=_sao_paulo_tz),
    id='daily_exchange_rate',
    replace_existing=True,
    misfire_grace_time=3600,
)
if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or not app.debug:
    _scheduler.start()
# ---------------------------------------------------------------------------


@app.before_request
def _ensure_daily_exchange_rate():
    _maybe_refresh_exchange_rate_on_request()

@app.after_request
def set_security_headers(response):
    # Prevent clickjacking
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    # Prevent MIME-type sniffing
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    # Limit referrer info sent to third parties
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    # Restrict browser features
    response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    # Mark session/personalised responses as private so Firebase CDN won't cache them
    if '__session' in request.cookies or session.modified:
        response.headers['Cache-Control'] = 'private'
    return response

# Add datetime to template context
@app.context_processor
def inject_now():
    return {
        'now': datetime.now,
        'support_whatsapp_number': whatsapp_service.SUPPORT_WHATSAPP_NUMBER,
        'support_whatsapp_display': whatsapp_service.SUPPORT_WHATSAPP_DISPLAY,
    }


def _site_url():
    return (os.getenv('SITE_URL') or '').rstrip('/')


def _abs_url(path):
    """Turn a relative path or url_for result into an absolute URL."""
    if not path:
        return None
    if path.startswith('http'):
        return path
    return _site_url() + path


def _basic_page_seo(title, description=None, path=None, og_image=None):
    """Default SEO dict for public pages."""
    site = _site_url()
    page_path = path if path is not None else request.path
    canonical = site + page_path
    image = og_image or f'{site}/static/images/banner1.jpg'
    return {
        'title': title,
        'description': description,
        'canonical_url': canonical,
        'og_title': title,
        'og_description': description or seo.HOMEPAGE_DESCRIPTION,
        'og_url': canonical,
        'og_type': 'website',
        'og_image': image,
        'twitter_card': 'summary_large_image',
    }


@app.template_filter('product_image_alt')
def product_image_alt_filter(product):
    return seo.product_image_alt(product)


@app.template_filter('category_path')
def category_path_filter(category_name):
    return seo.category_path(category_name)

# Translations for specifications
SPEC_TRANSLATIONS = {
    'cut': 'Lapidação',
    'color': 'Cor',
    'clarity': 'Pureza',
    'carat_weight': 'Peso em Quilates',
    'dimensions': 'Dimensões',
    'gemstone_type': 'Tipo de Gema',
    'origin': 'Origem',
    'treatment': 'Tratamento',
    'certification': 'Certificação',
    'shape': 'Forma',
    'size': 'Tamanho',
    'transparency': 'Transparência',
    'phenomenon': 'Fenômeno',
    'material': 'Material',
    'metal': 'Metal',
    'purity': 'Pureza do Metal',
    'weight': 'Peso',
    'length': 'Comprimento',
    'width': 'Largura',
    'height': 'Altura',
    'diameter': 'Diâmetro'
}

def translate_specifications(specs):
    """Translate specification keys to Portuguese."""
    if not specs or not isinstance(specs, dict):
        return {}
    return {SPEC_TRANSLATIONS.get(k, k): v for k, v in specs.items()}

def get_product_categories():
    """Get product categories with variants from Firestore settings (product_categories doc)."""
    categories_setting = get_setting('product_categories')
    if categories_setting and 'value' in categories_setting:
        # Check if it's the new format (dict with variants) or old format (list)
        if isinstance(categories_setting['value'], dict):
            categories = categories_setting['value']
            # Ensure all categories have description and variants with proper structure
            for cat_name, cat_data in categories.items():
                if not isinstance(cat_data, dict):
                    categories[cat_name] = {'description': '', 'variants': []}
                else:
                    if 'description' not in cat_data:
                        cat_data['description'] = ''
                    if 'variants' not in cat_data:
                        cat_data['variants'] = []
                    # Ensure variants have proper structure
                    if isinstance(cat_data['variants'], list) and len(cat_data['variants']) > 0:
                        # Check if variants are strings (old format) or dicts (new format)
                        if isinstance(cat_data['variants'][0], str):
                            # Convert old format to new format
                            cat_data['variants'] = [{'name': v, 'description': ''} for v in cat_data['variants']]
            return categories
        elif isinstance(categories_setting['value'], list):
            # Legacy list format — convert in memory without overwriting Firestore
            return {cat: {'description': '', 'variants': []} for cat in categories_setting['value']}
    return {}

def get_product_display_description(product, categories_data):
    """Return the display description for a product from its category/variant settings."""
    cat_data = categories_data.get(product.get('category', ''), {})
    variant = product.get('variant', '')
    if variant:
        for v in cat_data.get('variants', []):
            v_name = v.get('name') if isinstance(v, dict) else v
            if v_name == variant:
                v_desc = v.get('description', '') if isinstance(v, dict) else ''
                if v_desc:
                    return v_desc
    return cat_data.get('description', '')

def save_product_categories(categories):
    """Save product categories to settings."""
    setting_data = {
        'name': 'Categorias de Produtos',
        'description': 'Categorias e variantes de produtos disponíveis',
        'type': 'object',
        'value': categories
    }
    update_setting('product_categories', setting_data)

def get_default_specifications():
    """Get default specifications from settings or return default."""
    specs_setting = get_setting('default_specifications')
    if specs_setting and 'value' in specs_setting:
        return specs_setting['value']
    # Fallback to default specifications
    return ['Lapidação', 'Cor', 'Pureza', 'Dimensões', 'Peso em Quilates', 'Tipo de Gema']

def get_price_increase_rate():
    """Get the price increase percentage from settings."""
    setting = get_setting('price_increase_rate')
    if setting and 'value' in setting:
        try:
            return int(setting['value'])
        except (ValueError, TypeError):
            return 0
    return 0

def _product_total_price(p):
    """Calculate total product price (price_per_carat * carat_weight)."""
    price = float(p.get('price', 0))
    cw_raw = str(p.get('carat_weight', '') or '').strip()
    if cw_raw:
        cw_num = cw_raw.split()[0] if ' ' in cw_raw else cw_raw
        try:
            carat = float(cw_num.replace(',', '.'))
        except (ValueError, TypeError):
            carat = 1
    else:
        carat = 1
    return price * carat

def _apply_price_markup(products, rate):
    """Apply price increase percentage to product prices for front-end display."""
    if not rate:
        return products
    factor = 1 + rate / 100
    for p in products:
        p['price'] = round(p.get('price', 0) * factor, 2)
    return products

def get_cut_types():
    """Get cut types from settings or return default."""
    cut_types_setting = get_setting('cut_types')
    if cut_types_setting and 'value' in cut_types_setting:
        return cut_types_setting['value']
    # Default cut types
    return [
        'Brilhante',
        'Esmeralda',
        'Oval',
        'Princesa',
        'Cushion',
        'Pera',
        'Marquise',
        'Coração',
        'Radiante',
        'Asscher'
    ]

def save_cut_types(cut_types):
    """Save cut types to settings."""
    setting_data = {
        'name': 'Tipos de Lapidação',
        'description': 'Tipos de lapidação disponíveis para produtos',
        'type': 'array',
        'value': cut_types
    }
    update_setting('cut_types', setting_data)

# ============================================
# Newsletter Functions
# ============================================

def get_all_newsletter_subscribers():
    """Get all newsletter subscribers from Firestore."""
    try:
        subscribers_ref = db.collection('newsletter')
        docs = subscribers_ref.stream()
        
        subscribers = []
        for doc in docs:
            subscriber = doc.to_dict()
            subscriber['id'] = doc.id
            # Format date
            if 'subscribed_at' in subscriber:
                subscriber['subscribed_at'] = format_datetime_brt(subscriber['subscribed_at'])
            subscribers.append(subscriber)
        
        # Sort by subscription date (newest first)
        subscribers.sort(key=lambda x: x.get('subscribed_at', ''), reverse=True)
        return subscribers
    except Exception as e:
        print(f"Error getting newsletter subscribers: {e}")
        return []

def add_newsletter_subscriber(email):
    """Add a new newsletter subscriber."""
    try:
        # Check if email already exists
        existing = db.collection('newsletter').where('email', '==', email).limit(1).get()
        if len(list(existing)) > 0:
            return {'error': 'Este e-mail já está inscrito na newsletter.'}, 400
        
        subscriber_data = {
            'email': email,
            'subscribed_at': utc_now(),
            'active': True
        }
        
        db.collection('newsletter').add(subscriber_data)
        return {'message': 'Inscrição realizada com sucesso!'}, 200
    except Exception as e:
        print(f"Error adding newsletter subscriber: {e}")
        return {'error': 'Erro ao processar inscrição.'}, 500

def delete_newsletter_subscriber(subscriber_id):
    """Delete a newsletter subscriber."""
    try:
        db.collection('newsletter').document(subscriber_id).delete()
        return True
    except Exception as e:
        print(f"Error deleting newsletter subscriber: {e}")
        return False

# ============================================
# Authentication Decorators
# ============================================

def require_auth(f):
    """Decorator to require authentication for routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Unauthorized'}), 401
        
        id_token = auth_header.split('Bearer ')[1]
        
        try:
            decoded_token = firebase_auth.verify_id_token(id_token)
            request.user_id = decoded_token['uid']
            request.user_email = decoded_token.get('email')
            return f(*args, **kwargs)
        except Exception as e:
            print(f"Auth error: {e}")
            return jsonify({'error': 'Unauthorized'}), 401
    
    return decorated_function

_ADMIN_CACHE_TTL = 900  # re-verify Firebase every 15 minutes

def require_admin(f):
    """Decorator to require admin role for CRM routes"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Você precisa fazer login como administrador para acessar esta página.', 'error')
            return redirect(url_for('login'))

        user_id = session['user_id']

        # Use cached admin status to avoid a Firebase API call on every request
        cached_at = session.get('admin_verified_at', 0)
        if time.time() - cached_at < _ADMIN_CACHE_TTL:
            if not session.get('is_admin'):
                flash('Acesso negado. Você precisa de permissões de administrador.', 'error')
                return redirect(url_for('homepage'))
            return f(*args, **kwargs)

        # Cache expired — re-verify from Firebase
        try:
            user_record  = firebase_auth.get_user(user_id)
            custom_claims = user_record.custom_claims or {}
            is_admin = bool(custom_claims.get('admin'))
            session['is_admin']           = is_admin
            session['admin_verified_at']  = time.time()

            if not is_admin:
                flash('Acesso negado. Você precisa de permissões de administrador.', 'error')
                return redirect(url_for('homepage'))
        except Exception as e:
            print(f"Admin verification error: {e}")
            flash('Erro ao verificar permissões.', 'error')
            return redirect(url_for('login'))

        return f(*args, **kwargs)

    return decorated_function

def require_admin_api(f):
    """Decorator to require admin role for API routes (uses Bearer token)"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Unauthorized'}), 401
        
        id_token = auth_header.split('Bearer ')[1]
        
        try:
            # Verify token and get user
            decoded_token = firebase_auth.verify_id_token(id_token)
            user_id = decoded_token['uid']
            
            # Check if user has admin role
            user_record = firebase_auth.get_user(user_id)
            custom_claims = user_record.custom_claims or {}
            
            if not custom_claims.get('admin'):
                return jsonify({'error': 'Forbidden - Admin role required'}), 403
            
            # Store user info in request for use in the route
            request.user_id = user_id
            request.user_email = decoded_token.get('email')
            
            return f(*args, **kwargs)
        except Exception as e:
            print(f"Admin API verification error: {e}")
            return jsonify({'error': 'Unauthorized'}), 401
    
    return decorated_function

# ============================================
# Public Routes
# ============================================

# Produto de certificado gemológico — vendido via carrinho, oculto do catálogo
CERTIFICATE_PRODUCT_ID = 'IQROwNnZhK67ZQHEQram'
CATALOG_HIDDEN_PRODUCT_IDS = {CERTIFICATE_PRODUCT_ID}

PAID_ORDER_STATUSES = frozenset({'paid', 'authorized'})
MIN_CREDIT_CARD_BRL = 1.0


def _is_catalog_visible(product):
    """Produtos ativos visíveis na loja (exclui add-ons internos como certificado)."""
    return product.get('active', False) and product.get('id') not in CATALOG_HIDDEN_PRODUCT_IDS


@app.route('/')
def homepage():
    products = get_all_products()
    products = [p for p in products if _is_catalog_visible(p)]
    
    _categories_data = get_product_categories()
    for product in products:
        if 'specifications' in product:
            product['specifications'] = translate_specifications(product['specifications'])
        product['display_description'] = get_product_display_description(product, _categories_data)

    products.sort(key=_product_total_price, reverse=True)

    seen_categories = set()
    featured_products = []
    for p in products:
        cat = p.get('category', '')
        if cat not in seen_categories:
            seen_categories.add(cat)
            featured_products.append(p)
            if len(featured_products) >= 6:
                break

    if len(featured_products) < 6:
        for p in products:
            if p not in featured_products:
                featured_products.append(p)
                if len(featured_products) >= 6:
                    break

    _apply_price_markup(featured_products, get_price_increase_rate())

    site = _site_url()
    page_seo = _basic_page_seo(
        seo.HOMEPAGE_TITLE,
        seo.HOMEPAGE_DESCRIPTION,
        path='/',
        og_image=f'{site}/static/images/banner1.jpg',
    )
    page_seo['json_ld'] = seo.build_organization_json_ld(site)

    return render_template('homepage.html',
                         products=featured_products,
                         firebase_config=get_firebase_config(),
                         seo=page_seo)

def _get_dimension_bounds(products):
    """Compute min/max of physical dimensions from a product list for range-slider bounds."""
    def _bounds(field, default_max):
        vals = []
        for p in products:
            raw = str(p.get(field) or '').strip().replace(',', '.')
            try:
                v = float(raw)
                if v > 0:
                    vals.append(v)
            except (ValueError, TypeError):
                pass
        if not vals:
            return {'min': 0.0, 'max': float(default_max)}
        return {'min': 0.0, 'max': round(max(vals) + max(vals) * 0.1 + 1, 1)}

    carat_vals = []
    for p in products:
        cw = str(p.get('carat_weight') or '').strip()
        if cw:
            cw_num = cw.split()[0] if ' ' in cw else cw
            try:
                v = float(cw_num.replace(',', '.'))
                if v > 0:
                    carat_vals.append(v)
            except (ValueError, TypeError):
                pass

    return {
        'altura':      _bounds('altura',      50),
        'comprimento': _bounds('comprimento', 100),
        'largura':     _bounds('largura',     100),
        'peso': {'min': 0.0, 'max': round(max(carat_vals) + max(carat_vals) * 0.1 + 1, 2) if carat_vals else 50.0},
    }


def _parse_product_dimension(product, field):
    """Parse comprimento, largura, or carat weight (peso) from a product dict."""
    if field == 'peso':
        cw = str(product.get('carat_weight') or '').strip()
        if not cw:
            return None
        cw_num = cw.split()[0] if ' ' in cw else cw
        try:
            v = float(cw_num.replace(',', '.'))
            return v if v > 0 else None
        except (ValueError, TypeError):
            return None
    raw = str(product.get(field) or '').strip().replace(',', '.')
    try:
        v = float(raw)
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


def _value_to_bucket(value, step, decimals):
    """Map a numeric value to the same 1-unit bucket used by the filter dropdowns."""
    lo = 0.0
    safety = 0
    while safety < 10000:
        hi = lo + step
        min_val = 0.0 if lo == 0 else round(lo + 0.01, decimals)
        max_rounded = round(hi, decimals)
        if lo == 0 and value <= max_rounded + 0.0001:
            return {'min': min_val, 'max': max_rounded}
        if lo > 0 and min_val <= value <= max_rounded + 0.0001:
            return {'min': min_val, 'max': max_rounded}
        lo = hi
        safety += 1
    return None


def _bucket_label(min_val, max_rounded, unit, decimals):
    def _fmt(n):
        return f'{n:.{decimals}f}'.replace('.', ',')
    if min_val == 0:
        return f'de 0 {unit} a {_fmt(max_rounded)} {unit}'
    return f'de {_fmt(min_val)} {unit} a {_fmt(max_rounded)} {unit}'


def _get_dimension_buckets(products, field, step=1, decimals=2, unit='mm'):
    """Return only dimension buckets that contain at least one product."""
    seen = {}
    for product in products:
        val = _parse_product_dimension(product, field)
        if val is None:
            continue
        bucket = _value_to_bucket(val, step, decimals)
        if not bucket:
            continue
        key = (bucket['min'], bucket['max'])
        seen[key] = seen.get(key, 0) + 1

    buckets = []
    for (min_v, max_v), count in sorted(seen.items()):
        buckets.append({
            'min': min_v,
            'max': max_v,
            'label': _bucket_label(min_v, max_v, unit, decimals),
            'count': count,
        })
    return buckets


_PRICE_TIERS_BRL = [0, 500, 1_000, 2_000, 5_000, 10_000, 20_000, 50_000, 100_000, 200_000, 500_000, 999_999_999]


def _fmt_brl_int(n):
    return f'{int(n):,}'.replace(',', '.')


def _price_bucket_label(lo, hi, unit_suffix=''):
    if lo == 0:
        return f'Até R$ {_fmt_brl_int(hi)}{unit_suffix}'
    if hi >= 999_999_999:
        return f'Acima de R$ {_fmt_brl_int(lo)}{unit_suffix}'
    return f'R$ {_fmt_brl_int(lo)} a R$ {_fmt_brl_int(hi)}{unit_suffix}'


def _product_price_ct_brl(product, markup_factor, exchange_rate):
    """Effective price per carat in BRL (after markup and discount)."""
    price_usd = float(product.get('price', 0) or 0) * markup_factor
    discount = float(product.get('discount') or 0)
    if discount:
        price_usd *= 1 - discount / 100
    return price_usd * exchange_rate


def _product_total_brl_calc(product, markup_factor, exchange_rate):
    """Total display price in BRL (price/ct × carat × markup × exchange_rate, after discount)."""
    price_usd = float(product.get('price', 0) or 0) * markup_factor
    cw_raw = str(product.get('carat_weight', '') or '').strip()
    if cw_raw:
        cw_num = cw_raw.split()[0] if ' ' in cw_raw else cw_raw
        try:
            carat = float(cw_num.replace(',', '.'))
        except (ValueError, TypeError):
            carat = 1.0
    else:
        carat = 1.0
    discount = float(product.get('discount') or 0)
    total_usd = price_usd * carat
    if discount:
        total_usd *= 1 - discount / 100
    return total_usd * exchange_rate


def _get_price_buckets_brl(products, markup_rate, exchange_rate, kind='total'):
    """Return BRL price buckets containing at least one product. kind='total' or 'ct'."""
    if not exchange_rate or exchange_rate <= 0:
        return []
    markup_factor = 1 + markup_rate / 100 if markup_rate else 1.0
    prices = []
    for p in products:
        if kind == 'ct':
            val = _product_price_ct_brl(p, markup_factor, exchange_rate)
        else:
            val = _product_total_brl_calc(p, markup_factor, exchange_rate)
        if val > 0:
            prices.append(val)
    if not prices:
        return []
    tiers = _PRICE_TIERS_BRL
    suffix = '/ct' if kind == 'ct' else ''
    buckets = []
    for i in range(len(tiers) - 1):
        lo, hi = tiers[i], tiers[i + 1]
        count = sum(1 for pr in prices if lo <= pr < hi)
        if count > 0:
            buckets.append({'min': lo, 'max': hi, 'label': _price_bucket_label(lo, hi, suffix), 'count': count})
    return buckets


def _build_category_filter_options(product_list, pedras_order, pedras_key_fn, markup_rate=0, exchange_rate=0):
    """Build cut/pedra/dimension filter metadata for a product subset."""
    opts = {'cuts': set(), 'pedras': set()}
    for _p in product_list:
        _cut = _p.get('cut', '')
        if _cut:
            opts['cuts'].add(_cut)
        _np = _p.get('numero_pedras')
        if not _np and 'sem_pedras' in pedras_order:
            opts['pedras'].add('sem_pedras')
        elif _np is not None:
            try:
                _key = pedras_key_fn(int(_np))
                if _key:
                    opts['pedras'].add(_key)
            except (ValueError, TypeError):
                if 'sem_pedras' in pedras_order:
                    opts['pedras'].add('sem_pedras')
    opts['cuts'] = sorted(opts['cuts'])
    opts['pedras'] = [k for k in pedras_order if k in opts['pedras']]
    _dim_bounds = _get_dimension_bounds(product_list)
    opts['bounds'] = {k: {'min': v['min'], 'max': v['max']} for k, v in _dim_bounds.items()}
    opts['range_buckets'] = {
        'comprimento': _get_dimension_buckets(product_list, 'comprimento', unit='mm'),
        'largura': _get_dimension_buckets(product_list, 'largura', unit='mm'),
        'peso': _get_dimension_buckets(product_list, 'peso', unit='ct'),
        'preco_ct': _get_price_buckets_brl(product_list, markup_rate, exchange_rate, kind='ct'),
        'preco_total': _get_price_buckets_brl(product_list, markup_rate, exchange_rate, kind='total'),
    }
    return opts


@app.route('/o-que-procura')
def oqueprocura_page():
    """Página 'O que você procura?' com formulário de solicitação."""
    page_seo = _basic_page_seo(
        f'Encomende sua Gema | {seo.BRAND}',
        'Não encontrou a pedra ideal? Conte o que procura e nossa equipe gemológica busca ou lapida a gema perfeita para você.',
        path='/o-que-procura',
    )
    return render_template('oqueprocura.html', firebase_config=get_firebase_config(), seo=page_seo)


@app.route('/historia')
def historia_page():
    """Página institucional 'Nossa História' (tradição desde 1942)."""
    page_seo = _basic_page_seo(
        f'Nossa História | Gemas desde 1942 | {seo.BRAND}',
        'duas gerações lapidando gemas brasileiras em Minas Gerais. Conheça a história da Katsumi Joias desde a fundação por Wilson Alves Paixão.',
        path='/historia',
        og_image=f'{_site_url()}/static/images/nossahistoria.jpg',
    )
    return render_template('historia.html', seo=page_seo)


@app.route('/feiras')
def feiras_page():
    """Página institucional 'Feiras' — presença em eventos do setor."""
    page_seo = _basic_page_seo(
        f'Feiras e Eventos | {seo.BRAND}',
        'Onde encontrar a Katsumi Joias em feiras de gemas e joalheria no Brasil e no exterior.',
        path='/feiras',
    )
    return render_template('feiras.html', seo=page_seo)


# ---------------------------------------------------------------------------
# Guias / conteúdo educativo (SEO)
# ---------------------------------------------------------------------------
GUIDES = {
    'como-escolher-pedra-preciosa': {
        'title': 'Como escolher uma pedra preciosa',
        'subtitle': 'Os critérios que realmente importam na hora de comprar uma gema.',
        'read_min': 6,
        'banner_image': 'images/como-escolher.png',
        'intro': 'Comprar uma pedra preciosa é diferente de comprar qualquer outro produto: duas gemas do mesmo tipo podem ter valores radicalmente distintos. Entender o que está por trás desse valor protege o seu investimento e garante que você leve para casa exatamente o que deseja. Reunimos aqui, em linguagem simples, os critérios que nós mesmos usamos há gerações ao selecionar cada pedra.',
        'sections': [
            {'h': 'Cor: o critério número um', 'p': [
                'Na maioria das gemas coloridas, a cor responde pela maior parte do valor. Avaliamos três aspectos: o matiz (a cor em si), a saturação (a intensidade) e o tom (quão clara ou escura a pedra é).',
                'Em geral, as cores mais vivas e saturadas, sem serem escuras demais a ponto de “fechar” o brilho, são as mais valorizadas. Uma esmeralda verde-intensa ou uma turmalina Paraíba elétrica valem muito mais do que exemplares apagados.',
                'Na Katsumi Joias, cada pedra do catálogo traz fotos em luz natural e descrição das características principais — use-as como ponto de partida, mas saiba que o brilho real só se revela completamente nas mãos.'
            ]},
            {'h': 'Pureza: as inclusões', 'p': [
                'Inclusões são pequenas marcas naturais formadas dentro da pedra. Elas não são defeitos — são a impressão digital da gema e provam que ela é natural. O que avaliamos é se as inclusões comprometem o brilho ou a resistência.',
                'Vale lembrar que cada espécie tem um padrão próprio: esmeraldas quase sempre têm o chamado “jardim” de inclusões, enquanto uma água-marinha costuma ser bem mais limpa. Pedras sem inclusão visível à olho nu são raras e, por isso, mais caras — mas nem sempre são a melhor escolha estética ou emocional.'
            ]},
            {'h': 'Lapidação: o que liberta o brilho', 'p': [
                'A lapidação é onde a técnica encontra a arte. Uma boa lapidação distribui a luz de forma uniforme, sem zonas apagadas (“janelas”) nem áreas escuras. É justamente o ofício que cultivamos desde 1942.',
                'Proporção, simetria e acabamento determinam quanto da beleza natural da pedra realmente aparece aos olhos. Uma gema de cor excepcional mal lapidada perde metade do seu encanto — e do seu valor.'
            ]},
            {'h': 'Quilate: tamanho não é tudo', 'p': [
                'O quilate (ct) mede o peso, não o tamanho aparente. Pedras mais densas parecem menores para o mesmo peso. E o preço por quilate cresce de forma não linear: uma gema de 5 ct rara vale muito mais do que cinco gemas de 1 ct.',
                'Ao comparar opções no catálogo, observe também as medidas em milímetros (comprimento, largura e, quando aplicável, peso em ct) — elas ajudam a visualizar como a pedra se comportará na joia.'
            ]},
            {'h': 'Origem e confiança', 'p': [
                'Saber de onde vem a pedra e em quem confiar faz toda a diferença. Trabalhamos com gemas garimpadas e lapidadas por nós no coração de Minas Gerais, com calibragem conferida à mão — as mesmas que abastecem grandes joalherias.',
                'Para quem deseja documentação formal, oferecemos certificação gemológica independente opcional, contratada diretamente no carrinho. Veja nosso guia sobre <a href="/guias/certificacao-gemologica">como adquirir um laudo gemológico</a>.'
            ]},
        ],
    },
    'cuidados-com-gemas': {
        'title': 'Cuidados com suas gemas',
        'subtitle': 'Como limpar, guardar e preservar suas pedras preciosas por gerações.',
        'read_min': 5,
        'banner_image': 'images/cuidados-gema.png',
        'intro': 'Uma pedra preciosa pode durar várias vidas — desde que receba os cuidados certos. Cada gema tem uma dureza e uma sensibilidade próprias. Veja como mantê-las tão deslumbrantes quanto no primeiro dia, seja solta ou montada em joia.',
        'sections': [
            {'h': 'Limpeza do dia a dia', 'p': [
                'Para a maioria das gemas, água morna, sabão neutro e uma escova de cerdas macias resolvem. Enxágue bem e seque com um pano que não solte fiapos.',
                'Evite produtos de limpeza domésticos, que podem conter substâncias agressivas, e nunca use limpadores ultrassônicos sem antes confirmar que a pedra suporta — esmeraldas, opalas e outras gemas tratadas ou porosas podem ser danificadas.',
                'Pedras soltas recém-adquiridas podem ser limpas antes do primeiro uso ou montagem. Joias montadas pedem cuidado extra para não molhar partes metálicas sensíveis ou colas de engaste.'
            ]},
            {'h': 'Entenda a dureza', 'p': [
                'A escala de Mohs mede a resistência ao risco. Diamantes (10) e safiras (9) são muito resistentes; já gemas como a tanzanita ou a opala são mais delicadas e pedem cuidado redobrado contra impactos.',
                'Guarde pedras de durezas diferentes separadas: uma gema mais dura pode riscar uma mais macia se ficarem em contato. Um estojo com compartimentos individuais é o investimento mais simples e eficaz.'
            ]},
            {'h': 'Como guardar', 'p': [
                'O ideal é guardar cada peça individualmente, em saquinhos de tecido macio ou em compartimentos forrados. Mantenha longe de calor e de variações bruscas de temperatura, que podem afetar gemas sensíveis como quartzo ou ametista.',
                'Evite exposição prolongada ao sol direto — algumas pedras, como kunzita ou ametista, podem desbotar com o tempo.'
            ]},
            {'h': 'No dia a dia', 'p': [
                'Coloque suas joias por último, depois de perfume, cremes e maquiagem. Retire-as para atividades físicas, limpeza doméstica ou ao entrar no mar e na piscina — cloro e sal podem corroer metais e danificar tratamentos de superfície.',
                'Impactos contra superfícies duras são a principal causa de lascas em gemas lapidadas. Mesmo safiras e rubis, apesar da dureza, podem quebrar se receberem um golpe na direção errada.'
            ]},
            {'h': 'Revisão periódica', 'p': [
                'Em joias, verifique de tempos em tempos as cravações e fechos. Um especialista consegue reapertar engastes e devolver o brilho original com uma limpeza profissional.',
                'Se comprou uma gema solta conosco e pretende montá-la, consulte um ourives de confiança — ele saberá qual tipo de engaste preserva melhor a integridade da pedra.'
            ]},
        ],
    },
    'certificacao-gemologica': {
        'title': 'Certificação gemológica independente',
        'subtitle': 'Laudo técnico de autenticidade: o que é, o que garante e como contratar junto com a sua compra.',
        'read_min': 6,
        'banner_image': 'images/certificacao.png',
        'intro': 'Comprar uma gema de alto valor exige confiança além da palavra do vendedor. Para oferecer essa camada extra de segurança, a Katsumi Joias disponibiliza certificação gemológica independente opcional em qualquer pedra do catálogo. O laudo é emitido por laboratório especializado, com análise técnica realizada por gemólogos credenciados — documentando autenticidade, origem e características com precisão científica.',
        'sections': [
            {'h': 'O que é um laudo gemológico?', 'p': [
                'Um laudo gemológico é um relatório técnico emitido por um laboratório independente, separado do vendedor. O documento analisa a pedra com instrumentação profissional — microscópio, refratômetro, espectroscópio e outros equipamentos — e registra os resultados por escrito, assinado por gemólogos com formação reconhecida internacionalmente.',
                'É o mesmo padrão exigido por joalheiros de alto padrão, leiloeiros e seguradoras em transações de valor. O laudo não pertence à Katsumi Joias: é um documento independente que acompanha a gema para onde ela for.',
            ]},
            {'h': 'O que o certificado garante?', 'p': [
                'O laudo gemológico identifica o material (natural ou sintético), descreve cor, pureza, lapidação, dimensões e peso estimado, e registra eventuais tratamentos detectados. Para pedras coloridas como turmalinas, esmeraldas, tanzanitas e kunzitas, a análise confirma a autenticidade e documenta as características que determinam o valor.',
                'O certificado pode ser apresentado em revendas, montagens em joia, seguros e inventários. Representa a palavra de um terceiro independente sobre aquilo que a gema é — independentemente de quem a vendeu.',
            ]},
            {'h': 'Como contratar no carrinho', 'p': [
                'A certificação é opcional e pode ser contratada diretamente no momento da compra, sem precisar falar com um especialista antes. Ao adicionar uma gema ao carrinho, marque a opção <strong>Laudo Gemológico</strong> no item desejado. O valor aparece separado e é somado ao total no checkout.',
                'Você pode incluir laudo em uma ou mais pedras do mesmo pedido: cada gema marcada gera seu certificado individual. A opção também está disponível na página de cada produto.',
                'Após a confirmação do pagamento, coordenamos o envio da pedra ao laboratório. O prazo padrão para análise de pedras coloridas é de cerca de 2 dias úteis após o recebimento. O certificado é entregue junto com a gema ou conforme combinado com nossa equipe.',
            ]},
            {'h': 'Quanto custa?', 'p': [
                'O valor do laudo é exibido no carrinho ao marcar a opção, já convertido para reais conforme a cotação do dia. O preço é por pedra certificada: se você comprar duas gemas e contratar laudo nas duas, serão dois documentos independentes.',
                'O certificado não substitui a descrição do produto no site, mas complementa com análise laboratorial formal. Para pedras de alto valor, presentes especiais ou investimento em coleção, é um custo que se traduz diretamente em segurança e respaldo técnico.',
            ]},
            {'h': 'Por que vale a pena?', 'p': [
                'Com a proliferação de gemas sintéticas e tratamentos cada vez mais difíceis de detectar visualmente, um laudo independente protege seu investimento e dá respaldo técnico à compra. É especialmente relevante para presentes, coleções e peças de alto valor.',
                'Além da tranquilidade pessoal, o certificado agrega valor de revenda e facilita a confiança em transações futuras. Muitos clientes optam pelo laudo já na primeira compra conosco.',
            ]},
            {'h': 'Dúvidas?', 'p': [
                'Para gemas específicas, pedidos especiais ou dúvidas sobre o processo de certificação, fale com nossos especialistas pela página <a href="/o-que-procura">O que procura?</a>. Respondemos pessoalmente a cada consulta.',
            ]},
        ],
    },
    'como-funciona-o-envio': {
        'title': 'Como funciona o envio',
        'subtitle': 'Embalagem segura, Correios com seguro e rastreio até a sua porta.',
        'read_min': 4,
        'intro': 'Cada gema que vendemos é peça única — e merece o mesmo cuidado que dedicamos à lapidação. Por isso, todos os pedidos da Katsumi Joias são enviados pelos Correios com seguro contratado e código de rastreio, do nosso cofre até o endereço que você informar no checkout.',
        'sections': [
            {'h': 'Correios com seguro', 'p': [
                'Utilizamos os serviços dos Correios para entregas em todo o Brasil. O frete inclui <strong>seguro</strong>, que cobre o valor declarado do pedido durante o transporte — uma camada extra de proteção para gemas de alto valor.',
                'O custo do envio é calculado automaticamente no checkout com base no seu CEP e no valor do carrinho. Em compras acima do valor mínimo configurado, o frete pode ser gratuito para o seu estado.'
            ]},
            {'h': 'Embalagem discreta e segura', 'p': [
                'Suas pedras são conferidas individualmente, protegidas em estojos ou envelopes acolchoados e acondicionadas em caixa rígida, sem identificação externa do conteúdo. Priorizamos discrição e integridade da peça.',
                'Se você contratou laudo gemológico, a logística pode incluir passagem pelo laboratório antes do envio final. Nesse caso, nossa equipe combina prazos e mantém você informado por e-mail.'
            ]},
            {'h': 'Rastreio e acompanhamento', 'p': [
                'Assim que o pacote for postado, enviamos o <strong>código de rastreio</strong> por e-mail. Você também pode acompanhar o status em <a href="/account">Minha conta</a> e na página de confirmação do pedido.',
                'O prazo de entrega varia conforme região e modalidade dos Correios. Assim que houver movimentação no rastreio, você verá as atualizações no site dos Correios ou no app Meu Correios.'
            ]},
            {'h': 'Recebimento e conferência', 'p': [
                'Ao receber, confira se a embalagem está íntegra antes de assinar. Em caso de avaria visível na entrega, registre com o entregador e entre em contato conosco imediatamente — o seguro existe justamente para essas situações.',
                'Pedras soltas devem ser guardadas em local seguro até montagem em joia. Se tiver qualquer dúvida sobre a peça recebida, fale com nossos especialistas pelo WhatsApp ou responda ao e-mail de confirmação do pedido.'
            ]},
            {'h': 'Dúvidas sobre o seu envio?', 'p': [
                'Nossa equipe acompanha cada despacho. Se o rastreio não atualizar no prazo esperado ou se precisar alterar endereço antes da postagem, fale conosco o quanto antes.',
                'Para pedidos em andamento, tenha em mãos o número do pedido (disponível em <a href="/account">Minha conta</a>) — assim localizamos sua encomenda com agilidade.'
            ]},
        ],
    },
    'como-comprar-na-katsumi-joias': {
        'title': 'Como comprar na Katsumi Joias',
        'subtitle': 'Do catálogo à entrega: entenda cada etapa da sua compra online.',
        'read_min': 5,
        'banner_image': 'images/como-comprar.png',
        'banner_min_h': 'min-h-[440px] md:min-h-[540px] lg:min-h-[600px]',
        'banner_object_position': 'center 30%',
        'intro': 'Comprar gemas online pede confiança — especialmente quando cada pedra é única. Este guia explica, passo a passo, como funciona a experiência de compra na Katsumi Joias, desde a escolha no catálogo até o recebimento em casa.',
        'sections': [
            {'h': '1. Escolha no catálogo', 'p': [
                'Navegue por tipo de gema, use os filtros de cor, peso e dimensões para refinar a busca, e abra a ficha de cada produto para ver fotos, medidas e preço em reais (convertido automaticamente pela cotação do dia).',
                'Cada pedra listada é um exemplar real — não trabalhamos com fotos genéricas de catálogo. Se precisar de algo fora do site, nossa equipe de especialistas pode buscar sob medida pela página <a href="/o-que-procura">O que procura?</a>.'
            ]},
            {'h': '2. Carrinho e laudo gemológico', 'p': [
                'Adicione as gemas desejadas ao carrinho. Se quiser certificação independente, marque a opção <strong>Laudo Gemológico</strong> em cada item — o valor aparece no resumo antes do checkout. Veja detalhes no guia de <a href="/guias/certificacao-gemologica">certificação gemológica</a>.',
                'Revise quantidades e valores no carrinho lateral antes de seguir. Você pode continuar navegando e voltar quando quiser — o carrinho fica salvo no seu navegador.'
            ]},
            {'h': '3. Checkout e pagamento', 'p': [
                'No checkout, informe endereço de entrega, contato e CPF (obrigatório para emissão de nota e processamento do pagamento). Aceitamos PIX, cartão de crédito e boleto bancário via Pagar.me.',
                'No PIX, o QR Code e o código copia-e-cola ficam disponíveis na tela de confirmação do pedido. A reserva da gema é temporária — conclua o pagamento dentro do prazo indicado. Cartão aprovado na hora confirma o pedido imediatamente.'
            ]},
            {'h': '4. Confirmação e preparação', 'p': [
                'Assim que o pagamento é confirmado, você recebe um e-mail de confirmação e pode acompanhar o pedido em <a href="/account">Minha conta</a> ou na página de sucesso do checkout.',
                'Suas gemas passam por conferência individual antes do envio — o mesmo cuidado que aplicamos há três gerações na lapidação. Se incluiu laudo gemológico, a pedra segue ao laboratório antes ou conforme a logística do pedido.'
            ]},
            {'h': '5. Envio e rastreio', 'p': [
                'Despachamos com embalagem segura e rastreável para todo o Brasil. Quando o pacote sair, você recebe o código de rastreio por e-mail.',
                'Em caso de dúvida sobre status ou prazo, responda ao e-mail de confirmação do pedido ou acesse sua conta. Estamos à disposição para ajudar em cada etapa.'
            ]},
        ],
    },
    'entenda-tratamentos-em-gemas': {
        'title': 'Tratamentos em gemas: o que você precisa saber',
        'subtitle': 'Esclareça o que é comum, o que é aceitável e o que deve ser declarado antes de comprar.',
        'read_min': 5,
        'banner_image': 'images/tratamentos.png',
        'intro': 'Grande parte das gemas no mercado passa por algum tipo de tratamento — e isso não é necessariamente um problema. O que importa é saber o que foi feito, como isso afeta valor e durabilidade, e exigir transparência de quem vende. Explicamos aqui os tratamentos mais comuns e como a Katsumi Joias lida com essa questão.',
        'sections': [
            {'h': 'Por que gemas são tratadas?', 'p': [
                'Tratamentos existem há séculos: aquecimento, óleo, irradiação e outros processos buscam realçar cor, melhorar pureza aparente ou estabilizar a pedra. Muitos são aceitos pelo mercado internacional quando declarados corretamente.',
                'O problema surge quando tratamentos não são informados — ou quando processos agressivos simulam qualidades que a pedra não teria naturalmente. Por isso a transparência é central em qualquer compra responsável.'
            ]},
            {'h': 'Tratamentos mais comuns', 'p': [
                '<strong>Esmeraldas:</strong> óleo ou resina em fissuras é praticamente universal e aceito, desde que declarado. Esmeraldas “no oil” (sem óleo) são raríssimas e muito mais caras.',
                '<strong>Rubis e safiras:</strong> aquecimento é o tratamento mais comum e geralmente aceito. Difusão ou preenchimento de vidro alteram muito mais o valor e devem ser identificados.',
                '<strong>Quartzo, topázio, citrino:</strong> irradiação e aquecimento são frequentes para intensificar cor. Outras gemas podem receber impregnação ou tingimento — um laudo gemológico independente identifica esses processos.'
            ]},
            {'h': 'Natural, sintético e simulante', 'p': [
                'Pedra <strong>natural</strong> foi formada na natureza, podendo ou não ser tratada. <strong>Sintética</strong> tem a mesma composição química, mas foi criada em laboratório. <strong>Simulante</strong> imita a aparência sem ser a mesma substância (ex.: vidro ou cubic zirconia).',
                'Na Katsumi Joias trabalhamos com gemas naturais selecionadas. Para documentação formal de autenticidade, o laudo gemológico independente distingue natural de sintético e registra os tratamentos detectados.'
            ]},
            {'h': 'O que perguntar antes de comprar', 'p': [
                'Informe-se sobre tratamentos aceitos para aquela espécie. Compare preços com referência de mercado — um rubi muito barato para a cor apresentada pode esconder tratamentos não declarados.',
                'Quando o valor ou a finalidade da compra exigir segurança extra (presente, investimento, coleção), considere o <a href="/guias/certificacao-gemologica">laudo gemológico independente</a> contratado no carrinho — o laboratório documenta o que a análise revelar.'
            ]},
            {'h': 'Nosso compromisso', 'p': [
                'Descrevemos as características de cada pedra com honestidade e selecionamos exemplares que representam o que anunciamos. Dúvidas sobre um produto específico podem ser esclarecidas antes da compra — entre em contato ou use a página <a href="/o-que-procura">O que procura?</a> para conversar com nossos especialistas.'
            ]},
        ],
    },
}


@app.route('/guias')
def guias_index():
    """Índice de guias e conteúdo educativo."""
    guides = [{'slug': slug, **data} for slug, data in GUIDES.items()]
    page_seo = _basic_page_seo(
        f'Guias sobre Gemas | {seo.BRAND}',
        'Guias educativos sobre pedras preciosas, lapidação, laudos gemológicos e compra de gemas brasileiras.',
        path='/guias',
    )
    return render_template('guias.html', guides=guides, seo=page_seo)


@app.route('/guias/certificacao-gemologica-cga')
def guia_cga_redirect():
    """Redirect legacy CGA certification guide URL to the new clean slug."""
    return redirect(url_for('guia_artigo', slug='certificacao-gemologica'), 301)


@app.route('/guias/<slug>')
def guia_artigo(slug):
    """Renderiza um guia individual."""
    guide = GUIDES.get(slug)
    if not guide:
        return redirect(url_for('guias_index'))
    others = [{'slug': s, **d} for s, d in GUIDES.items() if s != slug]
    title = f"{guide['title']} | {seo.BRAND}"
    intro = guide.get('intro', '')[:160]
    page_seo = _basic_page_seo(title, intro, path=f'/guias/{slug}')
    return render_template('guia_artigo.html', guide=guide, slug=slug, others=others, seo=page_seo)


@app.route('/api/certificate-product')
def api_certificate_product():
    """Dados do produto de laudo gemológico independente para o carrinho."""
    product = get_product_by_id(CERTIFICATE_PRODUCT_ID)
    if not product:
        return jsonify({'error': 'Produto não encontrado'}), 404
    _apply_price_markup([product], get_price_increase_rate())
    return jsonify({
        'id': product['id'],
        'name': product.get('name', 'Laudo Gemológico Independente'),
        'price': product.get('price', 0),
        'discount': product.get('discount') or 0,
        'caratWeight': 1,
        'image': product.get('main_image_url') or '',
    })


def _render_products_catalog(fixed_category=None, category_slug_param=None):
    """Shared catalog renderer for /products and /pedras/<slug>."""
    all_products = get_all_products()
    active_products = [p for p in all_products if _is_catalog_visible(p)]
    _categories_data = get_product_categories()
    for product in active_products:
        if 'specifications' in product:
            product['specifications'] = translate_specifications(product['specifications'])
        product['display_description'] = get_product_display_description(product, _categories_data)

    def safe_float(value):
        if not value:
            return None
        try:
            if isinstance(value, str):
                value = value.replace(',', '.')
            return float(value)
        except (ValueError, TypeError):
            return None

    markup_rate = get_price_increase_rate()
    _exch_setting = get_exchange_rate()
    exchange_rate_val = 0.0
    if _exch_setting and 'value' in _exch_setting and 'rate' in (_exch_setting.get('value') or {}):
        try:
            exchange_rate_val = float(_exch_setting['value']['rate'])
        except (ValueError, TypeError):
            pass

    bounds = _get_dimension_bounds(active_products)

    _pedras_order = ['unico', 'par', 'trio', 'conjunto']
    _pedras_key = lambda n: 'unico' if n == 1 else ('par' if n == 2 else ('trio' if n == 3 else ('conjunto' if n > 3 else None)))

    category_options = {}
    _products_by_cat = {}
    for _p in active_products:
        _cat = _p.get('category', '')
        if not _cat:
            continue
        _products_by_cat.setdefault(_cat, []).append(_p)
    for _cat, _prods in _products_by_cat.items():
        category_options[_cat] = _build_category_filter_options(_prods, _pedras_order, _pedras_key, markup_rate, exchange_rate_val)
    category_options['__all__'] = _build_category_filter_options(active_products, _pedras_order, _pedras_key, markup_rate, exchange_rate_val)

    filter_args = request.args.to_dict()
    if fixed_category:
        filter_args['category'] = fixed_category
    products = _apply_product_filters(active_products, filter_args, safe_float, markup_rate=markup_rate, exchange_rate=exchange_rate_val)
    sort_by = request.args.get('sort_by', '')
    products = _sort_products(products, sort_by)
    search_query = request.args.get('search', '').strip()
    category = fixed_category or request.args.get('category')
    min_altura = request.args.get('min_altura')
    max_altura = request.args.get('max_altura')
    min_comprimento = request.args.get('min_comprimento')
    max_comprimento = request.args.get('max_comprimento')
    min_largura = request.args.get('min_largura')
    max_largura = request.args.get('max_largura')
    min_peso = request.args.get('min_peso')
    max_peso = request.args.get('max_peso')
    min_preco_ct = request.args.get('min_preco_ct')
    max_preco_ct = request.args.get('max_preco_ct')
    min_preco_total = request.args.get('min_preco_total')
    max_preco_total = request.args.get('max_preco_total')
    cut = request.args.get('cut')
    numero_pedras = request.args.get('numero_pedras')
    riviera = request.args.get('riviera') == 'true'

    total_count = len(products)
    per_page = 20
    page = request.args.get('page', 1, type=int)
    page = max(1, page)
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    products_page_items = products[start:start + per_page]

    def _pagination_url(p):
        args = {k: v for k, v in request.args.to_dict().items() if k != 'page'}
        if fixed_category:
            args.pop('category', None)
        args['page'] = p
        if fixed_category and category_slug_param:
            return url_for('category_page', category_slug=category_slug_param, **args)
        return url_for('products_page', **args)

    pagination = {
        'page': page,
        'per_page': per_page,
        'total_count': total_count,
        'total_pages': total_pages,
        'has_next': page < total_pages,
        'has_prev': page > 1,
        'next_url': _pagination_url(page + 1) if page < total_pages else None,
        'prev_url': _pagination_url(page - 1) if page > 1 else None,
    }

    _apply_price_markup(products_page_items, markup_rate)

    categories = sorted(set([p.get('category') for p in all_products if p.get('category')]))
    cut_types = get_cut_types()
    has_riviera_products = any(p.get('riviera') for p in active_products)

    site = _site_url()
    canonical = seo.catalog_canonical(site, category, page)
    page_title = seo.category_page_title(category)
    page_description = seo.category_meta_description(category)
    page_seo = {
        'title': page_title,
        'description': page_description,
        'canonical_url': canonical,
        'og_title': page_title,
        'og_description': page_description,
        'og_url': canonical,
        'og_type': 'website',
        'og_image': f'{site}/static/images/banner1.jpg',
        'twitter_card': 'summary_large_image',
        'prev_url': _abs_url(pagination['prev_url']),
        'next_url': _abs_url(pagination['next_url']),
    }

    return render_template('products.html',
                         products=products_page_items,
                         categories=categories,
                         cuts=cut_types,
                         bounds=bounds,
                         category_options=category_options,
                         pagination=pagination,
                         has_riviera_products=has_riviera_products,
                         filters={
                             'search': search_query,
                             'category': category,
                             'min_altura': min_altura,
                             'max_altura': max_altura,
                             'min_comprimento': min_comprimento,
                             'max_comprimento': max_comprimento,
                             'min_largura': min_largura,
                             'max_largura': max_largura,
                             'min_peso': min_peso,
                             'max_peso': max_peso,
                             'min_preco_ct': min_preco_ct,
                             'max_preco_ct': max_preco_ct,
                             'min_preco_total': min_preco_total,
                             'max_preco_total': max_preco_total,
                             'cut': cut,
                             'numero_pedras': numero_pedras,
                             'riviera': riviera,
                             'sort_by': sort_by,
                         },
                         seo=page_seo,
                         category_h1=seo.category_h1(category),
                         category_slug=category_slug_param,
                         firebase_config=get_firebase_config())


@app.route('/products')
def products_page():
    args = request.args.to_dict()
    if args.get('category') and set(args.keys()) <= {'category', 'page'}:
        slug = seo.category_slug(args['category'])
        page = args.get('page')
        target = url_for('category_page', category_slug=slug)
        if page and str(page) != '1':
            return redirect(f'{target}?page={page}', 301)
        return redirect(target, 301)
    return _render_products_catalog()


@app.route('/pedras/<category_slug>')
def category_page(category_slug):
    all_products = get_all_products()
    categories = sorted(set(p.get('category') for p in all_products if p.get('category')))
    cat = seo.resolve_category_from_slug(category_slug, categories)
    if not cat:
        return "Category not found", 404
    return _render_products_catalog(fixed_category=cat, category_slug_param=category_slug)

@app.route('/product/<product_id>')
def product_detail(product_id):
    product = get_product_by_id(product_id)

    if product is None:
        return "Product not found", 404
    if not product.get('active', False) and not _user_purchased_product(product_id):
        return "Product not found", 404

    # Translate specifications
    if 'specifications' in product:
        product['specifications'] = translate_specifications(product['specifications'])
    
    # Ensure gallery_urls exists
    if 'gallery_urls' not in product:
        product['gallery_urls'] = []

    categories_data = get_product_categories()
    product['display_description'] = get_product_display_description(product, categories_data)

    price_rate = get_price_increase_rate()
    if price_rate:
        product['price'] = round(product.get('price', 0) * (1 + price_rate / 100), 2)

    site = _site_url()
    exchange = get_exchange_rate()
    rate_data = exchange.get('value') if exchange else None
    product_url = f'{site}/product/{product_id}'
    image_url = seo.product_absolute_image(product, site)
    page_title = seo.product_page_title(product)
    page_description = seo.product_meta_description(product)
    json_ld = [
        seo.build_product_json_ld(product, site, rate_data, price_rate or 0),
        seo.build_breadcrumb_json_ld([
            {'name': 'Início', 'url': f'{site}/'},
            {'name': product.get('category') or 'Catálogo', 'url': f"{site}{seo.category_path(product.get('category', ''))}"},
            {'name': product.get('name') or 'Gema', 'url': product_url},
        ]),
    ]
    page_seo = {
        'title': page_title,
        'description': page_description,
        'canonical_url': product_url,
        'og_title': page_title,
        'og_description': page_description,
        'og_url': product_url,
        'og_type': 'product',
        'og_image': image_url,
        'twitter_card': 'summary_large_image',
        'json_ld': json_ld,
    }

    return render_template('product_detail.html',
                         product=product,
                         firebase_config=get_firebase_config(),
                         view_only=not product.get('active', False),
                         seo=page_seo)

@app.route('/cart')
def cart():
    """Shopping cart page."""
    return render_template('cart.html',
                         firebase_config=get_firebase_config())

@app.route('/api/exchange-rate')
def api_exchange_rate():
    """API endpoint to get current exchange rate (cached 5 min)."""
    now = time.time()
    if _exchange_rate_cache['data'] and now - _exchange_rate_cache['ts'] < _EXCHANGE_RATE_TTL:
        resp = jsonify(_exchange_rate_cache['data'])
        resp.headers['Cache-Control'] = 'public, max-age=300'
        return resp
    rate_setting = get_exchange_rate()
    if rate_setting and 'value' in rate_setting:
        _exchange_rate_cache['data'] = rate_setting['value']
        _exchange_rate_cache['ts']   = now
        resp = jsonify(rate_setting['value'])
        resp.headers['Cache-Control'] = 'public, max-age=300'
        return resp
    return jsonify({'error': 'Exchange rate not available'}), 404

@app.route('/api/update-exchange-rate', methods=['POST'])
@require_admin_api
def api_update_exchange_rate():
    """API endpoint to update exchange rate from external API."""
    try:
        rate_data = refresh_exchange_rate_from_api(source='admin_api', force=True)
        return jsonify(rate_data)
    except Exception as e:
        print(f"Error updating exchange rate: {e}")
        return jsonify({'error': 'Erro ao atualizar câmbio'}), 500


@app.route('/internal/cron/exchange-rate', methods=['POST'])
def cron_exchange_rate():
    """Cloud Scheduler endpoint — set EXCHANGE_RATE_CRON_SECRET in env."""
    secret = os.getenv('EXCHANGE_RATE_CRON_SECRET')
    if not secret or request.headers.get('X-Cron-Secret') != secret:
        return jsonify({'error': 'Unauthorized'}), 401
    try:
        rate_data = refresh_exchange_rate_from_api(source='cloud_scheduler', force=True)
        return jsonify({'success': True, **rate_data})
    except Exception as e:
        print(f"[ExchangeRate/cron] Error: {e}")
        return jsonify({'error': 'Erro ao atualizar câmbio'}), 500

@app.route('/checkout/paypal')
def checkout_paypal():
    """Redirect legacy PayPal route to unified Pagar.me checkout."""
    return redirect(url_for('checkout_page'), 301)

@app.route('/checkout/mercadopago')
def checkout_mercadopago():
    """Redirect legacy MercadoPago route to unified Pagar.me checkout."""
    return redirect(url_for('checkout_page'), 301)

# ============================================
# Pagar.me Checkout Routes
# ============================================

@app.route('/checkout')
def checkout_page():
    """Main checkout page (Pagar.me — BRL)."""
    exchange_rate_setting = get_exchange_rate()
    exchange_rate = None
    if exchange_rate_setting and 'value' in exchange_rate_setting:
        exchange_rate = exchange_rate_setting['value']

    return render_template(
        'checkout.html',
        pagarme_public_key=os.getenv('PAGARME_CHAVE_PUBLICA', ''),
        exchange_rate=exchange_rate,
        firebase_config=get_firebase_config(),
    )


@app.route('/pix/copiar/<order_id>')
def pix_copy_landing(order_id):
    """Minimal page to copy PIX code to clipboard (linked from transactional emails)."""
    order = get_order_by_id(order_id)
    if not order or not order.get('pix_qr_code'):
        return redirect(url_for('checkout_success', order_id=order_id))
    return render_template(
        'pix_copy.html',
        pix_code=order['pix_qr_code'],
        success_url=url_for('checkout_success', order_id=order_id, _external=False),
    )


@app.route('/checkout/success/<order_id>')
def checkout_success(order_id):
    """Order confirmation / payment result page."""
    order = get_order_by_id(order_id)
    if order:
        for ts_field in ('created_at', 'updated_at'):
            if ts_field in order and hasattr(order[ts_field], 'strftime'):
                order[ts_field] = format_datetime_brt(order[ts_field])
        if order.get('pix_expires_at') and hasattr(order['pix_expires_at'], 'strftime'):
            order['pix_expires_at'] = order['pix_expires_at'].isoformat()
        if order.get('items') and isinstance(order['items'], list):
            for item in order['items']:
                if isinstance(item, dict):
                    for k, v in item.items():
                        if hasattr(v, 'strftime'):
                            item[k] = format_datetime_brt(v)
    return render_template('checkout_success.html', order=order)


@app.route('/api/checkout/process', methods=['POST'])
@limiter.limit("10/minute")
def api_checkout_process():
    """
    Process a checkout order:
    1. Validate cart items against Firestore prices.
    2. Find or create customer in Pagar.me.
    3. Create order in Pagar.me.
    4. Persist order to Firestore.
    Returns JSON with order_id (Firestore) for redirect.
    """
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({'error': 'Dados inválidos.'}), 400

        # ── 1. Validate required fields ──
        required = ['name', 'email', 'cpf', 'phone', 'address', 'payment_method', 'cart_items']
        for field in required:
            if not data.get(field):
                return jsonify({'error': f'Campo obrigatório ausente: {field}'}), 400

        name           = data['name'].strip()
        email          = data['email'].strip().lower()
        cpf            = re.sub(r'\D', '', data['cpf'])
        phone          = re.sub(r'\D', '', data['phone'])
        address        = data['address']
        payment_method = data['payment_method']
        cart_items_raw = data['cart_items']

        if payment_method not in ('pix', 'credit_card', 'boleto'):
            return jsonify({'error': 'Método de pagamento inválido.'}), 400

        if not cart_items_raw or not isinstance(cart_items_raw, list):
            return jsonify({'error': 'Carrinho vazio.'}), 400

        # ── 2. Validate & price cart server-side ──
        validated_items, total_brl = _validate_cart_server(cart_items_raw)
        if not validated_items:
            return jsonify({'error': 'Nenhum produto válido no carrinho. Tente novamente.'}), 400

        # ── 3. Create/find customer in Pagar.me ──
        phone_area   = phone[:2] if len(phone) >= 2 else '11'
        phone_number = phone[2:] if len(phone) > 2 else phone

        pg_customer, err = pagarme_client.get_or_create_customer(
            name=name,
            email=email,
            document=cpf,
            phone_area_code=phone_area,
            phone_number=phone_number,
        )
        if err or not pg_customer:
            msg = err.get('message', 'Erro ao registrar cliente.') if err else 'Erro ao registrar cliente.'
            return jsonify({'error': msg}), 422

        # ── 4. Build order items for Pagar.me (in centavos) ──
        # (shipping amount is handled via the shipping object, not as a separate item)
        pg_items = []
        for item in validated_items:
            amount_centavos = max(1, int(round(item['price_brl'] * 100)))
            pg_items.append({
                'amount':      amount_centavos,
                'description': item['name'][:64],
                'quantity':    1,
                'code':        re.sub(r'[^a-zA-Z0-9_\-]', '', item['product_id'])[:52] or 'PROD',
            })

        # ── 5. Calculate shipping cost ──
        addr = address
        order_state = addr.get('state', '').strip().upper()
        shipping_cost_brl = calculate_shipping(order_state, total_brl)
        grand_total_brl   = round(total_brl + shipping_cost_brl, 2)

        if payment_method == 'credit_card' and grand_total_brl < MIN_CREDIT_CARD_BRL:
            return jsonify({
                'error': 'Pagamento no cartão disponível apenas para pedidos a partir de R$ 1,00. Use PIX para valores menores.',
            }), 400

        cep_clean = re.sub(r'\D', '', addr.get('cep', ''))
        shipping_amount_centavos = int(round(shipping_cost_brl * 100))
        shipping = {
            'amount': shipping_amount_centavos,
            'description': 'Entrega Katsumi Joias',
            'recipient_name': name,
            'address': {
                'zip_code': cep_clean,
                'line_1':   f"{addr.get('street','')}, {addr.get('number','')}",
                'line_2':   addr.get('complement', ''),
                'city':     addr.get('city', ''),
                'state':    addr.get('state', ''),
                'country':  'BR',
            }
        }

        # ── 6. Build payment extras ──
        payment_extra = {}
        if payment_method == 'credit_card':
            payment_extra['card_token']   = data.get('card_token', '')
            installments_raw = int(data.get('installments', 1))
            payment_extra['installments'] = max(1, min(5, installments_raw))
            if not payment_extra['card_token']:
                return jsonify({'error': 'Token do cartão não fornecido.'}), 400

        # Build billing address from shipping address (for credit card)
        billing_address = None
        if payment_method == 'credit_card':
            billing_address = {
                'line_1':   f"{addr.get('street', '')}, {addr.get('number', '')}",
                'line_2':   addr.get('complement', '') or addr.get('neighborhood', ''),
                'city':     addr.get('city', ''),
                'state':    addr.get('state', ''),
                'zip_code': cep_clean,
                'country':  'BR',
            }

        idempotency_key = f"{email}:{uuid.uuid4().hex}"

        # ── 7. Create order in Pagar.me ──
        pg_order, err = pagarme_client.create_order(
            customer_id=pg_customer['id'],
            items=pg_items,
            payment_method=payment_method,
            payment_extra=payment_extra,
            shipping=shipping,
            billing_address=billing_address,
            idempotency_key=idempotency_key,
        )
        if err or not pg_order:
            msg = err.get('message', 'Erro ao criar pedido no gateway.') if err else 'Erro ao criar pedido.'
            return jsonify({'error': msg}), 422

        # ── 8. Extract payment details ──
        pix_data    = pagarme_client.extract_pix_data(pg_order)    if payment_method == 'pix'          else {}
        boleto_data = pagarme_client.extract_boleto_data(pg_order) if payment_method == 'boleto'       else {}
        card_data   = pagarme_client.extract_card_data(pg_order)   if payment_method == 'credit_card'  else {}

        # ── 9. Determine order status ──
        pg_status = pg_order.get('status', 'pending')

        # ── 10. Upsert customer in Firestore ──
        firestore_customer_id = _upsert_customer_firestore(
            name=name, email=email, phone=phone, cpf=cpf, address=addr,
            pagarme_customer_id=pg_customer['id'],
            firebase_uid=_resolve_firebase_uid_from_request(),
        )

        # ── 11. Build Firestore order document ──
        order_doc = {
            'source':             SOURCE_WEBSITE,
            'pagarme_order_id':   pg_order.get('id', ''),
            'pagarme_customer_id': pg_customer['id'],
            'firestore_customer_id': firestore_customer_id,
            'firebase_uid':       _resolve_firebase_uid_from_request(),
            'status':             pg_status,
            'payment_method':     payment_method,
            'subtotal_brl':       total_brl,
            'shipping_cost_brl':  shipping_cost_brl,
            'amount_brl':         grand_total_brl,
            'amount_centavos':    int(round(grand_total_brl * 100)),
            'currency':           'BRL',
            'customer_name':      name,
            'customer_email':     email,
            'customer_phone':     phone,
            'customer_cpf':       cpf,
            'shipping_address':   addr,
            'items': [
                {
                    'product_id': item['product_id'],
                    'name':       item['name'],
                    'amount_brl': item['price_brl'],
                    'quantity':   1,
                    'image':      item.get('image', ''),
                }
                for item in validated_items
            ],
            # PIX
            'pix_qr_code':     pix_data.get('qr_code', ''),
            'pix_qr_code_url': pix_data.get('qr_code_url', ''),
            'pix_expires_at':  pix_data.get('expires_at', ''),
            # Boleto
            'boleto_url':      boleto_data.get('boleto_url', ''),
            'boleto_barcode':  boleto_data.get('boleto_barcode', ''),
            'boleto_due_at':   boleto_data.get('due_at', ''),
            'boleto_pdf':      boleto_data.get('pdf', ''),
            # Card
            'card_brand':          card_data.get('brand', ''),
            'card_last_four':      card_data.get('last_four', ''),
            'authorization_code':  card_data.get('authorization_code', ''),
        }

        saved_order = create_order_db(order_doc)
        if not saved_order:
            # Order was created in Pagar.me but failed to save locally — log and continue
            print(f"[Checkout] CRITICAL: Pagar.me order {pg_order.get('id')} created but NOT saved to Firestore!")
            return jsonify({
                'error': 'Pedido criado mas houve um erro ao salvar. Entre em contato com o suporte.'
            }), 500

        _send_checkout_transactional_emails(saved_order)

        return jsonify({
            'success':  True,
            'order_id': saved_order['id'],
        }), 200

    except Exception as e:
        print(f"[Checkout] Unexpected error: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': 'Erro interno. Por favor, tente novamente.'}), 500


@app.route('/api/pagarme/webhook', methods=['POST'])
def pagarme_webhook():
    """
    Handle Pagar.me webhook events to update order status.
    Signature verification uses the API key as HMAC secret.
    """
    raw_payload = request.get_data()
    signature   = request.headers.get('X-Hub-Signature', '')

    api_key = os.getenv('PAGARME_API_KEY', '')
    if not api_key:
        print("[Webhook] PAGARME_API_KEY not configured — rejecting request for security")
        return jsonify({'error': 'Service unavailable'}), 503
    if not pagarme_client.verify_webhook_signature(raw_payload, signature, api_key):
        print("[Webhook] Invalid signature — request rejected")
        return jsonify({'error': 'Invalid signature'}), 401

    try:
        event = request.get_json(silent=True) or {}
        event_type = event.get('type', '')
        event_data = event.get('data', {})

        print(f"[Webhook] Event: {event_type} | data_id: {event_data.get('id','')}")

        # Map event type to order status
        STATUS_MAP = {
            'order.paid':              'paid',
            'order.payment_failed':    'failed',
            'order.canceled':          'canceled',
            'order.created':           'pending',
            'order.closed':            'closed',
            'charge.paid':             None,  # handled by order.paid
            'charge.payment_failed':   None,
        }

        target_status = STATUS_MAP.get(event_type)

        if target_status and event_data.get('id'):
            pagarme_order_id = event_data['id']
            order = get_order_by_pagarme_id(pagarme_order_id)
            if order:
                previous_status = order.get('status')
                update_order_status(order['id'], target_status, {
                    'webhook_event': event_type,
                    'webhook_received_at': _now_brt_iso(),
                })
                print(f"[Webhook] Order {order['id']} updated to '{target_status}'")
                _handle_order_paid_side_effects(order['id'], previous_status)
            else:
                print(f"[Webhook] Order not found for pagarme_id: {pagarme_order_id}")

        return jsonify({'received': True}), 200

    except Exception as e:
        print(f"[Webhook] Error: {e}")
        return jsonify({'error': 'Internal error'}), 500


@app.route('/api/orders/<order_id>/refresh-status', methods=['POST'])
def api_refresh_order_status(order_id):
    """
    Manually poll Pagar.me for the current status of an order and update
    Firestore. Used when webhooks are not yet delivered (e.g. local dev or
    a user wants to verify their PIX payment on the success/account page).
    """
    PAGARME_STATUS_MAP = {
        'paid':          'paid',
        'authorized':    'paid',
        'pending':       'waiting_payment',
        'waiting_payment': 'waiting_payment',
        'processing':    'processing',
        'failed':        'failed',
        'canceled':      'canceled',
        'voided':        'canceled',
        'chargedback':   'failed',
    }

    try:
        order = get_order_by_id(order_id)
        if not order:
            return jsonify({'error': 'Pedido não encontrado'}), 404

        pagarme_order_id = order.get('pagarme_order_id')
        if not pagarme_order_id:
            return jsonify({'status': order.get('status', 'pending')}), 200

        pm_data, pm_err = pagarme_client.get_order(pagarme_order_id)
        if pm_err or not pm_data:
            return jsonify({'status': order.get('status', 'pending')}), 200

        pm_status  = pm_data.get('status', '')
        new_status = PAGARME_STATUS_MAP.get(pm_status, order.get('status', 'pending'))

        if new_status != order.get('status'):
            extra = {'pagarme_status_raw': pm_status, 'status_refreshed_at': _now_brt_iso()}

            # Try to capture pix/boleto data in case it was missing
            charges = pm_data.get('charges', [])
            if charges:
                first_charge = charges[0]
                last_txn = (first_charge.get('last_transaction') or {})
                pix_qr = last_txn.get('qr_code')
                pix_url = last_txn.get('qr_code_url')
                if pix_qr:
                    extra['pix_copy_paste'] = pix_qr
                if pix_url:
                    extra['pix_qr_code_url'] = pix_url

            update_order_status(order_id, new_status, extra)
            print(f"[RefreshStatus] Order {order_id}: {order.get('status')} → {new_status}")
            _handle_order_paid_side_effects(order_id, order.get('status'))

        return jsonify({'status': new_status, 'previous_status': order.get('status')}), 200

    except Exception as e:
        print(f"[RefreshStatus] Error: {e}")
        return jsonify({'error': 'Erro interno'}), 500


@app.route('/api/orders/<order_id>/regenerate-pix', methods=['POST'])
def api_regenerate_pix(order_id):
    """Generate a new PIX charge when the previous QR code has expired."""
    try:
        order = get_order_by_id(order_id)
        if not order:
            return jsonify({'error': 'Pedido não encontrado'}), 404

        if order.get('payment_method') != 'pix':
            return jsonify({'error': 'Este pedido não utiliza PIX.'}), 400

        if order.get('status') in ('paid', 'authorized', 'canceled'):
            return jsonify({'error': 'Não é possível gerar um novo PIX para este pedido.'}), 400

        ok, err_msg = _validate_order_products_available(order)
        if not ok:
            return jsonify({'error': err_msg}), 409

        pagarme_customer_id = order.get('pagarme_customer_id')
        if not pagarme_customer_id:
            return jsonify({'error': 'Pedido sem referência de pagamento.'}), 400

        # Pagar.me orders are created with closed=true — cannot add charges to them.
        # Create a fresh PIX order with the same items and shipping.
        pg_items = []
        for item in order.get('items', []):
            amount_centavos = max(1, int(round(float(item.get('amount_brl', 0)) * 100)))
            pg_items.append({
                'amount':      amount_centavos,
                'description': str(item.get('name', 'Produto'))[:64],
                'quantity':    max(1, int(item.get('quantity', 1))),
                'code':        re.sub(r'[^a-zA-Z0-9_\-]', '', str(item.get('product_id', 'PROD')))[:52] or 'PROD',
            })
        if not pg_items:
            return jsonify({'error': 'Pedido sem itens válidos.'}), 400

        addr = order.get('shipping_address') or {}
        cep_clean = re.sub(r'\D', '', addr.get('cep', ''))
        shipping_cost_brl = float(order.get('shipping_cost_brl', 0) or 0)
        shipping = {
            'amount': int(round(shipping_cost_brl * 100)),
            'description': 'Entrega Katsumi Joias',
            'recipient_name': order.get('customer_name', ''),
            'address': {
                'zip_code': cep_clean,
                'line_1':   f"{addr.get('street', '')}, {addr.get('number', '')}",
                'line_2':   addr.get('complement', '') or addr.get('neighborhood', ''),
                'city':     addr.get('city', ''),
                'state':    addr.get('state', ''),
                'country':  'BR',
            },
        }

        pg_order, err = pagarme_client.create_order(
            customer_id=pagarme_customer_id,
            items=pg_items,
            payment_method='pix',
            shipping=shipping,
            idempotency_key=f"pix-regen:{order_id}:{uuid.uuid4().hex}",
        )
        if err or not pg_order:
            msg = err.get('message', 'Erro ao gerar novo PIX.') if err else 'Erro ao gerar novo PIX.'
            return jsonify({'error': msg}), 422

        pix_data = pagarme_client.extract_pix_data(pg_order)
        new_status = pg_order.get('status', order.get('status', 'waiting_payment'))
        update_order_status(order_id, new_status, {
            'pagarme_order_id': pg_order.get('id', ''),
            'pix_qr_code': pix_data.get('qr_code', ''),
            'pix_qr_code_url': pix_data.get('qr_code_url', ''),
            'pix_expires_at': pix_data.get('expires_at', ''),
            'pix_regenerated_at': _now_brt_iso(),
        })

        print(f"[PIX] Regenerated (new order {pg_order.get('id')}) for order {order_id}")
        return jsonify({'success': True, 'status': new_status}), 200

    except Exception as e:
        print(f"[PIX] Regenerate error: {e}")
        return jsonify({'error': 'Erro interno'}), 500


# ── Internal helpers ──────────────────────────────────────────────────────────

def _validate_order_products_available(order):
    """Ensure gem line items are still active (not sold meanwhile)."""
    unavailable = []
    for item in order.get('items', []):
        product_id = item.get('product_id')
        if not product_id or product_id in CATALOG_HIDDEN_PRODUCT_IDS:
            continue
        product = get_product_by_id(product_id)
        label = (product or {}).get('name') or item.get('name') or product_id
        if not product or not product.get('active', False):
            unavailable.append(label)
    if not unavailable:
        return True, None
    names = ', '.join(unavailable[:3])
    if len(unavailable) > 3:
        names += '…'
    return False, f'Um ou mais produtos deste pedido já foram vendidos ({names}). Entre em contato conosco.'


def _validate_cpf(cpf):
    cpf = re.sub(r'\D', '', cpf or '')
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    for offset in (9, 10):
        total = sum(int(cpf[i]) * ((offset + 1) - i) for i in range(offset))
        digit = (total * 10 % 11) % 10
        if digit != int(cpf[offset]):
            return False
    return True


def _resolve_firebase_uid_from_request():
    """Resolve Firebase UID from Bearer token (checkout/account) or Flask session."""
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        try:
            decoded = firebase_auth.verify_id_token(auth_header.split('Bearer ', 1)[1])
            return decoded.get('uid', '') or ''
        except Exception:
            pass
    return session.get('user_id') or ''


def _send_checkout_transactional_emails(order):
    """Send ORDER_CREATED or ORDER_PAID after checkout without affecting the HTTP response."""
    order_id = order.get('id', '')
    short_id = (order_id[:8].upper() if order_id else '—')
    status = order.get('status', '')
    try:
        if email_service.is_paid_status(status):
            print(f"[Email] Checkout order #{short_id}: status={status} → ORDER_PAID")
            _mark_order_products_sold(order)
            if email_service.send_order_paid_email(order):
                update_order_status(order['id'], order['status'], {
                    'paid_email_sent_at': _now_brt_iso(),
                })
            else:
                print(f"[Email] Checkout order #{short_id}: ORDER_PAID not sent")
        else:
            print(f"[Email] Checkout order #{short_id}: status={status} → ORDER_CREATED")
            if status not in ('failed', 'canceled') and not email_service.send_order_created_email(order):
                print(f"[Email] Checkout order #{short_id}: ORDER_CREATED not sent")
    except Exception as exc:
        print(f"[Email] Checkout notification failed: {type(exc).__name__}: {exc}")


def _mark_order_products_sold(order):
    """Set sold products to draft and append sales history. Idempotent per order."""
    if not order or order.get('products_marked_sold'):
        return

    order_id = order.get('id', '')
    sold_at = _now_brt_iso()
    sale_base = {
        'order_id': order_id,
        'order_number': order_id[:8].upper() if order_id else '',
        'source': SOURCE_WEBSITE,
        'sale_mode': 'full',
        'customer_name': order.get('customer_name', ''),
        'customer_email': order.get('customer_email', ''),
        'amount_brl': order.get('amount_brl', 0),
        'sold_at': sold_at,
    }

    marked_any = False
    has_gem_items = False
    for item in order.get('items', []):
        product_id = item.get('product_id')
        if not product_id or product_id in CATALOG_HIDDEN_PRODUCT_IDS:
            continue
        has_gem_items = True
        sale_record = {
            **sale_base,
            'item_name': item.get('name', ''),
            'item_amount_brl': item.get('amount_brl', 0),
        }
        if record_product_sale(product_id, sale_record):
            marked_any = True

    if marked_any or not has_gem_items:
        update_order_status(order_id, order.get('status', 'paid'), {
            'products_marked_sold': True,
        })


def _handle_order_paid_side_effects(order_id, previous_status):
    """When an order transitions to paid: mark products sold and send confirmation email."""
    order = get_order_by_id(order_id)
    if not order or order.get('status') not in PAID_ORDER_STATUSES:
        return
    if (previous_status or '') in PAID_ORDER_STATUSES:
        return
    _mark_order_products_sold(order)
    _send_order_paid_email_if_needed(order_id, previous_status)


def _user_purchased_product(product_id, firebase_uid=None, email=None):
    """True if the user has a paid order containing this product (scoped draft access)."""
    uid = firebase_uid or session.get('user_id') or ''
    user_email = (email or session.get('user_email') or '').strip().lower()

    if not uid and not user_email:
        return False

    seen_ids = set()
    orders = []
    if uid:
        for o in get_orders_by_firebase_uid(uid):
            seen_ids.add(o.get('id'))
            orders.append(o)
    if user_email:
        for o in get_orders_by_customer_email(user_email):
            if o.get('id') not in seen_ids:
                orders.append(o)

    for order in orders:
        if order.get('status') not in PAID_ORDER_STATUSES:
            continue
        for item in order.get('items', []):
            if item.get('product_id') == product_id:
                return True
    return False


def _send_order_paid_email_if_needed(order_id, previous_status):
    """Send ORDER_PAID once when status transitions to paid."""
    try:
        order = get_order_by_id(order_id)
        if not order:
            return
        if not email_service.should_send_paid_email(order, previous_status):
            return
        if email_service.send_order_paid_email(order):
            update_order_status(order_id, order.get('status'), {
                'paid_email_sent_at': _now_brt_iso(),
            })
    except Exception as exc:
        print(f"[Email] ORDER_PAID notification failed: {type(exc).__name__}")


def _validate_cart_server(cart_items_raw):
    """
    Re-price each cart item using Firestore data + current exchange rate.
    Returns (validated_items, total_brl).
    validated_items: list of {product_id, name, price_brl, image}
    """
    exchange_setting = get_exchange_rate()
    exchange_rate    = None
    if exchange_setting and 'value' in exchange_setting:
        exchange_rate = float(exchange_setting['value'].get('rate', 0) or 0)

    markup_rate = get_price_increase_rate()
    cert_product = get_product_by_id(CERTIFICATE_PRODUCT_ID)

    validated = []
    total_brl = 0.0

    for raw in cart_items_raw:
        product_id = raw.get('id')
        if not product_id or product_id == CERTIFICATE_PRODUCT_ID:
            continue

        product = get_product_by_id(product_id)
        if not product or not product.get('active', False):
            continue

        price_usd = float(product.get('price', 0) or 0)

        carat_raw = str(product.get('carat_weight', '') or '').strip()
        if carat_raw:
            cw_num = carat_raw.split()[0] if ' ' in carat_raw else carat_raw
            try:
                carat = float(cw_num.replace(',', '.'))
            except (ValueError, TypeError):
                carat = 1.0
        else:
            carat = 1.0

        total_usd = price_usd * carat

        if markup_rate:
            total_usd = total_usd * (1 + markup_rate / 100)

        if exchange_rate:
            price_brl = total_usd * exchange_rate
        else:
            price_brl = total_usd

        total_brl += price_brl
        validated.append({
            'product_id': product['id'],
            'name':       product.get('name', 'Produto'),
            'price_brl':  round(price_brl, 2),
            'image':      product.get('main_image_url', ''),
            'wants_certificate': bool(raw.get('wantsCertificate')),
        })

        if raw.get('wantsCertificate') and cert_product and cert_product.get('active', False):
            cert_usd = float(cert_product.get('price', 0) or 0)
            if markup_rate:
                cert_usd = cert_usd * (1 + markup_rate / 100)
            cert_brl = cert_usd * exchange_rate if exchange_rate else cert_usd
            total_brl += cert_brl
            gem_name = product.get('name', 'Produto')
            validated.append({
                'product_id': CERTIFICATE_PRODUCT_ID,
                'name':       f"Laudo Gemológico: {gem_name}"[:80],
                'price_brl':  round(cert_brl, 2),
                'image':      cert_product.get('main_image_url', ''),
                'for_product_id': product['id'],
            })

    return validated, round(total_brl, 2)


def _upsert_customer_firestore(name, email, phone, cpf, address, pagarme_customer_id, firebase_uid=None):
    """
    Find existing customer in Firestore by email or firebase_uid
    and update with Pagar.me customer ID, or create a new record.
    Returns the Firestore customer document ID.
    """
    try:
        customers = get_all_customers()
        existing = None
        for c in customers:
            if firebase_uid and c.get('firebase_uid') == firebase_uid:
                existing = c; break
            elif c.get('email') == email:
                existing = c; break

        customer_data = {
            'name':               name,
            'email':              email,
            'phone':              phone,
            'cpf':                cpf,
            'address':            f"{address.get('street','')} {address.get('number','')}, {address.get('neighborhood','')}, {address.get('city','')} - {address.get('state','')}",
            'address_structured': {
                'cep':          address.get('cep', ''),
                'street':       address.get('street', ''),
                'number':       address.get('number', ''),
                'complement':   address.get('complement', ''),
                'neighborhood': address.get('neighborhood', ''),
                'city':         address.get('city', ''),
                'state':        address.get('state', ''),
            },
            'pagarme_customer_id': pagarme_customer_id,
        }
        if firebase_uid:
            customer_data['firebase_uid'] = firebase_uid

        if existing:
            # `source` marca a origem do cadastro — nunca é reescrita em compras futuras.
            update_customer(existing['id'], customer_data)
            return existing['id']
        else:
            customer_data['source'] = SOURCE_WEBSITE
            customer_data['person_type'] = 'cpf'
            result = create_customer(customer_data)
            return result['id'] if result else None
    except Exception as e:
        print(f"[Checkout] Error upserting customer in Firestore: {e}")
        return None


# ============================================
# CRM — Origem, auditoria e financeiro dos pedidos
# ============================================

SOURCE_LABELS = {SOURCE_WEBSITE: 'Site', SOURCE_INTERNAL: 'Interno'}

BRAZILIAN_STATES = (
    'AC', 'AL', 'AP', 'AM', 'BA', 'CE', 'DF', 'ES', 'GO', 'MA', 'MT', 'MS', 'MG',
    'PA', 'PB', 'PR', 'PE', 'PI', 'RJ', 'RN', 'RS', 'RO', 'RR', 'SC', 'SP', 'SE', 'TO',
)

INTERNAL_PAYMENT_METHODS = [
    ('pix', 'PIX'),
    ('dinheiro', 'Dinheiro'),
    ('transferencia', 'Transferência / TED'),
    ('boleto', 'Boleto'),
    ('cheque', 'Cheque'),
    ('cartao', 'Cartão'),
    ('outro', 'Outro'),
]
INTERNAL_PAYMENT_METHOD_LABELS = dict(INTERNAL_PAYMENT_METHODS)

ENTITY_LABELS = {
    'product': 'Produto',
    'customer': 'Cliente',
    'order': 'Pedido',
    'payment': 'Pagamento',
    'seller': 'Vendedor',
    'setting': 'Ajuste',
}

LOG_ACTION_LABELS = {
    LOG_ACTION_CREATE: 'Criação',
    LOG_ACTION_UPDATE: 'Edição',
    LOG_ACTION_DELETE: 'Deleção',
}

LOG_FIELD_LABELS = {
    'name': 'Nome', 'email': 'E-mail', 'phone': 'Telefone', 'address': 'Endereço',
    'address_structured': 'Endereço', 'cpf': 'CPF', 'cnpj': 'CNPJ',
    'razao_social': 'Razão social', 'person_type': 'Tipo de pessoa', 'source': 'Origem',
    'sku': 'SKU', 'price': 'Preço/ct (US$)', 'carat_weight': 'Peso (ct)',
    'numero_pedras': 'Nº de pedras', 'category': 'Categoria', 'variant': 'Variante',
    'cut': 'Lapidação', 'color': 'Cor', 'clarity': 'Pureza', 'discount': 'Desconto (%)',
    'altura': 'Altura (mm)', 'largura': 'Largura (mm)', 'comprimento': 'Comprimento (mm)',
    'active': 'Ativo na loja', 'riviera': 'Riviera', 'description': 'Descrição',
    'specifications': 'Especificações', 'youtube_url': 'Vídeo YouTube',
    'main_image_url': 'Imagem principal', 'image_2_url': 'Imagem 2', 'image_3_url': 'Imagem 3',
    'status': 'Status', 'payment_status': 'Status de pagamento', 'amount_brl': 'Valor (R$)',
    'amount_usd': 'Valor (US$)', 'balance_brl': 'Saldo devedor (R$)',
    'paid_total_brl': 'Total pago (R$)', 'next_charge_date': 'Próxima cobrança',
    'tracking_code': 'Código de rastreio', 'seller_name': 'Vendedor',
    'commission_pct': 'Comissão (%)', 'seller_commission_pct': 'Comissão (%)',
    'items': 'Itens', 'payments': 'Pagamentos', 'value': 'Valor',
    'sold_out': 'Esgotado', 'stock_initial_carat': 'Peso inicial (ct)',
    'stock_initial_pedras': 'Nº inicial de pedras',
    'subtotal_usd': 'Subtotal (US$)', 'discount_usd': 'Desconto (US$)',
}


@app.template_filter('source_label')
def source_label_filter(source):
    return SOURCE_LABELS.get(source, SOURCE_LABELS[SOURCE_INTERNAL])


@app.template_filter('phone_br')
def phone_br_filter(phone):
    """Format a digits-only phone as (11) 99999-9999 / (11) 3333-4444."""
    digits = re.sub(r'\D', '', str(phone or ''))
    if digits.startswith('55') and len(digits) > 11:
        digits = digits[2:]
    if len(digits) == 11:
        return f'({digits[:2]}) {digits[2:7]}-{digits[7:]}'
    if len(digits) == 10:
        return f'({digits[:2]}) {digits[2:6]}-{digits[6:]}'
    return phone or ''


@app.template_filter('doc_br')
def doc_br_filter(doc):
    """Format a digits-only CPF/CNPJ."""
    digits = re.sub(r'\D', '', str(doc or ''))
    if len(digits) == 11:
        return f'{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}'
    if len(digits) == 14:
        return f'{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}'
    return doc or ''


@app.template_filter('payment_method_label')
def payment_method_label_filter(method):
    return INTERNAL_PAYMENT_METHOD_LABELS.get(method, (method or '—').title())


@app.template_filter('log_field_label')
def log_field_label_filter(field):
    return LOG_FIELD_LABELS.get(field, (field or '').replace('_', ' ').capitalize())


def _current_actor():
    """Who is performing the action, for the audit log."""
    return {
        'actor_email': session.get('user_email') or 'admin',
        'actor_uid': session.get('user_id') or '',
    }


def _log_action(entity_type, entity_id, entity_label, action, changes=None, note='', automatic=False):
    """
    Record an audit entry. `automatic=True` marks changes made by the system as a
    side effect of another action (e.g. stock write-off when an order is created),
    so the log makes clear the user didn't edit that record by hand.
    """
    actor = _current_actor()
    create_activity_log(
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=entity_label,
        action=action,
        changes=changes,
        note=note,
        source='automatic' if automatic else 'manual',
        **actor,
    )


def _resolve_order_source(order):
    """Orders created before the source flag existed came from the website checkout."""
    source = order.get('source')
    if source in (SOURCE_WEBSITE, SOURCE_INTERNAL):
        return source
    return SOURCE_WEBSITE if order.get('pagarme_order_id') else SOURCE_INTERNAL


def _resolve_customer_source(customer):
    """Legacy customers with a gateway/auth identity came from the website."""
    source = customer.get('source')
    if source in (SOURCE_WEBSITE, SOURCE_INTERNAL):
        return source
    if customer.get('pagarme_customer_id') or customer.get('firebase_uid'):
        return SOURCE_WEBSITE
    return SOURCE_INTERNAL


def _order_paid_total(order):
    """Total received for an order, in BRL."""
    if _resolve_order_source(order) == SOURCE_WEBSITE:
        # Website orders are settled in full by the gateway.
        if order.get('status') in PAID_ORDER_STATUSES:
            return round(float(order.get('amount_brl') or 0), 2)
        return 0.0
    total = 0.0
    for payment in order.get('payments') or []:
        try:
            total += float(payment.get('amount_brl') or 0)
        except (ValueError, TypeError):
            continue
    return round(total, 2)


def _order_balance(order):
    """
    Outstanding balance in BRL (never negative).
    Only internal orders carry a manual receivable: website orders are either
    settled by the gateway or never collected by our team (abandoned/failed).
    """
    if _resolve_order_source(order) == SOURCE_WEBSITE:
        return 0.0
    total = float(order.get('amount_brl') or 0)
    return round(max(0.0, total - _order_paid_total(order)), 2)


def _order_payment_status(order):
    total = float(order.get('amount_brl') or 0)
    paid = _order_paid_total(order)
    if total <= 0:
        return 'paid'
    if paid <= 0.009:
        return 'open'
    return 'paid' if paid >= total - 0.009 else 'partial'


def _decorate_order(order):
    """Attach source + financial fields used across every CRM view."""
    order['source'] = _resolve_order_source(order)
    order['source_label'] = SOURCE_LABELS[order['source']]
    order['paid_total_brl'] = _order_paid_total(order)
    order['balance_brl'] = _order_balance(order)
    order['payment_status'] = _order_payment_status(order)
    return order


def _decorate_orders(orders):
    return [_decorate_order(o) for o in orders]


def _parse_brl_amount(raw):
    """Accept '1.234,56', '1234.56' or '1234,56' from the payment inputs."""
    text = str(raw or '').strip()
    if not text:
        return None
    text = re.sub(r'[^\d,.\-]', '', text)
    if ',' in text and '.' in text:
        text = text.replace('.', '').replace(',', '.')
    elif ',' in text:
        text = text.replace(',', '.')
    try:
        value = float(text)
    except (ValueError, TypeError):
        return None
    return round(value, 2) if value > 0 else None


def _today_brt_iso():
    return datetime.now(_sao_paulo_tz).date().isoformat()


def _now_brt_iso():
    """
    Current instant as an ISO string in horário de Brasília.
    Used for timestamps that are stored as strings and displayed by slicing,
    so the value reads correctly regardless of the server's timezone.
    """
    return datetime.now(_sao_paulo_tz).isoformat(timespec='seconds')


def _parse_iso_date(raw):
    text = str(raw or '').strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _format_date_br(raw):
    parsed = _parse_iso_date(raw)
    return parsed.strftime('%d/%m/%Y') if parsed else ''


@app.template_filter('date_br')
def date_br_filter(raw):
    return _format_date_br(raw) or '—'


def _persist_order_financials(order, extra_fields=None):
    """Recompute paid/balance/status from the payments array and save it."""
    payload = {
        'payments': order.get('payments') or [],
        'paid_total_brl': _order_paid_total(order),
        'balance_brl': _order_balance(order),
        'payment_status': _order_payment_status(order),
    }
    if _resolve_order_source(order) == SOURCE_INTERNAL:
        payload['status'] = 'paid' if payload['payment_status'] == 'paid' else 'pending'
    if extra_fields:
        payload.update(extra_fields)
    # Pedido quitado nunca mantém data de cobrança pendente.
    if payload['payment_status'] == 'paid':
        payload['next_charge_date'] = ''
    update_order_fields(order['id'], payload)
    return payload


def _register_order_payment(order, amount_brl, method, paid_at, note='', next_charge_date=None):
    """
    Append a payment to an order and refresh its balance.
    Returns (payment_dict, resulting_fields).
    """
    payment = {
        'id': uuid.uuid4().hex[:12],
        'amount_brl': round(float(amount_brl), 2),
        'method': method,
        'paid_at': paid_at or _today_brt_iso(),
        'note': (note or '').strip(),
        'registered_by': session.get('user_email') or 'admin',
        'registered_at': _now_brt_iso(),
    }
    order['payments'] = list(order.get('payments') or []) + [payment]

    extra = {}
    if next_charge_date is not None:
        extra['next_charge_date'] = next_charge_date

    result = _persist_order_financials(order, extra)
    _log_action(
        'payment', order['id'], f"Pedido #{order['id'][:8].upper()}", LOG_ACTION_CREATE,
        changes=[
            {'field': 'amount_brl', 'before': '—', 'after': f"R$ {payment['amount_brl']:.2f}"},
            {'field': 'payment_method', 'before': '—', 'after': INTERNAL_PAYMENT_METHOD_LABELS.get(method, method)},
            {'field': 'balance_brl', 'before': f"R$ {_order_balance({**order, 'payments': order['payments'][:-1]}):.2f}",
             'after': f"R$ {result['balance_brl']:.2f}"},
        ],
        note=payment['note'],
    )
    return payment, result


def _product_calibre(product):
    """Human-readable calibre (dimensions in mm) built from the product fields."""
    parts = []
    for field in ('comprimento', 'largura', 'altura'):
        raw = str(product.get(field) or '').strip().replace(',', '.')
        if raw:
            parts.append(raw)
    return ' × '.join(parts) + ' mm' if parts else ''


def _product_pedra_label(product):
    """The gem itself: variant when available, otherwise the category."""
    return (product.get('variant') or product.get('category') or '').strip()


def _serialize_product_for_order(product):
    """Payload consumed by the 'novo pedido' form when a SKU is picked."""
    return {
        'id': product.get('id'),
        'sku': product.get('sku') or '',
        'name': product.get('name') or '',
        'pedra': _product_pedra_label(product),
        'formato': product.get('cut') or '',
        'calibre': _product_calibre(product),
        'ct': parse_carat_value(product.get('carat_weight')),
        'numero_pedras': int(product.get('numero_pedras') or 0),
        'price_per_carat_usd': float(product.get('price') or 0),
        'category': product.get('category') or '',
        'variant': product.get('variant') or '',
        'active': bool(product.get('active')),
        'sold_out': bool(product.get('sold_out')),
        'image': product.get('main_image_url') or '',
    }


def _serialize_customer_for_order(customer):
    """Payload consumed by the 'novo pedido' form when a customer is picked."""
    address = customer.get('address_structured') or {}
    return {
        'id': customer.get('id'),
        'name': customer.get('name') or '',
        'razao_social': customer.get('razao_social') or '',
        'person_type': customer.get('person_type') or ('cnpj' if customer.get('cnpj') else 'cpf'),
        'email': customer.get('email') or '',
        'phone': customer.get('phone') or '',
        'cpf': customer.get('cpf') or '',
        'cnpj': customer.get('cnpj') or '',
        'address': customer.get('address') or '',
        'cep': address.get('cep') or '',
        'street': address.get('street') or '',
        'number': address.get('number') or '',
        'complement': address.get('complement') or '',
        'neighborhood': address.get('neighborhood') or '',
        'city': address.get('city') or '',
        'state': address.get('state') or '',
        'source': _resolve_customer_source(customer),
    }


def _current_exchange_rate():
    setting = get_exchange_rate()
    if setting and 'value' in setting:
        try:
            return float((setting['value'] or {}).get('rate') or 0)
        except (ValueError, TypeError):
            return 0.0
    return 0.0


def _build_address_structured(form, prefix=''):
    """Read the structured address fields shared by the customer forms."""
    def field(name):
        return (form.get(f'{prefix}{name}', '') or '').strip()

    return {
        'cep': field('cep'),
        'street': field('street'),
        'number': field('number'),
        'complement': field('complement'),
        'neighborhood': field('neighborhood'),
        'city': field('city'),
        'state': field('state').upper(),
    }


def _format_address_line(address):
    """One-line address used in listings and legacy `address` field."""
    if not address:
        return ''
    street = ' '.join(p for p in [address.get('street', ''), address.get('number', '')] if p).strip()
    tail = ', '.join(p for p in [
        address.get('complement', ''),
        address.get('neighborhood', ''),
        address.get('city', ''),
    ] if p)
    line = ', '.join(p for p in [street, tail] if p)
    if address.get('state'):
        line = f"{line} - {address['state']}" if line else address['state']
    if address.get('cep'):
        line = f"{line} (CEP {address['cep']})" if line else f"CEP {address['cep']}"
    return line


def _validate_cnpj(cnpj):
    digits = re.sub(r'\D', '', cnpj or '')
    if len(digits) != 14 or digits == digits[0] * 14:
        return False
    for size in (12, 13):
        weights = list(range(size - 7, 1, -1)) + list(range(9, 1, -1))
        total = sum(int(digits[i]) * weights[i] for i in range(size))
        remainder = total % 11
        check = 0 if remainder < 2 else 11 - remainder
        if check != int(digits[size]):
            return False
    return True


@app.route('/crm')
@require_admin
def crm_dashboard():
    """Dashboard principal do CRM: estoque, vendas, recebimentos e próximas cobranças."""
    products  = get_all_products()
    customers = get_all_customers()
    orders    = _decorate_orders(get_all_orders())

    # ── Estoque ──
    active_products   = [p for p in products if p.get('active')]
    draft_products    = [p for p in products if not p.get('active')]
    sold_out_products = [p for p in products if p.get('sold_out')]
    stock_carats = sum(parse_carat_value(p.get('carat_weight')) for p in active_products)
    stock_stones = 0
    for p in active_products:
        try:
            stock_stones += int(p.get('numero_pedras') or 0)
        except (ValueError, TypeError):
            pass
    exchange_rate = _current_exchange_rate()
    markup_factor = 1 + get_price_increase_rate() / 100
    stock_value_brl = sum(
        _product_total_brl_calc(p, markup_factor, exchange_rate) for p in active_products
    ) if exchange_rate else 0.0

    # ── Clientes ──
    customers_internal = sum(1 for c in customers if _resolve_customer_source(c) == SOURCE_INTERNAL)
    customers_website  = len(customers) - customers_internal

    # ── Pedidos e faturamento ──
    orders_internal = [o for o in orders if o['source'] == SOURCE_INTERNAL]
    orders_website  = [o for o in orders if o['source'] == SOURCE_WEBSITE]
    # Pedidos internos entram no faturamento na criação; do site, só quando pagos.
    billable_orders = [
        o for o in orders
        if o['source'] == SOURCE_INTERNAL or o.get('status') in PAID_ORDER_STATUSES
    ]

    total_billed     = sum(float(o.get('amount_brl') or 0) for o in billable_orders)
    total_received   = sum(o['paid_total_brl'] for o in billable_orders)
    total_receivable = sum(o['balance_brl'] for o in billable_orders)
    open_balance_orders = [o for o in billable_orders if o['balance_brl'] > 0.009]

    now_brt   = datetime.now(_sao_paulo_tz)
    today     = now_brt.date()
    cutoff_30 = (now_brt - timedelta(days=30)).date()

    def _order_date(order):
        raw = str(order.get('created_at') or '')[:10]
        for fmt in ('%d/%m/%Y', '%Y-%m-%d'):
            try:
                return datetime.strptime(raw, fmt).date()
            except (ValueError, TypeError):
                continue
        return None

    revenue_30d = 0.0
    orders_30d  = 0
    for o in billable_orders:
        created = _order_date(o)
        if created and created >= cutoff_30:
            orders_30d  += 1
            revenue_30d += float(o.get('amount_brl') or 0)

    # ── Próximas cobranças ──
    upcoming_charges = []
    for o in open_balance_orders:
        due = _parse_iso_date(o.get('next_charge_date'))
        upcoming_charges.append({
            'order_id':      o['id'],
            'order_number':  o['id'][:8].upper(),
            'customer_name': o.get('customer_name') or '—',
            'customer_id':   o.get('firestore_customer_id') or '',
            'balance_brl':   o['balance_brl'],
            'amount_brl':    float(o.get('amount_brl') or 0),
            'paid_total_brl': o['paid_total_brl'],
            'next_charge_date': o.get('next_charge_date') or '',
            'due_date':      due,
            'days_until':    (due - today).days if due else None,
            'overdue':       bool(due and due < today),
            'unscheduled':   due is None,
            'seller_name':   o.get('seller_name') or '',
        })
    # Vencidas e mais próximas primeiro; sem data agendada no fim.
    upcoming_charges.sort(key=lambda c: (c['due_date'] is None, c['due_date'] or today))
    overdue_count = sum(1 for c in upcoming_charges if c['overdue'])
    due_7d_total = sum(
        c['balance_brl'] for c in upcoming_charges
        if c['days_until'] is not None and 0 <= c['days_until'] <= 7
    )
    unscheduled_count = sum(1 for c in upcoming_charges if c['unscheduled'])

    # ── Ranking de vendedores ──
    seller_stats = {}
    for o in billable_orders:
        seller_id = o.get('seller_id')
        if not seller_id:
            continue
        stats = seller_stats.setdefault(seller_id, {
            'name': o.get('seller_name') or '—',
            'orders': 0, 'billed': 0.0, 'received': 0.0, 'commission': 0.0,
        })
        stats['orders']     += 1
        stats['billed']     += float(o.get('amount_brl') or 0)
        stats['received']   += o['paid_total_brl']
        stats['commission'] += float(o.get('commission_amount_brl') or 0)
    seller_ranking = sorted(seller_stats.values(), key=lambda s: s['billed'], reverse=True)[:6]

    # ── Maiores saldos devedores por cliente ──
    debtor_map = {}
    for o in open_balance_orders:
        key = o.get('firestore_customer_id') or (o.get('customer_email') or '').lower() or o['id']
        entry = debtor_map.setdefault(key, {
            'customer_id': o.get('firestore_customer_id') or '',
            'name': o.get('customer_name') or '—',
            'balance_brl': 0.0,
            'orders': 0,
        })
        entry['balance_brl'] += o['balance_brl']
        entry['orders']      += 1
    top_debtors = sorted(debtor_map.values(), key=lambda d: d['balance_brl'], reverse=True)[:6]

    return render_template(
        'crm_dashboard.html',
        total_products=len(products),
        active_products=len(active_products),
        draft_products=len(draft_products),
        sold_out_products=len(sold_out_products),
        stock_carats=stock_carats,
        stock_stones=stock_stones,
        stock_value_brl=stock_value_brl,
        total_customers=len(customers),
        customers_internal=customers_internal,
        customers_website=customers_website,
        total_orders=len(orders),
        orders_internal=len(orders_internal),
        orders_website=len(orders_website),
        total_billed=total_billed,
        total_received=total_received,
        total_receivable=total_receivable,
        open_balance_count=len(open_balance_orders),
        revenue_30d=revenue_30d,
        orders_30d=orders_30d,
        avg_ticket=(total_billed / len(billable_orders)) if billable_orders else 0.0,
        upcoming_charges=upcoming_charges[:12],
        upcoming_total=len(upcoming_charges),
        overdue_count=overdue_count,
        due_7d_total=due_7d_total,
        unscheduled_count=unscheduled_count,
        seller_ranking=seller_ranking,
        total_sellers=len(get_all_sellers()),
        top_debtors=top_debtors,
        recent_orders=orders[:6],
        exchange_rate=exchange_rate,
    )

def _parse_carat_weight(product):
    cw = str(product.get('carat_weight') or '').strip()
    if not cw:
        return 0.0
    try:
        return float(cw.split()[0].replace(',', '.'))
    except (ValueError, TypeError):
        return 0.0


def _product_sort_price(product):
    price = float(product.get('price') or 0)
    carat = _parse_carat_weight(product) or 1.0
    return price * carat


def _sort_products(products, sort_by):
    if sort_by == 'price_asc':
        return sorted(products, key=_product_sort_price)
    if sort_by == 'price_desc':
        return sorted(products, key=_product_sort_price, reverse=True)
    if sort_by == 'peso_asc':
        return sorted(products, key=_parse_carat_weight)
    if sort_by == 'peso_desc':
        return sorted(products, key=_parse_carat_weight, reverse=True)
    if sort_by == 'recentes':
        return sorted(products, key=lambda p: str(p.get('created_at') or ''), reverse=True)
    return products


def _apply_product_filters(products, request_args, safe_float, markup_rate=0, exchange_rate=0):
    """Apply search and filters to product list (shared logic for /products and /crm/produtos)."""
    search_query = request_args.get('search', '').strip()
    category = request_args.get('category')
    min_altura = request_args.get('min_altura')
    max_altura = request_args.get('max_altura')
    min_comprimento = request_args.get('min_comprimento')
    max_comprimento = request_args.get('max_comprimento')
    min_largura = request_args.get('min_largura')
    max_largura = request_args.get('max_largura')
    min_peso = request_args.get('min_peso')
    max_peso = request_args.get('max_peso')
    cut = request_args.get('cut')
    numero_pedras = request_args.get('numero_pedras')
    numero_pedras_min = request_args.get('numero_pedras_min')
    riviera = request_args.get('riviera') == 'true'

    if search_query:
        search_lower = search_query.lower()
        filtered_products = []
        for p in products:
            searchable_text = ' '.join([
                str(p.get('name', '')),
                str(p.get('description', '')),
                str(p.get('sku', '')),
                str(p.get('category', '')),
                str(p.get('variant', '')),
                str(p.get('color', '')),
                str(p.get('cut', '')),
            ]).lower()
            if search_lower in searchable_text:
                filtered_products.append(p)
        products = filtered_products

    if category:
        products = [p for p in products if p.get('category') == category]
    if min_altura:
        min_altura_f = safe_float(min_altura)
        if min_altura_f is not None:
            products = [p for p in products if p.get('altura') and safe_float(p.get('altura')) is not None and safe_float(p.get('altura')) >= min_altura_f]
    if max_altura:
        max_altura_f = safe_float(max_altura)
        if max_altura_f is not None:
            products = [p for p in products if p.get('altura') and safe_float(p.get('altura')) is not None and safe_float(p.get('altura')) <= max_altura_f]
    if min_comprimento:
        min_comprimento_f = safe_float(min_comprimento)
        if min_comprimento_f is not None:
            products = [p for p in products if p.get('comprimento') and safe_float(p.get('comprimento')) is not None and safe_float(p.get('comprimento')) >= min_comprimento_f]
    if max_comprimento:
        max_comprimento_f = safe_float(max_comprimento)
        if max_comprimento_f is not None:
            products = [p for p in products if p.get('comprimento') and safe_float(p.get('comprimento')) is not None and safe_float(p.get('comprimento')) <= max_comprimento_f]
    if min_largura:
        min_largura_f = safe_float(min_largura)
        if min_largura_f is not None:
            products = [p for p in products if p.get('largura') and safe_float(p.get('largura')) is not None and safe_float(p.get('largura')) >= min_largura_f]
    if max_largura:
        max_largura_f = safe_float(max_largura)
        if max_largura_f is not None:
            products = [p for p in products if p.get('largura') and safe_float(p.get('largura')) is not None and safe_float(p.get('largura')) <= max_largura_f]
    if min_peso:
        min_peso_f = safe_float(min_peso)
        if min_peso_f is not None:
            products = [p for p in products if p.get('carat_weight')]
            filtered = []
            for p in products:
                carat_str = p.get('carat_weight', '0').split()[0] if ' ' in str(p.get('carat_weight', '0')) else p.get('carat_weight', '0')
                carat_value = safe_float(carat_str)
                if carat_value is not None and carat_value >= min_peso_f:
                    filtered.append(p)
            products = filtered
    if max_peso:
        max_peso_f = safe_float(max_peso)
        if max_peso_f is not None:
            products = [p for p in products if p.get('carat_weight')]
            filtered = []
            for p in products:
                carat_str = p.get('carat_weight', '0').split()[0] if ' ' in str(p.get('carat_weight', '0')) else p.get('carat_weight', '0')
                carat_value = safe_float(carat_str)
                if carat_value is not None and carat_value <= max_peso_f:
                    filtered.append(p)
            products = filtered
    if cut:
        products = [p for p in products if p.get('cut') == cut]
    if numero_pedras:
        _keyword_map = {'unico': ('==', 1), 'par': ('==', 2), 'trio': ('==', 3), 'conjunto': ('>', 3)}
        if numero_pedras == 'sem_pedras':
            products = [p for p in products if not p.get('numero_pedras')]
        elif numero_pedras in _keyword_map:
            _op, _val = _keyword_map[numero_pedras]
            if _op == '==':
                products = [p for p in products if p.get('numero_pedras') == _val]
            else:
                products = [p for p in products if p.get('numero_pedras') and int(p.get('numero_pedras')) > _val]
        else:
            try:
                numero_pedras_int = int(numero_pedras)
                products = [p for p in products if p.get('numero_pedras') == numero_pedras_int]
            except (ValueError, TypeError):
                pass
    if numero_pedras_min:
        try:
            min_val = int(numero_pedras_min)
            products = [p for p in products if p.get('numero_pedras') and int(p.get('numero_pedras')) >= min_val]
        except (ValueError, TypeError):
            pass
    if riviera:
        products = [p for p in products if p.get('riviera') == True]

    min_preco_ct = request_args.get('min_preco_ct')
    max_preco_ct = request_args.get('max_preco_ct')
    min_preco_total = request_args.get('min_preco_total')
    max_preco_total = request_args.get('max_preco_total')

    if exchange_rate and (min_preco_ct or max_preco_ct):
        _mf = 1 + markup_rate / 100 if markup_rate else 1.0
        _min_pct = safe_float(min_preco_ct)
        _max_pct = safe_float(max_preco_ct)
        _filtered = []
        for p in products:
            val = _product_price_ct_brl(p, _mf, exchange_rate)
            if _min_pct is not None and val < _min_pct:
                continue
            if _max_pct is not None and _max_pct < 999_999_000 and val >= _max_pct:
                continue
            _filtered.append(p)
        products = _filtered

    if exchange_rate and (min_preco_total or max_preco_total):
        _mf = 1 + markup_rate / 100 if markup_rate else 1.0
        _min_pt = safe_float(min_preco_total)
        _max_pt = safe_float(max_preco_total)
        _filtered = []
        for p in products:
            val = _product_total_brl_calc(p, _mf, exchange_rate)
            if _min_pt is not None and val < _min_pt:
                continue
            if _max_pt is not None and _max_pt < 999_999_000 and val >= _max_pt:
                continue
            _filtered.append(p)
        products = _filtered

    return products


@app.route('/crm/produtos')
@require_admin
def crm_produtos():
    """Página de gerenciamento de produtos com filtros e busca."""
    def safe_float(value):
        if not value:
            return None
        try:
            if isinstance(value, str):
                value = value.replace(',', '.')
            return float(value)
        except (ValueError, TypeError):
            return None

    all_products = get_all_products()
    bounds = _get_dimension_bounds(all_products)
    products = list(all_products)
    search_query = request.args.get('search', '').strip()
    category = request.args.get('category')
    min_altura = request.args.get('min_altura')
    max_altura = request.args.get('max_altura')
    min_comprimento = request.args.get('min_comprimento')
    max_comprimento = request.args.get('max_comprimento')
    min_largura = request.args.get('min_largura')
    max_largura = request.args.get('max_largura')
    min_peso = request.args.get('min_peso')
    max_peso = request.args.get('max_peso')
    cut = request.args.get('cut')
    numero_pedras = request.args.get('numero_pedras')
    riviera = request.args.get('riviera') == 'true'

    _pedras_order = ['sem_pedras', 'unico', 'par', 'trio', 'conjunto']
    _pedras_key = lambda n: 'unico' if n == 1 else ('par' if n == 2 else ('trio' if n == 3 else ('conjunto' if n > 3 else None)))

    category_options = {}
    _products_by_cat = {}
    for _p in all_products:
        _cat = _p.get('category', '')
        if not _cat:
            continue
        _products_by_cat.setdefault(_cat, []).append(_p)
    for _cat, _prods in _products_by_cat.items():
        category_options[_cat] = _build_category_filter_options(_prods, _pedras_order, _pedras_key)
    category_options['__all__'] = _build_category_filter_options(all_products, _pedras_order, _pedras_key)

    products = _apply_product_filters(products, request.args, safe_float)

    # Status filter (active / draft) — sold products are inactive, so skip when filtering vendidos
    status = request.args.get('status', 'active') if 'status' not in request.args else request.args.get('status', '')
    sold_filter = request.args.get('sold', '')
    if sold_filter != 'yes':
        if status == 'active':
            products = [p for p in products if p.get('active', False)]
        elif status == 'draft':
            products = [p for p in products if not p.get('active', False)]

    if sold_filter == 'yes':
        products = [p for p in products if p.get('sales_history')]
    elif sold_filter == 'no':
        products = [p for p in products if not p.get('sales_history')]

    # Origem: filtra pelo canal em que o produto foi vendido (site x balcão/CRM)
    source_filter = request.args.get('source', '')
    if source_filter in (SOURCE_WEBSITE, SOURCE_INTERNAL):
        products = [
            p for p in products
            if any(
                (sale.get('source') or SOURCE_WEBSITE) == source_filter
                for sale in (p.get('sales_history') or [])
            )
        ]

    sort_by = request.args.get('sort_by', '')
    products = _sort_products(products, sort_by)

    total_count = len(products)

    # Paginação: limite de itens por página
    per_page = 20
    page = request.args.get('page', 1, type=int)
    page = max(1, page)
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    products_page = products[start:start + per_page]

    # URLs para próxima/anterior preservando filtros
    def _pagination_url(p):
        args = request.args.to_dict()
        args['page'] = p
        return url_for('crm_produtos', **args)
    pagination = {
        'page': page,
        'per_page': per_page,
        'total_count': total_count,
        'total_pages': total_pages,
        'has_next': page < total_pages,
        'has_prev': page > 1,
        'next_url': _pagination_url(page + 1) if page < total_pages else None,
        'prev_url': _pagination_url(page - 1) if page > 1 else None,
        'start_index': start + 1 if total_count else 0,
        'end_index': min(start + per_page, total_count),
    }

    categories = sorted(set([p.get('category') for p in get_all_products() if p.get('category')]))
    cut_types = get_cut_types()
    filters = {
        'search': search_query,
        'category': category,
        'min_altura': min_altura,
        'max_altura': max_altura,
        'min_comprimento': min_comprimento,
        'max_comprimento': max_comprimento,
        'min_largura': min_largura,
        'max_largura': max_largura,
        'min_peso': min_peso,
        'max_peso': max_peso,
        'cut': cut,
        'numero_pedras': numero_pedras,
        'riviera': riviera,
        'status': status,
        'sold': sold_filter,
        'source': source_filter,
        'sort_by': sort_by,
    }
    return render_template('crm_produtos.html', products=products_page, categories=categories, gem_types=categories, cuts=cut_types, filters=filters, pagination=pagination, bounds=bounds, category_options=category_options, source_options=SOURCE_LABELS)

@app.route('/crm/clientes')
@require_admin
def crm_clientes():
    """Página de gerenciamento de clientes."""
    customers = get_all_customers()
    for customer in customers:
        customer['source'] = _resolve_customer_source(customer)
        customer['source_label'] = SOURCE_LABELS[customer['source']]
    customers.sort(key=lambda c: (c.get('name') or '').lower())
    q = request.args.get('q', '').strip()
    return render_template(
        'crm_clientes.html',
        customers=customers,
        q=q,
        brazilian_states=BRAZILIAN_STATES,
    )

@app.route('/crm/pedidos')
@require_admin
def crm_pedidos():
    """Página de gerenciamento de pedidos com filtros de origem, status e cobrança."""
    orders = _decorate_orders(get_all_orders())

    source_filter  = request.args.get('source', '')
    status_filter  = request.args.get('status', '')
    payment_filter = request.args.get('payment_status', '')
    search_query   = request.args.get('search', '').strip()

    filtered = orders
    if source_filter in (SOURCE_WEBSITE, SOURCE_INTERNAL):
        filtered = [o for o in filtered if o['source'] == source_filter]
    if status_filter:
        filtered = [o for o in filtered if o.get('status') == status_filter]
    if payment_filter in ('open', 'partial', 'paid'):
        filtered = [o for o in filtered if o['payment_status'] == payment_filter]
    if search_query:
        needle = search_query.lower()
        filtered = [
            o for o in filtered
            if needle in ' '.join([
                o['id'].lower(),
                str(o.get('customer_name') or '').lower(),
                str(o.get('customer_email') or '').lower(),
                str(o.get('seller_name') or '').lower(),
                ' '.join(str(i.get('sku') or '') for i in (o.get('items') or [])).lower(),
            ])
        ]

    filters = {
        'source': source_filter,
        'status': status_filter,
        'payment_status': payment_filter,
        'search': search_query,
    }
    return render_template(
        'crm_pedidos.html',
        orders=filtered,
        all_orders_count=len(orders),
        filters=filters,
        source_options=SOURCE_LABELS,
    )

@app.route('/crm/solicitacoes')
@require_admin
def crm_solicitacoes():
    """Página de solicitações recebidas pelo formulário 'O que procura?'."""
    solicitacoes = get_all_solicitacoes()
    return render_template('crm_solicitacoes.html', solicitacoes=solicitacoes)


@app.route('/crm/api/solicitacoes/<solicitacao_id>/reply', methods=['POST'])
@require_admin
def crm_solicitacao_reply(solicitacao_id):
    """Post an admin reply (text + optional images) to a solicitação."""
    solicitacao = get_solicitacao_by_id(solicitacao_id)
    if not solicitacao:
        return jsonify({'error': 'Solicitação não encontrada.'}), 404

    message = (request.form.get('message') or '').strip()
    image_urls = []
    files = request.files.getlist('images') or request.files.getlist('images[]') or []

    for f in files[:5]:
        if not f or not f.filename:
            continue
        url = upload_image(f, f.filename, folder='solicitacao-replies')
        if url:
            image_urls.append(url)

    if not message and not image_urls:
        return jsonify({'error': 'Informe uma mensagem ou envie ao menos uma foto.'}), 400

    admin_name = session.get('user_email', 'Katsumi Joias')
    updated = add_solicitacao_reply(
        solicitacao_id,
        message,
        image_urls=image_urls,
        admin_name=admin_name,
    )
    if not updated:
        return jsonify({'error': 'Não foi possível salvar a resposta.'}), 500

    latest_reply = updated.get('replies', [])[-1] if updated.get('replies') else {}
    email_service.send_solicitacao_reply_email(
        solicitacao=updated,
        reply_message=latest_reply.get('message', message),
        reply_images=latest_reply.get('images', image_urls),
    )

    return jsonify({'success': True, 'replies_count': len(updated.get('replies', []))}), 200


@app.route('/crm/ajustes')
@require_admin
def crm_ajustes():
    """Página de ajustes do sistema."""
    settings = get_all_settings()
    return render_template('crm_ajustes.html', settings=settings)

@app.route('/crm/ajustes/<setting_key>/edit', methods=['GET', 'POST'])
@require_admin
def edit_setting(setting_key):
    """Editar uma configuração."""
    setting = get_setting(setting_key)
    if not setting:
        flash('Configuração não encontrada', 'error')
        return redirect(url_for('crm_ajustes'))
    
    if request.method == 'POST':
        try:
            setting_type = setting.get('type', 'string')
            
            if setting_type == 'array':
                # Get array values from form (one per line)
                value_text = request.form.get('value', '')
                value = [line.strip() for line in value_text.split('\n') if line.strip()]
            else:
                value = request.form.get('value', '')
            
            updated_data = {
                'name': setting['name'],
                'description': setting['description'],
                'type': setting_type,
                'value': value
            }
            
            if update_setting(setting_key, updated_data):
                flash('Configuração atualizada com sucesso!', 'success')
                return redirect(url_for('crm_ajustes'))
            else:
                flash('Erro ao atualizar configuração', 'error')
                
        except Exception as e:
            print(f"Error in edit_setting: {e}")
            flash('Erro ao atualizar configuração', 'error')
    
    setting['key'] = setting_key
    return render_template('setting_form.html', setting=setting)

@app.route('/crm/categories')
@require_admin
def crm_categories():
    """Página para gerenciar categorias e variantes de produtos."""
    categories = get_product_categories()
    return render_template('crm_categories.html', categories=categories)

@app.route('/crm/categories/add', methods=['POST'])
@require_admin
def crm_add_category():
    """Adicionar nova categoria."""
    try:
        category_name = request.form.get('category_name', '').strip()
        category_description = request.form.get('category_description', '').strip()
        
        if not category_name:
            flash('Nome da categoria é obrigatório', 'error')
            return redirect(url_for('crm_categories'))
        
        categories = get_product_categories()
        
        if category_name in categories:
            flash('Esta categoria já existe', 'error')
            return redirect(url_for('crm_categories'))
        
        categories[category_name] = {
            'description': category_description,
            'variants': []
        }
        save_product_categories(categories)
        
        flash(f'Categoria "{category_name}" adicionada com sucesso!', 'success')
        return redirect(url_for('crm_categories'))
    except Exception as e:
        print(f"Error adding category: {e}")
        flash('Erro ao adicionar categoria', 'error')
        return redirect(url_for('crm_categories'))

@app.route('/crm/categories/update', methods=['POST'])
@require_admin
def crm_update_category():
    """Atualizar descrição de categoria."""
    try:
        category_name = request.form.get('category_name', '').strip()
        category_description = request.form.get('category_description', '').strip()
        
        categories = get_product_categories()
        
        if category_name not in categories:
            flash('Categoria não encontrada', 'error')
            return redirect(url_for('crm_categories'))
        
        categories[category_name]['description'] = category_description
        save_product_categories(categories)
        
        flash(f'Descrição da categoria "{category_name}" atualizada com sucesso!', 'success')
        return redirect(url_for('crm_categories'))
    except Exception as e:
        print(f"Error updating category: {e}")
        flash('Erro ao atualizar categoria', 'error')
        return redirect(url_for('crm_categories'))

@app.route('/crm/categories/delete', methods=['POST'])
@require_admin
def crm_delete_category():
    """Excluir categoria."""
    try:
        category_name = request.form.get('category_name', '').strip()
        categories = get_product_categories()
        
        if category_name not in categories:
            flash('Categoria não encontrada', 'error')
            return redirect(url_for('crm_categories'))
        
        del categories[category_name]
        save_product_categories(categories)
        
        flash(f'Categoria "{category_name}" excluída com sucesso!', 'success')
        return redirect(url_for('crm_categories'))
    except Exception as e:
        print(f"Error deleting category: {e}")
        flash('Erro ao excluir categoria', 'error')
        return redirect(url_for('crm_categories'))

@app.route('/crm/categories/variant/add', methods=['POST'])
@require_admin
def crm_add_variant():
    """Adicionar variante a uma categoria."""
    try:
        category_name = request.form.get('category_name', '').strip()
        variant_name = request.form.get('variant_name', '').strip()
        variant_description = request.form.get('variant_description', '').strip()
        
        if not variant_name:
            flash('Nome da variante é obrigatório', 'error')
            return redirect(url_for('crm_categories'))
        
        categories = get_product_categories()
        
        if category_name not in categories:
            flash('Categoria não encontrada', 'error')
            return redirect(url_for('crm_categories'))
        
        # Check if variant already exists
        variant_names = [v['name'] if isinstance(v, dict) else v for v in categories[category_name]['variants']]
        if variant_name in variant_names:
            flash('Esta variante já existe nesta categoria', 'error')
            return redirect(url_for('crm_categories'))
        
        categories[category_name]['variants'].append({
            'name': variant_name,
            'description': variant_description
        })
        save_product_categories(categories)
        
        flash(f'Variante "{variant_name}" adicionada à categoria "{category_name}"!', 'success')
        return redirect(url_for('crm_categories'))
    except Exception as e:
        print(f"Error adding variant: {e}")
        flash('Erro ao adicionar variante', 'error')
        return redirect(url_for('crm_categories'))

@app.route('/crm/categories/variant/update', methods=['POST'])
@require_admin
def crm_update_variant():
    """Atualizar descrição de variante."""
    try:
        category_name = request.form.get('category_name', '').strip()
        variant_name = request.form.get('variant_name', '').strip()
        variant_description = request.form.get('variant_description', '').strip()
        
        categories = get_product_categories()
        
        if category_name not in categories:
            flash('Categoria não encontrada', 'error')
            return redirect(url_for('crm_categories'))
        
        # Find and update the variant
        for i, variant in enumerate(categories[category_name]['variants']):
            variant_dict = variant if isinstance(variant, dict) else {'name': variant, 'description': ''}
            if variant_dict['name'] == variant_name:
                categories[category_name]['variants'][i] = {
                    'name': variant_name,
                    'description': variant_description
                }
                save_product_categories(categories)
                flash(f'Descrição da variante "{variant_name}" atualizada com sucesso!', 'success')
                return redirect(url_for('crm_categories'))
        
        flash('Variante não encontrada', 'error')
        return redirect(url_for('crm_categories'))
    except Exception as e:
        print(f"Error updating variant: {e}")
        flash('Erro ao atualizar variante', 'error')
        return redirect(url_for('crm_categories'))

@app.route('/crm/categories/variant/delete', methods=['POST'])
@require_admin
def crm_delete_variant():
    """Excluir variante de uma categoria."""
    try:
        category_name = request.form.get('category_name', '').strip()
        variant_name = request.form.get('variant_name', '').strip()
        
        categories = get_product_categories()
        
        if category_name not in categories:
            flash('Categoria não encontrada', 'error')
            return redirect(url_for('crm_categories'))
        
        # Find and remove the variant
        for i, variant in enumerate(categories[category_name]['variants']):
            variant_dict = variant if isinstance(variant, dict) else {'name': variant}
            if variant_dict.get('name') == variant_name or variant == variant_name:
                categories[category_name]['variants'].pop(i)
                save_product_categories(categories)
                flash(f'Variante "{variant_name}" excluída!', 'success')
                return redirect(url_for('crm_categories'))
        
        flash('Variante não encontrada', 'error')
        return redirect(url_for('crm_categories'))
    except Exception as e:
        print(f"Error deleting variant: {e}")
        flash('Erro ao excluir variante', 'error')
        return redirect(url_for('crm_categories'))

@app.route('/crm/exchange-rate')
@require_admin
def crm_exchange_rate():
    """Página do CRM para visualizar e atualizar cotação do dólar."""
    rate_setting = get_exchange_rate()
    rate_data = rate_setting['value'] if rate_setting and 'value' in rate_setting else None
    
    # Get exchange rate increase setting
    increase_setting = get_setting('exchange_rate_increase')
    rate_increase = float(increase_setting['value']) if increase_setting and 'value' in increase_setting else 0.0
    
    return render_template('crm_exchange_rate.html', rate_data=rate_data, rate_increase=rate_increase)

@app.route('/crm/exchange-rate/update', methods=['POST'])
@require_admin
def crm_update_exchange_rate():
    """Atualizar cotação do dólar do CRM."""
    try:
        rate_data = refresh_exchange_rate_from_api(source='crm', force=True)
        market_rate = rate_data['market_rate']
        final_rate = rate_data['rate']
        flash(f'Cotação atualizada! Taxa de mercado: R$ {market_rate:.4f} | Taxa final: R$ {final_rate:.4f}', 'success')
        return redirect(url_for('crm_exchange_rate'))
    except requests.exceptions.ConnectionError:
        flash('Erro de conexão: Não foi possível conectar à API. Verifique sua conexão com a internet.', 'error')
        return redirect(url_for('crm_exchange_rate'))
    except requests.exceptions.Timeout:
        flash('Erro: A requisição demorou muito tempo. Tente novamente.', 'error')
        return redirect(url_for('crm_exchange_rate'))
    except requests.exceptions.HTTPError as e:
        flash(f'Erro HTTP: {e}', 'error')
        return redirect(url_for('crm_exchange_rate'))
    except KeyError as e:
        flash(f'Erro ao processar resposta da API: Campo {e} não encontrado', 'error')
        return redirect(url_for('crm_exchange_rate'))
    except Exception as e:
        flash(f'Erro inesperado: {str(e)}', 'error')
        print(f"Error updating exchange rate: {e}")
        return redirect(url_for('crm_exchange_rate'))

@app.route('/crm/exchange-rate/increase', methods=['POST'])
@require_admin
def crm_update_exchange_rate_increase():
    """Atualizar incremento da cotação do dólar."""
    try:
        increase = float(request.form.get('increase', 0))
        
        # Validate increase is not negative
        if increase < 0:
            flash('O incremento não pode ser negativo', 'error')
            return redirect(url_for('crm_exchange_rate'))
        
        # Save increase setting
        setting_data = {
            'name': 'Incremento da Cotação',
            'description': 'Valor adicional aplicado à cotação do dólar',
            'type': 'number',
            'value': increase
        }
        update_setting('exchange_rate_increase', setting_data)
        
        flash(f'Incremento de R$ {increase:.2f} salvo com sucesso! Clique em "Atualizar Cotação" para aplicar.', 'success')
        return redirect(url_for('crm_exchange_rate'))
    except Exception as e:
        print(f"Error updating exchange rate increase: {e}")
        flash('Erro ao salvar incremento', 'error')
        return redirect(url_for('crm_exchange_rate'))

@app.route('/crm/price-increase-rate', methods=['GET', 'POST'])
@require_admin
def crm_price_increase_rate():
    """Página para gerenciar taxa de aumento dos preços."""
    if request.method == 'POST':
        try:
            rate = int(request.form.get('rate', 0))
            if rate < 0:
                flash('A taxa não pode ser negativa.', 'error')
                return redirect(url_for('crm_price_increase_rate'))
            setting_data = {
                'name': 'Taxa de Aumento',
                'description': 'Porcentagem para aumentar os preços base dos produtos',
                'type': 'number',
                'value': rate
            }
            update_setting('price_increase_rate', setting_data)
            flash(f'Taxa de aumento atualizada para {rate}%!', 'success')
        except (ValueError, TypeError):
            flash('Valor inválido. Digite um número inteiro.', 'error')
        return redirect(url_for('crm_price_increase_rate'))

    rate = get_price_increase_rate()
    return render_template('crm_price_increase_rate.html', rate=rate)

@app.route('/crm/product/new', methods=['GET', 'POST'])
@require_admin
def new_product():
    if request.method == 'POST':
        try:
            # Get basic product data
            product_data = {
                'name': request.form['name'],
                'category': request.form['category'],
                'description': request.form.get('description', ''),
                'price': float(request.form['price']),
            }
            
            # Get variant if provided
            variant = request.form.get('variant', '').strip()
            if variant:
                product_data['variant'] = variant
            
            # SKU: gerado automaticamente para produto novo, digitado para produto já existente
            sku_mode = 'existing' if request.form.get('sku_mode') == 'existing' else 'new'
            sku = request.form.get('sku', '').strip()
            if sku_mode == 'new' and not sku:
                sku = get_next_numeric_sku()
            if not sku:
                flash('Informe o SKU do produto.', 'error')
                return redirect(url_for('new_product', sku_mode=sku_mode))
            if find_products_by_sku(sku):
                flash(f'O SKU {sku} já existe no sistema. Escolha outro código.', 'error')
                return redirect(url_for('new_product', sku_mode=sku_mode))
            product_data['sku'] = sku

            # Get discount if provided
            discount = request.form.get('discount', '').strip()
            if discount:
                try:
                    product_data['discount'] = float(discount)
                except (ValueError, TypeError):
                    pass
            
            # Get gemstone specific fields
            color = request.form.get('color', '').strip()
            carat_weight = request.form.get('carat_weight', '').strip()
            clarity = request.form.get('clarity', '').strip()
            cut = request.form.get('cut', '').strip()
            altura = request.form.get('altura', '').strip()
            comprimento = request.form.get('comprimento', '').strip()
            largura = request.form.get('largura', '').strip()
            numero_pedras = request.form.get('numero_pedras', '').strip()
            riviera = request.form.get('riviera') == 'on'
            
            if color:
                product_data['color'] = color
            if carat_weight:
                product_data['carat_weight'] = carat_weight
            if clarity:
                product_data['clarity'] = clarity
            if cut:
                product_data['cut'] = cut
            if altura:
                product_data['altura'] = altura
            if comprimento:
                product_data['comprimento'] = comprimento
            if largura:
                product_data['largura'] = largura
            if numero_pedras:
                try:
                    product_data['numero_pedras'] = int(numero_pedras)
                except (ValueError, TypeError):
                    pass
            product_data['riviera'] = riviera
            
            # Handle additional specifications
            specifications = {}
            spec_keys = request.form.getlist('spec_key[]')
            spec_values = request.form.getlist('spec_value[]')
            for key, value in zip(spec_keys, spec_values):
                if key and value:  # Only add if both key and value are provided
                    specifications[key] = value
            product_data['specifications'] = specifications
            
            # Handle main image
            main_image = request.files.get('main_image')
            if main_image and main_image.filename:
                main_image_url = upload_image(main_image, main_image.filename)
                if main_image_url:
                    product_data['main_image_url'] = main_image_url
            
            # Handle additional images
            additional_images = request.files.getlist('additional_images')
            for idx, image in enumerate(additional_images[:2], start=2):  # Process up to 2 additional images
                if image and image.filename:
                    image_url = upload_image(image, image.filename)
                    if image_url:
                        if idx == 2:
                            product_data['image_2_url'] = image_url
                        else:
                            product_data['image_3_url'] = image_url
            
            # YouTube URL
            youtube_url = request.form.get('youtube_url', '').strip()
            product_data['youtube_url'] = youtube_url if youtube_url else ''

            # Active na loja: só pode ser True se tiver imagem principal
            has_main_image = bool(product_data.get('main_image_url'))
            if not has_main_image:
                product_data['active'] = False
            else:
                product_data['active'] = request.form.get('active') == 'on'
            
            created = create_product(product_data)
            if created:
                _log_action(
                    'product', created.get('id'), f"{product_data['name']} ({sku})",
                    LOG_ACTION_CREATE,
                    changes=diff_for_log({}, product_data, ignore_fields=('specifications',)),
                )
                flash(f'Produto criado com sucesso! SKU {sku}', 'success')
                return redirect(url_for('crm_produtos'))
            else:
                flash('Erro ao criar produto', 'error')
                
        except Exception as e:
            print(f"Error in new_product: {e}")
            flash('Erro ao criar produto', 'error')
        
        return redirect(url_for('new_product'))
    
    categories = get_product_categories()
    default_specs = get_default_specifications()
    cut_types = get_cut_types()
    sku_mode = 'existing' if request.args.get('sku_mode') == 'existing' else 'new'
    return render_template('product_form.html',
                         product=None,
                         categories=categories,
                         default_specs=default_specs,
                         cut_types=cut_types,
                         sku_mode=sku_mode,
                         suggested_sku=get_next_numeric_sku() if sku_mode == 'new' else '')

@app.route('/crm/product/<product_id>/edit', methods=['GET', 'POST'])
@require_admin
def edit_product(product_id):
    product = get_product_by_id(product_id)
    if not product:
        flash('Produto não encontrado', 'error')
        return redirect(url_for('crm_dashboard'))
    
    if request.method == 'POST':
        try:
            # Get form data
            name = request.form.get('name')
            description = request.form.get('description')
            category = request.form.get('category')
            price = float(request.form.get('price', 0))
            
            # Get gemstone specific fields
            color = request.form.get('color', '').strip()
            carat_weight = request.form.get('carat_weight', '').strip()
            clarity = request.form.get('clarity', '').strip()
            cut = request.form.get('cut', '').strip()
            altura = request.form.get('altura', '').strip()
            comprimento = request.form.get('comprimento', '').strip()
            largura = request.form.get('largura', '').strip()
            numero_pedras = request.form.get('numero_pedras', '').strip()
            riviera = request.form.get('riviera') == 'on'
            
            # Handle additional specifications
            specifications = {}
            spec_keys = request.form.getlist('spec_key[]')
            spec_values = request.form.getlist('spec_value[]')
            for key, value in zip(spec_keys, spec_values):
                if key and value:  # Only add if both key and value are provided
                    specifications[key] = value
            
            # Handle main image
            main_image = request.files.get('main_image')
            if main_image and main_image.filename:
                # Delete old main image if it exists
                if product.get('main_image_url'):
                    old_image_name = product['main_image_url'].split('/')[-1]
                    delete_image(old_image_name)
                # Upload new main image
                main_image_url = upload_image(main_image, main_image.filename)
            else:
                main_image_url = product.get('main_image_url')
            
            # Handle additional images
            image_2_url = product.get('image_2_url')
            image_3_url = product.get('image_3_url')
            
            # Process new image uploads for positions 2 and 3
            new_images = request.files.getlist('additional_images')
            for idx, image in enumerate(new_images[:2], start=2):  # Only process up to 2 additional images
                if image and image.filename:
                    # Delete old image if it exists
                    old_url = product.get(f'image_{idx}_url')
                    if old_url:
                        old_image_name = old_url.split('/')[-1]
                        delete_image(old_image_name)
                    # Upload new image
                    new_url = upload_image(image, image.filename)
                    if idx == 2:
                        image_2_url = new_url
                    else:
                        image_3_url = new_url
            
            # Update product
            updated_product = {
                'name': name,
                'description': description,
                'category': category,
                'price': price,
                'specifications': specifications,
                'main_image_url': main_image_url,
                'image_2_url': image_2_url,
                'image_3_url': image_3_url
            }
            
            # Add variant if provided
            variant = request.form.get('variant', '').strip()
            if variant:
                updated_product['variant'] = variant
            
            # Add SKU if provided
            sku = request.form.get('sku', '').strip()
            if sku:
                if find_products_by_sku(sku, exclude_product_id=product_id):
                    flash(f'O SKU {sku} já pertence a outro produto.', 'error')
                    return redirect(url_for('edit_product', product_id=product_id))
                updated_product['sku'] = sku
            
            # Add discount if provided
            discount = request.form.get('discount', '').strip()
            if discount:
                try:
                    updated_product['discount'] = float(discount)
                except (ValueError, TypeError):
                    pass
            
            # Add gemstone fields if provided
            if color:
                updated_product['color'] = color
            if carat_weight:
                updated_product['carat_weight'] = carat_weight
            if clarity:
                updated_product['clarity'] = clarity
            if cut:
                updated_product['cut'] = cut
            if altura:
                updated_product['altura'] = altura
            if comprimento:
                updated_product['comprimento'] = comprimento
            if largura:
                updated_product['largura'] = largura
            if numero_pedras:
                try:
                    updated_product['numero_pedras'] = int(numero_pedras)
                except (ValueError, TypeError):
                    pass
            updated_product['riviera'] = riviera

            # YouTube URL
            youtube_url = request.form.get('youtube_url', '').strip()
            updated_product['youtube_url'] = youtube_url if youtube_url else ''
            
            # Active na loja: só pode ser True se tiver imagem principal
            has_main_image = bool(main_image_url)
            if not has_main_image:
                updated_product['active'] = False
            else:
                updated_product['active'] = request.form.get('active') == 'on'
            
            if update_product(product_id, updated_product):
                _log_action(
                    'product', product_id,
                    f"{updated_product.get('name') or product.get('name')} ({updated_product.get('sku') or product.get('sku') or '—'})",
                    LOG_ACTION_UPDATE,
                    changes=diff_for_log(product, updated_product),
                )
                flash('Produto atualizado com sucesso!', 'success')
                return redirect(url_for('crm_produtos'))
            else:
                flash('Erro ao atualizar produto', 'error')
                
        except Exception as e:
            print(f"Error in edit_product: {e}")
            flash('Erro ao atualizar produto', 'error')
    
    categories = get_product_categories()
    cut_types = get_cut_types()
    sales_history = sorted(
        product.get('sales_history') or [],
        key=lambda s: s.get('sold_at', ''),
        reverse=True,
    )
    related_orders = {}
    for sale in sales_history:
        oid = sale.get('order_id')
        if oid and oid not in related_orders:
            order = get_order_by_id(oid)
            if order:
                related_orders[oid] = order
    return render_template('product_form.html', 
                         product=product, 
                         edit_mode=True,
                         categories=categories,
                         cut_types=cut_types,
                         sales_history=sales_history,
                         related_orders=related_orders)

@app.route('/crm/product/<product_id>/delete', methods=['POST'])
@require_admin
def delete_product_route(product_id):
    product = get_product_by_id(product_id)
    if product is None:
        return "Product not found", 404
    
    # Delete main image if it exists
    if product.get('image_url'):
        image_name = product['image_url'].split('/')[-1]
        delete_image(image_name)
    
    # Delete gallery images
    for gallery_url in product.get('gallery_urls', []):
        image_name = gallery_url.split('/')[-1]
        delete_image(image_name)
    
    # Delete product
    delete_product(product_id)
    _log_action(
        'product', product_id,
        f"{product.get('name', 'Produto')} ({product.get('sku') or '—'})",
        LOG_ACTION_DELETE,
        changes=[
            {'field': 'sku', 'before': product.get('sku') or '—', 'after': '—'},
            {'field': 'name', 'before': product.get('name') or '—', 'after': '—'},
            {'field': 'price', 'before': str(product.get('price') or '—'), 'after': '—'},
        ],
    )

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'success': True})

    flash('Produto deletado com sucesso!', 'success')
    return redirect(url_for('crm_produtos'))

# ============== Customer Routes ==============

def _read_customer_form(form):
    """
    Build a customer payload from either the CPF (pessoa física) or the
    CNPJ (pessoa jurídica) form. Returns (customer_data, error_message).
    """
    person_type = (form.get('person_type') or 'cpf').strip().lower()
    if person_type not in ('cpf', 'cnpj'):
        person_type = 'cpf'

    address = _build_address_structured(form)
    email = (form.get('email', '') or '').strip().lower()
    phone = re.sub(r'\D', '', form.get('phone', '') or '')

    data = {
        'person_type':        person_type,
        'email':              email,
        'phone':              phone,
        'address_structured': address,
        'address':            _format_address_line(address),
    }

    if person_type == 'cnpj':
        razao_social = (form.get('razao_social', '') or '').strip()
        cnpj = re.sub(r'\D', '', form.get('cnpj', '') or '')
        if not razao_social:
            return None, 'Informe a razão social.'
        if cnpj and not _validate_cnpj(cnpj):
            return None, 'CNPJ inválido. Confira os dígitos.'
        data['razao_social'] = razao_social
        data['cnpj'] = cnpj
        data['cpf'] = ''
        # `name` é o campo usado em listagens e pedidos — para PJ, a razão social.
        data['name'] = (form.get('name', '') or '').strip() or razao_social
        data['trade_name'] = (form.get('name', '') or '').strip()
    else:
        name = (form.get('name', '') or '').strip()
        cpf = re.sub(r'\D', '', form.get('cpf', '') or '')
        if not name:
            return None, 'Informe o nome do cliente.'
        if cpf and not _validate_cpf(cpf):
            return None, 'CPF inválido. Confira os dígitos.'
        data['name'] = name
        data['cpf'] = cpf
        data['cnpj'] = ''
        data['razao_social'] = ''

    return data, None


@app.route('/crm/customer/new', methods=['GET', 'POST'])
@require_admin
def new_customer():
    if request.method == 'POST':
        try:
            customer_data, error = _read_customer_form(request.form)
            if error:
                flash(error, 'error')
                return redirect(request.form.get('return_to') or url_for('crm_clientes'))

            # Clientes cadastrados no CRM são sempre de origem interna.
            customer_data['source'] = SOURCE_INTERNAL

            if customer_data.get('email'):
                existing = get_customer_by_email(customer_data['email'])
                if existing:
                    flash(f"Já existe um cliente com o e-mail {customer_data['email']}.", 'error')
                    return redirect(url_for('crm_customer_detail', customer_id=existing['id']))

            created = create_customer(customer_data)
            if created:
                _log_action(
                    'customer', created.get('id'), customer_data['name'], LOG_ACTION_CREATE,
                    changes=diff_for_log({}, customer_data),
                )
                flash('Cliente criado com sucesso!', 'success')
                return redirect(url_for('crm_customer_detail', customer_id=created['id']))
            flash('Erro ao criar cliente', 'error')

        except Exception as e:
            print(f"Error in new_customer: {e}")
            flash('Erro ao criar cliente', 'error')

        return redirect(url_for('crm_clientes'))

    return render_template('customer_form.html', customer=None, brazilian_states=BRAZILIAN_STATES)

@app.route('/crm/customer/<customer_id>/edit', methods=['GET', 'POST'])
@require_admin
def edit_customer(customer_id):
    customer = get_customer_by_id(customer_id)
    if not customer:
        flash('Cliente não encontrado', 'error')
        return redirect(url_for('crm_clientes'))
    
    if request.method == 'POST':
        try:
            updated_customer, error = _read_customer_form(request.form)
            if error:
                flash(error, 'error')
                return redirect(url_for('edit_customer', customer_id=customer_id))

            if update_customer(customer_id, updated_customer):
                _log_action(
                    'customer', customer_id, updated_customer['name'], LOG_ACTION_UPDATE,
                    changes=diff_for_log(customer, updated_customer),
                )
                flash('Cliente atualizado com sucesso!', 'success')
                return redirect(url_for('crm_customer_detail', customer_id=customer_id))
            flash('Erro ao atualizar cliente', 'error')
                
        except Exception as e:
            print(f"Error in edit_customer: {e}")
            flash('Erro ao atualizar cliente', 'error')
    
    customer['source'] = _resolve_customer_source(customer)
    return render_template('customer_form.html', 
                         customer=customer, 
                         edit_mode=True,
                         brazilian_states=BRAZILIAN_STATES)

@app.route('/crm/customer/<customer_id>/delete', methods=['POST'])
@require_admin
def delete_customer_route(customer_id):
    customer = get_customer_by_id(customer_id)
    if customer is None:
        flash('Cliente não encontrado', 'error')
        return redirect(url_for('crm_clientes'))
    delete_customer(customer_id)
    _log_action(
        'customer', customer_id, customer.get('name', 'Cliente'), LOG_ACTION_DELETE,
        changes=[
            {'field': 'name', 'before': customer.get('name') or '—', 'after': '—'},
            {'field': 'email', 'before': customer.get('email') or '—', 'after': '—'},
        ],
    )
    flash('Cliente deletado com sucesso!', 'success')
    return redirect(url_for('crm_clientes'))


def _customer_orders(customer):
    """Every order linked to a customer, by document ID and by e-mail (deduplicated)."""
    orders = get_orders_by_customer_id(customer['id'])
    seen = {o['id'] for o in orders}
    if customer.get('email'):
        for order in get_orders_by_customer_email(customer['email']):
            if order['id'] not in seen:
                seen.add(order['id'])
                orders.append(order)
    orders.sort(key=_order_created_at_sort_key, reverse=True)
    return _decorate_orders([serialize_order_for_display(o) for o in orders])


@app.route('/crm/customer/<customer_id>')
@require_admin
def crm_customer_detail(customer_id):
    """CRM: customer detail with order history, consolidated balance and payment intake."""
    customer = get_customer_by_id(customer_id)
    if not customer:
        flash('Cliente não encontrado', 'error')
        return redirect(url_for('crm_clientes'))

    customer['source'] = _resolve_customer_source(customer)
    customer['source_label'] = SOURCE_LABELS[customer['source']]

    orders = _customer_orders(customer)
    open_orders = [o for o in orders if o['balance_brl'] > 0.009]

    total_billed = sum(
        float(o.get('amount_brl') or 0) for o in orders
        if o['source'] == SOURCE_INTERNAL or o.get('status') in PAID_ORDER_STATUSES
    )
    total_paid = sum(o['paid_total_brl'] for o in orders)
    total_balance = sum(o['balance_brl'] for o in open_orders)

    today = datetime.now(_sao_paulo_tz).date()
    next_charge = None
    for order in open_orders:
        due = _parse_iso_date(order.get('next_charge_date'))
        if due and (next_charge is None or due < next_charge):
            next_charge = due

    return render_template(
        'crm_cliente_detalhe.html',
        customer=customer,
        orders=orders,
        open_orders=open_orders,
        total_billed=total_billed,
        total_paid=total_paid,
        total_balance=total_balance,
        next_charge_date=next_charge.isoformat() if next_charge else '',
        next_charge_overdue=bool(next_charge and next_charge < today),
        payment_methods=INTERNAL_PAYMENT_METHODS,
        today_iso=today.isoformat(),
    )


@app.route('/crm/customer/<customer_id>/pagamentos', methods=['POST'])
@require_admin
def crm_customer_register_payment(customer_id):
    """
    Register a payment from the customer page against the consolidated balance.
    The amount is applied to the open orders in chronological order (oldest first),
    splitting across orders when it exceeds a single balance.
    """
    customer = get_customer_by_id(customer_id)
    if not customer:
        flash('Cliente não encontrado.', 'error')
        return redirect(url_for('crm_clientes'))

    amount = _parse_brl_amount(request.form.get('amount_brl'))
    method = (request.form.get('method') or '').strip()
    paid_at = (request.form.get('paid_at') or '').strip() or _today_brt_iso()
    note = (request.form.get('note') or '').strip()
    next_charge_date = (request.form.get('next_charge_date') or '').strip()

    if not amount:
        flash('Informe um valor de pagamento válido.', 'error')
        return redirect(url_for('crm_customer_detail', customer_id=customer_id))
    if method not in INTERNAL_PAYMENT_METHOD_LABELS:
        flash('Selecione a forma de pagamento.', 'error')
        return redirect(url_for('crm_customer_detail', customer_id=customer_id))

    orders = _customer_orders(customer)
    open_orders = [o for o in orders if o['balance_brl'] > 0.009]
    open_orders.sort(key=lambda o: str(o.get('created_at') or ''))

    if not open_orders:
        flash('Este cliente não possui saldo devedor em aberto.', 'error')
        return redirect(url_for('crm_customer_detail', customer_id=customer_id))

    remaining = amount
    applied_to = []
    for order in open_orders:
        if remaining <= 0.009:
            break
        chunk = min(remaining, order['balance_brl'])
        fresh = get_order_by_id(order['id'])
        if not fresh:
            continue
        fresh['id'] = order['id']
        _register_order_payment(
            fresh, chunk, method, paid_at,
            note=note or f'Pagamento registrado na ficha do cliente {customer.get("name", "")}'.strip(),
            next_charge_date=next_charge_date if next_charge_date else None,
        )
        applied_to.append(f"#{order['id'][:8].upper()} (R$ {chunk:.2f})")
        remaining = round(remaining - chunk, 2)

    if remaining > 0.009:
        flash(
            f'Pagamento de R$ {amount:.2f} registrado. Sobrou R$ {remaining:.2f} '
            f'acima do saldo devedor — o excedente não foi lançado.',
            'warning',
        )
    else:
        flash(f"Pagamento de R$ {amount:.2f} registrado em {', '.join(applied_to)}.", 'success')

    return redirect(url_for('crm_customer_detail', customer_id=customer_id))


@app.route('/crm/pedidos/<order_id>/tracking', methods=['POST'])
@require_admin
def crm_set_order_tracking(order_id):
    """Register shipping tracking code and notify the customer by email and WhatsApp."""
    tracking_code = (request.form.get('tracking_code') or '').strip().upper()
    return_to = (request.form.get('return_to') or '').strip()

    if not tracking_code:
        flash('Informe o código de rastreio.', 'error')
        return redirect(return_to or url_for('crm_order_detail', order_id=order_id))

    order = get_order_by_id(order_id)
    if not order:
        flash('Pedido não encontrado.', 'error')
        return redirect(return_to or url_for('crm_pedidos'))

    if not update_order_fields(order_id, {
        'tracking_code': tracking_code,
        'tracking_set_at': _now_brt_iso(),
    }):
        flash('Erro ao salvar o código de rastreio.', 'error')
        return redirect(return_to or url_for('crm_order_detail', order_id=order_id))

    _log_action(
        'order', order_id, f"Pedido #{order_id[:8].upper()}", LOG_ACTION_UPDATE,
        changes=[{
            'field': 'tracking_code',
            'before': order.get('tracking_code') or '—',
            'after': tracking_code,
        }],
    )

    order = get_order_by_id(order_id)
    if email_service.send_order_tracking_email(order, tracking_code):
        flash('Código de rastreio registrado. Cliente notificado por e-mail e WhatsApp.', 'success')
    else:
        flash('Código salvo, mas houve falha ao notificar o cliente. Verifique os logs.', 'warning')

    return redirect(return_to or url_for('crm_order_detail', order_id=order_id))


@app.route('/crm/pedidos/<order_id>')
@require_admin
def crm_order_detail(order_id):
    """CRM: order detail page with payment intake and outstanding balance."""
    order = get_order_by_id(order_id)
    if not order:
        flash('Pedido não encontrado', 'error')
        return redirect(url_for('crm_pedidos'))
    # Serialize timestamps
    for ts_field in ('created_at', 'updated_at'):
        val = order.get(ts_field)
        if val and hasattr(val, 'strftime'):
            order[ts_field] = format_datetime_brt(val)

    _decorate_order(order)
    payments = sorted(order.get('payments') or [], key=lambda p: str(p.get('paid_at') or ''), reverse=True)
    today = datetime.now(_sao_paulo_tz).date()
    due = _parse_iso_date(order.get('next_charge_date'))

    return render_template(
        'crm_pedido_detalhe.html',
        order=order,
        payments=payments,
        payment_methods=INTERNAL_PAYMENT_METHODS,
        today_iso=today.isoformat(),
        next_charge_overdue=bool(due and due < today),
    )


@app.route('/crm/pedidos/<order_id>/pagamentos', methods=['POST'])
@require_admin
def crm_order_add_payment(order_id):
    """Register a payment received for an order (partial or full, any method)."""
    order = get_order_by_id(order_id)
    if not order:
        flash('Pedido não encontrado.', 'error')
        return redirect(url_for('crm_pedidos'))
    order['id'] = order_id

    amount = _parse_brl_amount(request.form.get('amount_brl'))
    method = (request.form.get('method') or '').strip()
    paid_at = (request.form.get('paid_at') or '').strip() or _today_brt_iso()
    note = (request.form.get('note') or '').strip()
    next_charge_date = (request.form.get('next_charge_date') or '').strip()

    if not amount:
        flash('Informe um valor de pagamento válido.', 'error')
        return redirect(url_for('crm_order_detail', order_id=order_id))
    if method not in INTERNAL_PAYMENT_METHOD_LABELS:
        flash('Selecione a forma de pagamento.', 'error')
        return redirect(url_for('crm_order_detail', order_id=order_id))

    balance_before = _order_balance(order)
    if amount > balance_before + 0.009:
        flash(
            f'O valor informado (R$ {amount:.2f}) é maior que o saldo devedor '
            f'(R$ {balance_before:.2f}).',
            'error',
        )
        return redirect(url_for('crm_order_detail', order_id=order_id))

    remaining_after = round(balance_before - amount, 2)
    if remaining_after > 0.009 and not next_charge_date:
        flash('Pagamento parcial: informe a data da próxima cobrança.', 'error')
        return redirect(url_for('crm_order_detail', order_id=order_id))

    _, result = _register_order_payment(
        order, amount, method, paid_at, note=note,
        next_charge_date=next_charge_date if remaining_after > 0.009 else '',
    )

    if result['payment_status'] == 'paid':
        flash(f'Pagamento de R$ {amount:.2f} registrado. Pedido quitado!', 'success')
    else:
        flash(
            f"Pagamento de R$ {amount:.2f} registrado. Saldo devedor: "
            f"R$ {result['balance_brl']:.2f}. Próxima cobrança em "
            f"{_format_date_br(next_charge_date)}.",
            'success',
        )
    return redirect(url_for('crm_order_detail', order_id=order_id))


@app.route('/crm/pedidos/<order_id>/pagamentos/<payment_id>/delete', methods=['POST'])
@require_admin
def crm_order_delete_payment(order_id, payment_id):
    """Remove a wrongly registered payment and restore the balance."""
    order = get_order_by_id(order_id)
    if not order:
        flash('Pedido não encontrado.', 'error')
        return redirect(url_for('crm_pedidos'))
    order['id'] = order_id

    payments = list(order.get('payments') or [])
    removed = next((p for p in payments if p.get('id') == payment_id), None)
    if not removed:
        flash('Pagamento não encontrado.', 'error')
        return redirect(url_for('crm_order_detail', order_id=order_id))

    order['payments'] = [p for p in payments if p.get('id') != payment_id]
    result = _persist_order_financials(order)
    _log_action(
        'payment', order_id, f"Pedido #{order_id[:8].upper()}", LOG_ACTION_DELETE,
        changes=[
            {'field': 'amount_brl', 'before': f"R$ {float(removed.get('amount_brl') or 0):.2f}", 'after': '—'},
            {'field': 'balance_brl', 'before': f"R$ {float(order.get('balance_brl') or 0):.2f}",
             'after': f"R$ {result['balance_brl']:.2f}"},
        ],
    )
    flash('Pagamento removido. Saldo devedor recalculado.', 'success')
    return redirect(url_for('crm_order_detail', order_id=order_id))


@app.route('/crm/pedidos/<order_id>/proxima-cobranca', methods=['POST'])
@require_admin
def crm_order_set_next_charge(order_id):
    """Update only the next-charge reminder date for an order."""
    order = get_order_by_id(order_id)
    if not order:
        flash('Pedido não encontrado.', 'error')
        return redirect(url_for('crm_pedidos'))

    next_charge_date = (request.form.get('next_charge_date') or '').strip()
    if next_charge_date and not _parse_iso_date(next_charge_date):
        flash('Data inválida.', 'error')
        return redirect(url_for('crm_order_detail', order_id=order_id))

    update_order_fields(order_id, {'next_charge_date': next_charge_date})
    _log_action(
        'order', order_id, f"Pedido #{order_id[:8].upper()}", LOG_ACTION_UPDATE,
        changes=[{
            'field': 'next_charge_date',
            'before': _format_date_br(order.get('next_charge_date')) or '—',
            'after': _format_date_br(next_charge_date) or '—',
        }],
    )
    flash('Data da próxima cobrança atualizada.', 'success')
    return redirect(url_for('crm_order_detail', order_id=order_id))


# ============================================
# CRM — Criação de pedidos internos
# ============================================

@app.route('/crm/api/customers/search')
@require_admin
def crm_api_customer_search():
    """Autocomplete de clientes por nome, e-mail, CPF/CNPJ ou telefone."""
    query = (request.args.get('q') or '').strip().lower()
    if len(query) < 2:
        return jsonify({'results': []})

    digits = re.sub(r'\D', '', query)
    results = []
    for customer in get_all_customers():
        haystack = ' '.join([
            str(customer.get('name') or ''),
            str(customer.get('razao_social') or ''),
            str(customer.get('email') or ''),
        ]).lower()
        matched = query in haystack
        if not matched and digits:
            matched = digits in re.sub(r'\D', '', str(customer.get('cpf') or '')) \
                or digits in re.sub(r'\D', '', str(customer.get('cnpj') or '')) \
                or digits in re.sub(r'\D', '', str(customer.get('phone') or ''))
        if matched:
            results.append(_serialize_customer_for_order(customer))
        if len(results) >= 15:
            break

    results.sort(key=lambda c: c['name'].lower())
    return jsonify({'results': results})


@app.route('/crm/api/products/search')
@require_admin
def crm_api_product_search():
    """Autocomplete de produtos por SKU ou nome, com o estoque disponível."""
    query = (request.args.get('q') or '').strip().lower()
    if len(query) < 1:
        return jsonify({'results': []})

    results = []
    for product in get_all_products():
        if product.get('id') in CATALOG_HIDDEN_PRODUCT_IDS:
            continue
        if product.get('sold_out'):
            continue
        haystack = ' '.join([
            str(product.get('sku') or ''),
            str(product.get('name') or ''),
            str(product.get('category') or ''),
            str(product.get('variant') or ''),
        ]).lower()
        if query in haystack:
            results.append(_serialize_product_for_order(product))
        if len(results) >= 20:
            break

    results.sort(key=lambda p: (p['sku'] or 'zzz'))
    return jsonify({'results': results})


@app.route('/crm/api/products/next-sku')
@require_admin
def crm_api_next_sku():
    """Próximo SKU numérico disponível (maior SKU do banco + 1)."""
    return jsonify({'sku': get_next_numeric_sku()})


@app.route('/crm/api/products/check-sku')
@require_admin
def crm_api_check_sku():
    """Verifica em tempo real se um SKU já está em uso."""
    sku = (request.args.get('sku') or '').strip()
    exclude = (request.args.get('product_id') or '').strip() or None
    if not sku:
        return jsonify({'sku': '', 'available': False, 'reason': 'empty'})

    matches = find_products_by_sku(sku, exclude_product_id=exclude)
    if not matches:
        return jsonify({'sku': sku, 'available': True})
    conflict = matches[0]
    return jsonify({
        'sku': sku,
        'available': False,
        'reason': 'duplicate',
        'product': {
            'id': conflict.get('id'),
            'name': conflict.get('name') or 'Produto sem nome',
            'active': bool(conflict.get('active')),
        },
    })


def _build_internal_order_items(raw_items, exchange_rate):
    """
    Validate the item rows submitted by the 'novo pedido' form.
    Returns (items, stock_plan, subtotal_usd, error_message).
    stock_plan carries what must be deducted from each product after saving.
    """
    items = []
    stock_plan = []
    subtotal_usd = 0.0

    for index, raw in enumerate(raw_items or []):
        product_id = (raw.get('product_id') or '').strip()
        if not product_id:
            return None, None, 0.0, 'Selecione um produto válido em todas as linhas.'

        product = get_product_by_id(product_id)
        if not product:
            return None, None, 0.0, f"Produto não encontrado (linha {index + 1})."
        if product.get('sold_out'):
            return None, None, 0.0, f"O produto {product.get('sku') or product.get('name')} já está esgotado."

        sale_mode = 'partial' if (raw.get('sale_mode') == 'partial') else 'full'
        available_ct = parse_carat_value(product.get('carat_weight'))
        try:
            available_pedras = int(product.get('numero_pedras') or 0)
        except (ValueError, TypeError):
            available_pedras = 0

        if sale_mode == 'full':
            ct = available_ct
            pedras = available_pedras
        else:
            try:
                ct = round(float(str(raw.get('ct') or 0).replace(',', '.')), 3)
            except (ValueError, TypeError):
                return None, None, 0.0, f"Peso inválido na linha {index + 1}."
            try:
                pedras = int(raw.get('numero_pedras') or 0)
            except (ValueError, TypeError):
                return None, None, 0.0, f"Número de pedras inválido na linha {index + 1}."
            if ct <= 0:
                return None, None, 0.0, f"Informe o peso (ct) vendido na linha {index + 1}."
            if ct > available_ct + 0.0001:
                return None, None, 0.0, (
                    f"O produto {product.get('sku')} tem apenas {available_ct} ct disponíveis "
                    f"(solicitado: {ct} ct)."
                )
            if pedras > available_pedras:
                return None, None, 0.0, (
                    f"O produto {product.get('sku')} tem apenas {available_pedras} pedra(s) "
                    f"disponíveis (solicitado: {pedras})."
                )

        try:
            price_per_carat = round(float(str(raw.get('price_per_carat_usd') or product.get('price') or 0).replace(',', '.')), 2)
        except (ValueError, TypeError):
            return None, None, 0.0, f"Preço unitário inválido na linha {index + 1}."
        if price_per_carat <= 0:
            return None, None, 0.0, f"Informe o preço por quilate na linha {index + 1}."

        total_usd = round(price_per_carat * (ct or 1), 2)
        subtotal_usd += total_usd

        items.append({
            'product_id': product_id,
            'sku': product.get('sku') or '',
            'name': product.get('name') or '',
            'lote': (raw.get('lote') or '').strip(),
            'pedra': _product_pedra_label(product),
            'formato': product.get('cut') or '',
            'calibre': _product_calibre(product),
            'ct': ct,
            'numero_pedras': pedras,
            'price_per_carat_usd': price_per_carat,
            'total_usd': total_usd,
            'total_brl': round(total_usd * exchange_rate, 2) if exchange_rate else total_usd,
            'sale_mode': sale_mode,
            'image': product.get('main_image_url') or '',
            'quantity': 1,
        })
        stock_plan.append({
            'product_id': product_id,
            'sku': product.get('sku') or '',
            'name': product.get('name') or '',
            'ct': ct,
            'numero_pedras': pedras,
            'full_sale': sale_mode == 'full',
            'total_usd': total_usd,
            'index': index,
        })

    if not items:
        return None, None, 0.0, 'Adicione pelo menos um produto ao pedido.'

    return items, stock_plan, round(subtotal_usd, 2), None


@app.route('/crm/pedidos/novo', methods=['GET'])
@require_admin
def crm_new_order():
    """Formulário de criação de pedido interno."""
    exchange_rate = _current_exchange_rate()

    # Permite abrir o formulário já com o cliente escolhido (link da ficha do cliente)
    preset_customer = None
    preset_id = (request.args.get('customer_id') or '').strip()
    if preset_id:
        found = get_customer_by_id(preset_id)
        if found:
            preset_customer = _serialize_customer_for_order(found)

    return render_template(
        'crm_pedido_novo.html',
        sellers=get_all_sellers(only_active=True),
        exchange_rate=exchange_rate,
        payment_methods=INTERNAL_PAYMENT_METHODS,
        brazilian_states=BRAZILIAN_STATES,
        today_iso=_today_brt_iso(),
        preset_customer=preset_customer,
    )


@app.route('/crm/api/pedidos', methods=['POST'])
@require_admin
def crm_api_create_order():
    """
    Cria um pedido interno: valida os itens, grava o pedido, dá baixa no estoque
    (integral ou parcial) e registra o pagamento inicial quando informado.
    """
    data = request.get_json(silent=True) or {}

    customer_id = (data.get('customer_id') or '').strip()
    customer_name = (data.get('customer_name') or '').strip()
    if not customer_name:
        return jsonify({'error': 'Informe o cliente do pedido.'}), 400

    customer = get_customer_by_id(customer_id) if customer_id else None

    exchange_rate = _current_exchange_rate()
    items, stock_plan, subtotal_usd, error = _build_internal_order_items(data.get('items'), exchange_rate)
    if error:
        return jsonify({'error': error}), 400

    try:
        discount_usd = round(float(str(data.get('discount_usd') or 0).replace(',', '.')), 2)
    except (ValueError, TypeError):
        discount_usd = 0.0
    discount_usd = max(0.0, min(discount_usd, subtotal_usd))
    amount_usd = round(subtotal_usd - discount_usd, 2)
    amount_brl = round(amount_usd * exchange_rate, 2) if exchange_rate else amount_usd

    seller_id = (data.get('seller_id') or '').strip()
    seller = get_seller_by_id(seller_id) if seller_id else None
    commission_pct = float(seller.get('commission_pct') or 0) if seller else 0.0

    address = {
        'cep': (data.get('cep') or '').strip(),
        'street': (data.get('street') or '').strip(),
        'number': (data.get('number') or '').strip(),
        'complement': (data.get('complement') or '').strip(),
        'neighborhood': (data.get('neighborhood') or '').strip(),
        'city': (data.get('city') or '').strip(),
        'state': (data.get('state') or '').strip().upper(),
    }

    order_doc = {
        'source': SOURCE_INTERNAL,
        'status': 'pending',
        'payment_method': 'internal',
        'firestore_customer_id': customer_id,
        'customer_name': customer_name,
        'customer_email': (data.get('customer_email') or '').strip().lower(),
        'customer_phone': re.sub(r'\D', '', data.get('customer_phone') or ''),
        'customer_cpf': re.sub(r'\D', '', data.get('customer_cpf') or ''),
        'customer_cnpj': re.sub(r'\D', '', data.get('customer_cnpj') or ''),
        'customer_person_type': (customer or {}).get('person_type') or ('cnpj' if data.get('customer_cnpj') else 'cpf'),
        'shipping_address': address,
        'seller_id': seller_id,
        'seller_name': (seller or {}).get('name', ''),
        'seller_commission_pct': commission_pct,
        'commission_amount_brl': round(amount_brl * commission_pct / 100, 2),
        'commission_amount_usd': round(amount_usd * commission_pct / 100, 2),
        'exchange_rate_used': exchange_rate,
        'items': items,
        'subtotal_usd': subtotal_usd,
        'discount_usd': discount_usd,
        'amount_usd': amount_usd,
        'subtotal_brl': round(subtotal_usd * exchange_rate, 2) if exchange_rate else subtotal_usd,
        'shipping_cost_brl': 0.0,
        'amount_brl': amount_brl,
        'amount_centavos': int(round(amount_brl * 100)),
        'currency': 'BRL',
        'notes': (data.get('notes') or '').strip(),
        'payments': [],
        'paid_total_brl': 0.0,
        'balance_brl': amount_brl,
        'payment_status': 'open' if amount_brl > 0 else 'paid',
        'next_charge_date': (data.get('next_charge_date') or '').strip(),
        'created_by': session.get('user_email') or 'admin',
        'products_marked_sold': True,
    }

    saved = create_order_db(order_doc)
    if not saved:
        return jsonify({'error': 'Erro ao salvar o pedido. Tente novamente.'}), 500

    order_id = saved['id']
    order_number = order_id[:8].upper()

    # ── Baixa de estoque (integral ou parcial) ──
    stock_warnings = []
    sold_at = _now_brt_iso()
    for plan in stock_plan:
        sale_record = {
            'entry_key': f"{order_id}:{plan['index']}",
            'order_id': order_id,
            'order_number': order_number,
            'source': SOURCE_INTERNAL,
            'customer_name': customer_name,
            'customer_email': order_doc['customer_email'],
            'seller_name': order_doc['seller_name'],
            'sale_mode': 'full' if plan['full_sale'] else 'partial',
            'carat_sold': plan['ct'],
            'pedras_sold': plan['numero_pedras'],
            'item_amount_usd': plan['total_usd'],
            'amount_brl': round(plan['total_usd'] * exchange_rate, 2) if exchange_rate else plan['total_usd'],
            'sold_at': sold_at,
        }
        before = get_product_by_id(plan['product_id']) or {}
        result = apply_product_stock_sale(
            plan['product_id'], sale_record,
            carat_sold=plan['ct'], pedras_sold=plan['numero_pedras'],
            full_sale=plan['full_sale'],
        )
        if result is None:
            stock_warnings.append(plan['sku'] or plan['product_id'])
            continue
        _log_action(
            'product', plan['product_id'], f"{plan['name']} ({plan['sku']})", LOG_ACTION_UPDATE,
            changes=diff_for_log(
                before,
                {
                    'carat_weight': result.get('carat_weight'),
                    'numero_pedras': result.get('numero_pedras'),
                    'active': result.get('active'),
                    'sold_out': result.get('sold_out'),
                },
            ),
            note=(
                f"Baixa {'integral' if plan['full_sale'] else 'parcial'} pelo pedido #{order_number} "
                f"({plan['ct']} ct / {plan['numero_pedras']} pedra(s))"
            ),
            automatic=True,
        )

    # ── Pagamento inicial opcional ──
    initial_amount = _parse_brl_amount(data.get('initial_payment_amount'))
    initial_method = (data.get('initial_payment_method') or '').strip()
    if initial_amount and initial_method in INTERNAL_PAYMENT_METHOD_LABELS:
        saved['id'] = order_id
        _register_order_payment(
            saved,
            min(initial_amount, amount_brl),
            initial_method,
            (data.get('initial_payment_date') or '').strip() or _today_brt_iso(),
            note='Pagamento informado na criação do pedido',
            next_charge_date=order_doc['next_charge_date'],
        )

    _log_action(
        'order', order_id, f"Pedido #{order_number}", LOG_ACTION_CREATE,
        changes=[
            {'field': 'source', 'before': '—', 'after': SOURCE_INTERNAL},
            {'field': 'name', 'before': '—', 'after': customer_name},
            {'field': 'subtotal_usd', 'before': '—', 'after': f"US$ {subtotal_usd:.2f}"},
            {'field': 'discount_usd', 'before': '—', 'after': f"US$ {discount_usd:.2f}"},
            {'field': 'amount_usd', 'before': '—', 'after': f"US$ {amount_usd:.2f}"},
            {'field': 'amount_brl', 'before': '—', 'after': f"R$ {amount_brl:.2f}"},
            {'field': 'items', 'before': '—', 'after': f"{len(items)} item(ns)"},
            {'field': 'seller_name', 'before': '—', 'after': order_doc['seller_name'] or '—'},
        ],
    )

    response = {'success': True, 'order_id': order_id, 'order_number': order_number}
    if stock_warnings:
        response['warning'] = (
            'Pedido criado, mas houve falha na baixa de estoque de: ' + ', '.join(stock_warnings)
        )
    return jsonify(response), 200


# ============================================
# CRM — Vendedores
# ============================================

@app.route('/crm/ajustes/vendedores', methods=['GET', 'POST'])
@require_admin
def crm_vendedores():
    """Cadastro de vendedores e respectivas comissões."""
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        email = (request.form.get('email') or '').strip().lower()
        commission_raw = (request.form.get('commission_pct') or '').strip().replace(',', '.')

        if not name:
            flash('Informe o nome do vendedor.', 'error')
            return redirect(url_for('crm_vendedores'))
        try:
            commission_pct = round(float(commission_raw or 0), 2)
        except (ValueError, TypeError):
            flash('Comissão inválida. Use um número (ex.: 3,5).', 'error')
            return redirect(url_for('crm_vendedores'))
        if not 0 <= commission_pct <= 100:
            flash('A comissão deve estar entre 0% e 100%.', 'error')
            return redirect(url_for('crm_vendedores'))

        seller_data = {
            'name': name,
            'email': email,
            'commission_pct': commission_pct,
            'active': request.form.get('active', 'on') == 'on',
        }

        seller_id = (request.form.get('seller_id') or '').strip()
        if seller_id:
            existing = get_seller_by_id(seller_id)
            if not existing:
                flash('Vendedor não encontrado.', 'error')
                return redirect(url_for('crm_vendedores'))
            if update_seller(seller_id, seller_data):
                _log_action('seller', seller_id, name, LOG_ACTION_UPDATE,
                            changes=diff_for_log(existing, seller_data))
                flash('Vendedor atualizado com sucesso!', 'success')
            else:
                flash('Erro ao atualizar vendedor.', 'error')
        else:
            created = create_seller(seller_data)
            if created:
                _log_action('seller', created.get('id'), name, LOG_ACTION_CREATE,
                            changes=diff_for_log({}, seller_data))
                flash('Vendedor cadastrado com sucesso!', 'success')
            else:
                flash('Erro ao cadastrar vendedor.', 'error')

        return redirect(url_for('crm_vendedores'))

    sellers = get_all_sellers()
    orders = _decorate_orders(get_all_orders())

    # Produção por vendedor, usada para contexto na própria tela de cadastro
    stats = {}
    for order in orders:
        seller_id = order.get('seller_id')
        if not seller_id:
            continue
        entry = stats.setdefault(seller_id, {'orders': 0, 'billed': 0.0, 'received': 0.0, 'commission': 0.0})
        entry['orders'] += 1
        entry['billed'] += float(order.get('amount_brl') or 0)
        entry['received'] += order['paid_total_brl']
        entry['commission'] += float(order.get('commission_amount_brl') or 0)

    return render_template('crm_vendedores.html', sellers=sellers, stats=stats)


@app.route('/crm/ajustes/vendedores/<seller_id>/delete', methods=['POST'])
@require_admin
def crm_delete_vendedor(seller_id):
    seller = get_seller_by_id(seller_id)
    if not seller:
        flash('Vendedor não encontrado.', 'error')
        return redirect(url_for('crm_vendedores'))
    delete_seller(seller_id)
    _log_action('seller', seller_id, seller.get('name', 'Vendedor'), LOG_ACTION_DELETE,
                changes=[{'field': 'name', 'before': seller.get('name') or '—', 'after': '—'}])
    flash('Vendedor removido.', 'success')
    return redirect(url_for('crm_vendedores'))


# ============================================
# CRM — Logs de atividade
# ============================================

@app.route('/crm/logs')
@require_admin
def crm_logs():
    """Auditoria: tudo que foi criado, editado ou deletado na plataforma."""
    entity_type = (request.args.get('entity_type') or '').strip()
    action = (request.args.get('action') or '').strip()
    actor = (request.args.get('actor') or '').strip()
    search = (request.args.get('search') or '').strip().lower()

    logs = get_activity_logs(
        limit=400,
        entity_type=entity_type or None,
        action=action or None,
        actor_email=actor or None,
    )

    if search:
        logs = [
            log for log in logs
            if search in ' '.join([
                str(log.get('entity_label') or ''),
                str(log.get('actor_email') or ''),
                str(log.get('note') or ''),
                str(log.get('entity_id') or ''),
            ]).lower()
        ]

    return render_template(
        'crm_logs.html',
        logs=logs,
        filters={'entity_type': entity_type, 'action': action, 'actor': actor, 'search': search},
        entity_labels=ENTITY_LABELS,
        action_labels=LOG_ACTION_LABELS,
        actors=get_distinct_log_actors(),
    )


# ============================================
# Customer Authentication Routes
# ============================================

def get_firebase_config():
    """Get Firebase config for client-side SDK"""
    return {
        'apiKey': os.getenv('FIREBASE_API_KEY'),
        'authDomain': os.getenv('FIREBASE_AUTH_DOMAIN'),
        'projectId': os.getenv('FIREBASE_PROJECT_ID'),
        'storageBucket': os.getenv('FIREBASE_STORAGE_BUCKET'),
        'messagingSenderId': os.getenv('FIREBASE_MESSAGING_SENDER_ID'),
        'appId': os.getenv('FIREBASE_APP_ID')
    }

@app.route('/login')
def login():
    """Customer login page"""
    return render_template('login.html', firebase_config=get_firebase_config())

@app.route('/login/finish')
def login_finish():
    """Complete passwordless email link sign-in"""
    return render_template('login_finish.html', firebase_config=get_firebase_config())

@app.route('/register')
def register():
    """Redirect legacy registration to magic-link login"""
    return redirect(url_for('login'))

@app.route('/account')
def account():
    """Customer account page"""
    # Check if user is logged in
    is_logged_in = 'user_id' in session
    return render_template('customer_account.html', 
                         firebase_config=get_firebase_config(),
                         is_logged_in=is_logged_in)

@app.route('/forgot-password')
def forgot_password():
    """Redirect legacy password reset to magic-link login"""
    return redirect(url_for('login'))

@app.route('/privacy-policy')
def privacy_policy():
    """Privacy policy page"""
    return render_template('privacy_policy.html', firebase_config=get_firebase_config())

@app.route('/terms-of-service')
def terms_of_service():
    """Terms of service page"""
    return render_template('terms_of_service.html', firebase_config=get_firebase_config())

@app.route('/api/auth/logout', methods=['POST'])
@limiter.limit("20/minute")
def api_logout():
    """Clear Flask session on logout."""
    session.clear()
    return jsonify({'success': True}), 200

def _is_valid_email(email):
    return bool(re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email or ''))


@app.route('/robots.txt')
def robots_txt():
    body = (
        'User-agent: *\n'
        'Disallow: /account\n'
        'Disallow: /cart\n'
        'Disallow: /crm\n'
        'Disallow: /checkout\n'
        f'Sitemap: {_site_url()}/sitemap.xml\n'
    )
    return Response(body, mimetype='text/plain')


@app.route('/sitemap.xml')
def sitemap_xml():
    site = _site_url()
    urls = [
        ('/', 'daily', '1.0'),
        ('/products', 'daily', '0.9'),
        ('/historia', 'monthly', '0.7'),
        ('/o-que-procura', 'monthly', '0.6'),
        ('/feiras', 'monthly', '0.5'),
        ('/guias', 'weekly', '0.8'),
    ]
    for slug in GUIDES:
        urls.append((f'/guias/{slug}', 'monthly', '0.7'))

    all_products = get_all_products()
    active = [p for p in all_products if _is_catalog_visible(p)]
    categories = sorted(set(p.get('category') for p in active if p.get('category')))
    for cat in categories:
        urls.append((seo.category_path(cat), 'daily', '0.85'))
    for p in active:
        pid = p.get('id')
        if pid:
            urls.append((f'/product/{pid}', 'weekly', '0.8'))

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path, changefreq, priority in urls:
        loc = site + path
        lines.append('  <url>')
        lines.append(f'    <loc>{loc}</loc>')
        lines.append(f'    <changefreq>{changefreq}</changefreq>')
        lines.append(f'    <priority>{priority}</priority>')
        lines.append('  </url>')
    lines.append('</urlset>')
    return Response('\n'.join(lines), mimetype='application/xml')

@app.route('/api/auth/send-login-link', methods=['POST'])
@limiter.limit("5/minute")
def send_login_link():
    """Generate Firebase email sign-in link, wrap it in an opaque token, and send via Brevo."""
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()

    if not _is_valid_email(email):
        return jsonify({'error': 'Informe um e-mail válido.'}), 400

    success_message = (
        'Se o e-mail estiver correto, você receberá um link de acesso em instantes. '
        'Verifique também a pasta de spam.'
    )

    try:
        action_code_settings = firebase_auth.ActionCodeSettings(
            url=f'{_site_url()}/login/finish',
            handle_code_in_app=True,
        )
        firebase_link = firebase_auth.generate_sign_in_with_email_link(email, action_code_settings)
    except Exception as exc:
        exc_text = str(exc)
        if 'OPERATION_NOT_ALLOWED' in exc_text:
            return jsonify({
                'error': (
                    'Login por link não está habilitado no Firebase. '
                    'Em Authentication → Sign-in method → E-mail/Senha, ative '
                    '"Email link (passwordless sign-in)" e salve.'
                )
            }), 503
        return jsonify({
            'error': 'Não foi possível gerar o link de acesso. Tente novamente em instantes.'
        }), 500

    token = str(uuid.uuid4())
    expires_at = utc_now() + timedelta(hours=1)
    save_login_token(token, firebase_link, email, expires_at)

    masked_link = f'{_site_url()}/auth/login?token={token}'
    if not email_service.send_login_link_email(email, masked_link):
        print(f"[Login] Failed to send login link email to {email}")

    return jsonify({'success': True, 'message': success_message}), 200


@app.route('/auth/login')
@limiter.limit("20/minute")
def auth_login_token():
    """Exchange an opaque login token for the real Firebase sign-in link."""
    token = (request.args.get('token') or '').strip()
    if not token:
        return render_template('login.html',
                               error='Link inválido. Solicite um novo link de acesso.',
                               firebase_config=get_firebase_config()), 400

    doc = get_and_delete_login_token(token)
    if not doc:
        return render_template('login.html',
                               error='Este link já foi utilizado ou expirou. Solicite um novo.',
                               firebase_config=get_firebase_config()), 410

    expires_at = doc.get('expires_at')
    if expires_at:
        now_ts = utc_now().timestamp()
        if hasattr(expires_at, 'timestamp'):
            if getattr(expires_at, 'tzinfo', None) is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            expired = expires_at.timestamp() < now_ts
        else:
            try:
                exp_dt = datetime.fromisoformat(str(expires_at))
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                expired = exp_dt.timestamp() < now_ts
            except Exception:
                expired = False
        if expired:
            return render_template('login.html',
                                   error='Este link expirou. Solicite um novo link de acesso.',
                                   firebase_config=get_firebase_config()), 410

    firebase_link = doc.get('firebase_link', '')
    if not firebase_link:
        return render_template('login.html',
                               error='Link inválido. Solicite um novo link de acesso.',
                               firebase_config=get_firebase_config()), 400

    return redirect(firebase_link, 302)

@app.route('/api/auth/verify', methods=['POST'])
@limiter.limit("15/minute")
def verify_auth():
    """Verify Firebase ID token and create/update session"""
    try:
        data = request.get_json()
        id_token = data.get('idToken')
        
        if not id_token:
            return jsonify({'error': 'No token provided'}), 400
        
        # Verify the token
        decoded_token = firebase_auth.verify_id_token(id_token)
        uid = decoded_token['uid']
        email = decoded_token.get('email')
        
        # Get user info from Firebase Auth to get display name
        user_record = firebase_auth.get_user(uid)
        display_name = user_record.display_name if user_record.display_name else ''
        
        # Check if customer already exists using targeted Firestore queries
        existing_by_uid   = get_customer_by_firebase_uid(uid)
        existing_customer = None
        customer_id       = None
        customer_exists   = bool(existing_by_uid)

        if not customer_exists and email:
            existing_by_email = get_customer_by_email(email)
            if existing_by_email:
                existing_customer = existing_by_email
                customer_id       = existing_by_email.get('id')

        if not customer_exists:
            # Use display name if available, otherwise use part of email
            customer_name = display_name if display_name else email.split('@')[0]
            
            if existing_customer:
                # Update existing customer with firebase_uid
                customer_data = {
                    'name': existing_customer.get('name', customer_name),
                    'email': email,
                    'phone': existing_customer.get('phone', ''),
                    'address': existing_customer.get('address', ''),
                    'firebase_uid': uid
                }
                update_customer(customer_id, customer_data)
                print(f"Updated existing customer with firebase_uid: {email}")
            else:
                # Create new customer
                customer_data = {
                    'name': customer_name,
                    'email': email,
                    'phone': '',
                    'address': '',
                    'firebase_uid': uid,
                    'created_at': _now_brt_iso()
                }
                create_customer(customer_data)
                print(f"Created new customer for: {email} (name: {customer_name})")
        
        # Store in session
        session['user_id'] = uid
        session['user_email'] = email
        
        return jsonify({
            'success': True,
            'uid': uid,
            'email': email
        }), 200
        
    except Exception as e:
        print(f"Verification error: {e}")
        return jsonify({'error': 'Unauthorized'}), 401

@app.route('/api/account/data')
@require_auth
def get_account_data():
    """Get customer account data"""
    try:
        customer_data = {
            'name': '',
            'phone': '',
            'cpf': '',
            'address': '',
            'address_structured': {}
        }
        
        customer = get_customer_by_firebase_uid(request.user_id)
        if not customer and request.user_email:
            customer = get_customer_by_email(request.user_email)
        if customer:
            customer_data['name']               = customer.get('name', '')
            customer_data['phone']              = customer.get('phone', '')
            customer_data['cpf']                = customer.get('cpf', '')
            customer_data['address']            = customer.get('address', '')
            customer_data['address_structured'] = customer.get('address_structured', {})
            created = customer.get('created_at')
            if created and hasattr(created, 'strftime'):
                _months_pt = (
                    'janeiro', 'fevereiro', 'março', 'abril', 'maio', 'junho',
                    'julho', 'agosto', 'setembro', 'outubro', 'novembro', 'dezembro',
                )
                customer_data['member_since'] = f"{_months_pt[created.month - 1]} de {created.year}"

        return jsonify(customer_data), 200

    except Exception as e:
        print(f"Error getting account data: {e}")
        return jsonify({'error': 'Erro ao carregar dados'}), 500

@app.route('/api/account/orders')
@require_auth
def get_account_orders():
    """Get customer order history (by Firebase UID and email)."""
    try:
        orders_by_uid = get_orders_by_firebase_uid(request.user_id)
        orders_by_email = (
            get_orders_by_customer_email(request.user_email.strip().lower())
            if request.user_email else []
        )

        merged = {}
        for o in orders_by_uid + orders_by_email:
            merged[o.get('id')] = o

        orders = sorted(
            merged.values(),
            key=_order_created_at_sort_key,
            reverse=True,
        )

        safe_orders = []
        for o in orders:
            pix_expires = o.get('pix_expires_at')
            if pix_expires and hasattr(pix_expires, 'strftime'):
                pix_expires = pix_expires.isoformat()
            is_paid = o.get('status') in PAID_ORDER_STATUSES
            safe_items = []
            for item in o.get('items', []):
                safe_item = dict(item)
                pid = item.get('product_id')
                if is_paid and pid and pid not in CATALOG_HIDDEN_PRODUCT_IDS:
                    safe_item['view_url'] = url_for('product_detail', product_id=pid)
                safe_items.append(safe_item)
            created_at = o.get('created_at', '')
            if hasattr(created_at, 'strftime'):
                created_at = format_datetime_brt(created_at)
            safe_orders.append({
                'id':             o.get('id', ''),
                'status':         o.get('status', ''),
                'payment_method': o.get('payment_method', ''),
                'amount_brl':     o.get('amount_brl', 0),
                'created_at':     created_at,
                'pix_expires_at': pix_expires or '',
                'items':          safe_items,
            })
        return jsonify(safe_orders), 200
    except Exception as e:
        print(f"Error getting orders: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/account/solicitacoes')
@require_auth
def get_account_solicitacoes():
    """Get the logged-in customer's 'O que procura?' submissions."""
    try:
        solicitacoes = get_solicitacoes_by_email(request.user_email) if request.user_email else []
        safe = []
        for s in solicitacoes:
            safe.append({
                'id':          s.get('id', ''),
                'description': s.get('description', ''),
                'created_at':  s.get('created_at_formatted', '—'),
                'status':      s.get('status', 'open'),
                'replies':     s.get('replies', []),
            })
        return jsonify(safe), 200
    except Exception as e:
        print(f"Error getting account solicitacoes: {e}")
        return jsonify({'error': 'Erro ao carregar solicitações'}), 500


@app.route('/api/account/update', methods=['POST'])
@require_auth
def update_account():
    """Update customer account data"""
    try:
        data = request.get_json()
        name = data.get('name', '')
        phone = data.get('phone', '')
        cpf = re.sub(r'\D', '', data.get('cpf', '') or '')
        address_structured = data.get('address_structured', {})

        existing_customer = get_customer_by_firebase_uid(request.user_id)
        if not existing_customer and request.user_email:
            existing_customer = get_customer_by_email(request.user_email)

        if cpf and not _validate_cpf(cpf):
            return jsonify({'error': 'CPF inválido.'}), 400
        if not cpf and existing_customer:
            cpf = existing_customer.get('cpf', '')

        address_str = ''
        if address_structured:
            parts = []
            street = address_structured.get('street', '')
            number = address_structured.get('number', '')
            complement = address_structured.get('complement', '')
            neighborhood = address_structured.get('neighborhood', '')
            city = address_structured.get('city', '')
            state = address_structured.get('state', '')
            if street or number:
                parts.append(f"{street} {number}".strip())
            if complement:
                parts.append(complement)
            if neighborhood:
                parts.append(neighborhood)
            if city or state:
                parts.append(f"{city} - {state}".strip(' -'))
            address_str = ', '.join(filter(None, parts))

        customer_data = {
            'name': name,
            'email': request.user_email,
            'phone': phone,
            'cpf': cpf,
            'address': address_str,
            'address_structured': address_structured,
            'firebase_uid': request.user_id
        }
        if existing_customer:
            customer_data['pagarme_customer_id'] = existing_customer.get('pagarme_customer_id', '')
            update_customer(existing_customer['id'], customer_data)
        else:
            customer_data['created_at'] = _now_brt_iso()
            create_customer(customer_data)

        return jsonify({'success': True}), 200

    except Exception as e:
        print(f"Error updating account: {e}")
        return jsonify({'error': 'Erro ao salvar dados'}), 500

# ============================================
# Cut Types Management Routes
# ============================================

@app.route('/crm/cut-types', methods=['GET', 'POST'])
@require_admin
def crm_cut_types():
    """Manage cut types."""
    if request.method == 'POST':
        try:
            # Get cut types from form
            cut_types = []
            cut_type_inputs = request.form.getlist('cut_type[]')
            
            for cut_type in cut_type_inputs:
                if cut_type.strip():  # Only add non-empty cut types
                    cut_types.append(cut_type.strip())
            
            # Save cut types
            save_cut_types(cut_types)
            flash('Tipos de lapidação atualizados com sucesso!', 'success')
            return redirect(url_for('crm_cut_types'))
            
        except Exception as e:
            print(f"Error saving cut types: {e}")
            flash('Erro ao salvar tipos de lapidação', 'error')
    
    cut_types = get_cut_types()
    return render_template('crm_cut_types.html', cut_types=cut_types)

# ============================================
# Admin Role Management Routes
# ============================================

@app.route('/crm/roles')
@require_admin
def crm_roles():
    """Página para gerenciar roles dos usuários."""
    customers = get_all_customers()
    return render_template('crm_roles.html', 
                         customers=customers,
                         firebase_config=get_firebase_config())

@app.route('/api/admin/set-role', methods=['POST'])
@require_admin_api
def set_user_role():
    """Set custom claims for a user (admin only)."""
    try:
        data = request.get_json()
        email = data.get('email')
        is_admin = data.get('is_admin', False)
        
        if not email:
            return jsonify({'error': 'Email is required'}), 400
        
        # Get user by email
        user = firebase_auth.get_user_by_email(email)
        
        # Set custom claims
        firebase_auth.set_custom_user_claims(user.uid, {'admin': is_admin})
        
        return jsonify({
            'success': True,
            'message': f'Role atualizada para {email}'
        }), 200
        
    except firebase_auth.UserNotFoundError:
        return jsonify({'error': 'Usuário não encontrado'}), 404
    except Exception as e:
        print(f"Error setting role: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/auth/check-admin')
@require_auth
def check_admin():
    """Check if current user is admin."""
    try:
        user_record = firebase_auth.get_user(request.user_id)
        custom_claims = user_record.custom_claims or {}
        is_admin = custom_claims.get('admin', False)
        
        return jsonify({'is_admin': is_admin}), 200
    except Exception as e:
        print(f"Error checking admin: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/auth/check-admin-by-email')
@require_admin_api
def check_admin_by_email():
    """Check if a user is admin by email (admin only)."""
    try:
        email = request.args.get('email')
        if not email:
            return jsonify({'error': 'Email required'}), 400
        
        # Get target user
        try:
            target_user = firebase_auth.get_user_by_email(email)
            target_claims = target_user.custom_claims or {}
            is_admin = target_claims.get('admin', False)
            
            return jsonify({'is_admin': is_admin}), 200
        except firebase_auth.UserNotFoundError:
            # User not in Firebase Auth yet
            return jsonify({'is_admin': False}), 200
            
    except Exception as e:
        print(f"Error checking admin by email: {e}")
        return jsonify({'error': str(e)}), 500

# ============================================
# Newsletter Routes
# ============================================

@app.route('/api/newsletter/subscribe', methods=['POST'])
@limiter.limit("5/hour")
def api_newsletter_subscribe():
    """API endpoint to subscribe to newsletter."""
    try:
        data = request.get_json()
        email = data.get('email', '').strip().lower()
        
        if not email:
            return jsonify({'error': 'E-mail é obrigatório'}), 400
        
        # Basic email validation
        import re
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(email_pattern, email):
            return jsonify({'error': 'E-mail inválido'}), 400
        
        result, status_code = add_newsletter_subscriber(email)
        return jsonify(result), status_code
        
    except Exception as e:
        print(f"Error in newsletter subscription: {e}")
        return jsonify({'error': 'Erro ao processar inscrição'}), 500

@app.route('/api/solicitacoes/submit', methods=['POST'])
@limiter.limit("10/hour")
def api_solicitacoes_submit():
    """API endpoint to submit a 'O que procura?' request."""
    import re
    try:
        data = request.get_json(silent=True) or {}
        name        = (data.get('name') or '').strip()
        email       = (data.get('email') or '').strip().lower()
        phone       = (data.get('phone') or '').strip()
        description = (data.get('description') or '').strip()

        if not name:
            return jsonify({'error': 'Nome é obrigatório'}), 400
        if not email or not re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', email):
            return jsonify({'error': 'E-mail inválido'}), 400
        if not phone:
            return jsonify({'error': 'Telefone é obrigatório'}), 400
        if not description:
            return jsonify({'error': 'Descrição é obrigatória'}), 400

        # Create or locate customer
        customer_id = None
        try:
            existing = get_customer_by_email(email)
            if existing:
                customer_id = existing.get('id') or existing.get('firestore_id')
            else:
                created = create_customer({
                    'name':  name,
                    'email': email,
                    'phone': phone,
                    'source': 'solicitacao',
                })
                customer_id = created.get('id') if created else None
        except Exception as ce:
            print(f"Customer upsert error in solicitacao: {ce}")

        create_solicitacao(
            name=name,
            email=email,
            phone=phone,
            description=description,
            customer_id=customer_id,
        )
        return jsonify({'ok': True}), 200

    except Exception as e:
        print(f"Error in solicitacao submit: {e}")
        return jsonify({'error': 'Erro ao processar solicitação'}), 500


@app.route('/crm/newsletter')
@require_admin
def crm_newsletter():
    """CRM page to manage newsletter subscribers."""
    subscribers = get_all_newsletter_subscribers()
    return render_template('crm_newsletter.html', subscribers=subscribers)

@app.route('/crm/newsletter/delete/<subscriber_id>', methods=['POST'])
@require_admin
def crm_delete_subscriber(subscriber_id):
    """Delete a newsletter subscriber."""
    if delete_newsletter_subscriber(subscriber_id):
        flash('Inscrito removido com sucesso!', 'success')
    else:
        flash('Erro ao remover inscrito.', 'error')
    return redirect(url_for('crm_newsletter'))

@app.route('/crm/newsletter/export')
@require_admin
def crm_export_newsletter():
    """Export newsletter subscribers to CSV."""
    try:
        import csv
        from io import StringIO
        from flask import make_response
        
        subscribers = get_all_newsletter_subscribers()
        
        # Create CSV in memory
        si = StringIO()
        writer = csv.writer(si)
        writer.writerow(['E-mail', 'Data de Inscrição', 'Status'])
        
        for subscriber in subscribers:
            writer.writerow([
                subscriber.get('email', ''),
                subscriber.get('subscribed_at', ''),
                'Ativo' if subscriber.get('active', True) else 'Inativo'
            ])
        
        # Create response
        output = make_response(si.getvalue())
        output.headers["Content-Disposition"] = "attachment; filename=newsletter_subscribers.csv"
        output.headers["Content-type"] = "text/csv"
        
        return output
        
    except Exception as e:
        print(f"Error exporting newsletter: {e}")
        flash('Erro ao exportar lista.', 'error')
        return redirect(url_for('crm_newsletter'))

# ============================================
# Shipping / Frete Routes
# ============================================

@app.route('/crm/frete', methods=['GET', 'POST'])
@require_admin
def crm_frete():
    """CRM page to manage shipping prices per state and free-shipping threshold."""
    from database import DEFAULT_STATE_SHIPPING, DEFAULT_FREE_SHIPPING_MINIMUM

    if request.method == 'POST':
        try:
            free_min = request.form.get('free_shipping_minimum', DEFAULT_FREE_SHIPPING_MINIMUM)
            try:
                free_min = float(str(free_min).replace(',', '.'))
                if free_min < 0:
                    raise ValueError
            except (ValueError, TypeError):
                flash('Valor mínimo de frete grátis inválido.', 'error')
                return redirect(url_for('crm_frete'))

            states_data = {}
            for state in DEFAULT_STATE_SHIPPING:
                raw = request.form.get(f'state_{state}', '0')
                try:
                    val = float(str(raw).replace(',', '.'))
                    if val < 0:
                        val = 0.0
                except (ValueError, TypeError):
                    val = 0.0
                states_data[state] = val

            if save_shipping_settings(free_min, states_data):
                flash('Configurações de frete salvas com sucesso!', 'success')
            else:
                flash('Erro ao salvar configurações de frete.', 'error')
            return redirect(url_for('crm_frete'))

        except Exception as e:
            print(f"Error saving shipping settings: {e}")
            flash('Erro inesperado ao salvar frete.', 'error')
            return redirect(url_for('crm_frete'))

    shipping = get_shipping_settings()
    return render_template('crm_frete.html',
                           shipping=shipping,
                           default_states=DEFAULT_STATE_SHIPPING)


@app.route('/api/shipping/calculate', methods=['GET'])
def api_shipping_calculate():
    """
    Public API: calculate shipping cost for a given state and cart total.
    Query params: state (e.g. SP), total (cart total in BRL)
    Returns: { shipping_cost, free_shipping, minimum }
    """
    state = request.args.get('state', '').strip().upper()
    try:
        total = float(request.args.get('total', '0') or '0')
    except (ValueError, TypeError):
        total = 0.0

    shipping_config = get_shipping_settings()
    minimum = float(shipping_config.get('free_shipping_minimum', 300.0))
    free = total >= minimum
    cost = 0.0 if free else float(shipping_config.get('states', {}).get(state, 0.0))

    return jsonify({
        'shipping_cost': cost,
        'free_shipping': free,
        'minimum': minimum,
        'state': state,
    })


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000) 