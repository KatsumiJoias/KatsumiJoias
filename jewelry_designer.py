"""
"Crie sua jóia" — catálogo, regras de fabricação e montagem do prompt.

Princípios:
  * O prompt NUNCA recebe texto cru do usuário nas partes estruturais: cada escolha é
    uma chave de catálogo, e só o descritor (fixo, em inglês) entra no template.
  * A descrição livre vira apenas "detalhes adicionais", depois de sanitizada,
    limitada em tamanho e moderada.
  * Combinações que a oficina não fabrica são bloqueadas com mensagem clara.
"""
import hashlib
import json
import os
import re
import unicodedata

import requests

PROMPT_VERSION = 'v1'
MAX_DETAILS_CHARS = 500
MAX_EDIT_CHARS = 300

# ---------------------------------------------------------------------------
# Catálogo (chave -> rótulo PT + descritor EN usado no prompt)
# ---------------------------------------------------------------------------

JEWELRY_TYPES = {
    'anel': {
        'label': 'Anel', 'en': 'fine jewelry ring',
        'counts': ['0', '1', '3', '5', 'pave'], 'max_size': 'grande',
        'accents': ['nenhum', 'halo', 'aro_cravejado'],
        'view_a': 'three-quarter view of the ring standing upright, showing the center setting and the shank',
        'view_b': 'front view of the ring slightly from above, showing the profile of the setting',
    },
    'alianca': {
        'label': 'Aliança', 'en': 'wedding band',
        'counts': ['0', '1', 'pave'], 'max_size': 'pequena',
        'accents': ['nenhum'],
        'shapes': ['redondo', 'princesa', 'baguete'],
        'view_a': 'three-quarter view of a single band standing upright',
        'view_b': 'side view of a single band showing its width and profile',
    },
    'pingente': {
        'label': 'Pingente com corrente', 'en': 'pendant on a delicate fine chain',
        'counts': ['0', '1', '3', 'pave'], 'max_size': 'grande',
        'accents': ['nenhum', 'halo'],
        'view_a': 'pendant hanging naturally, chain softly curved, frontal view',
        'view_b': 'close three-quarter view of the pendant showing the bail and setting depth',
    },
    'colar': {
        'label': 'Colar', 'en': 'statement necklace',
        'counts': ['1', '3', '5', 'pave'], 'max_size': 'media',
        'accents': ['nenhum', 'halo'],
        'view_a': 'necklace laid flat in an elegant curve, full piece visible',
        'view_b': 'close-up of the central section of the necklace',
    },
    'brinco': {
        'label': 'Brincos (par)', 'en': 'matching pair of earrings',
        'counts': ['0', '1', '3', 'pave'], 'max_size': 'media',
        'accents': ['nenhum', 'halo'],
        'view_a': 'both earrings side by side, frontal view',
        'view_b': 'both earrings at a slight angle showing the backs and posts',
    },
    'pulseira': {
        'label': 'Pulseira', 'en': 'bracelet',
        'counts': ['0', '1', '5', 'pave'], 'max_size': 'media',
        'accents': ['nenhum'],
        'view_a': 'bracelet arranged in an oval, full piece visible from slightly above',
        'view_b': 'close-up three-quarter view of the bracelet clasp area and links',
    },
}

METALS = {
    'ouro_amarelo': {'label': 'Ouro 18k amarelo', 'en': 'polished 18k yellow gold', 'swatch': '#D4AF37'},
    'ouro_branco': {'label': 'Ouro 18k branco', 'en': 'polished 18k white gold with rhodium finish', 'swatch': '#D9D9DE'},
    'ouro_rose': {'label': 'Ouro 18k rosé', 'en': 'polished 18k rose gold', 'swatch': '#D8A08C'},
    'platina': {'label': 'Platina 950', 'en': 'polished platinum 950', 'swatch': '#B8BAC2'},
    'prata': {'label': 'Prata 925', 'en': 'polished sterling silver 925', 'swatch': '#C4C4C8'},
}

