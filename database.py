import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from env_utils import cloud_run_gcp_project_id, env_str, firebase_storage_bucket, project_id_from_credentials_json
import uuid
from werkzeug.utils import secure_filename
import tempfile
import firebase_admin
from firebase_admin import credentials, firestore, storage

try:
    from PIL import Image
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:
    HEIC_SUPPORTED = False

# Load environment variables
load_dotenv()


def _resolve_storage_bucket_name():
    """Firebase Storage bucket; required for storage.bucket() at import time."""
    return firebase_storage_bucket()


_STORAGE_BUCKET_NAME = _resolve_storage_bucket_name()

# Initialize Firebase Admin SDK
try:
    # Try to get existing app
    firebase_app = firebase_admin.get_app()
except ValueError:
    # Initialize if not already initialized
    firebase_credentials_json = os.getenv("FIREBASE_CREDENTIALS_JSON")
    firebase_credentials_path = os.getenv("FIREBASE_CREDENTIALS_PATH")
    
    if firebase_credentials_json:
        # Use JSON string from environment variable (recommended for production)
        import json
        try:
            cred_dict = json.loads(firebase_credentials_json)
            cred = credentials.Certificate(cred_dict)
            firebase_app = firebase_admin.initialize_app(cred, {
                'storageBucket': _STORAGE_BUCKET_NAME,
            })
            print("Firebase inicializado via FIREBASE_CREDENTIALS_JSON")
        except json.JSONDecodeError as e:
            print(f"✗ ERRO: JSON inválido em FIREBASE_CREDENTIALS_JSON: {e}")
            print("Execute: python fix_env.py para corrigir")
            raise
    elif firebase_credentials_path:
        # Use service account file path (for local development)
        cred = credentials.Certificate(firebase_credentials_path)
        firebase_app = firebase_admin.initialize_app(cred, {
            'storageBucket': _STORAGE_BUCKET_NAME,
        })
    else:
        # Use default credentials (for Cloud Run/GCP environments with default service account)
        firebase_app = firebase_admin.initialize_app(options={
            'storageBucket': _STORAGE_BUCKET_NAME,
        })

# Initialize Firestore client (Firebase Console → Firestore; default database id is "(default)")
def _create_firestore_client():
    database_id = (os.getenv('FIRESTORE_DATABASE_ID') or '(default)').strip()
    if database_id in ('', '(default)'):
        return firestore.client()
    try:
        return firestore.client(database_id=database_id)
    except TypeError as exc:
        raise RuntimeError(
            f'FIRESTORE_DATABASE_ID={database_id!r} requer firebase-admin>=6.2. '
            'Ative o venv do projeto e rode: pip install -r requirements.txt'
        ) from exc


db = _create_firestore_client()


def _log_firebase_bootstrap():
    cred_mode = 'adc'
    if os.getenv('FIREBASE_CREDENTIALS_JSON'):
        cred_mode = 'json'
    elif os.getenv('FIREBASE_CREDENTIALS_PATH'):
        cred_mode = 'file'
    fs_project = getattr(db, 'project', None) or project_id_from_credentials_json() or '?'
    print(f'[Firebase] Admin SDK credentials={cred_mode} firestore_project={fs_project!r}')
    env_pid = env_str('FIREBASE_PROJECT_ID')
    if env_pid and fs_project != '?' and env_pid != fs_project:
        print(
            f'[Firebase] AVISO: FIREBASE_PROJECT_ID={env_pid!r} != Firestore {fs_project!r} '
            f'(ajuste o secret / .env para {fs_project!r}).'
        )
    if cred_mode == 'adc':
        host_project = cloud_run_gcp_project_id()
        if host_project and fs_project != '?' and host_project != fs_project:
            print(
                f'[Firebase] Cloud Run hospedado em GCP {host_project!r}, dados em {fs_project!r}. '
                'Configure secret FIREBASE_CREDENTIALS_JSON (conteúdo do account_service.json).'
            )


_log_firebase_bootstrap()

# Initialize Storage bucket
bucket = storage.bucket(_STORAGE_BUCKET_NAME)

def upload_image(file, filename, folder='product-images'):
    """
    Upload an image to Firebase Storage.
    
    Args:
        file: FileStorage object from Flask
        filename: Original filename
    
    Returns:
        str: Public URL of the uploaded image
    """
    if not file:
        return None
        
    temp_path = None
    converted_path = None
    try:
        filename = secure_filename(filename)
        file_extension = os.path.splitext(filename)[1].lower()

        # Save original upload to a temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix=file_extension) as temp_file:
            file.save(temp_file.name)
            temp_path = temp_file.name

        # Convert HEIC/HEIF to JPEG so browsers can display it
        if file_extension in ('.heic', '.heif'):
            if not HEIC_SUPPORTED:
                print("pillow-heif not installed — cannot convert HEIC image")
                return None
            img = Image.open(temp_path)
            img = img.convert('RGB')
            with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp_jpg:
                converted_path = tmp_jpg.name
            img.save(converted_path, 'JPEG', quality=92)
            upload_path = converted_path
            unique_filename = f"{folder}/{uuid.uuid4()}.jpg"
        else:
            upload_path = temp_path
            unique_filename = f"{folder}/{uuid.uuid4()}{file_extension}"

        # Upload to Firebase Storage
        blob = bucket.blob(unique_filename)
        blob.upload_from_filename(upload_path)
        blob.make_public()
        return blob.public_url

    except Exception as e:
        print(f"Error in upload_image: {e}")
        return None

    finally:
        for path in (temp_path, converted_path):
            if path:
                try:
                    os.unlink(path)
                except Exception:
                    pass


