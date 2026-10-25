"""
Tipos de produto da Katsumi Joias.

O catálogo é "jewelry-first": a maior parte dos produtos são joias (anéis, colares,
brincos...) com preço fixo em reais. Gemas avulsas continuam suportadas com o modelo
legado (preço em US$ por quilate × peso × markup × câmbio).

Campos de uma joia (product_type == 'joia'):
    price_brl            preço fixo da peça em R$ (antes do desconto)
    metal                ouro | prata | platina | outro
    metal_color          amarelo | branco | rose | negro
    gold_karat           18k | 14k | 10k | 925 | 950 ...
    metal_weight_g       peso do metal em gramas
    main_stone           pedra principal (texto livre, ex. "Diamante")
    stone_count          quantidade de pedras cravadas
    stone_carat_weight   peso total das pedras (ct) — informativo, não entra no preço
    ring_size            aro / tamanho
    stock_qty            unidades em estoque (decrementa a cada venda)
    made_to_order        peça sob encomenda (prazo em production_days)

Produtos sem product_type são tratados como gema (dados legados).
"""

PRODUCT_TYPE_JOIA = 'joia'
PRODUCT_TYPE_GEMA = 'gema'
PRODUCT_TYPES = (PRODUCT_TYPE_JOIA, PRODUCT_TYPE_GEMA)
PRODUCT_TYPE_LABELS = {PRODUCT_TYPE_JOIA: 'Joia', PRODUCT_TYPE_GEMA: 'Gema'}

METALS = [
    ('ouro', 'Ouro'),
    ('prata', 'Prata'),
    ('platina', 'Platina'),
    ('outro', 'Outro'),
]
METAL_COLORS = [
    ('amarelo', 'Amarelo'),
    ('branco', 'Branco'),
    ('rose', 'Rosé'),
    ('negro', 'Negro (ródio negro)'),
]
METAL_PURITIES = ['18k', '14k', '10k', '925', '950']

METAL_LABELS = dict(METALS)
METAL_COLOR_LABELS = dict(METAL_COLORS)

JOIA_FIELDS = (
    'price_brl', 'metal', 'metal_color', 'gold_karat', 'metal_weight_g', 'main_stone',
    'stone_count', 'stone_carat_weight', 'ring_size', 'stock_qty', 'made_to_order',
    'production_days',
)


def product_kind(product):
    if not product:
        return PRODUCT_TYPE_GEMA
    return PRODUCT_TYPE_JOIA if product.get('product_type') == PRODUCT_TYPE_JOIA else PRODUCT_TYPE_GEMA


def is_joia(product):
    return product_kind(product) == PRODUCT_TYPE_JOIA


def parse_number(raw, default=0.0):
    """Lê números em formato BR ('1.234,56'), US ('1234.56') ou com unidade ('2,5 ct')."""
    if raw is None or raw == '':
        return default
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().lower()
    for token in ('r$', 'us$', '$', 'ct', 'g', 'mm'):
        text = text.replace(token, '')
    text = text.strip().split()[0] if text.strip() else ''
    if not text:
        return default
    if ',' in text and '.' in text:
        text = text.replace('.', '').replace(',', '.')
    elif ',' in text:
        text = text.replace(',', '.')
    try:
        return float(text)
    except (ValueError, TypeError):
        return default


def price_brl(product):
    return max(0.0, parse_number(product.get('price_brl'), 0.0))


def stock_qty(product):
    """Unidades disponíveis de uma joia (mínimo 1 quando não informado)."""
    raw = product.get('stock_qty')
    if raw in (None, ''):
        return 1
    try:
        return max(0, int(float(raw)))
    except (ValueError, TypeError):
        return 1


def discount_factor(product):
    discount = parse_number(product.get('discount'), 0.0)
    if discount <= 0:
        return 1.0
    return max(0.0, 1 - min(discount, 100) / 100)


def carat_weight(product):
    return parse_number(product.get('carat_weight'), 0.0)


def base_total_brl(product, exchange_rate, markup_factor=1.0):
    """Preço cheio (antes do desconto) em R$."""
    if is_joia(product):
        return price_brl(product)
    price_usd = parse_number(product.get('price'), 0.0) * (markup_factor or 1.0)
    carat = carat_weight(product) or 1.0
    return price_usd * carat * (exchange_rate or 0.0)


def final_total_brl(product, exchange_rate, markup_factor=1.0):
    """Preço final em R$ (com desconto)."""
    return base_total_brl(product, exchange_rate, markup_factor) * discount_factor(product)