STONES = {
    'nenhuma': {'label': 'Sem pedra', 'en': '', 'kind': 'none', 'swatch': '#FFFFFF'},
    'diamante': {'label': 'Diamante', 'en': 'colorless natural diamond with realistic fire and scintillation', 'kind': 'faceted', 'swatch': '#EEF2F8'},
    'esmeralda': {'label': 'Esmeralda', 'en': 'vivid green natural emerald', 'kind': 'faceted', 'swatch': '#148C50'},
    'rubi': {'label': 'Rubi', 'en': 'deep red natural ruby', 'kind': 'faceted', 'swatch': '#AF1432'},
    'safira': {'label': 'Safira azul', 'en': 'royal blue natural sapphire', 'kind': 'faceted', 'swatch': '#193CA0'},
    'turmalina_paraiba': {'label': 'Turmalina Paraíba', 'en': 'electric neon blue-green Paraíba tourmaline', 'kind': 'faceted', 'swatch': '#28C8C8'},
    'morganita': {'label': 'Morganita', 'en': 'soft peach-pink morganite', 'kind': 'faceted', 'swatch': '#F0AAAA'},
    'tanzanita': {'label': 'Tanzanita', 'en': 'violet-blue tanzanite', 'kind': 'faceted', 'swatch': '#5546AA'},
    'ametista': {'label': 'Ametista', 'en': 'purple amethyst', 'kind': 'faceted', 'swatch': '#8C50AA'},
    'topazio_azul': {'label': 'Topázio azul', 'en': 'sky blue topaz', 'kind': 'faceted', 'swatch': '#5AAAE6'},
    'perola': {'label': 'Pérola', 'en': 'lustrous white cultured pearl', 'kind': 'pearl', 'swatch': '#F5F0E6'},
}

SHAPES = {
    'redondo': {'label': 'Redondo (brilhante)', 'en': 'round brilliant cut'},
    'oval': {'label': 'Oval', 'en': 'oval cut'},
    'gota': {'label': 'Gota', 'en': 'pear cut'},
    'esmeralda': {'label': 'Esmeralda (retangular)', 'en': 'emerald step cut'},
    'princesa': {'label': 'Princesa', 'en': 'princess cut'},
    'cushion': {'label': 'Cushion (almofada)', 'en': 'cushion cut'},
    'marquise': {'label': 'Marquise', 'en': 'marquise cut'},
    'coracao': {'label': 'Coração', 'en': 'heart cut'},
    'baguete': {'label': 'Baguete', 'en': 'baguette cut'},
}
PEARL_SHAPES = ['redondo', 'gota']

SIZES = {
    'pequena': {'label': 'Pequena (até ~0,3 ct)', 'en': 'small, delicate', 'rank': 1},
    'media': {'label': 'Média (~0,5 a 1 ct)', 'en': 'medium-sized', 'rank': 2},
    'grande': {'label': 'Grande (acima de 1,5 ct)', 'en': 'large statement', 'rank': 3},
}

COUNTS = {
    '0': {'label': 'Nenhuma', 'en': ''},
    '1': {'label': '1 pedra (solitário)', 'en': 'a single center stone'},
    '3': {'label': '3 pedras (trilogia)', 'en': 'three stones set side by side'},
    '5': {'label': '5 pedras', 'en': 'five stones set in a row'},
    'pave': {'label': 'Cravejado (pavê)', 'en': 'many small stones in a pavé setting'},
}

ACCENTS = {
    'nenhum': {'label': 'Sem detalhes', 'en': ''},
    'halo': {'label': 'Halo de diamantes', 'en': 'surrounded by a halo of small diamonds'},
    'aro_cravejado': {'label': 'Aro cravejado de diamantes', 'en': 'with a band set with small diamonds'},
}

FINISHES = {
    'polido': {'label': 'Polido (alto brilho)', 'en': 'high polish'},
    'acetinado': {'label': 'Acetinado (fosco)', 'en': 'satin brushed'},
    'texturizado': {'label': 'Texturizado', 'en': 'subtle hand-hammered texture'},
}

FIELDS = ('jewelry_type', 'metal', 'stone', 'shape', 'size', 'count', 'accent', 'finish')

STYLE_TEMPLATE = (
    'Professional fine jewelry product photograph, macro shot, seamless pure white infinite '
    'background, studio softbox lighting, realistic reflections on the metal, realistic '
    'brilliance and refraction in the gemstones, tack-sharp focus, high-end catalog look. '
    'No hands, no people, no mannequin, no text, no logo, no watermark, no props.'
)

