"""SEO helpers — titles, meta, JSON-LD, canonical URLs, slugs."""
import json
import re
import unicodedata

import product_types

BRAND = 'Katsumi Joias'
SITE_NAME = BRAND
TAGLINE = 'A arte de transformar histórias em joias eternas'

INSTAGRAM_URL = ''
YOUTUBE_URL = ''

HOMEPAGE_TITLE = (
    'Katsumi Joias | Joias sob medida, diamantes e pedras preciosas'
)
HOMEPAGE_DESCRIPTION = (
    'KATSUMI — a arte de transformar histórias em joias eternas. Anéis, alianças, '
    'colares e brincos em ouro e pedras preciosas, joias sob medida com modelagem 3D '
    'e atendimento personalizado.'
)
BANNER_ALT = 'Joias Katsumi em ouro com diamantes e pedras preciosas'

# H1 / title labels por categoria (fallback gerado a partir do nome)
CATEGORY_SEO = {
    'Anéis': {
        'h1': 'Anéis',
        'title_label': 'Anéis em Ouro e Pedras Preciosas',
        'description': 'Anéis Katsumi em ouro 18k com diamantes e pedras preciosas — solitários, aparadores e peças autorais.',
    },
    'Alianças': {
        'h1': 'Alianças',
        'title_label': 'Alianças de Casamento e Noivado',
        'description': 'Alianças em ouro 18k, lisas ou cravejadas, feitas com acabamento artesanal e personalizáveis.',
    },
    'Colares': {
        'h1': 'Colares e Pingentes',
        'title_label': 'Colares e Pingentes',
        'description': 'Colares e pingentes Katsumi em ouro e pedras preciosas, do delicado ao statement.',
    },
    'Brincos': {
        'h1': 'Brincos',
        'title_label': 'Brincos em Ouro e Pedras',
        'description': 'Brincos Katsumi — pontos de luz, argolas e peças com pedras preciosas.',
    },
    'Pulseiras': {
        'h1': 'Pulseiras',
        'title_label': 'Pulseiras e Rivieras',
        'description': 'Pulseiras e rivieras em ouro com diamantes e pedras preciosas.',
    },
}

CATALOG_TITLE = f'Joias | Catálogo | {BRAND}'
CATALOG_DESCRIPTION = (
    'Catálogo Katsumi Joias: anéis, alianças, colares, brincos e pulseiras em ouro, '
    'com diamantes e pedras preciosas.'
)
GEMS_CATALOG_TITLE = f'Pedras Preciosas | Catálogo | {BRAND}'
GEMS_CATALOG_DESCRIPTION = (
    'Diamantes e pedras preciosas selecionadas pela Katsumi Joias — gemas avulsas '
    'para criar a sua joia sob medida.'
)

# Resolve o tipo (joia/gema) de uma categoria pelo nome — registrado pelo app
_category_kind_resolver = None


def set_category_kind_resolver(fn):
    global _category_kind_resolver
    _category_kind_resolver = fn


def _kind_of_category(category_name):
    if _category_kind_resolver and category_name:
        try:
            return _category_kind_resolver(category_name)
        except Exception:
            return None
    return None


def category_slug(name):
    """Turmalina -> turmalina, Anéis -> aneis."""
    if not name:
        return ''
    s = unicodedata.normalize('NFKD', str(name))
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    return s.strip('-')


def resolve_category_from_slug(slug, category_names):
    """Match slug to a category name from the catalog."""
    slug = (slug or '').lower().strip()
    if not slug:
        return None
    for name in category_names:
        if category_slug(name) == slug:
            return name
    return None


def category_path(category_name, kind=None):
    """/joias/<slug> para tipos de joia, /pedras/<slug> para gemas."""
    slug = category_slug(category_name)
    if not slug:
        return '/joias'
    kind = kind or _kind_of_category(category_name) or product_types.PRODUCT_TYPE_GEMA
    prefix = 'joias' if kind == product_types.PRODUCT_TYPE_JOIA else 'pedras'
    return f'/{prefix}/{slug}'