def upload_bytes(data, path, content_type='image/png'):
    """Upload raw bytes to Firebase Storage under `path` and return the public URL."""
    if not data:
        return None
    try:
        blob = bucket.blob(path)
        blob.cache_control = 'public, max-age=31536000'
        blob.upload_from_string(data, content_type=content_type)
        blob.make_public()
        return blob.public_url
    except Exception as e:
        print(f"Error in upload_bytes ({path}): {e}")
        return None


def download_public_url(url, timeout=20):
    """Download bytes from one of our Storage URLs (used by the image edit flow)."""
    if not url:
        return None
    try:
        if url.startswith('/static/'):
            local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), url.lstrip('/'))
            with open(local_path, 'rb') as fh:
                return fh.read()
        prefix = f'https://storage.googleapis.com/{bucket.name}/'
        if url.startswith(prefix):
            blob = bucket.blob(url[len(prefix):])
            return blob.download_as_bytes()
        import requests
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.content
    except Exception as e:
        print(f"Error downloading {url}: {e}")
        return None

def delete_image(filename):
    """
    Delete an image from Firebase Storage.
    
    Args:
        filename: Name of the file to delete (can be full URL or just filename)
    """
    if not filename:
        return
        
    try:
        # Extract filename from URL if it's a full URL
        if filename.startswith('http'):
            # Extract the path after the bucket name
            filename = filename.split('/')[-1]
        
        # Add product-images prefix if not present
        if not filename.startswith('product-images/'):
            filename = f"product-images/{filename}"
        
        # Delete from Firebase Storage
        try:
            blob = bucket.blob(filename)
            blob.delete()
        except Exception as e:
            print(f"Error deleting from Firebase Storage: {e}")
            
    except Exception as e:
        print(f"Error in delete_image: {e}")

# ============== Origem dos registros (source) ==============
# Gravado em inglês no banco; traduzido apenas na camada de apresentação.
SOURCE_WEBSITE = 'Website'
SOURCE_INTERNAL = 'Internal'


def get_all_products():
    """Get all products from Firestore."""
    try:
        products_ref = db.collection('products')
        docs = products_ref.stream()
        
        products = []
        for doc in docs:
            product_data = doc.to_dict()
            product_data['id'] = doc.id
            products.append(product_data)
        
        return products
    except Exception as e:
        err = str(e)
        print(f"Error getting all products: {e}")
        if '403' in err or 'Permission' in err or 'Missing' in err:
            print(
                '[Firestore] Cloud Run service account may lack roles/datastore.user — '
                'see README IAM checklist.'
            )
        return []

def get_product_by_id(product_id):
    """Get a product by its ID from Firestore."""
    try:
        doc_ref = db.collection('products').document(product_id)
        doc = doc_ref.get()
        
        if doc.exists:
            product_data = doc.to_dict()
            product_data['id'] = doc.id
            return product_data
        else:
            return None
    except Exception as e:
        print(f"Error getting product by ID: {e}")
        return None

def create_product(product_data):
    """Create a new product in Firestore."""
    try:
        # Add timestamp
        from datetime import datetime
        product_data['created_at'] = utc_now()
        product_data['updated_at'] = utc_now()
        
        # Create document with auto-generated ID
        doc_ref = db.collection('products').document()
        doc_ref.set(product_data)
        
        # Return the created product with its ID
        product_data['id'] = doc_ref.id
        return product_data
    except Exception as e:
        print(f"Error creating product: {e}")
        return None

def update_product(product_id, product_data):
    """Update an existing product in Firestore."""
    try:
        from datetime import datetime
        product_data['updated_at'] = utc_now()
        
        doc_ref = db.collection('products').document(product_id)
        doc_ref.update(product_data)
        return True
    except Exception as e:
        print(f"Error updating product: {e}")
        return False


def record_product_sale(product_id, sale_record):
    """
    Mark a product as sold: set to draft (active=False) and append to sales_history.
    Idempotent per order_id — won't duplicate entries for the same order.
    """
    try:
        product = get_product_by_id(product_id)
        if not product:
            return False

        history = list(product.get('sales_history') or [])
        order_id = sale_record.get('order_id')
        if order_id and any(h.get('order_id') == order_id for h in history):
            if product.get('active', False):
                update_product(product_id, {'active': False})
            return True

        history.append(sale_record)
        update_data = {
            'active': False,
            'sold_out': True,
            'sales_history': history,
            'last_sold_at': sale_record.get('sold_at'),
        }
        # Joias podem ter mais de uma unidade em estoque: só sai do ar ao zerar
        if product.get('product_type') == 'joia':
            try:
                qty = int(float(product.get('stock_qty') if product.get('stock_qty') not in (None, '') else 1))
            except (ValueError, TypeError):
                qty = 1
            sold_units = max(1, int(sale_record.get('quantity') or 1))
            remaining = max(0, qty - sold_units)
            update_data['stock_qty'] = remaining
            if remaining > 0 or product.get('made_to_order'):
                update_data['active'] = bool(product.get('active', False))
                update_data['sold_out'] = False
        return update_product(product_id, update_data)
    except Exception as e:
        print(f"Error recording product sale: {e}")
        return False

