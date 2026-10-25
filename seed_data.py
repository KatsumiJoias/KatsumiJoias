"""
Popula o Firestore com dados de exemplo para todas as coleções usadas pelo site e pelo CRM.

Uso:
    python seed_data.py                # cria/atualiza os dados de exemplo (pede confirmação)
    python seed_data.py --yes          # sem confirmação
    python seed_data.py --upload       # envia as imagens geradas para o Firebase Storage
    python seed_data.py --wipe         # remove tudo que foi criado por este script

Todo documento criado recebe `seed: True` e um ID fixo com prefixo `seed-`, então rodar de
novo apenas sobrescreve os mesmos registros. Configurações existentes (cotação, lapidações,
categorias) nunca são sobrescritas: o script só acrescenta o que estiver faltando.

Sem `--upload`, as imagens ficam em static/seed/ e só aparecem quando o site roda localmente.
"""
import argparse
import io
import math
import os
import sys
from datetime import timedelta

from PIL import Image, ImageDraw, ImageFont

from database import (
    db, bucket, utc_now, get_setting, update_setting, upload_bytes,
    SOURCE_INTERNAL, SOURCE_WEBSITE,
)
import jewelry_designer as jd

SEED_PREFIX = 'seed-'
SEED_COLLECTIONS = (
    'products', 'customers', 'sellers', 'orders', 'solicitacoes', 'newsletter',
    'activity_logs', 'jewelry_designs', 'quotes',
)
LOCAL_IMAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'seed')
STORAGE_PREFIX = 'seed/'

# ---------------------------------------------------------------------------
# Catálogo de exemplo
# ---------------------------------------------------------------------------

JEWELRY_CATEGORIES = {
    'Anéis': {'description': 'Anéis e solitários feitos à mão.', 'order': 10},
    'Alianças': {'description': 'Alianças para celebrar o para sempre.', 'order': 20},
    'Colares': {'description': 'Colares e pingentes com pedras preciosas.', 'order': 30},
    'Brincos': {'description': 'Brincos delicados e statement.', 'order': 40},
    'Pulseiras': {'description': 'Pulseiras e braceletes.', 'order': 50},
}
GEM_CATEGORIES = {
    'Esmeraldas': {'description': 'Esmeraldas naturais selecionadas.', 'order': 110},
    'Turmalinas': {'description': 'Turmalinas de cores vibrantes.', 'order': 120},
}
DEFAULT_CUT_TYPES = ['Brilhante', 'Esmeralda', 'Oval', 'Princesa', 'Cushion', 'Pera', 'Marquise',
                     'Coração', 'Radiante', 'Asscher', 'Baguete']

METAL_RGB = {
    'amarelo': (212, 175, 55),
    'branco': (205, 207, 214),
    'rose': (216, 160, 140),
    'prata': (196, 196, 200),
    'platina': (184, 186, 194),
}
STONE_RGB = {
    'Diamante': (236, 242, 250),
    'Esmeralda': (20, 140, 80),
    'Rubi': (175, 20, 50),
    'Safira': (25, 60, 160),
    'Turmalina Paraíba': (40, 200, 200),
    'Morganita': (240, 170, 170),
    'Pérola': (245, 240, 230),
    '': None,
}

JOIAS = [
    # (slug, nome, categoria, metal, cor, pureza, pedra, n_pedras, ct, peso_g, aro, preço, estoque, sob_encomenda, forma)
    ('solitario-diamante', 'Solitário Clássico Diamante', 'Anéis', 'ouro', 'amarelo', '18k', 'Diamante', 1, 0.5, 3.2, '16', 8900.00, 2, False, 'ring'),
    ('anel-esmeralda-halo', 'Anel Esmeralda Halo', 'Anéis', 'ouro', 'branco', '18k', 'Esmeralda', 13, 1.35, 4.1, '15', 12400.00, 1, False, 'ring'),
    ('anel-rubi-trilogia', 'Anel Trilogia Rubi', 'Anéis', 'ouro', 'rose', '18k', 'Rubi', 3, 0.9, 3.8, '14', 9650.00, 0, True, 'ring'),
    ('alianca-classica', 'Par de Alianças Clássicas', 'Alianças', 'ouro', 'amarelo', '18k', '', 0, 0, 8.0, '', 5400.00, 4, False, 'band'),
    ('alianca-cravejada', 'Aliança Cravejada Pavê', 'Alianças', 'ouro', 'branco', '18k', 'Diamante', 20, 0.4, 3.5, '17', 6900.00, 2, False, 'band'),
    ('pingente-gota-safira', 'Pingente Gota Safira', 'Colares', 'ouro', 'branco', '18k', 'Safira', 1, 1.1, 2.6, '', 7300.00, 1, False, 'pendant'),
    ('colar-paraiba', 'Colar Turmalina Paraíba', 'Colares', 'platina', 'branco', '950', 'Turmalina Paraíba', 5, 2.4, 6.2, '', 28900.00, 1, False, 'pendant'),
    ('colar-perola', 'Colar Pérola Essencial', 'Colares', 'prata', '', '925', 'Pérola', 1, 0, 3.0, '', 890.00, 6, False, 'pendant'),
    ('brinco-ponto-luz', 'Brincos Ponto de Luz', 'Brincos', 'ouro', 'amarelo', '18k', 'Diamante', 2, 0.3, 1.4, '', 3900.00, 3, False, 'earrings'),
    ('brinco-morganita', 'Brincos Morganita Halo', 'Brincos', 'ouro', 'rose', '18k', 'Morganita', 2, 1.6, 3.0, '', 6200.00, 1, False, 'earrings'),
    ('pulseira-riviera', 'Pulseira Riviera Diamantes', 'Pulseiras', 'ouro', 'branco', '18k', 'Diamante', 42, 3.0, 11.5, '', 24500.00, 1, False, 'bracelet'),
    ('pulseira-elos', 'Pulseira Elos Ouro', 'Pulseiras', 'ouro', 'amarelo', '18k', '', 0, 0, 9.8, '', 7800.00, 2, False, 'bracelet'),
]

