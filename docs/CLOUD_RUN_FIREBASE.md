# Cloud Run + Firebase — checklist de produção

Use este roteiro na ordem. Cada etapa tem **como validar** antes de passar à próxima.

## A. Código no ar

1. Faça **push para `main`** (deploy automático) ou deploy manual da imagem.
2. Abra `/login`, DevTools → **Console**: não deve haver `SyntaxError`.
3. Clique **Entrar com Google**: o botão deve reagir (popup ou mensagem na tela).

## B. Secrets / variáveis no Cloud Run

Em **Cloud Run → katsumijoias-app → Edit → Variables & Secrets**, cada secret deve conter o **valor real** (copiado do Firebase Console → Configurações do projeto → Seus apps → Web), nunca a palavra `None` nem placeholder.

| Secret / variável | Onde copiar |
|-------------------|-------------|
| `FIREBASE_API_KEY` | apiKey do app Web |
| `FIREBASE_APP_ID` | appId (ex.: `1:510058748229:web:…`) |
| `FIREBASE_PROJECT_ID` | `katsumijoias` |
| `FIREBASE_AUTH_DOMAIN` | authDomain (ex.: `katsumijoias-6cc8d.firebaseapp.com`) |
| `FIREBASE_STORAGE_BUCKET` | `katsumijoias.firebasestorage.app` |
| `FIREBASE_MESSAGING_SENDER_ID` | Número **dentro do appId** (`510058748229`), não o `491109262402` da URL do Run |
| `SITE_URL` | `https://katsumijoias-app-491109262402.southamerica-east1.run.app` (sem `/` final) |
| `FLASK_SECRET_KEY` | string aleatória longa |
| `API_KEY_BREVO` | Brevo (e-mail transacional) |

**Validar:** View Source em `/login` e confira `#firebase-config-data` — JSON com domínio e projectId corretos.

## C. Firebase Authentication (Console)

1. **Authentication → Sign-in method → Google** → **Ativar** → e-mail de suporte → **Salvar**.
2. **Authentication → Sign-in method → E-mail/Senha** → **Ativar** + marcar **Email link (passwordless sign-in)** → **Salvar**.
3. **Authentication → Settings → Authorized domains** → incluir `katsumijoias-app-491109262402.southamerica-east1.run.app`.

**Validar Google:** após A+B+C, login com Google abre popup (ou erro claro na UI, não botão morto).

**Validar e-mail:** `POST /api/auth/send-login-link` retorna **200** (não 500/503). Se 503 com texto de permissão, vá para D.

## D. Permissões da service account do Cloud Run

Cloud Run → serviço → **Security** → copie o e-mail da service account.

No Cloud Shell (`PROJECT=katsumijoias`, ajuste `SA_EMAIL`):

```bash
PROJECT=katsumijoias
SA_EMAIL="491109262402-compute@developer.gserviceaccount.com"

for ROLE in roles/datastore.user roles/firebaseauth.admin roles/serviceusage.serviceUsageConsumer; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="$ROLE" \
    --quiet
done
```

**Alternativa robusta:** secret `FIREBASE_CREDENTIALS_JSON` com o JSON da service account do Firebase (mesmo `account_service.json` local). O Admin SDK usa essa identidade em vez da SA padrão do Run.

**Validar catálogo:** `/joias` lista peças (seed com `active: true`).

**Validar e-mail:** enviar link; logs do Run sem `generate_sign_in_with_email_link failed`.

## E. E-mail (Brevo + alias)

1. Brevo: verificar domínio `katsumijoias.com.br`.
2. Remetente `contato@katsumijoias.com.br` autorizado.
3. `API_KEY_BREVO` no Run.

**Validar:** receber e-mail de teste; link abre `/auth/login?token=…` e conclui em `/login/finish`.

## F. GCP APIs

Em **APIs & Services → Library**, habilitar no projeto `katsumijoias`:

- Identity Toolkit API
- Cloud Firestore API

---

## Diagnóstico rápido

| Sintoma | Causa mais provável |
|---------|---------------------|
| Botão Google não clica | JavaScript quebrado (ver Console) — corrigido em `static/js/login-page.js` |
| `?email=` na URL e reload | Form GET sem JS — corrigido (`method="post"`, script externo) |
| `auth/operation-not-allowed` | Google desativado no Firebase (secção C.1) |
| `send-login-link` 500 | SA sem `firebaseauth.admin` ou Email link off (D + C.2) |
| Catálogo vazio | SA sem `datastore.user` (D) |