SKU_PAD_WIDTH = 4


def _numeric_sku_value(sku):
    """Return the integer value of a purely numeric SKU, or None."""
    raw = str(sku or '').strip()
    if not raw or not raw.isdigit():
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def get_next_numeric_sku():
    """
    Next SKU for a brand-new product: highest numeric SKU in the catalog + 1,
    zero-padded to the 4-digit convention already in use.
    """
    try:
        highest = 0
        for doc in db.collection('products').select(['sku']).stream():
            value = _numeric_sku_value((doc.to_dict() or {}).get('sku'))
            if value is not None and value > highest:
                highest = value
        return str(highest + 1).zfill(SKU_PAD_WIDTH)
    except Exception as e:
        print(f"Error computing next numeric SKU: {e}")
        return ''


def find_products_by_sku(sku, exclude_product_id=None):
    """Return every product sharing the given SKU (optionally ignoring one document)."""
    normalized = str(sku or '').strip()
    if not normalized:
        return []
    try:
        matches = []
        for doc in db.collection('products').where('sku', '==', normalized).stream():
            if exclude_product_id and doc.id == exclude_product_id:
                continue
            data = doc.to_dict()
            data['id'] = doc.id
            matches.append(data)
        return matches
    except Exception as e:
        print(f"Error finding products by SKU: {e}")
        return []


def parse_carat_value(raw):
    """Parse a carat weight that may be stored as '2.5', '2,5 ct' or a number."""
    if raw is None or raw == '':
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text:
        return 0.0
    token = text.split()[0] if ' ' in text else text
    try:
        return float(token.replace(',', '.'))
    except (ValueError, TypeError):
        return 0.0


def _format_carat_value(value):
    """Store carats back in the same plain-number format the forms produce."""
    rounded = round(float(value), 3)
    if rounded <= 0:
        return '0'
    text = f'{rounded:.3f}'.rstrip('0').rstrip('.')
    return text or '0'


def apply_product_stock_sale(product_id, sale_record, carat_sold=None, pedras_sold=None, full_sale=True):
    """
    Register a sale against a product's stock.

    full_sale=True  → whole product leaves the catalog (active=False).
    full_sale=False → deduct `carat_sold` / `pedras_sold` from the remaining lot and
                      only deactivate when nothing is left.

    Idempotent per (order_id, item_index) pair recorded in sale_record.
    Returns dict with the resulting stock, or None on failure.
    """
    try:
        product = get_product_by_id(product_id)
        if not product:
            return None

        history = list(product.get('sales_history') or [])
        entry_key = sale_record.get('entry_key')
        if entry_key and any(h.get('entry_key') == entry_key for h in history):
            return {
                'carat_weight': product.get('carat_weight'),
                'numero_pedras': product.get('numero_pedras'),
                'active': product.get('active', False),
                'already_applied': True,
            }

        if product.get('product_type') == 'joia':
            # Joias: estoque em unidades (stock_qty); `pedras_sold` = unidades vendidas
            try:
                qty = int(float(product.get('stock_qty') if product.get('stock_qty') not in (None, '') else 1))
            except (ValueError, TypeError):
                qty = 1
            units = max(1, int(pedras_sold or 1))
            remaining = max(0, qty - units)
            update_data = {
                'sales_history': history + [sale_record],
                'stock_qty': remaining,
                'last_sold_at': sale_record.get('sold_at'),
            }
            if remaining <= 0 and not product.get('made_to_order'):
                update_data['active'] = False
                update_data['sold_out'] = True
            update_product(product_id, update_data)
            return {
                'stock_qty': remaining,
                'active': update_data.get('active', product.get('active', False)),
            }

        current_carat = parse_carat_value(product.get('carat_weight'))
        try:
            current_pedras = int(product.get('numero_pedras') or 0)
        except (ValueError, TypeError):
            current_pedras = 0

        update_data = {'sales_history': history + [sale_record]}

        # Preserve the original lot size the first time we deduct from it.
        if 'stock_initial_carat' not in product:
            update_data['stock_initial_carat'] = current_carat
        if 'stock_initial_pedras' not in product:
            update_data['stock_initial_pedras'] = current_pedras

        if full_sale:
            remaining_carat = 0.0
            remaining_pedras = 0
        else:
            remaining_carat = max(0.0, current_carat - float(carat_sold or 0))
            remaining_pedras = max(0, current_pedras - int(pedras_sold or 0))

        depleted = full_sale or remaining_carat <= 0.0001 or remaining_pedras <= 0

        if depleted:
            update_data['active'] = False
            update_data['sold_out'] = True
            if not full_sale:
                update_data['carat_weight'] = _format_carat_value(remaining_carat)
                update_data['numero_pedras'] = remaining_pedras
        else:
            update_data['carat_weight'] = _format_carat_value(remaining_carat)
            update_data['numero_pedras'] = remaining_pedras

        update_data['last_sold_at'] = sale_record.get('sold_at')

        if not update_product(product_id, update_data):
            return None

        return {
            'carat_weight': update_data.get('carat_weight', product.get('carat_weight')),
            'numero_pedras': update_data.get('numero_pedras', current_pedras),
            'active': update_data.get('active', product.get('active', False)),
            'depleted': depleted,
        }
    except Exception as e:
        print(f"Error applying product stock sale: {e}")
        return None