def base_total_usd(product, exchange_rate, markup_factor=1.0):
    """Preço cheio em US$ (joias são convertidas pelo câmbio do dia)."""
    if is_joia(product):
        return price_brl(product) / exchange_rate if exchange_rate else 0.0
    price_usd = parse_number(product.get('price'), 0.0) * (markup_factor or 1.0)
    return price_usd * (carat_weight(product) or 1.0)


def apply_display_price(product, exchange_rate, markup_factor=1.0):
    """
    Normaliza o campo `price` para exibição na loja (idempotente).

    Gemas: price = US$/ct já com markup.
    Joias: price = equivalente em US$ da peça inteira (price_brl / câmbio). O front
           usa `price_brl` diretamente quando disponível, então o valor em reais é exato.
    """
    if product.get('_display_priced'):
        return product
    if is_joia(product):
        brl = price_brl(product)
        product['price'] = round(brl / exchange_rate, 6) if exchange_rate else 0.0
        product['price_brl'] = brl
        product['carat_weight'] = ''
    elif markup_factor and markup_factor != 1.0:
        product['price'] = round(parse_number(product.get('price'), 0.0) * markup_factor, 2)
    product['product_type'] = product_kind(product)
    product['_display_priced'] = True
    return product


def metal_label(product):
    """Ex.: 'Ouro 18k amarelo', 'Prata 925', 'Platina 950'."""
    metal = METAL_LABELS.get(product.get('metal') or '', product.get('metal') or '')
    purity = (product.get('gold_karat') or '').strip()
    color = METAL_COLOR_LABELS.get(product.get('metal_color') or '', '')
    parts = [p for p in (metal, purity) if p]
    label = ' '.join(parts)
    if color and product.get('metal') in ('ouro', 'outro', ''):
        label = f'{label} {color.lower()}'.strip()
    return label


def jewelry_summary(product):
    """Linha curta usada em cards/carrinho: 'Ouro 18k amarelo · Esmeralda'."""
    parts = [metal_label(product)]
    stone = (product.get('main_stone') or '').strip()
    if stone:
        parts.append(stone)
    return ' · '.join(p for p in parts if p)


def jewelry_specs(product):
    """Lista (rótulo, valor) com as características técnicas de uma joia."""
    specs = []
    if product.get('category'):
        specs.append(('Tipo', product.get('category')))
    metal = metal_label(product)
    if metal:
        specs.append(('Metal', metal))
    weight_g = parse_number(product.get('metal_weight_g'), 0.0)
    if weight_g:
        specs.append(('Peso do metal', f'{weight_g:.2f} g'.replace('.', ',')))
    if product.get('main_stone'):
        specs.append(('Pedra principal', product.get('main_stone')))
    if product.get('cut'):
        specs.append(('Lapidação', product.get('cut')))
    count = int(parse_number(product.get('stone_count'), 0))
    if count:
        specs.append(('Quantidade de pedras', str(count)))
    stone_ct = parse_number(product.get('stone_carat_weight'), 0.0)
    if stone_ct:
        specs.append(('Peso total das pedras', f'{stone_ct:.2f} ct'.replace('.', ',')))
    if product.get('ring_size'):
        specs.append(('Aro / tamanho', str(product.get('ring_size'))))
    dims = [str(product.get(f) or '').strip() for f in ('comprimento', 'largura', 'altura')]
    dims = [d for d in dims if d]
    if dims:
        specs.append(('Dimensões', ' × '.join(dims) + ' mm'))
    if product.get('made_to_order'):
        days = int(parse_number(product.get('production_days'), 0))
        specs.append(('Produção', f'Sob encomenda · {days} dias úteis' if days else 'Sob encomenda'))
    return specs


# ---------------------------------------------------------------------------
# Categorias (settings/product_categories)
# ---------------------------------------------------------------------------

def category_kind(cat_data):
    if isinstance(cat_data, dict) and cat_data.get('kind') == PRODUCT_TYPE_JOIA:
        return PRODUCT_TYPE_JOIA
    return PRODUCT_TYPE_GEMA


def normalize_category(cat_data):
    if not isinstance(cat_data, dict):
        cat_data = {}
    cat_data.setdefault('description', '')
    cat_data.setdefault('variants', [])
    cat_data['kind'] = category_kind(cat_data)
    cat_data.setdefault('image_url', '')
    cat_data.setdefault('show_in_nav', True)
    try:
        cat_data['order'] = int(cat_data.get('order', 100))
    except (ValueError, TypeError):
        cat_data['order'] = 100
    return cat_data


def categories_of_kind(categories, kind):
    """Retorna [(nome, dados)] de um tipo, ordenados por `order` e nome."""
    items = [(name, normalize_category(data)) for name, data in (categories or {}).items()]
    items = [(n, d) for n, d in items if d['kind'] == kind]
    items.sort(key=lambda it: (it[1].get('order', 100), it[0].lower()))
    return items
