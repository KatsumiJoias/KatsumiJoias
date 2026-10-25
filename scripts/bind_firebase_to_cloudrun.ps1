# Bind the LOCAL Firebase service account (katsumijoias-6cc8d) to Cloud Run
# (hosted on GCP project katsumijoias).
#
# Prerequisites: gcloud CLI logged in, Secret Manager API enabled.
# Run from the repo root:
#   powershell -ExecutionPolicy Bypass -File scripts\bind_firebase_to_cloudrun.ps1

$ErrorActionPreference = 'Stop'

$HostProject = 'katsumijoias'
$FirebaseProject = 'katsumijoias-6cc8d'
$Region = 'southamerica-east1'
$Service = 'katsumijoias-app'
$SecretName = 'FIREBASE_CREDENTIALS_JSON'
$KeyFile = Join-Path $PSScriptRoot '..\account_service.json' | Resolve-Path

if (-not (Test-Path $KeyFile)) {
    Write-Error "Nao achei account_service.json em $($KeyFile). Esse arquivo e o do Firebase $FirebaseProject."
}

$raw = Get-Content -Raw -Path $KeyFile
$parsed = $raw | ConvertFrom-Json
if ($parsed.project_id -ne $FirebaseProject) {
    Write-Error "account_service.json tem project_id='$($parsed.project_id)'. Esperado '$FirebaseProject'."
}

Write-Host "Service account: $($parsed.client_email)"
Write-Host "Criando/atualizando secret $SecretName no GCP $HostProject..."

gcloud config set project $HostProject | Out-Null
gcloud services enable secretmanager.googleapis.com --project $HostProject --quiet

$exists = $false
try {
    gcloud secrets describe $SecretName --project $HostProject --quiet 2>$null | Out-Null
    $exists = $true
} catch { $exists = $false }

if ($exists) {
    $raw | gcloud secrets versions add $SecretName --project $HostProject --data-file=-
} else {
    $raw | gcloud secrets create $SecretName --project $HostProject --data-file=- --replication-policy=automatic
}

$sa = gcloud run services describe $Service `
    --region $Region `
    --project $HostProject `
    --format="value(spec.template.spec.serviceAccountName)"

if (-not $sa) {
    $num = gcloud projects describe $HostProject --format="value(projectNumber)"
    $sa = "$num-compute@developer.gserviceaccount.com"
}

Write-Host "Cloud Run service account: $sa"
gcloud secrets add-iam-policy-binding $SecretName `
    --project $HostProject `
    --member="serviceAccount:$sa" `
    --role="roles/secretmanager.secretAccessor" `
    --quiet

Write-Host "Anexando secret + FIREBASE_PROJECT_ID ao servico $Service..."
gcloud run services update $Service `
    --region $Region `
    --project $HostProject `
    --set-secrets="${SecretName}=${SecretName}:latest" `
    --update-env-vars="FIREBASE_PROJECT_ID=${FirebaseProject}" `
    --quiet

Write-Host ""
Write-Host "Pronto. Nova revisao deve logar:"
Write-Host "  [Firebase] Admin SDK via FIREBASE_CREDENTIALS_JSON project='katsumijoias-6cc8d'"
Write-Host "  [Firebase] Firestore client project='katsumijoias-6cc8d'"
Write-Host ""
Write-Host "Se o catalogo ainda vier vazio, conceda a esta SA acesso no Firebase $FirebaseProject:"
Write-Host "  gcloud projects add-iam-policy-binding $FirebaseProject --member=serviceAccount:$sa --role=roles/datastore.user"
Write-Host "  gcloud projects add-iam-policy-binding $FirebaseProject --member=serviceAccount:$sa --role=roles/firebaseauth.admin"
