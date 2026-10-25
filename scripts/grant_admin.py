#!/usr/bin/env python3
"""Grant Firebase custom claim admin=true (first CRM admin bootstrap).

Usage (from repo root, with venv and account_service.json / .env):
  python scripts/grant_admin.py --email leofinardi@hotmail.com
  python scripts/grant_admin.py --uid Lzuczd2kiRRIpreYFOT6BzwUktc2
  python scripts/grant_admin.py --email x@y.com --revoke
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import database  # noqa: F401 — initializes Firebase Admin
from firebase_admin import auth as firebase_auth


def main():
    parser = argparse.ArgumentParser(description='Set Firebase admin custom claim')
    parser.add_argument('--email', help='User email in Firebase Auth')
    parser.add_argument('--uid', help='Firebase Auth UID')
    parser.add_argument('--revoke', action='store_true', help='Remove admin claim')
    args = parser.parse_args()

    if not args.email and not args.uid:
        parser.error('Informe --email ou --uid')

    if args.uid:
        user = firebase_auth.get_user(args.uid)
    else:
        user = firebase_auth.get_user_by_email(args.email.strip().lower())

    admin = not args.revoke
    firebase_auth.set_custom_user_claims(user.uid, {'admin': admin})

    refreshed = firebase_auth.get_user(user.uid)
    claims = refreshed.custom_claims or {}
    print(f"OK uid={user.uid} email={user.email} admin={claims.get('admin', False)}")
    if admin:
        print("Peça ao usuario sair e entrar de novo no site; depois acesse /crm.")


if __name__ == '__main__':
    main()