# Palavras que não devem chegar ao modelo (conteúdo fora de contexto, pedidos de texto/marca,
# tentativas de "prompt injection")
_BLOCKED_PATTERNS = [
    r'\bignor[ea]\b', r'\bprompt\b', r'\bsystem\b', r'instru[cç][oõã]', r'\bjailbreak\b',
    r'\bnud[eoa]s?\b', r'\bnua\b', r'\bsexo\b', r'\bsexual', r'\bsexy\b', r'\bporn', r'\bsangue\b',
    r'\barmas?\b', r'\bfacas?\b', r'\bdrogas?\b',
    r'\bcartier\b', r'\btiffany\b', r'\bbulgari\b', r'\bbvlgari\b', r'\bvivara\b', r'\bpandora\b',
    r'\bchanel\b', r'\bdior\b', r'\blouis\s*vuitton\b', r'\bgucci\b', r'\bvan\s*cleef\b',
    r'marca\s*d.?[aá]gua', r'\bwatermark\b', r'\blogotipo', r'\blogomarca', r'\blogo\s+d[aoe]\b',
    r'\bpessoas?\b', r'\b(modelo|mulher|homem|crian[cç]a)\s+(usando|vestindo)\b', r'\bcelebridade',
]
_BLOCKED_RE = re.compile('|'.join(_BLOCKED_PATTERNS), re.IGNORECASE)


class DesignValidationError(ValueError):
    def __init__(self, errors):
        super().__init__('; '.join(errors))
        self.errors = errors


# ---------------------------------------------------------------------------
# Catálogo exposto ao front
# ---------------------------------------------------------------------------

def public_catalog():
    def opts(table, extra=()):
        return [
            {'key': k, 'label': v['label'], **{e: v[e] for e in extra if e in v}}
            for k, v in table.items()
        ]
    return {
        'jewelry_types': [
            {'key': k, 'label': v['label'], 'counts': v['counts'], 'max_size': v['max_size'],
             'accents': v['accents'], 'shapes': v.get('shapes')}
            for k, v in JEWELRY_TYPES.items()
        ],
        'metals': opts(METALS, ('swatch',)),
        'stones': [{'key': k, 'label': v['label'], 'kind': v['kind'], 'swatch': v['swatch']} for k, v in STONES.items()],
        'shapes': opts(SHAPES),
        'pearl_shapes': PEARL_SHAPES,
        'sizes': [{'key': k, 'label': v['label'], 'rank': v['rank']} for k, v in SIZES.items()],
        'counts': opts(COUNTS),
        'accents': opts(ACCENTS),
        'finishes': opts(FINISHES),
        'max_details_chars': MAX_DETAILS_CHARS,
        'max_edit_chars': MAX_EDIT_CHARS,
    }


# ---------------------------------------------------------------------------
# Validação
# ---------------------------------------------------------------------------

def normalize_choices(raw):
    """Mantém apenas chaves conhecidas do catálogo; aplica padrões coerentes."""
    raw = raw or {}
    choices = {f: str(raw.get(f) or '').strip().lower() for f in FIELDS}
    if choices['stone'] in ('', 'nenhuma'):
        choices.update({'stone': 'nenhuma', 'shape': '', 'size': '', 'count': '0', 'accent': choices['accent'] or 'nenhum'})
    choices['accent'] = choices['accent'] or 'nenhum'
    choices['finish'] = choices['finish'] or 'polido'
    return choices