def delete_product(product_id):
    """Delete a product from Firestore."""
    try:
        # Get product data first to get image URLs
        product = get_product_by_id(product_id)
        if product:
            # Delete main image if exists
            if product.get('main_image_url'):
                delete_image(product['main_image_url'])
            
            # Delete additional images if exist
            if product.get('image_2_url'):
                delete_image(product['image_2_url'])
            
            if product.get('image_3_url'):
                delete_image(product['image_3_url'])
        
        # Delete product from Firestore
        db.collection('products').document(product_id).delete()
        return True
    except Exception as e:
        print(f"Error deleting product: {e}")
        return False

# ============== Customer Functions ==============

def get_all_customers():
    """Get all customers from Firestore."""
    try:
        customers_ref = db.collection('customers')
        docs = customers_ref.stream()
        
        customers = []
        for doc in docs:
            customer_data = doc.to_dict()
            customer_data['id'] = doc.id
            customers.append(customer_data)
        
        return customers
    except Exception as e:
        print(f"Error getting all customers: {e}")
        return []

def get_customer_by_firebase_uid(firebase_uid):
    """Find a single customer by Firebase UID using an indexed query (avoids full collection scan)."""
    try:
        docs = db.collection('customers').where('firebase_uid', '==', firebase_uid).limit(1).stream()
        for doc in docs:
            data = doc.to_dict()
            data['id'] = doc.id
            return data
        return None
    except Exception as e:
        print(f"Error getting customer by firebase_uid: {e}")
        return None

def get_customer_by_email(email):
    """Find a single customer by email using an indexed query (avoids full collection scan)."""
    try:
        docs = db.collection('customers').where('email', '==', email).limit(1).stream()
        for doc in docs:
            data = doc.to_dict()
            data['id'] = doc.id
            return data
        return None
    except Exception as e:
        print(f"Error getting customer by email: {e}")
        return None

def get_customer_by_id(customer_id):
    """Get a customer by its ID from Firestore."""
    try:
        doc_ref = db.collection('customers').document(customer_id)
        doc = doc_ref.get()
        
        if doc.exists:
            customer_data = doc.to_dict()
            customer_data['id'] = doc.id
            return customer_data
        else:
            return None
    except Exception as e:
        print(f"Error getting customer by ID: {e}")
        return None

def create_customer(customer_data):
    """Create a new customer in Firestore."""
    try:
        # Add timestamp
        from datetime import datetime
        customer_data['created_at'] = utc_now()
        customer_data['updated_at'] = utc_now()
        
        # Create document with auto-generated ID
        doc_ref = db.collection('customers').document()
        doc_ref.set(customer_data)
        
        # Return the created customer with its ID
        customer_data['id'] = doc_ref.id
        return customer_data
    except Exception as e:
        print(f"Error creating customer: {e}")
        return None

def update_customer(customer_id, customer_data):
    """Update an existing customer in Firestore."""
    try:
        from datetime import datetime
        customer_data['updated_at'] = utc_now()
        
        doc_ref = db.collection('customers').document(customer_id)
        doc_ref.update(customer_data)
        return True
    except Exception as e:
        print(f"Error updating customer: {e}")
        return False

def delete_customer(customer_id):
    """Delete a customer from Firestore."""
    try:
        db.collection('customers').document(customer_id).delete()
        return True
    except Exception as e:
        print(f"Error deleting customer: {e}")
        return False

# ============== Settings Functions ==============

def get_setting(setting_key):
    """Get a setting by its key from Firestore."""
    try:
        doc_ref = db.collection('settings').document(setting_key)
        doc = doc_ref.get()
        
        if doc.exists:
            return doc.to_dict()
        else:
            return None
    except Exception as e:
        err = str(e)
        print(f"Error getting setting: {e}")
        if 'does not exist for project' in err:
            print(
                'Firestore: nenhum banco (default) neste project_id da service account. '
                'Firebase Console (mesmo project_id do account_service.json): '
                'Build > Firestore Database > Criar banco. '
                'Se o banco tiver outro ID, defina FIRESTORE_DATABASE_ID no .env.'
            )
        return None

def get_all_settings():
    """Get all settings from Firestore."""
    try:
        settings_ref = db.collection('settings')
        docs = settings_ref.stream()
        
        settings = []
        for doc in docs:
            setting_data = doc.to_dict()
            setting_data['key'] = doc.id
            settings.append(setting_data)
        
        return settings
    except Exception as e:
        print(f"Error getting all settings: {e}")
        return []

def update_setting(setting_key, setting_data):
    """Update or create a setting in Firestore."""
    try:
        from datetime import datetime
        setting_data['updated_at'] = utc_now()
        
        doc_ref = db.collection('settings').document(setting_key)
        doc = doc_ref.get()
        
        if not doc.exists:
            setting_data['created_at'] = utc_now()
        
        doc_ref.set(setting_data)
        return True
    except Exception as e:
        print(f"Error updating setting: {e}")
        return False

