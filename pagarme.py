"""
Pagar.me API v5 client for Katsumi Joias.

All monetary values in Pagar.me are in centavos (1 BRL = 100 centavos).
Authentication uses HTTP Basic Auth with the secret API key as username and empty password.
"""

import os
import re
import base64
import hmac
import hashlib
import json
import uuid
import requests
from datetime import datetime, timedelta, timezone

PAGARME_BASE_URL = "https://api.pagar.me/core/v5"


def _get_api_key():
    key = os.getenv("PAGARME_API_KEY", "")
    if not key:
        print("[Pagar.me] WARNING: PAGARME_API_KEY not set")
    return key


def _get_public_key():
    return os.getenv("PAGARME_CHAVE_PUBLICA", "")


def _get_headers(idempotency_key=None):
    """Build authorization headers for Pagar.me API v5."""
    api_key = _get_api_key()
    token = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("utf-8")
    headers = {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if idempotency_key:
        headers["idempotency-key"] = idempotency_key
    return headers


def _request(method, path, data=None, params=None, idempotency_key=None):
    """
    Make an authenticated request to Pagar.me API v5.
    Returns (response_data, error_dict).
    error_dict is None on success.
    """
    url = f"{PAGARME_BASE_URL}{path}"
    headers = _get_headers(idempotency_key)

    try:
        response = requests.request(
            method,
            url,
            headers=headers,
            json=data,
            params=params,
            timeout=30,
        )

        try:
            response_data = response.json()
        except ValueError:
            response_data = {}

        if not response.ok:
            error_msg = response_data.get("message", f"HTTP {response.status_code}")
            errors = response_data.get("errors", {})
            print(f"[Pagar.me] Error {response.status_code} on {method} {path}: {error_msg} | {errors}")
            return None, {
                "message": error_msg,
                "errors": errors,
                "status_code": response.status_code,
            }

        return response_data, None

    except requests.exceptions.Timeout:
        msg = "Timeout ao conectar ao gateway de pagamento. Tente novamente."
        print(f"[Pagar.me] Timeout on {method} {path}")
        return None, {"message": msg}
    except requests.exceptions.ConnectionError:
        msg = "Erro de conexão com o gateway de pagamento."
        print(f"[Pagar.me] ConnectionError on {method} {path}")
        return None, {"message": msg}
    except Exception as e:
        print(f"[Pagar.me] Unexpected error on {method} {path}: {e}")
        return None, {"message": str(e)}


# ──────────────────────────────────────────────────────────────────────────────
# Customers
# ──────────────────────────────────────────────────────────────────────────────

def find_customer_by_email(email):
    """Search for an existing customer by email in Pagar.me."""
    result, error = _request("GET", "/customers", params={"email": email})
    if error:
        return None, error
    customers = result.get("data", [])
    if customers:
        return customers[0], None
    return None, None


def create_customer(name, email, document, phone_area_code, phone_number, phone_country_code="55"):
    """
    Create a customer in Pagar.me.
    document: CPF (11 digits) or CNPJ (14 digits) — digits only.
    Returns (customer_data, error).
    """
    doc_clean = re.sub(r"\D", "", document or "")
    phone_clean = re.sub(r"\D", "", phone_number or "")
    area_clean = re.sub(r"\D", "", phone_area_code or "11")

    customer_type = "individual" if len(doc_clean) == 11 else "company"
    doc_type = "CPF" if len(doc_clean) == 11 else "CNPJ"

    payload = {
        "name": name,
        "email": email,
        "type": customer_type,
        "document": doc_clean,
        "document_type": doc_type,
        "phones": {
            "mobile_phone": {
                "country_code": phone_country_code,
                "area_code": area_clean,
                "number": phone_clean,
            }
        },
    }

    return _request("POST", "/customers", payload)


def get_or_create_customer(name, email, document, phone_area_code, phone_number):
    """
    Find an existing customer by email or create a new one.
    Returns (customer_data, error).
    """
    existing, err = find_customer_by_email(email)
    if err:
        # Non-fatal: proceed to create
        print(f"[Pagar.me] Could not search customer, will create: {err}")
    if existing:
        return existing, None
    return create_customer(name, email, document, phone_area_code, phone_number)


# ──────────────────────────────────────────────────────────────────────────────
# Orders
# ──────────────────────────────────────────────────────────────────────────────

def _build_pix_payment():
    return {
        "payment_method": "pix",
        "pix": {
            "expires_in": 3600,  # 1 hour to pay
        },
    }


def _build_credit_card_payment(card_token, installments=1, holder_name=None, billing_address=None):
    """
    Build credit_card payment for Pagar.me v5.
    When using card_token, billing_address must be nested under credit_card.card
    (not on credit_card directly) — required for antifraud.
    """
    credit_card = {
        "recurrence": False,
        "installments": max(1, min(5, int(installments))),
        "statement_descriptor": "KATSUMI JOIAS",
        "card_token": card_token,
    }
    if billing_address:
        credit_card["card"] = {"billing_address": billing_address}

    return {
        "payment_method": "credit_card",
        "credit_card": credit_card,
    }


def _build_boleto_payment():
    due_at = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "payment_method": "boleto",
        "boleto": {
            "instructions": "Pague este boleto para confirmar sua compra na Katsumi Joias.",
            "due_at": due_at,
            "document_number": uuid.uuid4().hex[:8].upper(),
            "type": "DM",
        },
    }