def validate_choices(choices):
    """Retorna lista de avisos; lança DesignValidationError com mensagens em PT."""
    errors, warnings = [], []
    jt = JEWELRY_TYPES.get(choices['jewelry_type'])
    if not jt:
        errors.append('Escolha o tipo de joia.')
    if choices['metal'] not in METALS:
        errors.append('Escolha o metal.')
    stone = STONES.get(choices['stone'])
    if not stone:
        errors.append('Escolha a pedra (ou “Sem pedra”).')
    if choices['finish'] not in FINISHES:
        errors.append('Acabamento inválido.')
    if choices['accent'] not in ACCENTS:
        errors.append('Detalhe inválido.')
    if errors:
        raise DesignValidationError(errors)

    type_label = jt['label']
    if stone['kind'] == 'none':
        if choices['count'] not in ('0', ''):
            errors.append('Sem pedra principal, a quantidade de pedras deve ser “Nenhuma”.')
        if '0' not in jt['counts']:
            errors.append(f'{type_label} precisa de pelo menos uma pedra.')
        if choices['accent'] == 'halo':
            errors.append('O halo de diamantes envolve uma pedra central — escolha uma pedra principal.')
    else:
        if choices['shape'] not in SHAPES:
            errors.append('Escolha o formato da pedra.')
        if choices['size'] not in SIZES:
            errors.append('Escolha o tamanho da pedra.')
        if choices['count'] not in COUNTS or choices['count'] == '0':
            errors.append('Escolha a quantidade de pedras.')
        if errors:
            raise DesignValidationError(errors)

        if choices['count'] not in jt['counts']:
            allowed = ', '.join(COUNTS[c]['label'] for c in jt['counts'] if c != '0')
            errors.append(f'Para {type_label.lower()}, fabricamos: {allowed}.')
        allowed_shapes = jt.get('shapes')
        if allowed_shapes and choices['shape'] not in allowed_shapes:
            names = ', '.join(SHAPES[s]['label'] for s in allowed_shapes)
            errors.append(f'Em {type_label.lower()} trabalhamos com os formatos: {names}.')
        if SIZES[choices['size']]['rank'] > SIZES[jt['max_size']]['rank']:
            errors.append(f'Para {type_label.lower()}, o tamanho máximo de pedra é “{SIZES[jt["max_size"]]["label"]}”.')
        if choices['count'] == 'pave' and choices['size'] != 'pequena':
            errors.append('A cravação pavê usa pedras pequenas — escolha o tamanho “Pequena”.')
        if choices['count'] in ('3', '5') and choices['size'] == 'grande':
            errors.append('Várias pedras grandes na mesma peça não ficam estáveis nem confortáveis — escolha “Média” ou “Pequena”.')
        if stone['kind'] == 'pearl':
            if choices['shape'] not in PEARL_SHAPES:
                errors.append('Pérolas não são lapidadas: escolha o formato “Redondo” ou “Gota”.')
            if choices['count'] == 'pave':
                errors.append('Pérolas não são cravadas em pavê.')
            if choices['accent'] == 'aro_cravejado' and choices['jewelry_type'] == 'alianca':
                errors.append('Aliança com pérola não é fabricada.')
        if choices['accent'] == 'halo' and choices['count'] != '1':
            errors.append('O halo de diamantes é feito em volta de uma única pedra central.')
        if choices['stone'] == 'esmeralda' and choices['shape'] in ('princesa', 'marquise', 'coracao'):
            warnings.append('Esmeraldas são mais frágeis em cantos vivos; a oficina pode sugerir outro formato.')
        if choices['metal'] == 'prata' and (choices['stone'] == 'diamante' or choices['size'] == 'grande'):
            warnings.append('Para diamantes e pedras grandes recomendamos ouro 18k ou platina pela durabilidade da cravação.')

    if choices['accent'] not in jt['accents']:
        allowed = ', '.join(ACCENTS[a]['label'] for a in jt['accents'])
        errors.append(f'Detalhes disponíveis para {type_label.lower()}: {allowed}.')

    if errors:
        raise DesignValidationError(errors)
    return warnings


def choice_labels(choices):
    """Rótulos legíveis (PT) das escolhas — gravados junto com o design/orçamento."""
    return {
        'jewelry_type': JEWELRY_TYPES.get(choices.get('jewelry_type'), {}).get('label', ''),
        'metal': METALS.get(choices.get('metal'), {}).get('label', ''),
        'stone': STONES.get(choices.get('stone'), {}).get('label', ''),
        'shape': SHAPES.get(choices.get('shape'), {}).get('label', ''),
        'size': SIZES.get(choices.get('size'), {}).get('label', ''),
        'count': COUNTS.get(choices.get('count'), {}).get('label', ''),
        'accent': ACCENTS.get(choices.get('accent'), {}).get('label', ''),
        'finish': FINISHES.get(choices.get('finish'), {}).get('label', ''),
    }


# ---------------------------------------------------------------------------
# Texto livre: sanitização + moderação
# ---------------------------------------------------------------------------