def initialize_default_settings():
    """Initialize default settings if they don't exist."""
    try:
        # Check if default_specifications setting exists
        specifications_setting = get_setting('default_specifications')
        if not specifications_setting:
            # Create default product specifications
            default_specifications = {
                'name': 'Especificações Padrão',
                'description': 'Especificações que aparecem automaticamente ao criar um novo produto',
                'type': 'array',
                'value': ['Lapidação', 'Cor', 'Pureza', 'Dimensões', 'Peso em Quilates', 'Tipo de Gema']
            }
            update_setting('default_specifications', default_specifications)
            print("Default specifications setting created")
    except Exception as e:
        print(f"Error initializing default settings: {e}")

# ============== Currency Exchange Functions ==============

def get_exchange_rate():
    """Get the current USD to BRL exchange rate from settings."""
    return get_setting('exchange_rate')

def update_exchange_rate(rate_data):
    """Update the USD to BRL exchange rate in settings."""
    setting_data = {
        'name': 'Cotação do Dólar',
        'description': 'Taxa de câmbio USD → BRL atualizada',
        'type': 'object',
        'value': rate_data
    }
    update_setting('exchange_rate', setting_data)

# ============== Order Functions ==============

BRT = ZoneInfo('America/Sao_Paulo')


def utc_now():
    """
    Timezone-aware current instant, used for every persisted timestamp.
    Firestore reads naive datetimes back as UTC, so writing naive local time
    would shift every displayed timestamp by the server's UTC offset.
    """
    return datetime.now(timezone.utc)


def format_datetime_brt(dt):
    """Format a datetime for display in horário de Brasília (UTC-3)."""
    if not dt:
        return ''
    if isinstance(dt, str):
        return dt
    try:
        if getattr(dt, 'tzinfo', None) is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BRT).strftime('%d/%m/%Y %H:%M')
    except Exception:
        return str(dt)


def create_order_db(order_data):
    """Create a new order in Firestore orders collection."""
    try:
        from datetime import datetime
        order_data['created_at'] = utc_now()
        order_data['updated_at'] = utc_now()

        doc_ref = db.collection('orders').document()
        doc_ref.set(order_data)
        order_data['id'] = doc_ref.id
        return order_data
    except Exception as e:
        print(f"Error creating order in Firestore: {e}")
        return None


def get_order_by_id(order_id):
    """Get an order by Firestore document ID."""
    try:
        doc_ref = db.collection('orders').document(order_id)
        doc = doc_ref.get()
        if doc.exists:
            data = doc.to_dict()
            data['id'] = doc.id
            return data
        return None
    except Exception as e:
        print(f"Error getting order by ID: {e}")
        return None


def get_order_by_pagarme_id(pagarme_order_id):
    """Get an order by its Pagar.me order ID."""
    try:
        docs = db.collection('orders').where('pagarme_order_id', '==', pagarme_order_id).limit(1).get()
        for doc in docs:
            data = doc.to_dict()
            data['id'] = doc.id
            return data
        return None
    except Exception as e:
        print(f"Error getting order by pagarme_id: {e}")
        return None


def update_order_status(order_id, status, extra_data=None):
    """Update order status and optionally other fields in Firestore."""
    try:
        from datetime import datetime
        update_data = {'status': status, 'updated_at': utc_now()}
        if extra_data:
            update_data.update(extra_data)
        db.collection('orders').document(order_id).update(update_data)
        return True
    except Exception as e:
        print(f"Error updating order status: {e}")
        return False


def update_order_fields(order_id, extra_data):
    """Update arbitrary fields on an order document."""
    if not extra_data:
        return False
    try:
        from datetime import datetime
        update_data = dict(extra_data)
        update_data['updated_at'] = utc_now()
        db.collection('orders').document(order_id).update(update_data)
        return True
    except Exception as e:
        print(f"Error updating order fields: {e}")
        return False


def _order_created_at_sort_key(order):
    ts = order.get('created_at')
    if hasattr(ts, 'strftime'):
        if getattr(ts, 'tzinfo', None) is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts
    return datetime.min.replace(tzinfo=timezone.utc)


def serialize_order_for_display(order):
    """Return a copy of the order with BRT-formatted timestamps."""
    data = dict(order)
    for ts_field in ('created_at', 'updated_at'):
        if ts_field in data and hasattr(data[ts_field], 'strftime'):
            data[ts_field] = format_datetime_brt(data[ts_field])
    return data


def get_all_orders():
    """Get all orders from Firestore, sorted by creation date (newest first)."""
    try:
        docs = db.collection('orders').stream()
        orders = []
        for doc in docs:
            data = doc.to_dict()
            data['id'] = doc.id
            orders.append(data)
        orders.sort(key=_order_created_at_sort_key, reverse=True)
        return [serialize_order_for_display(o) for o in orders]
    except Exception as e:
        print(f"Error getting all orders: {e}")
        return []


def get_orders_by_firebase_uid(firebase_uid):
    """Get all orders belonging to a specific customer (by Firebase UID)."""
    try:
        docs = db.collection('orders').where('firebase_uid', '==', firebase_uid).stream()
        orders = []
        for doc in docs:
            data = doc.to_dict()
            data['id'] = doc.id
            orders.append(data)
        orders.sort(key=_order_created_at_sort_key, reverse=True)
        return orders
    except Exception as e:
        print(f"Error getting orders by firebase_uid: {e}")
        return []


def get_orders_by_customer_email(email):
    """Get all orders belonging to a specific customer (by email)."""
    try:
        docs = db.collection('orders').where('customer_email', '==', email).stream()
        orders = []
        for doc in docs:
            data = doc.to_dict()
            data['id'] = doc.id
            orders.append(data)
        orders.sort(key=_order_created_at_sort_key, reverse=True)
        return orders
    except Exception as e:
        print(f"Error getting orders by customer_email: {e}")
        return []


