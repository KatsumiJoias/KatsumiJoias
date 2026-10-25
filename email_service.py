"""Transactional email delivery via Brevo (Sendinblue) API."""

import os
import re
from datetime import datetime, timedelta, timezone

import requests
from flask import render_template
from markupsafe import Markup, escape

import whatsapp_service
from env_utils import env_str, public_site_url

_BRT = timezone(timedelta(hours=-3))

BREVO_API_URL = 'https://api.brevo.com/v3/smtp/email'
SENDER = {'name': 'Katsumi Joias', 'email': 'contato@katsumijoias.com.br'}
PAID_STATUSES = frozenset({'paid', 'authorized'})


def is_paid_status(status):
    return status in PAID_STATUSES


def should_send_paid_email(order, previous_status):
    return (
        is_paid_status(order.get('status', ''))
        and not is_paid_status(previous_status or '')
        and not order.get('paid_email_sent_at')
    )


def _log_email(message):
    """Write email events to stdout (visible in dev server / Cloud Run logs)."""
    print(f"[Email] {message}")


def _get_api_key():
    return env_str('API_KEY_BREVO')


def _format_order_number(order):
    order_id = order.get('id', '')
    return order_id[:8].upper() if order_id else 's/n'


def _format_brl(value):
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = 0.0
    formatted = f'{amount:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
    return f'R$ {formatted}'


def _site_url():
    from flask import has_request_context, request
    req = request if has_request_context() else None
    return public_site_url(req)


def _parse_datetime(value):
    """Parse a datetime value and return it as BRT (UTC-3)."""
    if value is None:
        return None
    dt = None
    if hasattr(value, 'strftime'):
        dt = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        # Try ISO with explicit timezone first (covers 'Z' and '+HH:MM')
        try:
            dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        except ValueError:
            # Fall back to naive formats — treat as UTC since Cloud Run uses UTC
            for fmt in ('%d/%m/%Y %H:%M', '%d/%m/%Y %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S'):
                try:
                    naive = datetime.strptime(raw.replace('Z', ''), fmt)
                    dt = naive.replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
        if dt is None:
            return None

    # Firestore Timestamps have a .tzinfo already; naive datetimes are UTC (Cloud Run)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(_BRT)


def _format_order_date(order):
    created = _parse_datetime(order.get('created_at'))
    if created:
        return created.strftime('%d/%m/%Y')
    return datetime.now(_BRT).strftime('%d/%m/%Y')


def _format_pix_expiration(order):
    expires = _parse_datetime(order.get('pix_expires_at'))
    if expires:
        return expires.strftime('%d/%m/%Y às %H:%M')

    created = _parse_datetime(order.get('created_at'))
    if created:
        fallback = created + timedelta(hours=1)
        return fallback.strftime('%d/%m/%Y às %H:%M')

    return (datetime.now(_BRT) + timedelta(hours=1)).strftime('%d/%m/%Y às %H:%M')


def _format_paid_at(order):
    for field in ('paid_at', 'webhook_received_at', 'paid_email_sent_at', 'updated_at'):
        paid = _parse_datetime(order.get(field))
        if paid:
            return paid.strftime('%d/%m/%Y às %H:%M')
    return None


def _format_shipping_address(order):
    addr = order.get('shipping_address') or {}
    if not isinstance(addr, dict) or not addr:
        return None

    lines = []
    street = (addr.get('street') or '').strip()
    number = (addr.get('number') or '').strip()
    if street or number:
        line = f'{street}, {number}'.strip(', ')
        complement = (addr.get('complement') or '').strip()
        if complement:
            line = f'{line}, {complement}'
        lines.append(line)

    neighborhood = (addr.get('neighborhood') or '').strip()
    if neighborhood:
        lines.append(neighborhood)

    city = (addr.get('city') or '').strip()
    state = (addr.get('state') or '').strip()
    cep = (addr.get('cep') or '').strip()
    city_line = ', '.join(part for part in (city, state) if part)
    if cep:
        city_line = f'{city_line}, {cep}' if city_line else cep
    if city_line:
        lines.append(city_line)

    if not lines:
        return None
    return Markup('<br>'.join(escape(line) for line in lines))


def _pix_code_for_email(raw_code):
    """Split PIX into spans to reduce e-mail client auto-linkification."""
    if not raw_code:
        return Markup('')
    parts = []
    for i in range(0, len(raw_code), 4):
        chunk = escape(raw_code[i:i + 4])
        parts.append(f'<span style="color:#333333;text-decoration:none;">{chunk}</span>')
    return Markup(''.join(parts))