def category_url(site_url, category_name, page=None, kind=None):
    """Absolute canonical URL for a category listing."""
    base = site_url.rstrip('/') + category_path(category_name, kind)
    if page and int(page) > 1:
        return f'{base}?page={int(page)}'
    return base


def _category_meta(category, kind=None):
    if not category:
        if kind == product_types.PRODUCT_TYPE_GEMA:
            return {'h1': 'Pedras Preciosas', 'title_label': None, 'description': GEMS_CATALOG_DESCRIPTION}
        return {'h1': 'Joias', 'title_label': None, 'description': CATALOG_DESCRIPTION}
    cfg = CATEGORY_SEO.get(category)
    if cfg:
        return cfg
    kind = kind or _kind_of_category(category)
    if kind == product_types.PRODUCT_TYPE_JOIA:
        return {
            'h1': category,
            'title_label': category,
            'description': f'{category} Katsumi Joias — design exclusivo, ouro e pedras selecionadas, acabamento artesanal.',
        }
    return {
        'h1': category,
        'title_label': category,
        'description': f'{category} selecionadas pela Katsumi Joias — pedras avulsas para joias sob medida.',
    }


def category_page_title(category, kind=None):
    if not category:
        return GEMS_CATALOG_TITLE if kind == product_types.PRODUCT_TYPE_GEMA else CATALOG_TITLE
    meta = _category_meta(category, kind)
    label = meta['title_label'] or meta['h1']
    return f'{label} | {BRAND}'


def category_h1(category, kind=None):
    return _category_meta(category, kind)['h1']


def category_meta_description(category, kind=None):
    return _category_meta(category, kind)['description']


def format_carat_display(product):
    """Return carat string like '9,28 ct' or None."""
    raw = str(product.get('carat_weight') or '').strip()
    if not raw:
        return None
    num = raw.split()[0] if ' ' in raw else raw
    try:
        val = float(num.replace(',', '.'))
        if val == int(val):
            formatted = f'{int(val)}'
        else:
            formatted = f'{val:.2f}'.replace('.', ',')
    except (ValueError, TypeError):
        if 'ct' in raw.lower():
            return raw
        return f'{raw} ct'
    return f'{formatted} ct'


def product_page_title(product):
    name = product.get('name') or 'Joia'
    if product_types.is_joia(product):
        return f'{name} | {BRAND}'
    carat = format_carat_display(product)
    if carat:
        return f'{name} — {carat} | {BRAND}'
    return f'{name} | {BRAND}'


def product_meta_description(product):
    if product_types.is_joia(product):
        summary = product_types.jewelry_summary(product)
        cat = (product.get('category') or 'Joia').strip()
        intro = f'{product.get("name") or cat}' + (f' — {summary}' if summary else '')
        return f'{intro}. Design exclusivo e acabamento artesanal {BRAND}.'

    body_parts = []
    cat = (product.get('category') or '').strip()
    color = (product.get('color') or '').strip()
    cut = (product.get('cut') or '').strip()
    carat = format_carat_display(product)
    if cat:
        desc = f'{cat} {color.lower()}'.strip() if color else cat
        body_parts.append(desc)
    if cut:
        body_parts.append(f'lapidação {cut}')
    if carat:
        body_parts.append(carat)
    intro = ', '.join(body_parts) if body_parts else (product.get('name') or 'Pedra preciosa')
    return f'{intro}. Pedra selecionada pela {BRAND}, ideal para a sua joia sob medida.'


def product_image_alt(product):
    """Descriptive alt text for product images."""
    name = (product.get('name') or '').strip()
    if product_types.is_joia(product):
        summary = product_types.jewelry_summary(product)
        return ' — '.join(p for p in (name or product.get('category') or 'Joia', summary) if p) + f' | {BRAND}'
    bits = []
    cat = (product.get('category') or '').strip()
    color = (product.get('color') or '').strip()
    if cat:
        bits.append(f'{cat}{" " + color.lower() if color else ""}'.strip())
    if name and name not in ' '.join(bits):
        bits.append(name)
    carat = format_carat_display(product)
    if carat:
        bits.append(carat.replace(' ct', ' quilates'))
    bits.append(BRAND)
    return ' '.join(bits)