# ============== Shipping Settings Functions ==============

SHIPPING_SETTING_KEY = 'shipping_config'

# All 27 Brazilian states with sensible default prices (R$)
DEFAULT_STATE_SHIPPING = {
    'AC': 55.0, 'AL': 25.0, 'AP': 55.0, 'AM': 55.0,
    'BA': 30.0, 'CE': 30.0, 'DF': 25.0, 'ES': 25.0,
    'GO': 25.0, 'MA': 35.0, 'MT': 35.0, 'MS': 30.0,
    'MG': 20.0, 'PA': 40.0, 'PB': 30.0, 'PR': 20.0,
    'PE': 25.0, 'PI': 35.0, 'RJ': 20.0, 'RN': 30.0,
    'RS': 20.0, 'RO': 45.0, 'RR': 55.0, 'SC': 20.0,
    'SP': 15.0, 'SE': 25.0, 'TO': 40.0,
}

DEFAULT_FREE_SHIPPING_MINIMUM = 300.0


def get_shipping_settings():
    """
    Return the full shipping config dict:
    {
        'free_shipping_minimum': float,
        'states': { 'SP': float, 'RJ': float, ... }
    }
    """
    setting = get_setting(SHIPPING_SETTING_KEY)
    if setting and 'value' in setting:
        value = setting['value']

        # Firestore sometimes returns the value as a plain Python/JSON string
        # (e.g. if it was written via the generic text editor). Parse it back.
        if isinstance(value, str):
            import json, ast
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                try:
                    value = ast.literal_eval(value)
                except (ValueError, SyntaxError):
                    value = {}

        if not isinstance(value, dict):
            value = {}

        # Ensure both required keys exist
        if 'states' not in value:
            value['states'] = dict(DEFAULT_STATE_SHIPPING)
        if 'free_shipping_minimum' not in value:
            value['free_shipping_minimum'] = DEFAULT_FREE_SHIPPING_MINIMUM
        return value

    # Return defaults if not configured yet
    return {
        'free_shipping_minimum': DEFAULT_FREE_SHIPPING_MINIMUM,
        'states': dict(DEFAULT_STATE_SHIPPING),
    }


def save_shipping_settings(free_shipping_minimum, states):
    """
    Persist shipping config to Firestore settings collection.
    states: dict of { 'SP': float, 'RJ': float, ... }
    """
    setting_data = {
        'name': 'Frete por Região',
        'description': 'Tabela de frete por estado e valor mínimo para frete grátis',
        'type': 'object',
        'value': {
            'free_shipping_minimum': float(free_shipping_minimum),
            'states': {k: float(v) for k, v in states.items()},
        }
    }
    return update_setting(SHIPPING_SETTING_KEY, setting_data)


def calculate_shipping(state_abbr, order_total_brl):
    """
    Return the shipping cost (R$) for the given state and order total.
    Returns 0.0 if order qualifies for free shipping.
    """
    config = get_shipping_settings()
    minimum = float(config.get('free_shipping_minimum', DEFAULT_FREE_SHIPPING_MINIMUM))
    if order_total_brl >= minimum:
        return 0.0
    states = config.get('states', DEFAULT_STATE_SHIPPING)
    state_up = (state_abbr or '').strip().upper()
    return float(states.get(state_up, 0.0))


# Initialize default settings when module is loaded
initialize_default_settings()


# ---------------------------------------------------------------------------
# Solicitações (O que procura?)
# ---------------------------------------------------------------------------

def _format_solicitacao_timestamp(ts):
    if ts and hasattr(ts, 'strftime'):
        return format_datetime_brt(ts)
    return '—'


def _format_solicitacao_replies(replies):
    formatted = []
    for reply in replies or []:
        if not isinstance(reply, dict):
            continue
        formatted.append({
            'message': reply.get('message', ''),
            'images': reply.get('images') or [],
            'created_at': _format_solicitacao_timestamp(reply.get('created_at')),
            'admin_name': reply.get('admin_name', 'Katsumi Joias'),
        })
    return formatted


def _normalize_solicitacao_doc(doc_id, data):
    d = dict(data or {})
    d['id'] = doc_id
    cid = d.get('customer_id')
    if isinstance(cid, dict):
        d['customer_id'] = cid.get('id')
    elif not isinstance(cid, str):
        d['customer_id'] = None
    d['status'] = d.get('status') or ('answered' if d.get('replies') else 'open')
    d['created_at_formatted'] = _format_solicitacao_timestamp(d.get('created_at'))
    d['replies'] = _format_solicitacao_replies(d.get('replies'))
    return d


def create_solicitacao(name, email, phone, description, customer_id=None):
    """
    Saves a 'O que procura?' form submission to Firestore.
    Returns the new document ID.
    """
    data = {
        'name':        name,
        'email':       email,
        'phone':       phone,
        'description': description,
        'customer_id': customer_id,
        'status':      'open',
        'replies':     [],
        'created_at':  firestore.SERVER_TIMESTAMP,
    }
    doc_ref = db.collection('solicitacoes').document()
    doc_ref.set(data)
    return doc_ref.id