def _prepare_order_context(order):
    items = []
    for item in order.get('items') or []:
        qty = item.get('quantity', 1) or 1
        price = item.get('amount_brl', 0) or 0
        line_total = float(price) * qty
        items.append({
            'name': item.get('name', 'Produto'),
            'quantity': qty,
            'price': _format_brl(line_total),
            'price_formatted': _format_brl(price),
            'subtotal_formatted': _format_brl(line_total),
            'details': item.get('details', '') or '',
        })

    payment_method = order.get('payment_method', '') or ''
    payment_labels = {
        'pix': 'PIX',
        'credit_card': 'Cartão de crédito',
        'boleto': 'Boleto bancário',
    }
    shipping_cost = float(order.get('shipping_cost_brl') or 0)
    order_id = order.get('id', '')
    site_url = _site_url()
    order_url = f'{site_url}/checkout/success/{order_id}' if order_id else site_url

    return {
        'customer_name': order.get('customer_name', 'Cliente'),
        'order_number': _format_order_number(order),
        'order_date': _format_order_date(order),
        'paid_at': _format_paid_at(order),
        'items': items,
        'subtotal_formatted': _format_brl(order.get('subtotal_brl', 0)),
        'shipping': _format_brl(shipping_cost) if shipping_cost > 0 else None,
        'shipping_formatted': _format_brl(order.get('shipping_cost_brl', 0)),
        'shipping_address': _format_shipping_address(order),
        'total': _format_brl(order.get('amount_brl', 0)),
        'total_formatted': _format_brl(order.get('amount_brl', 0)),
        'payment_method': payment_method,
        'payment_method_label': payment_labels.get(payment_method, payment_method),
        'pix_code': order.get('pix_qr_code', '') or '',
        'pix_code_html': _pix_code_for_email(order.get('pix_qr_code', '') or ''),
        'pix_expiration': _format_pix_expiration(order),
        'order_url': order_url,
        'copy_pix_url': f'{site_url}/pix/copiar/{order_id}' if order_id and order.get('pix_qr_code') else order_url,
        'retry_url': order_url if order_id else f'{site_url}/checkout',
        'site_url': site_url,
        'current_year': datetime.now().year,
    }