def create_order(
    customer_id,
    items,
    payment_method,
    payment_extra=None,
    shipping=None,
    billing_address=None,
    idempotency_key=None,
):
    """
    Create an order in Pagar.me.

    Parameters
    ----------
    customer_id : str
        Pagar.me customer ID (cus_xxx).
    items : list of dicts
        Each item: {amount (centavos), description (str), quantity (int), code (str)}.
    payment_method : str
        One of: 'pix', 'credit_card', 'boleto'.
    payment_extra : dict, optional
        For credit_card: {'card_token': str, 'installments': int}
    shipping : dict, optional
        {'amount': 0, 'description': str, 'recipient_name': str,
         'address': {'zip_code', 'line_1', 'city', 'state', 'country'}}
    idempotency_key : str, optional
        Prevents duplicate orders on retries.

    Returns
    -------
    (order_data, error)
    """
    if payment_extra is None:
        payment_extra = {}

    # Sanitize item fields
    sanitized_items = []
    for item in items:
        desc = str(item.get("description", "Produto"))[:64]
        code = re.sub(r"[^a-zA-Z0-9_\-]", "", str(item.get("code", "PROD")))[:52] or "PROD"
        sanitized_items.append({
            "amount": int(item["amount"]),
            "description": desc,
            "quantity": max(1, int(item.get("quantity", 1))),
            "code": code,
        })

    # Build payment object
    if payment_method == "pix":
        payment = _build_pix_payment()
    elif payment_method == "credit_card":
        card_token = payment_extra.get("card_token")
        if not card_token:
            return None, {"message": "Token do cartão não fornecido."}
        payment = _build_credit_card_payment(
            card_token,
            installments=payment_extra.get("installments", 1),
            holder_name=payment_extra.get("holder_name"),
            billing_address=billing_address,
        )
    elif payment_method == "boleto":
        payment = _build_boleto_payment()
    else:
        return None, {"message": f"Método de pagamento inválido: {payment_method}"}

    payload = {
        "items": sanitized_items,
        "customer_id": customer_id,
        "payments": [payment],
        "closed": True,
    }

    # Add shipping if provided (optional for Pagar.me)
    if shipping:
        payload["shipping"] = shipping

    return _request("POST", "/orders", payload, idempotency_key=idempotency_key)


def get_order(pagarme_order_id):
    """Fetch order details from Pagar.me. Returns (order_data, error)."""
    return _request("GET", f"/orders/{pagarme_order_id}")


def create_pix_charge_for_order(pagarme_order_id, amount_centavos=None, idempotency_key=None):
    """
    Create a new PIX charge associated with an existing Pagar.me order (e.g. after QR expiry).
    Uses POST /charges with order_id. The Pagar.me v5 /charges endpoint requires
    payment info nested inside a 'payment' object.
    """
    payload = {
        "order_id": pagarme_order_id,
        "payment": {
            "payment_method": "pix",
            "pix": {
                "expires_in": 3600,
            },
        },
    }
    if amount_centavos:
        payload["amount"] = int(amount_centavos)
    return _request(
        "POST",
        "/charges",
        payload,
        idempotency_key=idempotency_key,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Webhook verification
# ──────────────────────────────────────────────────────────────────────────────

def verify_webhook_signature(raw_payload: bytes, signature_header: str, secret: str = None) -> bool:
    """
    Verify the Pagar.me webhook HMAC-SHA256 signature.
    Pagar.me sends the signature as hex in the X-Hub-Signature header.
    """
    if not signature_header:
        return False
    if secret is None:
        secret = _get_api_key()
    try:
        parts = signature_header.split("=", 1)
        if len(parts) != 2 or parts[0] != "sha256":
            return False
        received_sig = parts[1]
        expected_sig = hmac.new(
            secret.encode("utf-8"),
            raw_payload,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(received_sig, expected_sig)
    except Exception as e:
        print(f"[Pagar.me] Webhook signature error: {e}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def extract_pix_data(order_data):
    """Extract PIX QR code and copy-paste code from order or charge response."""
    try:
        last_transaction = order_data.get('last_transaction')
        if last_transaction and last_transaction.get('transaction_type') == 'pix':
            return {
                'qr_code': last_transaction.get('qr_code', ''),
                'qr_code_url': last_transaction.get('qr_code_url', ''),
                'expires_at': last_transaction.get('expires_at', ''),
            }

        charges = order_data.get('charges', [])
        for charge in charges:
            last_transaction = charge.get('last_transaction', {})
            if last_transaction.get('transaction_type') == 'pix':
                return {
                    'qr_code': last_transaction.get('qr_code', ''),
                    'qr_code_url': last_transaction.get('qr_code_url', ''),
                    'expires_at': last_transaction.get('expires_at', ''),
                }
    except Exception:
        pass
    return {}


def extract_boleto_data(order_data):
    """Extract boleto URL and barcode from order response."""
    try:
        charges = order_data.get("charges", [])
        for charge in charges:
            last_transaction = charge.get("last_transaction", {})
            if last_transaction.get("transaction_type") == "boleto":
                return {
                    "boleto_url": last_transaction.get("url", ""),
                    "boleto_barcode": last_transaction.get("line", ""),
                    "due_at": last_transaction.get("due_at", ""),
                    "pdf": last_transaction.get("pdf", ""),
                }
    except Exception:
        pass
    return {}


def extract_card_data(order_data):
    """Extract credit card transaction result from order response."""
    try:
        charges = order_data.get("charges", [])
        for charge in charges:
            last_transaction = charge.get("last_transaction", {})
            if last_transaction.get("transaction_type") == "credit_card":
                return {
                    "status": charge.get("status", ""),
                    "acquirer_name": last_transaction.get("acquirer_name", ""),
                    "acquirer_tid": last_transaction.get("acquirer_tid", ""),
                    "authorization_code": last_transaction.get("acquirer_auth_code", ""),
                    "brand": last_transaction.get("card", {}).get("brand", ""),
                    "last_four": last_transaction.get("card", {}).get("last_four_digits", ""),
                }
    except Exception:
        pass
    return {}