def sanitize_free_text(text, max_chars=MAX_DETAILS_CHARS):
    """Remove HTML/URLs/caracteres de controle e limita o tamanho. Retorna (texto, erro)."""
    if not text:
        return '', None
    text = unicodedata.normalize('NFC', str(text))
    text = re.sub(r'<[^>]*>', ' ', text)
    text = re.sub(r'https?://\S+|www\.\S+', ' ', text)
    text = re.sub(r'[\x00-\x1f\x7f]', ' ', text)
    text = re.sub(r'[^\w\s.,;:!?()\-–/%+°ºª&\'"áàâãéêíóôõúüçÁÀÂÃÉÊÍÓÔÕÚÜÇ]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) > max_chars:
        return '', f'A descrição pode ter no máximo {max_chars} caracteres.'
    if _BLOCKED_RE.search(text):
        return '', ('Sua descrição contém termos que não podemos usar na geração da imagem '
                    '(marcas, pessoas/partes do corpo, textos ou conteúdo fora do contexto de joias). '
                    'Descreva apenas detalhes da peça.')
    return text, None


def moderate_text(text):
    """
    Moderação via OpenAI (omni-moderation-latest). Sem OPENAI_API_KEY a checagem local
    (sanitize_free_text) é a única barreira. Retorna (ok, categorias).
    """
    if not text:
        return True, []
    api_key = (os.getenv('OPENAI_API_KEY') or '').strip()
    if not api_key or os.getenv('DESIGNER_MODERATION', '1') == '0':
        return True, []
    try:
        resp = requests.post(
            'https://api.openai.com/v1/moderations',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={'model': os.getenv('OPENAI_MODERATION_MODEL', 'omni-moderation-latest'), 'input': text},
            timeout=15,
        )
        if resp.status_code >= 400:
            print(f'[designer] moderation HTTP {resp.status_code}: {resp.text[:200]}')
            return True, []
        result = (resp.json().get('results') or [{}])[0]
        if result.get('flagged'):
            cats = [k for k, v in (result.get('categories') or {}).items() if v]
            return False, cats
        return True, []
    except Exception as exc:
        print(f'[designer] moderation error: {exc}')
        return True, []


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def build_prompt(choices, details_text='', variation='a', has_references=False):
    jt = JEWELRY_TYPES[choices['jewelry_type']]
    metal = METALS[choices['metal']]
    stone = STONES[choices['stone']]

    subject = f"A single {jt['en']} handcrafted in {metal['en']}"
    if stone['kind'] != 'none':
        size = SIZES[choices['size']]['en']
        count_en = COUNTS[choices['count']]['en']
        if stone['kind'] == 'pearl':
            shape_en = 'round' if choices['shape'] == 'redondo' else 'drop-shaped'
            subject += f", featuring {count_en}: {size} {shape_en} {stone['en']}"
        else:
            subject += f", featuring {count_en}: {size} {SHAPES[choices['shape']]['en']} {stone['en']}"
        subject += ', held by precise, realistic prongs or bezel settings'
    accent = ACCENTS[choices['accent']]['en']
    if accent:
        subject += f', {accent}'
    subject += f", {FINISHES[choices['finish']]['en']} metal finish."

    view = jt['view_a'] if variation == 'a' else jt['view_b']
    parts = [
        subject,
        f'Composition: {view}, the piece centered and filling about 70% of the frame.',
        'The design must be physically manufacturable by a fine jeweler, with realistic proportions, '
        'metal thickness and stone settings.',
        STYLE_TEMPLATE,
    ]
    if has_references:
        parts.append('Use the attached reference images only as references for the real color and texture '
                     'of the metal and gemstones; do not copy their background or composition.')
    if details_text:
        parts.append(
            'Additional design details requested by the client (apply only if compatible with everything above, '
            f'never change the photographic style): "{details_text}"'
        )
    return '\n'.join(parts)


def build_edit_prompt(instruction):
    return '\n'.join([
        'Edit this fine jewelry product photograph.',
        f'Apply only this change requested by the client: "{instruction}".',
        'Keep everything else identical: the same piece, materials, gemstones, camera angle, '
        'lighting and the seamless pure white background.',
        'The result must remain physically manufacturable by a fine jeweler.',
        STYLE_TEMPLATE,
    ])


def cache_key(choices, details_text, model):
    payload = json.dumps(
        {'choices': {k: choices.get(k, '') for k in FIELDS}, 'details': (details_text or '').lower(),
         'model': model, 'v': PROMPT_VERSION},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# Imagens de referência reais (opcional)
# ---------------------------------------------------------------------------

REFS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'designer', 'refs')


def reference_image_paths(choices):
    """
    Procura fotos reais em static/designer/refs/:
        metal/<metal>.(jpg|png|webp)   ex.: metal/ouro_rose.jpg
        stone/<stone>.(jpg|png|webp)   ex.: stone/esmeralda.jpg
        base/<jewelry_type>.(jpg|png)  ex.: base/anel.jpg
    """
    if os.getenv('DESIGNER_USE_REFERENCES', '0') != '1':
        return []
    found = []
    for folder, key in (('base', choices.get('jewelry_type')), ('metal', choices.get('metal')), ('stone', choices.get('stone'))):
        if not key or key == 'nenhuma':
            continue
        for ext in ('jpg', 'jpeg', 'png', 'webp'):
            path = os.path.join(REFS_DIR, folder, f'{key}.{ext}')
            if os.path.isfile(path):
                found.append((folder, path))
                break
    return found