GEMAS = [
    # (slug, nome, categoria, variante, preço US$/ct, ct, lapidação, cor, pureza, mm)
    ('esmeralda-colombia', 'Esmeralda Oval 2,15 ct', 'Esmeraldas', 'Colombiana', 1850.0, '2.15', 'Oval', 'Verde intenso', 'Eye clean', ('9.1', '7.2', '4.8')),
    ('esmeralda-par', 'Par de Esmeraldas Esmeralda-cut', 'Esmeraldas', 'Brasileira', 900.0, '3.40', 'Esmeralda', 'Verde', 'Leve inclusão', ('8.0', '6.0', '4.1')),
    ('turmalina-paraiba', 'Turmalina Paraíba Cushion', 'Turmalinas', 'Paraíba', 4200.0, '1.05', 'Cushion', 'Azul neon', 'VS', ('6.4', '6.2', '4.0')),
    ('turmalina-rosa', 'Turmalina Rosa Pera', 'Turmalinas', 'Rubelita', 320.0, '4.80', 'Pera', 'Rosa', 'VS', ('14.2', '9.1', '6.5')),
]

CUSTOMERS = [
    ('ana', 'Ana Beatriz Souza', 'ana.souza@example.com', '11987654321', '52998224725', 'São Paulo', 'SP', SOURCE_WEBSITE),
    ('carlos', 'Carlos Henrique Lima', 'carlos.lima@example.com', '11976543210', '15350946056', 'Campinas', 'SP', SOURCE_INTERNAL),
    ('mariana', 'Mariana Tanaka', 'mariana.tanaka@example.com', '11965432109', '39053344705', 'São Paulo', 'SP', SOURCE_WEBSITE),
    ('rafael', 'Rafael Oliveira', 'rafael.oliveira@example.com', '21954321098', '11144477735', 'Rio de Janeiro', 'RJ', SOURCE_INTERNAL),
    ('juliana', 'Juliana Costa', 'juliana.costa@example.com', '31943210987', '74682489070', 'Belo Horizonte', 'MG', SOURCE_WEBSITE),
]

SELLERS = [
    ('patricia', 'Patrícia Mendes', 'patricia@katsumijoias.com.br', 5.0),
    ('bruno', 'Bruno Kato', 'bruno@katsumijoias.com.br', 3.5),
]


# ---------------------------------------------------------------------------
# Imagens de exemplo (Pillow)
# ---------------------------------------------------------------------------

def _font(size):
    for name in ('georgia.ttf', 'DejaVuSerif.ttf', 'times.ttf'):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _shade(rgb, factor):
    return tuple(max(0, min(255, int(c * factor))) for c in rgb)


