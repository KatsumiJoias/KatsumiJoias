"""Aplica o mapeamento aprovado: novas fotos Pexels/iStock → nomes usados nos templates."""
import os
import shutil
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static', 'images')

# destino (como no site) → arquivo fonte atual
MAPPING = {
    'banner1.jpg': 'pexels-charlotte-agyarko-2160603691-36845933.jpg',
    'banner2.jpg': 'pexels-coppertist-wu-313365563-15955333.jpg',
    'banner3.jpg': 'pexels-dax-dexter-delada-2150239947-31451756.jpg',
    'nossahistoria.jpg': 'pexels-tima-miroshnichenko-6263157.jpg',
    'nossa-historia.png': 'pexels-vedat-28933799.jpg',
    'faca-parte.png': 'pexels-celine-3776818-7115127.jpg',
    'oqueprocurapage.jpg': 'pexels-omer-faruk-balci-683760635-17925109.jpg',
    'oqueprocura.jpg': 'pexels-vedat-28933800.jpg',
    'outra-diferente.jpg': 'pexels-kunal-lakhotia-781256899-37485309.jpg',
}

MAX_W = {
    'banner1.jpg': 2400,
    'banner2.jpg': 2400,
    'banner3.jpg': 2400,
    'nossahistoria.jpg': 1920,
    'nossa-historia.png': 1920,
    'faca-parte.png': 1920,
    'oqueprocurapage.jpg': 2560,
    'oqueprocura.jpg': 1600,
    'outra-diferente.jpg': 1920,
}


def save_image(src_path, dest_path, max_w):
    im = Image.open(src_path)
    if im.mode not in ('RGB', 'L'):
        im = im.convert('RGB')
    w, h = im.size
    if w > max_w:
        nh = int(h * max_w / w)
        im = im.resize((max_w, nh), Image.Resampling.LANCZOS)
    ext = os.path.splitext(dest_path)[1].lower()
    if ext == '.png':
        im.save(dest_path, 'PNG', optimize=True)
    else:
        im.save(dest_path, 'JPEG', quality=88, optimize=True)


def main():
    sources_used = set(MAPPING.values())
    for dest, src in MAPPING.items():
        src_path = os.path.join(ROOT, src)
        dest_path = os.path.join(ROOT, dest)
        if not os.path.isfile(src_path):
            raise FileNotFoundError(f'Fonte ausente: {src}')
        if os.path.abspath(src_path) != os.path.abspath(dest_path) and os.path.isfile(dest_path):
            os.remove(dest_path)
        save_image(src_path, dest_path, MAX_W[dest])
        print(f'OK  {src}  →  {dest}')

    # Remove arquivos fonte Pexels/iStock restantes (sugestões já aplicadas ou não usadas)
    for name in os.listdir(ROOT):
        lower = name.lower()
        if lower.endswith('.gitkeep'):
            continue
        if name in MAPPING:
            continue
        if name in sources_used:
            path = os.path.join(ROOT, name)
            if os.path.isfile(path):
                os.remove(path)
                print(f'DEL fonte  {name}')
            continue
        if name.startswith('pexels-') or name.startswith('istockphoto-'):
            os.remove(os.path.join(ROOT, name))
            print(f'DEL extra  {name}')


if __name__ == '__main__':
    main()
