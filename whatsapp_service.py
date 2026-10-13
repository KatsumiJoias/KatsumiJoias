"""Transactional WhatsApp delivery via WhatsGW API."""

import os
import re
import uuid

import requests

WHATSGW_API_URL = 'https://app.whatsgw.com.br/api/WhatsGw/Send'
DEFAULT_SITE_URL = os.getenv('SITE_URL', '')
SUPPORT_WHATSAPP_NUMBER = os.getenv('SUPPORT_WHATSAPP_NUMBER', '5531911500029').strip()
SUPPORT_WHATSAPP_DISPLAY = '(31) 9115-0029'


def support_wa_me_url(text=''):
    """Public wa.me link for the support line (widget and site CTAs)."""
    base = f'https://wa.me/{SUPPORT_WHATSAPP_NUMBER}'
    if not text:
        return base
    from urllib.parse import quote
    return f'{base}?text={quote(text)}'


def _log(message):
    print(f"[WhatsApp] {message}")


def _get_api_key():
    return os.getenv('WHATSGW_API_KEY', '').strip()


def _get_sender_phone():
    return os.getenv('WHATSGW_PHONE_NUMBER', '').strip()


def _get_instancia_id():
    return os.getenv('WHATSGW_INSTANCIA_ID', '').strip()


def _site_url():
    return (os.getenv('SITE_URL') or DEFAULT_SITE_URL).rstrip('/')


def normalize_phone(phone):
    """Return digits only, prefixed with 55 when missing country code."""
    digits = re.sub(r'\D', '', phone or '')
    if not digits:
        return ''
    if digits.startswith('55'):
        return digits
    if len(digits) in (10, 11):
        return f'55{digits}'
    return digits


def _format_order_number(order):
    order_id = order.get('id', '') if isinstance(order, dict) else ''
    return order_id[:8].upper() if order_id else 's/n'


def _format_brl(value):
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = 0.0
    formatted = f'{amount:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
    return f'R$ {formatted}'


def send_text_message(contact_phone, message_body, message_custom_id=None):
    """Send a plain-text WhatsApp message to a customer phone number."""
    try:
        api_key = _get_api_key()
        sender_phone = _get_sender_phone()
        if not api_key:
            _log('WHATSGW_API_KEY not configured — skipping send')
            return False
        if not sender_phone:
            _log('WHATSGW_PHONE_NUMBER not configured — skipping send')
            return False

        contact = normalize_phone(contact_phone)
        sender = normalize_phone(sender_phone)
        if not contact:
            _log('Missing or invalid recipient phone — skipping send')
            return False

        payload = {
            'apikey': api_key,
            'phone_number': sender,
            'contact_phone_number': contact,
            'message_type': 'text',
            'message_body': message_body,
            'check_status': '1',
            'message_to_group': '0',
            'simule_typing': '1',
            'message_chat_type': 'user',
        }
        if message_custom_id:
            payload['message_custom_id'] = message_custom_id
        else:
            payload['message_custom_id'] = f'kj-{uuid.uuid4().hex[:16]}'

        instancia_id = _get_instancia_id()
        if instancia_id:
            payload['w_instancia_id'] = instancia_id

        response = requests.post(
            WHATSGW_API_URL,
            data=payload,
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=20,
        )
        if response.status_code == 200:
            _log(f'Sent successfully to {contact[-4:].rjust(len(contact), "*")}')
            return True
        _log(f'API error status={response.status_code} response={(response.text or "")[:200]}')
        return False
    except Exception as exc:
        _log(f'send_text_message failed: {type(exc).__name__}: {exc}')
        return False