def _html_to_text(html_content):
    text = re.sub(r'(?i)<br\s*/?>', '\n', html_content or '')
    text = re.sub(r'(?i)</p>', '\n\n', text)
    text = re.sub(r'(?i)</tr>', '\n', text)
    text = re.sub(r'(?i)</h[1-6]>', '\n\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _send_email(to_email, to_name, subject, html_content):
    api_key = _get_api_key()
    if not api_key:
        _log_email('API_KEY_BREVO not configured — skipping send')
        return False

    payload = {
        'sender': SENDER,
        'to': [{'email': to_email, 'name': to_name or to_email}],
        'subject': subject,
        'htmlContent': html_content,
        'textContent': _html_to_text(html_content),
    }

    try:
        response = requests.post(
            BREVO_API_URL,
            json=payload,
            headers={
                'api-key': api_key,
                'Content-Type': 'application/json',
                'Accept': 'application/json',
            },
            timeout=15,
        )
        if response.status_code in (200, 201):
            _log_email(f'Sent successfully (HTTP {response.status_code})')
            return True

        _log_email(f'Brevo API error status={response.status_code} response={(response.text or "")[:200]}')
        return False
    except requests.RequestException as exc:
        _log_email(f'Request failed: {type(exc).__name__}')
        return False


def send_order_created_email(order):
    """Notify customer that the order was received and payment is pending."""
    try:
        to_email = (order.get('customer_email') or '').strip()
        if not to_email:
            _log_email('ORDER_CREATED skipped — missing customer email')
            return False

        context = _prepare_order_context(order)
        html_content = render_template('emails/order_created.html', **context)
        subject = f"Recebemos seu pedido #{context['order_number']} | aguardando pagamento"
        _log_email(f'ORDER_CREATED sending to order #{context["order_number"]}')
        sent = _send_email(to_email, context['customer_name'], subject, html_content)
        try:
            whatsapp_service.send_order_created_whatsapp(order)
        except Exception as wa_exc:
            _log_email(f'ORDER_CREATED WhatsApp failed: {type(wa_exc).__name__}')
        return sent
    except Exception as exc:
        _log_email(f'ORDER_CREATED failed: {type(exc).__name__}: {exc}')
        return False


def send_order_paid_email(order):
    """Notify customer that payment was confirmed."""
    try:
        to_email = (order.get('customer_email') or '').strip()
        if not to_email:
            _log_email('ORDER_PAID skipped — missing customer email')
            return False

        context = _prepare_order_context(order)
        html_content = render_template('emails/order_paid.html', **context)
        subject = f"Pagamento confirmado! Pedido #{context['order_number']}"
        _log_email(f'ORDER_PAID sending to order #{context["order_number"]}')
        sent = _send_email(to_email, context['customer_name'], subject, html_content)
        try:
            whatsapp_service.send_order_paid_whatsapp(order)
        except Exception as wa_exc:
            _log_email(f'ORDER_PAID WhatsApp failed: {type(wa_exc).__name__}')
        return sent
    except Exception as exc:
        _log_email(f'ORDER_PAID failed: {type(exc).__name__}: {exc}')
        return False


def send_login_link_email(email, login_link):
    """Send passwordless sign-in link via Brevo."""
    try:
        to_email = (email or '').strip().lower()
        if not to_email or not login_link:
            _log_email('LOGIN_LINK skipped — missing email or link')
            return False

        context = {
            'login_link': login_link,
            'site_url': _site_url(),
            'current_year': datetime.now().year,
        }
        html_content = render_template('emails/login_link.html', **context)
        subject = 'Seu link de acesso | Katsumi Joias'
        _log_email(f'LOGIN_LINK sending to {to_email}')
        return _send_email(to_email, to_email.split('@')[0], subject, html_content)
    except Exception as exc:
        _log_email(f'LOGIN_LINK failed: {type(exc).__name__}: {exc}')
        return False


def send_solicitacao_reply_email(solicitacao, reply_message, reply_images=None):
    """Notify customer that their special request received an admin reply."""
    try:
        to_email = (solicitacao.get('email') or '').strip().lower()
        if not to_email:
            _log_email('SOLICITACAO_REPLY skipped — missing email')
            return False

        site_url = _site_url()
        context = {
            'customer_name': solicitacao.get('name', 'Cliente'),
            'request_excerpt': (solicitacao.get('description') or '')[:280],
            'reply_message': reply_message or '',
            'reply_images': reply_images or [],
            'account_url': f'{site_url}/account',
            'site_url': site_url,
            'current_year': datetime.now().year,
        }
        html_content = render_template('emails/solicitacao_reply.html', **context)
        subject = 'Resposta à sua solicitação | Katsumi Joias'
        _log_email(f'SOLICITACAO_REPLY sending to {to_email}')
        sent = _send_email(to_email, context['customer_name'], subject, html_content)
        try:
            whatsapp_service.send_solicitacao_reply_whatsapp(solicitacao, reply_message)
        except Exception as wa_exc:
            _log_email(f'SOLICITACAO_REPLY WhatsApp failed: {type(wa_exc).__name__}')
        return sent
    except Exception as exc:
        _log_email(f'SOLICITACAO_REPLY failed: {type(exc).__name__}: {exc}')
        return False


def send_order_tracking_email(order, tracking_code):
    """Notify customer that the order was shipped with a tracking code."""
    try:
        to_email = (order.get('customer_email') or '').strip()
        if not to_email:
            _log_email('ORDER_TRACKING skipped — missing customer email')
            return False

        context = _prepare_order_context(order)
        context['tracking_code'] = tracking_code
        context['tracking_url'] = 'https://rastreamento.correios.com.br/app/index.php'
        html_content = render_template('emails/order_tracking.html', **context)
        subject = f'Seu pedido #{context["order_number"]} foi despachado | código de rastreio'
        _log_email(f'ORDER_TRACKING sending to order #{context["order_number"]}')
        sent = _send_email(to_email, context['customer_name'], subject, html_content)
        try:
            whatsapp_service.send_order_tracking_whatsapp(order, tracking_code)
        except Exception as wa_exc:
            _log_email(f'ORDER_TRACKING WhatsApp failed: {type(wa_exc).__name__}')
        return sent
    except Exception as exc:
        _log_email(f'ORDER_TRACKING failed: {type(exc).__name__}: {exc}')
        return False


def send_admin_quote_notification(quote):
    """Avisa a equipe (QUOTES_NOTIFY_EMAIL) sobre um novo pedido de orçamento do 'Crie sua jóia'."""
    to_email = (os.getenv('QUOTES_NOTIFY_EMAIL') or '').strip()
    if not to_email:
        return False
    labels = quote.get('labels') or {}
    number = quote.get('id', '')[:8].upper()
    crm_url = f"{_site_url()}/crm/orcamentos/{quote.get('id', '')}"
    rows = ''.join(
        f'<tr><td style="padding:4px 12px 4px 0;color:#666">{k}</td><td style="padding:4px 0">{v}</td></tr>'
        for k, v in (
            ('Cliente', quote.get('customer_name', '')),
            ('E-mail', quote.get('customer_email', '')),
            ('Telefone', quote.get('customer_phone', '') or '—'),
            ('Tipo', labels.get('jewelry_type', '')),
            ('Metal', labels.get('metal', '')),
            ('Pedra', ' · '.join(x for x in (labels.get('stone'), labels.get('shape'), labels.get('size'), labels.get('count')) if x)),
            ('Detalhes', quote.get('details_text') or '—'),
            ('Observações', quote.get('notes') or '—'),
        )
    )
    html_content = (
        f'<div style="font-family:Arial,sans-serif;font-size:14px;color:#111">'
        f'<h2 style="font-weight:normal">Novo pedido de orçamento #{number}</h2>'
        f'<p><img src="{quote.get("selected_image_url", "")}" alt="Prévia" width="320" style="border:1px solid #eee"></p>'
        f'<table>{rows}</table>'
        f'<p><a href="{crm_url}">Abrir no CRM</a></p></div>'
    )
    return _send_email(to_email, 'Katsumi Joias', f'Novo orçamento “Crie sua jóia” #{number}', html_content)