def product_price_brl(product, exchange_rate_data, price_markup_rate=0):
    """Total price in BRL for schema.org (after markup and discount)."""
    if product_types.is_joia(product):
        return round(product_types.final_total_brl(product, 0), 2)
    if not exchange_rate_data or 'rate' not in exchange_rate_data:
        return None
    price_usd = float(product.get('price', 0) or 0)
    if price_markup_rate:
        price_usd *= 1 + float(price_markup_rate) / 100
    cw_raw = str(product.get('carat_weight', '') or '').strip()
    if cw_raw:
        cw_num = cw_raw.split()[0] if ' ' in cw_raw else cw_raw
        try:
            carat = float(cw_num.replace(',', '.'))
        except (ValueError, TypeError):
            carat = 1
    else:
        carat = 1
    total_usd = price_usd * carat
    discount = float(product.get('discount') or 0)
    if discount:
        total_usd *= 1 - discount / 100
    return round(total_usd * float(exchange_rate_data['rate']), 2)


def product_absolute_image(product, site_url):
    url = product.get('main_image_url') or ''
    if not url:
        return f'{site_url.rstrip("/")}/static/images/logo.png'
    if url.startswith('http'):
        return url
    return f'{site_url.rstrip("/")}{url}' if url.startswith('/') else url


def build_product_json_ld(product, site_url, exchange_rate_data=None, price_markup_rate=0):
    price_brl = product_price_brl(product, exchange_rate_data, price_markup_rate)
    availability = (
        'https://schema.org/InStock'
        if product.get('active', False)
        else 'https://schema.org/OutOfStock'
    )
    data = {
        '@context': 'https://schema.org',
        '@type': 'Product',
        'name': product.get('name') or 'Joia',
        'description': product_meta_description(product),
        'brand': {'@type': 'Brand', 'name': BRAND},
        'image': product_absolute_image(product, site_url),
    }
    if product.get('sku'):
        data['sku'] = str(product.get('sku'))
    if price_brl:
        data['offers'] = {
            '@type': 'Offer',
            'price': f'{price_brl:.2f}',
            'priceCurrency': 'BRL',
            'availability': availability,
            'url': f'{site_url.rstrip("/")}/product/{product.get("id", "")}',
        }
    return data


def build_breadcrumb_json_ld(items):
    """items: list of {name, url}"""
    elements = []
    for i, item in enumerate(items, start=1):
        el = {
            '@type': 'ListItem',
            'position': i,
            'name': item['name'],
        }
        if item.get('url'):
            el['item'] = item['url']
        elements.append(el)
    return {
        '@context': 'https://schema.org',
        '@type': 'BreadcrumbList',
        'itemListElement': elements,
    }


def build_organization_json_ld(site_url):
    same_as = [u for u in (INSTAGRAM_URL, YOUTUBE_URL) if u]
    data = {
        '@context': 'https://schema.org',
        '@type': 'JewelryStore',
        'name': BRAND,
        'slogan': TAGLINE,
        'url': site_url.rstrip('/'),
        'logo': f'{site_url.rstrip("/")}/static/images/logo.png',
    }
    if same_as:
        data['sameAs'] = same_as
    return data


def json_ld_script(data):
    return json.dumps(data, ensure_ascii=False)


def catalog_canonical(site_url, category=None, page=1, kind=None):
    """Canonical URL do catálogo — /joias, /pedras ou a página da categoria."""
    page = int(page or 1)
    site = site_url.rstrip('/')
    if category:
        return category_url(site, category, page if page > 1 else None, kind)
    base = f'{site}/pedras' if kind == product_types.PRODUCT_TYPE_GEMA else f'{site}/joias'
    if page > 1:
        return f'{base}?page={page}'
    return base