def send_order_created_whatsapp(order):
    try:
        phone = (order.get('customer_phone') or '').strip()
        if not phone:
            _log('ORDER_CREATED skipped — missing customer phone')
            return False

        name = order.get('customer_name', 'Cliente').split()[0]
        order_number = _format_order_number(order)
        total = _format_brl(order.get('amount_brl', 0))
        order_url = f"{_site_url()}/checkout/success/{order.get('id', '')}"

        payment = order.get('payment_method', '')
        if payment == 'pix':
            payment_line = 'Conclua o pagamento via *PIX* para garantirmos suas gemas.'
        elif payment == 'credit_card':
            payment_line = 'Estamos aguardando a confirmação do pagamento no *cartão*.'
        else:
            payment_line = 'Estamos aguardando a confirmação do pagamento.'

        message = (
            f'Olá, {name}! 💎\n\n'
            f'Recebemos seu pedido *#{order_number}* na Katsumi Joias.\n'
            f'Valor total: *{total}*\n\n'
            f'{payment_line}\n'
            f'Acompanhe em: {order_url}\n\n'
            f'Dúvidas? Responda esta mensagem ou fale conosco: {SUPPORT_WHATSAPP_DISPLAY}'
        )
        _log(f'ORDER_CREATED sending for order #{order_number}')
        return send_text_message(phone, message, message_custom_id=f'order-created-{order.get("id", "")[:12]}')
    except Exception as exc:
        _log(f'ORDER_CREATED failed: {type(exc).__name__}: {exc}')
        return False


def send_order_paid_whatsapp(order):
    try:
        phone = (order.get('customer_phone') or '').strip()
        if not phone:
            _log('ORDER_PAID skipped — missing customer phone')
            return False

        name = order.get('customer_name', 'Cliente').split()[0]
        order_number = _format_order_number(order)
        order_url = f"{_site_url()}/checkout/success/{order.get('id', '')}"

        message = (
            f'Pagamento confirmado, {name}! ✅\n\n'
            f'Seu pedido *#{order_number}* foi pago com sucesso.\n'
            f'Suas gemas entram em preparação e você receberá o código de rastreio assim que despacharmos.\n\n'
            f'Acompanhe: {order_url}\n\n'
            f'Katsumi Joias · Gemas brasileiras lapidadas desde 1942'
        )
        _log(f'ORDER_PAID sending for order #{order_number}')
        return send_text_message(phone, message, message_custom_id=f'order-paid-{order.get("id", "")[:12]}')
    except Exception as exc:
        _log(f'ORDER_PAID failed: {type(exc).__name__}: {exc}')
        return False


def send_order_tracking_whatsapp(order, tracking_code):
    try:
        phone = (order.get('customer_phone') or '').strip()
        if not phone:
            _log('ORDER_TRACKING skipped — missing customer phone')
            return False

        name = order.get('customer_name', 'Cliente').split()[0]
        order_number = _format_order_number(order)
        tracking_url = 'https://rastreamento.correios.com.br/app/index.php'

        message = (
            f'Olá, {name}! 📦\n\n'
            f'Seu pedido *#{order_number}* foi despachado.\n'
            f'Código de rastreio: *{tracking_code}*\n\n'
            f'Acompanhe nos Correios:\n{tracking_url}\n\n'
            f'Katsumi Joias · Obrigado pela confiança'
        )
        _log(f'ORDER_TRACKING sending for order #{order_number}')
        return send_text_message(phone, message, message_custom_id=f'order-tracking-{order.get("id", "")[:12]}')
    except Exception as exc:
        _log(f'ORDER_TRACKING failed: {type(exc).__name__}: {exc}')
        return False


def send_solicitacao_reply_whatsapp(solicitacao, reply_message):
    try:
        phone = (solicitacao.get('phone') or '').strip()
        if not phone:
            _log('SOLICITACAO_REPLY skipped — missing phone')
            return False

        name = (solicitacao.get('name') or 'Cliente').split()[0]
        excerpt = (reply_message or '').strip()
        if len(excerpt) > 500:
            excerpt = excerpt[:497] + '...'

        message = (
            f'Olá, {name}! 💎\n\n'
            f'Nossa equipe respondeu à sua solicitação na Katsumi Joias:\n\n'
            f'{excerpt}\n\n'
            f'Veja mais detalhes em: {_site_url()}/account'
        )
        _log('SOLICITACAO_REPLY sending')
        return send_text_message(phone, message, message_custom_id=f'solicitacao-{solicitacao.get("id", "")[:12]}')
    except Exception as exc:
        _log(f'SOLICITACAO_REPLY failed: {type(exc).__name__}: {exc}')
        return False