def get_solicitacao_by_id(solicitacao_id):
    """Return a single solicitação by Firestore document ID."""
    if not solicitacao_id:
        return None
    try:
        doc = db.collection('solicitacoes').document(solicitacao_id).get()
        if not doc.exists:
            return None
        return _normalize_solicitacao_doc(doc.id, doc.to_dict())
    except Exception as e:
        print(f"Error getting solicitacao by id: {e}")
        return None


def add_solicitacao_reply(solicitacao_id, message, image_urls=None, admin_name='Katsumi Joias'):
    """Append an admin reply to a solicitação. Returns updated doc or None."""
    if not solicitacao_id:
        return None
    message = (message or '').strip()
    image_urls = [u for u in (image_urls or []) if u]
    if not message and not image_urls:
        return None
    try:
        doc_ref = db.collection('solicitacoes').document(solicitacao_id)
        doc = doc_ref.get()
        if not doc.exists:
            return None
        reply = {
            'message': message,
            'images': image_urls,
            'admin_name': admin_name,
            'created_at': utc_now(),
        }
        existing = doc.to_dict().get('replies') or []
        doc_ref.update({
            'replies': existing + [reply],
            'status': 'answered',
            'updated_at': firestore.SERVER_TIMESTAMP,
        })
        return get_solicitacao_by_id(solicitacao_id)
    except Exception as e:
        print(f"Error adding solicitacao reply: {e}")
        return None


def get_solicitacoes_by_email(email):
    """
    Returns all solicitações for a given customer email, newest first.
    """
    if not email:
        return []
    try:
        docs = db.collection('solicitacoes').where('email', '==', email).stream()
        result = []
        for doc in docs:
            d = _normalize_solicitacao_doc(doc.id, doc.to_dict())
            ts = doc.to_dict().get('created_at')
            d['_sort_ts'] = ts if ts and hasattr(ts, 'timestamp') else None
            result.append(d)
        result.sort(key=lambda x: x['_sort_ts'].timestamp() if x.get('_sort_ts') else 0, reverse=True)
        for d in result:
            d.pop('_sort_ts', None)
        return result
    except Exception as e:
        print(f"Error getting solicitacoes by email: {e}")
        return []


def get_all_solicitacoes():
    """
    Returns all solicitações ordered by creation date descending.
    """
    docs = db.collection('solicitacoes').order_by(
        'created_at', direction=firestore.Query.DESCENDING
    ).stream()
    result = []
    for doc in docs:
        result.append(_normalize_solicitacao_doc(doc.id, doc.to_dict()))
    return result


# ---------------------------------------------------------------------------
# Login tokens — opaque UUID tokens that map to Firebase sign-in links
# ---------------------------------------------------------------------------

def save_login_token(token: str, firebase_link: str, email: str, expires_at) -> bool:
    """Store a one-time login token with its associated Firebase sign-in link."""
    try:
        db.collection('login_tokens').document(token).set({
            'firebase_link': firebase_link,
            'email': email,
            'expires_at': expires_at,
            'created_at': utc_now(),
        })
        return True
    except Exception as e:
        print(f"Error saving login token: {e}")
        return False


def get_and_delete_login_token(token: str):
    """
    Atomically read and delete a login token document.
    Returns the doc dict on success, or None if not found.
    Does NOT validate expiry — caller is responsible.
    """
    try:
        ref = db.collection('login_tokens').document(token)
        doc = ref.get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        ref.delete()
        return data
    except Exception as e:
        print(f"Error retrieving login token: {e}")
        return None


# ---------------------------------------------------------------------------
# Vendedores (equipe comercial + comissão)
# ---------------------------------------------------------------------------

def get_all_sellers(only_active=False):
    """Return every seller, alphabetically by name."""
    try:
        sellers = []
        for doc in db.collection('sellers').stream():
            data = doc.to_dict()
            data['id'] = doc.id
            if only_active and not data.get('active', True):
                continue
            sellers.append(data)
        sellers.sort(key=lambda s: (s.get('name') or '').lower())
        return sellers
    except Exception as e:
        print(f"Error getting sellers: {e}")
        return []


def get_seller_by_id(seller_id):
    """Return a single seller by document ID."""
    if not seller_id:
        return None
    try:
        doc = db.collection('sellers').document(seller_id).get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        data['id'] = doc.id
        return data
    except Exception as e:
        print(f"Error getting seller by ID: {e}")
        return None


def create_seller(seller_data):
    """Create a seller. Returns the created document including its ID."""
    try:
        seller_data = dict(seller_data)
        seller_data['created_at'] = utc_now()
        seller_data['updated_at'] = utc_now()
        doc_ref = db.collection('sellers').document()
        doc_ref.set(seller_data)
        seller_data['id'] = doc_ref.id
        return seller_data
    except Exception as e:
        print(f"Error creating seller: {e}")
        return None


def update_seller(seller_id, seller_data):
    """Update a seller document."""
    try:
        payload = dict(seller_data)
        payload['updated_at'] = utc_now()
        db.collection('sellers').document(seller_id).update(payload)
        return True
    except Exception as e:
        print(f"Error updating seller: {e}")
        return False


def delete_seller(seller_id):
    """Delete a seller document."""
    try:
        db.collection('sellers').document(seller_id).delete()
        return True
    except Exception as e:
        print(f"Error deleting seller: {e}")
        return False


