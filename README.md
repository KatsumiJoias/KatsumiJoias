# Katsumi Joias — Sistema de E-commerce

Sistema completo de e-commerce para joias, desenvolvido com Flask e Firebase, implantado no Google Cloud Run.

## Características

- 🛍️ **Loja Online**: Catálogo de produtos com carrinho de compras e checkout
- 🎛️ **CRM Administrativo**: Painel completo com dashboard, KPIs e gestão
  - Gerenciamento de Produtos e Categorias
  - Gerenciamento de Clientes e Vendedores
  - Gerenciamento de Pedidos
  - Solicitações e Newsletter
  - Logs de Atividade
  - Configurações do Sistema (câmbio, frete, markup)
- 💳 **Pagamentos**: Integração com Pagar.me (cartão, Pix, boleto)
- 📧 **E-mail Transacional**: Integração com Brevo (Sendinblue)
- 📱 **WhatsApp**: Notificações via WhatsGW
- 🔐 **Autenticação**: Login por link mágico (passwordless) + Firebase Auth
- 🔥 **Firebase**: Firestore (banco de dados) + Storage (imagens)
- 🐳 **Docker**: Containerização para deploy
- ☁️ **Cloud Run**: Deploy automático via GitHub Actions
- 🔍 **SEO**: Sitemap, meta tags e dados estruturados

## Tecnologias

| Camada      | Tecnologia                               |
| ----------- | ---------------------------------------- |
| Backend     | Flask (Python 3.11)                      |
| Database    | Firebase Firestore                       |
| Storage     | Firebase Storage                         |
| Pagamentos  | Pagar.me                                 |
| E-mail      | Brevo (Sendinblue)                       |
| WhatsApp    | WhatsGW                                  |
| Container   | Docker                                   |
| Deploy      | Google Cloud Run + GitHub Actions        |
| CI/CD       | GitHub Actions                           |

## Instalação Local

### Pré-requisitos

- Python 3.11+
- Conta Firebase com Firestore e Storage ativados
- Service Account Key do Firebase

### Setup

1. Clone o repositório:
```bash
git clone https://github.com/KatsumiJoias/KatsumiJoias.git
cd KatsumiJoias
```

2. Crie um ambiente virtual:
```bash
python -m venv venv
source venv/bin/activate  # No Windows: venv\Scripts\activate
```

3. Instale as dependências:
```bash
pip install -r requirements.txt
```

