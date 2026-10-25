"""
Provedor de desenvolvimento: desenha uma "joia" esquemática com Pillow.

Permite testar todo o fluxo (fila, polling, storage, orçamento) sem chave de API.
Ative com IMAGE_PROVIDER_PRIMARY=mock (é o padrão quando nenhuma chave está configurada).
"""
import io
import math
import random
import time

from .base import GeneratedImage, ImageGenerationProvider

_METAL_COLORS = {
    'yellow gold': (212, 175, 55),
    'white gold': (200, 200, 205),
    'rose gold': (214, 150, 130),
    'platinum': (180, 182, 190),
    'silver': (192, 192, 196),
}
_STONE_COLORS = {
    'diamond': (235, 240, 248),
    'emerald': (20, 140, 80),
    'ruby': (175, 20, 50),
    'sapphire': (25, 60, 160),
    'paraiba': (40, 200, 200),
    'morganite': (240, 170, 170),
    'tanzanite': (85, 70, 170),
    'amethyst': (140, 80, 170),
    'topaz': (90, 170, 230),
    'pearl': (245, 240, 230),
}


def _pick(prompt, table, default):
    """Primeiro termo da tabela que aparece no prompt (pela posição no texto)."""
    low = prompt.lower()
    found = [(low.find(key), value) for key, value in table.items() if key in low]
    return min(found, key=lambda item: item[0])[1] if found else default


class MockImageProvider(ImageGenerationProvider):
    name = 'mock'

    def is_configured(self):
        return True

    @property
    def model(self):
        return 'mock-pillow'

    def estimated_cost_usd(self):
        return 0.0

    def _render(self, prompt, seed, label):
        from PIL import Image, ImageDraw, ImageFilter

        rnd = random.Random(seed)
        size = 1024
        img = Image.new('RGB', (size, size), (250, 250, 250))
        draw = ImageDraw.Draw(img)
        metal = _pick(prompt, _METAL_COLORS, _METAL_COLORS['yellow gold'])
        stone = _pick(prompt, _STONE_COLORS, None)

        # sombra suave
        shadow = Image.new('L', (size, size), 0)
        ImageDraw.Draw(shadow).ellipse((300, 760, 724, 820), fill=70)
        img.paste((215, 215, 215), (0, 0), shadow.filter(ImageFilter.GaussianBlur(25)))

        cx, cy = size // 2, size // 2 + 60
        r_out, r_in = 260 + rnd.randint(-20, 20), 215
        draw.ellipse((cx - r_out, cy - r_out * 0.62, cx + r_out, cy + r_out * 0.62), fill=metal)
        draw.ellipse((cx - r_in, cy - r_in * 0.55, cx + r_in, cy + r_in * 0.55), fill=(250, 250, 250))
        highlight = tuple(min(255, c + 45) for c in metal)
        draw.arc((cx - r_out + 15, cy - r_out * 0.6, cx + r_out - 15, cy + r_out * 0.6), 200, 330, fill=highlight, width=10)

        if stone:
            count = 3 if 'three' in prompt.lower() else (5 if 'five' in prompt.lower() else 1)
            sr = 70 if count == 1 else 48
            offsets = [0] if count == 1 else [(-1.6 + 1.6 * i * (2 / (count - 1))) for i in range(count)]
            for off in offsets:
                sx = cx + int(off * sr * 1.3)
                sy = cy - int(r_out * 0.62) - sr + 25 + int(abs(off) * 12)
                pts = [(sx + sr * math.cos(a), sy + sr * math.sin(a)) for a in [k * math.pi / 4 for k in range(8)]]
                draw.polygon(pts, fill=stone, outline=tuple(max(0, c - 60) for c in stone))
                draw.polygon([(sx, sy - sr * 0.7), (sx + sr * 0.6, sy), (sx, sy + sr * 0.7), (sx - sr * 0.6, sy)],
                             outline=tuple(min(255, c + 60) for c in stone))
                for _ in range(6):
                    px, py = sx + rnd.randint(-sr // 2, sr // 2), sy + rnd.randint(-sr // 2, sr // 2)
                    draw.ellipse((px - 3, py - 3, px + 3, py + 3), fill=(255, 255, 255))

        draw.text((40, 40), f'MOCK · {label}', fill=(150, 150, 150))
        draw.text((40, size - 60), 'Imagem de teste (sem provedor de IA configurado)', fill=(170, 170, 170))
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=88)
        return buf.getvalue()

    def generate(self, prompt, *, size='1024x1024', reference_images=None):
        started = time.time()
        seed = random.randint(1, 2**31 - 1)
        time.sleep(1.5)
        data = self._render(prompt, seed, 'geração')
        return GeneratedImage(data=data, mime_type='image/jpeg', provider=self.name, model=self.model,
                              response_id=f'mock-{seed}', seed=seed, latency_ms=int((time.time() - started) * 1000))

    def edit(self, image, prompt, *, size='1024x1024', mime_type='image/png'):
        from PIL import Image, ImageDraw, ImageEnhance

        started = time.time()
        seed = random.randint(1, 2**31 - 1)
        time.sleep(1.0)
        try:
            img = Image.open(io.BytesIO(image)).convert('RGB')
            img = ImageEnhance.Brightness(img).enhance(1.04)
            ImageDraw.Draw(img).text((40, 80), 'MOCK · ajuste aplicado', fill=(120, 120, 120))
            buf = io.BytesIO()
            img.save(buf, 'JPEG', quality=88)
            data = buf.getvalue()
        except Exception:
            data = self._render(prompt, seed, 'ajuste')
        return GeneratedImage(data=data, mime_type='image/jpeg', provider=self.name, model=self.model,
                              response_id=f'mock-edit-{seed}', seed=seed, latency_ms=int((time.time() - started) * 1000))
