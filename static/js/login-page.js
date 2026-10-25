/**
 * Login page: Google popup + magic-link request.
 * Requires #firebase-config-data (from base.html) and optional #login-page-data.
 */
import { initializeApp, getApps, getApp } from 'https://www.gstatic.com/firebasejs/10.7.1/firebase-app.js';
import { getAuth, signInWithPopup, GoogleAuthProvider } from 'https://www.gstatic.com/firebasejs/10.7.1/firebase-auth.js';

const EMAIL_KEY = 'emailForSignIn';
const NEXT_KEY = 'katsumi_login_next';

function readJson(id) {
    const el = document.getElementById(id);
    if (!el) return null;
    try {
        return JSON.parse(el.textContent);
    } catch (e) {
        console.error(`Invalid JSON in #${id}`, e);
        return null;
    }
}

function showMessage(message, isError = false) {
    const messageDiv = document.getElementById('auth-message');
    const messageText = document.getElementById('auth-message-text');
    if (!messageDiv || !messageText) return;

    messageDiv.classList.remove('hidden', 'bg-red-50', 'bg-green-50');
    messageText.classList.remove('text-red-800', 'text-green-800');

    if (isError) {
        messageDiv.classList.add('bg-red-50');
        messageText.classList.add('text-red-800');
    } else {
        messageDiv.classList.add('bg-green-50');
        messageText.classList.add('text-green-800');
    }

    messageText.textContent = message;
}

function safeNext(value) {
    return (typeof value === 'string' && /^\/(?!\/)[^\s\\]*$/.test(value)) ? value : '';
}

function redirectAfterLogin() {
    const next = safeNext(window.localStorage.getItem(NEXT_KEY)) || '/account';
    window.localStorage.removeItem(NEXT_KEY);
    window.location.href = next;
}

async function handleSuccessfulLogin(user) {
    const idToken = await user.getIdToken();
    const response = await fetch('/api/auth/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ idToken }),
    });

    if (response.ok) {
        window.localStorage.removeItem(EMAIL_KEY);
        showMessage('Login realizado com sucesso! Redirecionando...', false);
        setTimeout(redirectAfterLogin, 1500);
    } else {
        showMessage('Não foi possível verificar sua autenticação. Tente novamente.', true);
    }
}

function mapGoogleError(error) {
    switch (error.code) {
        case 'auth/popup-closed-by-user':
            return 'Login cancelado. Você fechou a janela de login.';
        case 'auth/popup-blocked':
            return 'Pop-up bloqueado. Permita pop-ups para este site.';
        case 'auth/cancelled-popup-request':
            return 'Login cancelado. Abra apenas uma janela de login por vez.';
        case 'auth/account-exists-with-different-credential':
            return 'Este e-mail já usa link de acesso. Use "Enviar link de acesso" com o mesmo e-mail.';
        case 'auth/network-request-failed':
            return 'Erro de conexão. Verifique sua internet.';
        case 'auth/operation-not-allowed':
            return (
                'Login com Google não está habilitado no Firebase. ' +
                'Abra Firebase Console, Authentication, Sign-in method, Google, Ativar, Salvar.'
            );
        default:
            return 'Não foi possível fazer login com Google.';
    }
}

function bindLoginPage(auth) {
    const pageData = readJson('login-page-data') || {};
    if (pageData.serverError) {
        showMessage(pageData.serverError, true);
    }

    const next = safeNext(new URLSearchParams(window.location.search).get('next'));
    if (next) window.localStorage.setItem(NEXT_KEY, next);
    else window.localStorage.removeItem(NEXT_KEY);

    const form = document.getElementById('login-form');
    const googleBtn = document.getElementById('google-signin-btn');
    if (!form || !googleBtn) {
        console.error('Login form elements missing');
        return;
    }

    form.addEventListener('submit', async (e) => {
        e.preventDefault();

        const emailInput = document.getElementById('email');
        const loginBtn = document.getElementById('login-btn');
        const email = (emailInput?.value || '').trim().toLowerCase();

        if (loginBtn) {
            loginBtn.disabled = true;
            loginBtn.textContent = 'Enviando...';
        }

        try {
            window.localStorage.setItem(EMAIL_KEY, email);

            const response = await fetch('/api/auth/send-login-link', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email }),
            });

            const data = await response.json().catch(() => ({}));

            if (response.ok) {
                showMessage(data.message || 'Link enviado! Verifique seu e-mail.', false);
            } else {
                showMessage(data.error || 'Não foi possível enviar o link. Tente novamente.', true);
            }
        } catch (error) {
            console.error('Send link error:', error);
            showMessage('Erro de conexão. Verifique sua internet.', true);
        } finally {
            if (loginBtn) {
                loginBtn.disabled = false;
                loginBtn.textContent = 'Enviar link de acesso';
            }
        }
    });

    const googleProvider = new GoogleAuthProvider();
    googleBtn.addEventListener('click', async () => {
        try {
            const result = await signInWithPopup(auth, googleProvider);
            await handleSuccessfulLogin(result.user);
        } catch (error) {
            console.error('Google sign in error:', error);
            showMessage(mapGoogleError(error), true);
        }
    });
}

const firebaseConfig = readJson('firebase-config-data');
if (!firebaseConfig || !firebaseConfig.apiKey) {
    showMessage('Configuração Firebase ausente no servidor. Contate o suporte.', true);
} else {
    const app = getApps().length ? getApp() : initializeApp(firebaseConfig);
    const auth = getAuth(app);
    bindLoginPage(auth);
}