4. Configure o Firebase:
   - Acesse o [Firebase Console](https://console.firebase.google.com/)
   - Selecione o projeto `katsumijoias`
   - Ative Firestore Database
   - Ative Firebase Storage
   - Crie e baixe a Service Account Key (IAM & Admin → Service Accounts)

5. Configure as variáveis de ambiente — crie um arquivo `.env`:
```env
# --- Obrigatórias ---
FLASK_SECRET_KEY=sua-chave-secreta-aqui
FIREBASE_STORAGE_BUCKET=katsumijoias.firebasestorage.app
FIREBASE_CREDENTIALS_JSON='{"type":"service_account","project_id":"katsumijoias",...}'
# OU aponte para o arquivo JSON:
# FIREBASE_CREDENTIALS_PATH=./serviceAccountKey.json

# --- Firebase Client SDK (para autenticação no frontend) ---
FIREBASE_API_KEY=...
FIREBASE_AUTH_DOMAIN=katsumijoias.firebaseapp.com
FIREBASE_PROJECT_ID=katsumijoias
FIREBASE_MESSAGING_SENDER_ID=...
FIREBASE_APP_ID=...

# --- Pagamentos (Pagar.me) ---
PAGARME_API_KEY=sk_...
PAGARME_CHAVE_PUBLICA=pk_...

# --- E-mail (Brevo / Sendinblue) ---
API_KEY_BREVO=xkeysib-...

# --- WhatsApp (WhatsGW) ---
WHATSGW_API_KEY=...
WHATSGW_PHONE_NUMBER=5531...
WHATSGW_INSTANCIA_ID=...
SUPPORT_WHATSAPP_NUMBER=5511971581675

# --- Crie sua jóia (geração de imagens) ---
OPENAI_API_KEY=sk-...
IMAGE_PROVIDER_PRIMARY=openai          # openai | google | mock
OPENAI_IMAGE_MODEL=gpt-image-2.5-sunburst   # ou gpt-image-2.5-flare
GEMINI_API_KEY=...
IMAGE_PROVIDER_FALLBACK=google         # usado se o principal falhar
QUOTES_NOTIFY_EMAIL=atendimento@katsumijoias.com.br

# --- Outros ---
SITE_URL=https://katsumijoias.com.br
EXCHANGE_RATE_CRON_SECRET=um-segredo-para-cron
```

7. (Opcional) Popule o banco com dados de exemplo:
```bash
python seed_data.py            # joias, gemas, clientes, pedidos, orçamentos...
python seed_data.py --upload   # idem, enviando as imagens ao Firebase Storage
python seed_data.py --wipe     # remove tudo que o seed criou
```

8. Execute a aplicação:
```bash
python app.py
```

Acesse: http://localhost:5000

## Variáveis de Ambiente

| Variável                       | Obrigatória | Descrição                                        |
| ------------------------------ | :---------: | ------------------------------------------------ |
| `FLASK_SECRET_KEY`             | ✅          | Chave secreta do Flask para sessões              |
| `FIREBASE_CREDENTIALS_JSON`   | ✅*         | JSON da Service Account do Firebase              |
| `FIREBASE_CREDENTIALS_PATH`   | ✅*         | Caminho para o arquivo JSON da SA (*alternativa*)|
| `FIREBASE_STORAGE_BUCKET`     | ✅          | Bucket do Firebase Storage                       |
| `FIREBASE_API_KEY`            | ✅          | API Key do Firebase (client SDK)                 |
| `FIREBASE_AUTH_DOMAIN`        | ✅          | Auth domain do Firebase                          |
| `FIREBASE_PROJECT_ID`         | ✅          | Project ID do Firebase                           |
| `FIREBASE_MESSAGING_SENDER_ID`| ✅          | Messaging Sender ID do Firebase                  |
| `FIREBASE_APP_ID`             | ✅          | App ID do Firebase                               |
| `SITE_URL`                    | ✅ (prod)   | URL pública (`https://…run.app` ou domínio custom.) — login por e-mail e links transacionais |
| `PAGARME_API_KEY`             | ✅          | Chave de API (secret) do Pagar.me                |
| `PAGARME_CHAVE_PUBLICA`       | ✅          | Chave pública do Pagar.me                        |
| `API_KEY_BREVO`               |             | Chave de API do Brevo para envio de e-mails      |
| `WHATSGW_API_KEY`             |             | Chave de API do WhatsGW                          |
| `WHATSGW_PHONE_NUMBER`        |             | Número do WhatsApp para envio                    |
| `WHATSGW_INSTANCIA_ID`        |             | ID da instância WhatsGW                          |
| `SUPPORT_WHATSAPP_NUMBER`     |             | Número de suporte exibido no site (padrão 5511971581675) |
| `EXCHANGE_RATE_CRON_SECRET`   |             | Segredo para endpoint de atualização de câmbio   |

> *Forneça `FIREBASE_CREDENTIALS_JSON` **ou** `FIREBASE_CREDENTIALS_PATH` (não ambos).

### Crie sua jóia (imagens por IA)

Sem nenhuma chave configurada o estúdio funciona em modo **mock** (imagens ilustrativas locais), útil para testar o fluxo.

| Variável                           | Padrão                     | Descrição |
| ---------------------------------- | -------------------------- | --------- |
| `OPENAI_API_KEY`                   | —                          | Chave da OpenAI (geração, ajuste e moderação de texto) |
| `OPENAI_ORG_ID`                    | —                          | Organização OpenAI (opcional) |
| `OPENAI_IMAGE_MODEL`               | `gpt-image-2.5-sunburst`   | Modelo de geração (`gpt-image-2.5-flare` também é aceito) |
| `OPENAI_IMAGE_EDIT_MODEL`          | igual ao de geração        | Modelo do "Ajustar esta imagem" |
| `OPENAI_IMAGE_QUALITY`             | `high`                     | Qualidade da imagem |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY`| —                          | Chave do Google AI Studio (provedor alternativo) |
| `GEMINI_IMAGE_MODEL`               | `gemini-nano-banana-2.1`   | Modelo de imagem do Google |
| `IMAGE_PROVIDER_PRIMARY`           | automático                 | `openai`, `google` ou `mock` |
| `IMAGE_PROVIDER_FALLBACK`          | `google` se houver chave   | Provedor usado quando o principal falha |
| `IMAGE_PROVIDER_MAX_RETRIES`       | `2`                        | Tentativas por imagem antes do fallback |
| `DESIGNER_IMAGES_PER_GENERATION`   | `2`                        | Variações por geração (1 ou 2) |
| `DESIGNER_DAILY_LIMIT_PER_USER`    | `10`                       | Gerações por cliente por dia |
| `DESIGNER_MAX_CONCURRENT_PER_USER` | `1`                        | Gerações simultâneas por cliente |
| `DESIGNER_DAILY_BUDGET_USD`        | `25`                       | Teto diário de gasto estimado (todas as gerações) |
| `DESIGNER_COST_PER_IMAGE_USD`      | `0.17`                     | Custo estimado por imagem OpenAI (para o teto/relatório) |
| `GEMINI_COST_PER_IMAGE_USD`        | `0.04`                     | Custo estimado por imagem Google |
| `DESIGNER_CACHE` / `DESIGNER_CACHE_TTL_DAYS` | `1` / `30`       | Reaproveita imagens de escolhas idênticas |
| `DESIGNER_MODERATION`              | `1`                        | `0` desliga a moderação do texto livre |
| `DESIGNER_USE_REFERENCES`          | `0`                        | `1` envia as imagens de `static/designer/refs/` como referência |
| `QUOTES_NOTIFY_EMAIL`              | —                          | E-mail que recebe aviso de novo orçamento |

Os jobs de geração rodam em threads do próprio processo, por isso o Cloud Run usa `--no-cpu-throttling` (já configurado no workflow).

## Deploy — GitHub Actions + Cloud Run

O deploy é automático: a cada push na branch `main`, o GitHub Actions builda a imagem Docker, envia para o Artifact Registry e atualiza o Cloud Run.

### Informações do projeto GCP

- **Projeto GCP**: `katsumijoias`
- **Serviço Cloud Run**: `katsumijoias-app`
- **Região**: `southamerica-east1` (São Paulo)
- **Artifact Registry**: `southamerica-east1-docker.pkg.dev/katsumijoias/docker-repo`

### Configurar os Secrets no GitHub

No repositório GitHub, vá em **Settings → Secrets and variables → Actions** e adicione:

| Secret       | Valor                                                                 |
| ------------ | --------------------------------------------------------------------- |
| `GCP_SA_KEY` | Conteúdo completo do JSON da Service Account de **deploy** (`github-deploy@...`) |

O **project ID** (`katsumijoias`) e a **região** (`southamerica-east1`) já estão fixos em `.github/workflows/deploy.yml` — não use secrets `GCP_PROJECT_ID` / `GCP_REGION` (evita confundir nome "KatsumiJoias" com o ID).

> **Erro:** `credentials_json` vazio → falta `GCP_SA_KEY`.  
> **Erro:** `not the project name` → secret antigo `GCP_PROJECT_ID` errado; após o workflow atualizado, só `GCP_SA_KEY` é necessário.

### Service Account necessária

Crie uma Service Account no GCP com os seguintes papéis:
- `roles/run.admin` — gerenciar serviços Cloud Run
- `roles/iam.serviceAccountUser` — atuar como service account
- `roles/artifactregistry.writer` — push de imagens Docker

```bash
# Criar a Service Account
gcloud iam service-accounts create github-deploy \
  --display-name="GitHub Actions Deploy" \
  --project=katsumijoias

# Atribuir papéis
for ROLE in roles/run.admin roles/iam.serviceAccountUser roles/artifactregistry.writer; do
  gcloud projects add-iam-policy-binding katsumijoias \
    --member="serviceAccount:github-deploy@katsumijoias.iam.gserviceaccount.com" \
    --role="$ROLE"
done

# Gerar a chave JSON (use como GCP_SA_KEY)
gcloud iam service-accounts keys create sa-key.json \
  --iam-account=github-deploy@katsumijoias.iam.gserviceaccount.com
```

### Criar o repositório Artifact Registry (uma vez)

```bash
gcloud artifacts repositories create docker-repo \
  --repository-format=docker \
  --location=southamerica-east1 \
  --project=katsumijoias
```

### Deploy manual (opcional)

```bash
# Build e push da imagem
docker build -t southamerica-east1-docker.pkg.dev/katsumijoias/docker-repo/katsumijoias-app:latest .
docker push southamerica-east1-docker.pkg.dev/katsumijoias/docker-repo/katsumijoias-app:latest

# Deploy no Cloud Run
gcloud run deploy katsumijoias-app \
  --image=southamerica-east1-docker.pkg.dev/katsumijoias/docker-repo/katsumijoias-app:latest \
  --region=southamerica-east1 \
  --project=katsumijoias \
  --allow-unauthenticated \
  --no-cpu-throttling
```

### Variáveis de ambiente no Cloud Run (obrigatório)

O workflow **não** copia o `.env`. No Console: **Cloud Run → katsumijoias-app → Edit → Variables & secrets**, ou use os mesmos nomes do `.env` local.

Mínimo para subir:

| Variável | Exemplo |
|----------|---------|
| `FLASK_SECRET_KEY` | string longa aleatória |
| `FIREBASE_STORAGE_BUCKET` | `katsumijoias.firebasestorage.app` |
| `FIREBASE_PROJECT_ID` | `katsumijoias` |
| `FIREBASE_API_KEY` | do Firebase Console (app Web) |
| `FIREBASE_AUTH_DOMAIN` | `katsumijoias.firebaseapp.com` |
| `FIREBASE_MESSAGING_SENDER_ID` | do Console |
| `FIREBASE_APP_ID` | do Console |

Para o **Crie sua jóia**, adicione também `OPENAI_API_KEY` e/ou `GEMINI_API_KEY` — de preferência como
*secret* (Secret Manager → "Reference a secret" na mesma tela) — além de `IMAGE_PROVIDER_PRIMARY`,
`OPENAI_IMAGE_MODEL` e `QUOTES_NOTIFY_EMAIL`.

No Cloud Run, o Admin SDK usa a **service account do serviço** (ADC) — não precisa de `FIREBASE_CREDENTIALS_PATH`. Garanta que essa SA tenha acesso Firestore/Storage (ou use secret `FIREBASE_CREDENTIALS_JSON`).

**Checklist pós-deploy (login e catálogo):**

| Problema | Causa comum | O que fazer |
|----------|-------------|-------------|
| Google login abre `none` (DNS error) | Variáveis Firebase no Cloud Run com valor literal `None` ou vazias | Em **Variables**, use os valores reais do Firebase Console (Configurações → Seus apps → Web). Remova entradas erradas. `FIREBASE_AUTH_DOMAIN` deve ser algo como `katsumijoias-6cc8d.firebaseapp.com` (copie do Console). |
| `POST /api/auth/send-login-link` → 500 | `SITE_URL` ausente ou link não habilitado no Firebase | Defina `SITE_URL=https://katsumijoias-app-….run.app` (sem barra final). Em Authentication → E-mail/Senha, ative **Email link (passwordless sign-in)**. |
| E-mail não chega | `API_KEY_BREVO` ausente ou remetente não verificado | Crie chave em Brevo; verifique o domínio `katsumijoias.com.br` e o remetente `contato@katsumijoias.com.br` (já usado no código). |
| Catálogo vazio com dados no Firestore | SA do Cloud Run sem permissão no Firestore | IAM → conta `…@…gserviceaccount.com` do serviço Run → papéis **Cloud Datastore User** e **Firebase Admin** (ou SA dedicada via `FIREBASE_CREDENTIALS_JSON`). |

Após corrigir variáveis, faça **Redeploy** ou **Edit & deploy new revision** no Cloud Run.

## Firebase

### Setup

1. Instale o Firebase CLI:
```bash
npm install -g firebase-tools
firebase login
```

2. O projeto já está configurado em `.firebaserc` e `firebase.json`.

### Deploy de Regras

```bash
# Firestore rules
firebase deploy --only firestore:rules

# Storage rules
firebase deploy --only storage
```

### Coleções do Firestore

| Coleção          | Descrição                                   |
| ---------------- | ------------------------------------------- |
| `products`       | Catálogo (joias `product_type=joia` e gemas) |
| `jewelry_designs`| Criações do "Crie sua jóia" (escolhas, prompt, imagens, custo) |
| `designer_cache` | Cache de imagens por combinação de escolhas |
| `designer_usage` | Uso/custo diário do gerador de imagens      |
| `quotes`         | Orçamentos de joias personalizadas          |
| `customers`      | Cadastro de clientes                        |
| `orders`         | Pedidos                                     |
| `settings`       | Configurações do sistema                    |
| `newsletter`     | Inscrições na newsletter                    |
| `solicitacoes`   | Solicitações/contato                        |
| `login_tokens`   | Tokens de login mágico (server-side only)   |
| `sellers`        | Cadastro de vendedores                      |
| `activity_logs`  | Logs de atividade do CRM                    |

## Estrutura do Projeto

```
KatsumiJoias/
├── app.py                          # Aplicação Flask principal
├── database.py                     # Operações Firestore e Storage
├── email_service.py                # Integração Brevo (e-mails)
├── whatsapp_service.py             # Integração WhatsGW
├── pagarme.py                      # Integração Pagar.me (pagamentos)
├── seo.py                          # SEO (sitemap, meta tags)
├── product_types.py                # Joia x gema: preços, estoque, specs
├── jewelry_designer.py             # Crie sua jóia: catálogo, regras, prompts, moderação
├── designer_service.py             # Crie sua jóia: jobs, limites, cache, orçamentos
├── image_generation/               # Provedores de imagem (OpenAI, Google, mock)
├── seed_data.py                    # Dados de exemplo (python seed_data.py --help)
├── templates/                      # Templates HTML (Jinja2)
│   ├── base.html                   # Template base do site
│   ├── homepage.html               # Página inicial
│   ├── products.html               # Catálogo de produtos
│   ├── product_detail.html         # Detalhes do produto
│   ├── cart.html                   # Carrinho de compras
│   ├── checkout.html               # Checkout
│   ├── login.html                  # Login
│   ├── customer_account.html       # Conta do cliente
│   ├── crm_base.html              # Base do CRM
│   ├── crm_dashboard.html         # Dashboard CRM
│   ├── crm_produtos.html          # Produtos (CRM)
│   ├── crm_clientes.html          # Clientes (CRM)
│   ├── crm_pedidos.html           # Pedidos (CRM)
│   ├── crm_vendedores.html        # Vendedores (CRM)
│   ├── crm_solicitacoes.html      # Solicitações (CRM)
│   ├── crm_newsletter.html        # Newsletter (CRM)
│   ├── crm_logs.html              # Logs (CRM)
│   ├── crm_ajustes.html           # Configurações (CRM)
│   └── emails/                     # Templates de e-mail
├── static/                         # Arquivos estáticos
├── Dockerfile                      # Configuração Docker
├── requirements.txt                # Dependências Python
├── firebase.json                   # Configuração Firebase
├── .firebaserc                     # Projeto Firebase
├── firestore.rules                 # Regras de segurança Firestore
├── storage.rules                   # Regras de segurança Storage
└── .github/workflows/deploy.yml    # CI/CD (GitHub Actions → Cloud Run)
```

## Rotas Principais

### Loja (público)

| Rota                 | Descrição                 |
| -------------------- | ------------------------- |
| `/`                  | Página inicial            |
| `/joias`, `/joias/<tipo>` | Catálogo de joias    |
| `/pedras`, `/pedras/<tipo>` | Gemas avulsas      |
| `/product/<id>`      | Detalhes do produto       |
| `/crie-sua-joia`     | Estúdio de criação com IA |
| `/historia`          | Nossa história            |
| `/cart`              | Carrinho de compras       |
| `/checkout`          | Checkout                  |
| `/login`             | Login do cliente          |
| `/account`           | Conta do cliente          |

### CRM (administrativo)

| Rota                  | Descrição                |
| --------------------- | ------------------------ |
| `/crm`                | Dashboard                |
| `/crm/produtos`       | Gerenciar produtos       |
| `/crm/clientes`       | Gerenciar clientes       |
| `/crm/pedidos`        | Gerenciar pedidos        |
| `/crm/orcamentos`     | Orçamentos do Crie sua jóia |
| `/crm/vendedores`     | Gerenciar vendedores     |
| `/crm/solicitacoes`   | Solicitações             |
| `/crm/newsletter`     | Newsletter               |
| `/crm/logs`           | Logs de atividade        |
| `/crm/ajustes`        | Configurações do sistema |

## Segurança

- ✅ Regras de segurança Firestore (client-side)
- ✅ Regras de segurança Storage
- ✅ Variáveis de ambiente para todos os secrets
- ✅ Service Account para Firebase Admin SDK
- ✅ Rate limiting (Flask-Limiter)
- ✅ Login tokens server-side only

## Monitoramento

### Logs do Cloud Run

```bash
gcloud run services logs read katsumijoias-app \
  --region=southamerica-east1 \
  --limit=50
```

### Firebase Console

- **Firestore**: https://console.firebase.google.com/project/katsumijoias/firestore
- **Storage**: https://console.firebase.google.com/project/katsumijoias/storage
- **Auth**: https://console.firebase.google.com/project/katsumijoias/authentication

### Cloud Run Console

- https://console.cloud.google.com/run/detail/southamerica-east1/katsumijoias-app/metrics?project=katsumijoias

## Licença

Propriedade de Katsumi Joias. Todos os direitos reservados.