def _draw_stone(draw, cx, cy, r, rgb):
    if not rgb:
        return
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=rgb, outline=_shade(rgb, 0.7), width=max(2, r // 8))
    draw.polygon([(cx, cy - r * 0.6), (cx + r * 0.55, cy), (cx, cy + r * 0.6), (cx - r * 0.55, cy)],
                 outline=_shade(rgb, 1.25) if sum(rgb) < 600 else (180, 190, 205))
    draw.ellipse((cx - r * 0.45, cy - r * 0.55, cx - r * 0.15, cy - r * 0.25), fill=(255, 255, 255))


def render_image(shape, metal_rgb, stone_rgb, caption, size=800):
    img = Image.new('RGB', (size, size), (250, 247, 242))
    draw = ImageDraw.Draw(img)
    for i in range(size // 2, 0, -8):
        tone = 250 - int((size // 2 - i) * 0.04)
        draw.ellipse((size / 2 - i, size / 2 - i, size / 2 + i, size / 2 + i), fill=(tone, tone - 3, tone - 9))

    c = size / 2
    metal_dark = _shade(metal_rgb, 0.75)
    if shape in ('ring', 'band'):
        width = 34 if shape == 'ring' else 60
        draw.ellipse((c - 170, c - 120, c + 170, c + 220), outline=metal_dark, width=width + 6)
        draw.ellipse((c - 167, c - 117, c + 167, c + 217), outline=metal_rgb, width=width)
        if shape == 'ring':
            draw.polygon([(c - 40, c - 120), (c + 40, c - 120), (c + 22, c - 160), (c - 22, c - 160)], fill=metal_rgb)
            _draw_stone(draw, c, c - 185, 58, stone_rgb)
        elif stone_rgb:
            for k in range(9):
                ang = math.radians(200 + k * 17.5)
                _draw_stone(draw, c + 167 * math.cos(ang), c + 50 + 167 * math.sin(ang), 11, stone_rgb)
    elif shape == 'pendant':
        draw.arc((c - 260, c - 480, c + 260, c + 20), 20, 160, fill=metal_rgb, width=6)
        draw.ellipse((c - 14, c - 40, c + 14, c), outline=metal_rgb, width=6)
        draw.polygon([(c, c - 5), (c + 70, c + 110), (c, c + 200), (c - 70, c + 110)], fill=metal_rgb)
        if stone_rgb:
            draw.polygon([(c, c + 12), (c + 56, c + 110), (c, c + 182), (c - 56, c + 110)], fill=stone_rgb)
    elif shape == 'earrings':
        for dx in (-150, 150):
            draw.line((c + dx, c - 200, c + dx, c - 90), fill=metal_rgb, width=8)
            draw.ellipse((c + dx - 70, c - 90, c + dx + 70, c + 50), outline=metal_rgb, width=10)
            _draw_stone(draw, c + dx, c - 20, 50, stone_rgb or metal_rgb)
    elif shape == 'bracelet':
        draw.ellipse((c - 260, c - 150, c + 260, c + 150), outline=metal_dark, width=40)
        draw.ellipse((c - 256, c - 146, c + 256, c + 146), outline=metal_rgb, width=32)
        if stone_rgb:
            for k in range(24):
                ang = math.radians(k * 15)
                _draw_stone(draw, c + 256 * math.cos(ang), c + 146 * math.sin(ang), 12, stone_rgb)
    else:  # gema avulsa
        pts = [(c + 200 * math.cos(math.radians(a)), c + 150 * math.sin(math.radians(a))) for a in range(0, 360, 30)]
        draw.polygon(pts, fill=stone_rgb, outline=_shade(stone_rgb, 0.6))
        for p in pts:
            draw.line((c, c, p[0], p[1]), fill=_shade(stone_rgb, 1.2), width=2)
        draw.ellipse((c - 90, c - 70, c - 30, c - 30), fill=(255, 255, 255))

    font = _font(30)
    draw.text((c, size - 90), caption, fill=(60, 50, 40), font=font, anchor='mm')
    draw.text((c, size - 45), 'KATSUMI', fill=(150, 125, 60), font=_font(22), anchor='mm')

    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=88)
    return buf.getvalue()


def store_image(name, data, upload):
    if upload:
        url = upload_bytes(data, f'{STORAGE_PREFIX}{name}.jpg', content_type='image/jpeg')
        if url:
            return url, f'{STORAGE_PREFIX}{name}.jpg'
        print(f'  ! Falha ao enviar {name}.jpg — usando cópia local.')
    os.makedirs(LOCAL_IMAGE_DIR, exist_ok=True)
    with open(os.path.join(LOCAL_IMAGE_DIR, f'{name}.jpg'), 'wb') as fh:
        fh.write(data)
    return f'/static/seed/{name}.jpg', ''


# ---------------------------------------------------------------------------
# Configurações
# ---------------------------------------------------------------------------

def seed_settings():
    added = []
    current = get_setting('product_categories') or {}
    value = current.get('value') if isinstance(current.get('value'), dict) else {}
    if isinstance(current.get('value'), list):
        value = {name: {'description': '', 'variants': []} for name in current['value']}
    for kind, cats in (('joia', JEWELRY_CATEGORIES), ('gema', GEM_CATEGORIES)):
        for name, meta in cats.items():
            if name in value:
                continue
            value[name] = {
                'description': meta['description'], 'variants': [], 'kind': kind,
                'image_url': '', 'show_in_nav': True, 'order': meta['order'],
            }
            added.append(name)
    if added:
        update_setting('product_categories', {
            'name': 'Categorias de Produtos',
            'description': 'Categorias e variantes de produtos disponíveis',
            'type': 'object',
            'value': value,
        })
    print(f'  categorias: {len(added)} adicionada(s)')

    if not get_setting('cut_types'):
        update_setting('cut_types', {
            'name': 'Tipos de Lapidação', 'description': 'Tipos de lapidação disponíveis para produtos',
            'type': 'array', 'value': DEFAULT_CUT_TYPES,
        })
        print('  lapidações: criadas')

    if not get_setting('exchange_rate'):
        update_setting('exchange_rate', {
            'name': 'Cotação do Dólar', 'description': 'Taxa de câmbio USD → BRL atualizada', 'type': 'object',
            'value': {'rate': 5.60, 'market_rate': 5.50, 'increase': 0.10,
                      'date': utc_now().strftime('%Y-%m-%d'), 'updated_at': utc_now().isoformat()},
        })
        print('  cotação: criada (R$ 5,60)')

    for key, name, desc in (
        ('exchange_rate_increase', 'Incremento da Cotação', 'Valor adicional aplicado à cotação do dólar'),
        ('price_increase_rate', 'Taxa de Aumento', 'Porcentagem para aumentar os preços base dos produtos'),
    ):
        if not get_setting(key):
            update_setting(key, {'name': name, 'description': desc, 'type': 'number', 'value': 0})

    meta = get_setting('seed_meta') or {}
    previous = meta.get('added_categories') or []
    update_setting('seed_meta', {
        'name': 'Dados de exemplo', 'description': 'Controle do seed_data.py', 'type': 'object',
        'added_categories': sorted(set(previous) | set(added)),
    })


# ---------------------------------------------------------------------------
# Coleções
# ---------------------------------------------------------------------------

class Writer:
    """Agrupa gravações em batches de até 400 operações."""

    def __init__(self):
        self.batch = db.batch()
        self.count = 0
        self.total = 0

    def set(self, collection, doc_id, data):
        data = dict(data)
        data['seed'] = True
        self.batch.set(db.collection(collection).document(doc_id), data)
        self.count += 1
        self.total += 1
        if self.count >= 400:
            self.flush()

    def flush(self):
        if self.count:
            self.batch.commit()
            self.batch = db.batch()
            self.count = 0


def _ago(days=0, hours=0):
    return utc_now() - timedelta(days=days, hours=hours)


def seed_products(writer, upload):
    products = {}
    for idx, (slug, name, cat, metal, color, purity, stone, n_stones, stone_ct, weight, size, price, stock, mto, shape) in enumerate(JOIAS, 1):
        metal_rgb = METAL_RGB.get(metal if metal in ('prata', 'platina') else color, METAL_RGB['amarelo'])
        images = []
        for view in range(2):
            caption = name if view == 0 else f'{name} · detalhe'
            data = render_image(shape, metal_rgb, STONE_RGB.get(stone), caption)
            url, _ = store_image(f'joia-{slug}-{view + 1}', data, upload)
            images.append(url)
        doc_id = f'{SEED_PREFIX}joia-{slug}'
        doc = {
            'product_type': 'joia',
            'name': name,
            'category': cat,
            'sku': f'SEED-J{idx:02d}',
            'description': f'{name} confeccionado à mão no ateliê Katsumi. Peça de exemplo gerada pelo seed.',
            'price_brl': price,
            'price': 0.0,
            'metal': metal,
            'metal_color': color,
            'gold_karat': purity,
            'metal_weight_g': weight,
            'main_stone': stone,
            'stone_count': n_stones,
            'stone_carat_weight': stone_ct,
            'ring_size': size,
            'stock_qty': stock,
            'made_to_order': mto,
            'production_days': 25 if mto else 0,
            'carat_weight': '',
            'riviera': False,
            'discount': 10.0 if idx in (3, 8) else 0,
            'main_image_url': images[0],
            'image_2_url': images[1],
            'youtube_url': '',
            'specifications': {},
            'active': stock > 0 or mto,
            'sold_out': stock == 0 and not mto,
            'created_at': _ago(days=60 - idx),
            'updated_at': _ago(days=idx),
        }
        writer.set('products', doc_id, doc)
        products[doc_id] = doc

    for idx, (slug, name, cat, variant, price_ct, ct, cut, color, clarity, dims) in enumerate(GEMAS, 1):
        rgb = {'Esmeraldas': STONE_RGB['Esmeralda'], 'Turmalinas': STONE_RGB['Turmalina Paraíba']}[cat]
        if variant == 'Rubelita':
            rgb = (200, 60, 110)
        data = render_image('gem', METAL_RGB['amarelo'], rgb, name)
        url, _ = store_image(f'gema-{slug}', data, upload)
        pedras = 2 if 'Par' in name else 1
        doc_id = f'{SEED_PREFIX}gema-{slug}'
        doc = {
            'product_type': 'gema',
            'name': name,
            'category': cat,
            'variant': variant,
            'sku': f'SEED-G{idx:02d}',
            'description': f'{name}. Gema avulsa de exemplo.',
            'price': price_ct,
            'carat_weight': ct,
            'cut': cut,
            'color': color,
            'clarity': clarity,
            'comprimento': dims[0],
            'largura': dims[1],
            'altura': dims[2],
            'numero_pedras': pedras,
            'stock_initial_carat': float(ct),
            'stock_initial_pedras': pedras,
            'riviera': False,
            'discount': 0,
            'main_image_url': url,
            'youtube_url': '',
            'specifications': {'Lapidação': cut, 'Cor': color, 'Pureza': clarity, 'Peso em Quilates': ct},
            'active': True,
            'sold_out': False,
            'created_at': _ago(days=90 - idx),
            'updated_at': _ago(days=idx),
        }
        writer.set('products', doc_id, doc)
        products[doc_id] = doc
    print(f'  produtos: {len(JOIAS)} joias + {len(GEMAS)} gemas')
    return products


def seed_people(writer):
    customers = {}
    for idx, (key, name, email, phone, cpf, city, state, source) in enumerate(CUSTOMERS, 1):
        address = {
            'cep': f'0{idx}310-100', 'street': 'Rua das Flores', 'number': str(100 + idx * 7),
            'complement': 'Apto 12' if idx % 2 else '', 'neighborhood': 'Centro', 'city': city, 'state': state,
        }
        doc_id = f'{SEED_PREFIX}cliente-{key}'
        doc = {
            'name': name, 'email': email, 'phone': phone, 'cpf': cpf, 'person_type': 'cpf',
            'address_structured': address,
            'address': f"{address['street']}, {address['number']} - {address['neighborhood']}, {city}/{state}",
            'source': source, 'firebase_uid': '',
            'created_at': _ago(days=120 - idx * 9), 'updated_at': _ago(days=idx),
        }
        writer.set('customers', doc_id, doc)
        customers[doc_id] = doc

    sellers = {}
    for key, name, email, pct in SELLERS:
        doc_id = f'{SEED_PREFIX}vendedor-{key}'
        doc = {'name': name, 'email': email, 'commission_pct': pct, 'active': True,
               'created_at': _ago(days=200), 'updated_at': _ago(days=10)}
        writer.set('sellers', doc_id, doc)
        sellers[doc_id] = doc
    print(f'  clientes: {len(customers)} · vendedores: {len(sellers)}')
    return customers, sellers


def _internal_joia_item(product, rate, units=1):
    unit_usd = round(product['price_brl'] * (1 - (product.get('discount') or 0) / 100) / rate, 2)
    total = round(unit_usd * units, 2)
    return {
        'product_id': product['_id'], 'sku': product['sku'], 'name': product['name'], 'lote': '',
        'pedra': product['main_stone'], 'formato': f"{product['metal'].title()} {product['gold_karat']}",
        'calibre': product['ring_size'], 'ct': units, 'numero_pedras': units,
        'price_per_carat_usd': unit_usd, 'total_usd': total, 'total_brl': round(total * rate, 2),
        'sale_mode': 'partial', 'image': product['main_image_url'], 'quantity': units,
    }


def seed_orders(writer, products, customers, sellers, rate):
    for pid, p in products.items():
        p['_id'] = pid
    joias = [p for p in products.values() if p['product_type'] == 'joia']
    cust = list(customers.items())
    sell = list(sellers.items())

    def address_of(c):
        return dict(c['address_structured'])

    orders = []
    # Pedidos do site (pagos e pendentes)
    for n, (status, method, prods, c_idx, days) in enumerate((
        ('paid', 'pix', [joias[0]], 0, 20),
        ('paid', 'credit_card', [joias[8], joias[7]], 2, 9),
        ('pending', 'boleto', [joias[5]], 4, 1),
    ), 1):
        cid, c = cust[c_idx]
        items = []
        for p in prods:
            brl = round(p['price_brl'] * (1 - (p.get('discount') or 0) / 100), 2)
            items.append({'product_id': p['_id'], 'name': p['name'], 'amount_brl': brl,
                          'quantity': 1, 'image': p['main_image_url']})
        subtotal = round(sum(i['amount_brl'] for i in items), 2)
        orders.append((f'{SEED_PREFIX}pedido-site-{n}', {
            'source': SOURCE_WEBSITE, 'status': status, 'payment_method': method,
            'firestore_customer_id': cid, 'firebase_uid': '',
            'customer_name': c['name'], 'customer_email': c['email'], 'customer_phone': c['phone'],
            'customer_cpf': c['cpf'], 'shipping_address': address_of(c), 'items': items,
            'subtotal_brl': subtotal, 'shipping_cost_brl': 0.0, 'amount_brl': subtotal,
            'amount_centavos': int(round(subtotal * 100)), 'currency': 'BRL',
            'card_brand': 'visa' if method == 'credit_card' else '',
            'card_last_four': '4242' if method == 'credit_card' else '',
            'products_marked_sold': True,
            'created_at': _ago(days=days), 'updated_at': _ago(days=max(0, days - 1)),
        }))

    # Pedidos internos (balcão) com pagamento parcial e quitado
    for n, (prods, c_idx, s_idx, paid_ratio, days) in enumerate((
        ([joias[1]], 1, 0, 0.5, 15),
        ([joias[3], joias[9]], 3, 1, 1.0, 5),
    ), 1):
        cid, c = cust[c_idx]
        sid, s = sell[s_idx]
        items = [_internal_joia_item(p, rate) for p in prods]
        subtotal_usd = round(sum(i['total_usd'] for i in items), 2)
        amount_brl = round(subtotal_usd * rate, 2)
        paid = round(amount_brl * paid_ratio, 2)
        payments = [{
            'id': f'seedpay{n:05d}', 'amount_brl': paid, 'method': 'pix',
            'paid_at': _ago(days=days).strftime('%Y-%m-%d'), 'note': 'Entrada',
            'registered_by': 'seed', 'registered_at': _ago(days=days).isoformat(),
        }] if paid else []
        balance = round(amount_brl - paid, 2)
        orders.append((f'{SEED_PREFIX}pedido-interno-{n}', {
            'source': SOURCE_INTERNAL, 'status': 'paid' if balance <= 0 else 'pending',
            'payment_method': 'internal', 'firestore_customer_id': cid,
            'customer_name': c['name'], 'customer_email': c['email'], 'customer_phone': c['phone'],
            'customer_cpf': c['cpf'], 'customer_cnpj': '', 'customer_person_type': 'cpf',
            'shipping_address': address_of(c), 'seller_id': sid, 'seller_name': s['name'],
            'seller_commission_pct': s['commission_pct'],
            'commission_amount_brl': round(amount_brl * s['commission_pct'] / 100, 2),
            'commission_amount_usd': round(subtotal_usd * s['commission_pct'] / 100, 2),
            'exchange_rate_used': rate, 'items': items, 'subtotal_usd': subtotal_usd,
            'discount_usd': 0.0, 'amount_usd': subtotal_usd, 'subtotal_brl': amount_brl,
            'shipping_cost_brl': 0.0, 'amount_brl': amount_brl,
            'amount_centavos': int(round(amount_brl * 100)), 'currency': 'BRL',
            'notes': 'Pedido de exemplo (seed).', 'payments': payments, 'paid_total_brl': paid,
            'balance_brl': balance, 'payment_status': 'paid' if balance <= 0 else 'partial',
            'next_charge_date': (utc_now() + timedelta(days=10)).strftime('%Y-%m-%d') if balance > 0 else '',
            'created_by': 'seed', 'products_marked_sold': True,
            'created_at': _ago(days=days), 'updated_at': _ago(days=max(0, days - 2)),
        }))

    for doc_id, doc in orders:
        writer.set('orders', doc_id, doc)
    print(f'  pedidos: {len(orders)}')
    return orders


def seed_misc(writer, customers, orders):
    cust = list(customers.items())
    solicitacoes = (
        ('Procuro um anel de noivado em ouro branco com diamante oval de ~1 ct, orçamento até R$ 15 mil.', 'open', []),
        ('Gostaria de transformar a aliança da minha avó em um pingente.', 'answered', [{
            'message': 'Olá! Conseguimos sim. Vamos agendar uma visita ao ateliê para avaliarmos a peça?',
            'images': [], 'created_at': _ago(days=2), 'admin_name': 'Katsumi Joias',
        }]),
        ('Vocês fazem brincos com turmalina paraíba sob encomenda?', 'open', []),
    )
    for n, (text, status, replies) in enumerate(solicitacoes, 1):
        cid, c = cust[n % len(cust)]
        writer.set('solicitacoes', f'{SEED_PREFIX}solicitacao-{n}', {
            'name': c['name'], 'email': c['email'], 'phone': c['phone'], 'description': text,
            'customer_id': cid, 'status': status, 'replies': replies, 'created_at': _ago(days=n * 3),
        })

    for n, email in enumerate(('contato.exemplo1@example.com', 'contato.exemplo2@example.com',
                               'ana.souza@example.com', 'juliana.costa@example.com'), 1):
        writer.set('newsletter', f'{SEED_PREFIX}newsletter-{n}',
                   {'email': email, 'subscribed_at': _ago(days=n * 11), 'active': True})

    logs = [
        ('product', f'{SEED_PREFIX}joia-solitario-diamante', 'Solitário Clássico Diamante (SEED-J01)', 'create', []),
        ('product', f'{SEED_PREFIX}joia-anel-esmeralda-halo', 'Anel Esmeralda Halo (SEED-J02)', 'update',
         [{'field': 'price_brl', 'before': '11900', 'after': '12400'}]),
        ('order', orders[0][0], f'Pedido #{orders[0][0][:8].upper()}', 'create', []),
        ('customer', cust[0][0], cust[0][1]['name'], 'create', []),
    ]
    for n, (etype, eid, label, action, changes) in enumerate(logs, 1):
        writer.set('activity_logs', f'{SEED_PREFIX}log-{n}', {
            'entity_type': etype, 'entity_id': eid, 'entity_label': label, 'action': action,
            'changes': changes, 'actor_email': 'seed@katsumijoias.com.br', 'actor_uid': '',
            'source': '', 'note': 'Registro de exemplo', 'created_at': _ago(days=n),
        })
    print(f'  solicitações: {len(solicitacoes)} · newsletter: 4 · logs: {len(logs)}')


def seed_designer(writer, customers, upload):
    cust = list(customers.items())
    examples = (
        ({'jewelry_type': 'anel', 'metal': 'ouro_branco', 'stone': 'esmeralda', 'shape': 'oval',
          'size': 'media', 'count': '1', 'accent': 'halo', 'finish': 'polido'},
         'Gostaria de um aro fino e delicado.', 'em_analise', 0),
        ({'jewelry_type': 'pingente', 'metal': 'ouro_amarelo', 'stone': 'turmalina_paraiba', 'shape': 'gota',
          'size': 'media', 'count': '1', 'accent': 'nenhum', 'finish': 'polido'},
         '', 'novo', 2),
    )
    shape_map = {'anel': 'ring', 'alianca': 'band', 'pingente': 'pendant', 'colar': 'pendant',
                 'brinco': 'earrings', 'pulseira': 'bracelet'}
    metal_map = {'ouro_amarelo': 'amarelo', 'ouro_branco': 'branco', 'ouro_rose': 'rose',
                 'platina': 'platina', 'prata': 'prata'}
    for n, (choices, details, quote_status, c_idx) in enumerate(examples, 1):
        cid, c = cust[c_idx]
        stone_hex = jd.STONES[choices['stone']]['swatch'].lstrip('#')
        stone_rgb = tuple(int(stone_hex[i:i + 2], 16) for i in (0, 2, 4))
        labels = jd.choice_labels(choices)
        images = []
        for idx in range(2):
            data = render_image(shape_map[choices['jewelry_type']], METAL_RGB[metal_map[choices['metal']]],
                                stone_rgb, f'Criação {n} · variação {idx + 1}')
            url, path = store_image(f'design-{n}-{idx + 1}', data, upload)
            images.append({'index': idx, 'status': 'done', 'url': url, 'storage_path': path,
                           'provider': 'mock', 'model': 'seed', 'response_id': '', 'seed': None,
                           'cost_usd': 0.0, 'fallback_used': False})
        prompts = [jd.build_prompt(choices, details, variation=v) for v in ('a', 'b')]
        design_id = f'{SEED_PREFIX}design-{n}'
        quote_id = f'{SEED_PREFIX}orcamento-{n}'
        created = _ago(days=n * 2)
        writer.set('jewelry_designs', design_id, {
            'user_id': '', 'user_email': c['email'], 'kind': 'generate', 'choices': choices, 'labels': labels,
            'details_text': details, 'warnings': [], 'prompt': prompts[0], 'prompts': prompts,
            'prompt_version': jd.PROMPT_VERSION, 'reference_images': [], 'cache_key': '',
            'provider_primary': 'mock', 'model_primary': 'seed', 'requested_count': 2,
            'estimated_cost_usd': 0.0, 'actual_cost_usd': 0.0, 'ip_hash': '', 'status': 'completed',
            'images': images, 'quote_id': quote_id,
            'created_at': created, 'updated_at': created, 'completed_at': created,
        })
        writer.set('quotes', quote_id, {
            'user_id': '', 'customer_email': c['email'], 'customer_name': c['name'],
            'customer_phone': c['phone'], 'firestore_customer_id': cid,
            'design_id': design_id, 'root_design_id': design_id, 'selected_image_index': 0,
            'selected_image_url': images[0]['url'], 'choices': choices, 'labels': labels,
            'details_text': details, 'edit_instructions': [],
            'designs': [{'design_id': design_id, 'kind': 'generate', 'prompt': prompts[0],
                         'prompt_version': jd.PROMPT_VERSION, 'edit_instruction': '',
                         'images': images, 'actual_cost_usd': 0.0, 'created_at': created, 'completed_at': created}],
            'total_generation_cost_usd': 0.0, 'notes': 'Orçamento de exemplo (seed).',
            'budget_range': 'R$ 10 a 20 mil', 'status': quote_status,
            'quoted_price_brl': 14800.0 if quote_status == 'em_analise' else None,
            'admin_notes': '', 'customer_message': '', 'production_days': None,
            'history': [{'status': 'novo', 'at': created, 'by': c['email'], 'note': 'Solicitação enviada pelo site'}],
            'created_at': created, 'updated_at': created,
        })
    print(f'  criações: {len(examples)} · orçamentos: {len(examples)}')


# ---------------------------------------------------------------------------
# Limpeza
# ---------------------------------------------------------------------------

def wipe(upload):
    total = 0
    for name in SEED_COLLECTIONS:
        batch, pending = db.batch(), 0
        for doc in db.collection(name).where('seed', '==', True).stream():
            batch.delete(doc.reference)
            pending += 1
            total += 1
            if pending >= 400:
                batch.commit()
                batch, pending = db.batch(), 0
        if pending:
            batch.commit()

    meta = get_setting('seed_meta') or {}
    added = meta.get('added_categories') or []
    current = get_setting('product_categories') or {}
    if added and isinstance(current.get('value'), dict):
        value = {k: v for k, v in current['value'].items() if k not in added}
        update_setting('product_categories', {**{k: v for k, v in current.items() if k not in ('created_at', 'updated_at')},
                                              'value': value})
    db.collection('settings').document('seed_meta').delete()

    if upload:
        for blob in bucket.list_blobs(prefix=STORAGE_PREFIX):
            blob.delete()
    if os.path.isdir(LOCAL_IMAGE_DIR):
        for fname in os.listdir(LOCAL_IMAGE_DIR):
            if fname.endswith('.jpg'):
                os.remove(os.path.join(LOCAL_IMAGE_DIR, fname))
    print(f'Removidos {total} documento(s) de exemplo e {len(added)} categoria(s).')


# ---------------------------------------------------------------------------

def _confirm(message):
    answer = input(f'{message} Digite SIM para continuar: ').strip().upper()
    return answer == 'SIM'


def main():
    parser = argparse.ArgumentParser(description='Dados de exemplo para o site/CRM Katsumi Joias.')
    parser.add_argument('--wipe', action='store_true', help='remove os dados criados por este script')
    parser.add_argument('--yes', action='store_true', help='não pedir confirmação')
    parser.add_argument('--upload', action='store_true', help='enviar imagens para o Firebase Storage (pasta seed/)')
    args = parser.parse_args()

    target = f"projeto={db.project} · banco={os.getenv('FIRESTORE_DATABASE_ID') or '(default)'}"
    if args.wipe:
        if not args.yes and not _confirm(f'Remover os dados de exemplo de {target}?'):
            sys.exit('Cancelado.')
        wipe(args.upload)
        return

    if not args.yes and not _confirm(f'Gravar dados de exemplo em {target}?'):
        sys.exit('Cancelado.')

    print('Configurações…')
    seed_settings()
    rate_setting = get_setting('exchange_rate') or {}
    rate = float((rate_setting.get('value') or {}).get('rate') or 5.6)

    writer = Writer()
    print('Coleções…')
    products = seed_products(writer, args.upload)
    customers, sellers = seed_people(writer)
    orders = seed_orders(writer, products, customers, sellers, rate)
    seed_misc(writer, customers, orders)
    seed_designer(writer, customers, args.upload)
    writer.flush()
    print(f'Pronto: {writer.total} documento(s) gravados. Para remover: python seed_data.py --wipe')
    if not args.upload:
        print('Imagens salvas em static/seed/ (use --upload para enviá-las ao Storage e vê-las em produção).')


if __name__ == '__main__':
    main()
