"""SEO helpers — titles, meta, JSON-LD, canonical URLs, slugs."""
import json
import re
import unicodedata

BRAND = 'Katsumi Joias'
SITE_NAME = BRAND

INSTAGRAM_URL = ''
YOUTUBE_URL = ''

HOMEPAGE_TITLE = (
    'Katsumi Joias | Gemas Brasileiras Lapidadas desde 1942 | '
    'Esmeraldas, Turmalinas e Mais'
)
HOMEPAGE_DESCRIPTION = (
    'Compre pedras preciosas brasileiras lapidadas à mão desde 1942. '
    'Turmalinas, Esmeraldas, Tanzanitas e mais — curadoria gemológica '
    'direta de Minas Gerais.'
)
BANNER_ALT = (
    'Gemas preciosas brasileiras lapidadas à mão pela Katsumi Joias desde 1942'
)

# H1 / title labels per category (fallback: plural + "Brasileiras")
CATEGORY_SEO = {
    'Turmalina': {
        'h1': 'Turmalinas Brasileiras',
        'title_label': 'Turmalinas Brasileiras',
        'description': (
            'Turmalinas brasileiras lapidadas à mão, direto de Minas Gerais. '
            'Paraíbas, verdes e raras — peças únicas com laudo gemológico disponível.'
        ),
    },
    'Esmeralda': {
        'h1': 'Esmeraldas de Minas Gerais',
        'title_label': 'Esmeraldas de Minas Gerais',
        'description': (
            'Esmeraldas brasileiras selecionadas e lapidadas pela Katsumi Joias. '
            'Curadoria gemológica desde 1942 — peças únicas para joalheiros e colecionadores.'
        ),
    },
    'Tanzanita': {
        'h1': 'Tanzanitas Brasileiras',
        'title_label': 'Tanzanitas',
        'description': (
            'Tanzanitas lapidadas com curadoria Katsumi Joias. '
            'Gemas únicas com certificação gemológica disponível.'
        ),
    },
    'Topázio': {
        'h1': 'Topázios Brasileiros',
        'title_label': 'Topázios Brasileiros',
        'description': (
            'Topázios brasileiros lapidados à mão. '
            'Peças únicas selecionadas em Minas Gerais desde 1942.'
        ),
    },
    'Kunzita': {
        'h1': 'Kunzitas Brasileiras',
        'title_label': 'Kunzitas',
        'description': (
            'Kunzitas brasileiras com lapidação própria Katsumi Joias. '
            'Gemas raras e peças únicas para joias exclusivas.'
        ),
    },
    'Morganita': {
        'h1': 'Morganitas Brasileiras',
        'title_label': 'Morganitas',
        'description': (
            'Morganitas lapidadas com tradição gemológica desde 1942. '
            'Peças únicas direto de Minas Gerais.'
        ),
    },
}

CATALOG_TITLE = 'Gemas Brasileiras Lapidadas | Catálogo | Katsumi Joias'
CATALOG_DESCRIPTION = (
    'Catálogo de gemas brasileiras lapidadas à mão. '
    'Turmalinas, esmeraldas, tanzanitas e mais — peças únicas Katsumi Joias.'
)


def category_slug(name):
    """Turmalina -> turmalina, Topázio -> topazio."""
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


def category_path(category_name):
    slug = category_slug(category_name)
    return f'/pedras/{slug}' if slug else '/products'


def category_url(site_url, category_name, page=None):
    """Absolute canonical URL for a category listing."""
    base = site_url.rstrip('/') + category_path(category_name)
    if page and int(page) > 1:
        return f'{base}?page={int(page)}'
    return base


def _category_meta(category):
    if not category:
        return {'h1': 'Catálogo de Gemas', 'title_label': None, 'description': CATALOG_DESCRIPTION}
    cfg = CATEGORY_SEO.get(category)
    if cfg:
        return cfg
    label = category if category.endswith('s') else f'{category}s Brasileiras'
    return {
        'h1': label,
        'title_label': label,
        'description': (
            f'{label} lapidadas à mão pela Katsumi Joias. '
            f'Peças únicas de Minas Gerais com laudo gemológico independente disponível.'
        ),
    }


def category_page_title(category):
    meta = _category_meta(category)
    if not category:
        return CATALOG_TITLE
    label = meta['title_label'] or meta['h1']
    return f'{label} à Venda | Pedras Lapidadas | {BRAND}'


def category_h1(category):
    if not category:
        return 'Catálogo de Gemas'
    return _category_meta(category)['h1']


def category_meta_description(category):
    if not category:
        return CATALOG_DESCRIPTION
    return _category_meta(category)['description']


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
    name = product.get('name') or 'Gema'
    carat = format_carat_display(product)
    if carat:
        return f'{name} — {carat} | {BRAND}'
    return f'{name} | {BRAND}'


def product_meta_description(product):
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

    intro = ', '.join(body_parts) if body_parts else (product.get('name') or 'Gema brasileira')
    return (
        f'{intro}. Peça única com laudo gemológico independente disponível. '
        f'{BRAND} — gemas de Minas Gerais desde 1942.'
    )


def product_image_alt(product):
    """Descriptive alt text for product images."""
    bits = []
    cat = (product.get('category') or '').strip()
    color = (product.get('color') or '').strip()
    if cat:
        bits.append(f'{cat}{" " + color.lower() if color else ""}'.strip())
    name = (product.get('name') or 'Gema').strip()
    if name and name not in ' '.join(bits):
        bits.append(name)
    carat = format_carat_display(product)
    if carat:
        bits.append(carat.replace(' ct', ' quilates'))
    bits.append('lapidação própria Katsumi Joias')
    return ' '.join(bits)


def product_price_brl(product, exchange_rate_data, price_markup_rate=0):
    """Total price in BRL for schema.org (after markup and discount)."""
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
        'name': product.get('name') or 'Gema',
        'description': product_meta_description(product),
        'brand': {'@type': 'Brand', 'name': BRAND},
        'image': product_absolute_image(product, site_url),
    }
    if price_brl is not None:
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
    return {
        '@context': 'https://schema.org',
        '@type': 'Organization',
        'name': BRAND,
        'url': site_url.rstrip('/'),
        'logo': f'{site_url.rstrip("/")}/static/images/logo.png',
        'foundingDate': '1942',
        'sameAs': [INSTAGRAM_URL, YOUTUBE_URL],
    }


def json_ld_script(data):
    if isinstance(data, list):
        payload = data
    else:
        payload = data
    return json.dumps(payload, ensure_ascii=False)


def catalog_canonical(site_url, category=None, page=1):
    """Canonical URL for catalog — prefers /pedras/slug when filtered by category."""
    page = int(page or 1)
    if category:
        return category_url(site_url, category, page if page > 1 else None)
    if page > 1:
        return f'{site_url.rstrip("/")}/products?page={page}'
    return f'{site_url.rstrip("/")}/products'