# ---------------------------------------------------------------------------
# Logs de atividade (auditoria de criação / edição / deleção)
# ---------------------------------------------------------------------------

LOG_ACTION_CREATE = 'create'
LOG_ACTION_UPDATE = 'update'
LOG_ACTION_DELETE = 'delete'


def _loggable_value(value):
    """Flatten a value into something readable and Firestore-safe for the log."""
    if value is None or value == '':
        return '—'
    if isinstance(value, bool):
        return 'Sim' if value else 'Não'
    if isinstance(value, (int, float)):
        # 2 e 2.0 devem gerar o mesmo texto, senão o diff acusa mudança inexistente
        return str(int(value)) if float(value).is_integer() else f'{float(value):g}'
    if isinstance(value, str):
        return value if len(value) <= 300 else value[:297] + '…'
    if isinstance(value, dict):
        parts = [f'{k}: {_loggable_value(v)}' for k, v in sorted(value.items()) if v not in (None, '')]
        return '; '.join(parts)[:300] or '—'
    if isinstance(value, (list, tuple)):
        return f'{len(value)} item(ns)'
    if hasattr(value, 'strftime'):
        return format_datetime_brt(value)
    return str(value)[:300]


LOG_IGNORED_FIELDS = {
    'updated_at', 'created_at', 'sku_mode', 'id',
    'products_marked_sold', 'amount_centavos',
}


def diff_for_log(before, after, ignore_fields=None):
    """
    Build a list of {field, before, after} for every field that actually changed.
    Fields absent from `after` are treated as untouched (partial updates).
    Values are compared after formatting, so equivalent representations
    (None vs '', 3 vs 3.0) don't produce phantom entries.
    """
    ignored = set(ignore_fields or ()) | LOG_IGNORED_FIELDS
    before = before or {}
    after = after or {}
    changes = []
    for field in after.keys():
        if field in ignored:
            continue
        old_display = _loggable_value(before.get(field))
        new_display = _loggable_value(after.get(field))
        if old_display == new_display:
            continue
        changes.append({
            'field': field,
            'before': old_display,
            'after': new_display,
        })
    changes.sort(key=lambda c: c['field'])
    return changes


def create_activity_log(entity_type, entity_id, entity_label, action,
                        changes=None, actor_email='', actor_uid='', source=None, note=''):
    """Persist one audit entry. Never raises — logging must not break the operation."""
    try:
        db.collection('activity_logs').document().set({
            'entity_type': entity_type,
            'entity_id': entity_id or '',
            'entity_label': entity_label or '',
            'action': action,
            'changes': changes or [],
            'actor_email': actor_email or 'sistema',
            'actor_uid': actor_uid or '',
            'source': source or '',
            'note': note or '',
            'created_at': utc_now(),
        })
        return True
    except Exception as e:
        print(f"Error creating activity log: {e}")
        return False


def get_activity_logs(limit=300, entity_type=None, action=None, actor_email=None):
    """Return audit entries newest-first, optionally filtered."""
    try:
        query = db.collection('activity_logs')
        if entity_type:
            query = query.where('entity_type', '==', entity_type)
        if action:
            query = query.where('action', '==', action)
        if actor_email:
            query = query.where('actor_email', '==', actor_email)
        query = query.order_by('created_at', direction=firestore.Query.DESCENDING).limit(limit)

        logs = []
        for doc in query.stream():
            data = doc.to_dict()
            data['id'] = doc.id
            data['created_at_formatted'] = format_datetime_brt(data.get('created_at'))
            logs.append(data)
        return logs
    except Exception as e:
        # A missing composite index shows up here — fall back to an unfiltered scan.
        print(f"Error getting activity logs: {e}")
        try:
            logs = []
            for doc in db.collection('activity_logs').stream():
                data = doc.to_dict()
                data['id'] = doc.id
                logs.append(data)
            logs.sort(key=_order_created_at_sort_key, reverse=True)
            filtered = [
                l for l in logs
                if (not entity_type or l.get('entity_type') == entity_type)
                and (not action or l.get('action') == action)
                and (not actor_email or l.get('actor_email') == actor_email)
            ][:limit]
            for l in filtered:
                l['created_at_formatted'] = format_datetime_brt(l.get('created_at'))
            return filtered
        except Exception as inner:
            print(f"Error on activity log fallback: {inner}")
            return []


def get_distinct_log_actors(limit=500):
    """Actor e-mails present in the log, for the filter dropdown."""
    try:
        actors = set()
        for doc in db.collection('activity_logs').select(['actor_email']).limit(limit).stream():
            email = (doc.to_dict() or {}).get('actor_email')
            if email:
                actors.add(email)
        return sorted(actors)
    except Exception as e:
        print(f"Error getting log actors: {e}")
        return []


# ---------------------------------------------------------------------------
# Pedidos internos — consultas auxiliares
# ---------------------------------------------------------------------------

def get_orders_by_customer_id(customer_id):
    """Orders linked to a Firestore customer document, newest first."""
    if not customer_id:
        return []
    try:
        orders = []
        for doc in db.collection('orders').where('firestore_customer_id', '==', customer_id).stream():
            data = doc.to_dict()
            data['id'] = doc.id
            orders.append(data)
        orders.sort(key=_order_created_at_sort_key, reverse=True)
        return orders
    except Exception as e:
        print(f"Error getting orders by customer_id: {e}")
        return []
