// Everything a signed-out (or not-yet-admitted) visitor can see: sign in,
// verify email, "no access", and setup/error screens.

import { auth, signInWithEmailAndPassword, signOut, sendEmailVerification, sendPasswordResetEmail } from '../firebase.js';
import { h, clear, toast, withBusy, withPasswordToggle } from '../ui.js';

// Same wording for a wrong password and an unknown email, so the form can't be
// used to find out which addresses have accounts.
function describeAuthError(err) {
    switch (err && err.code) {
        case 'auth/invalid-credential':
        case 'auth/wrong-password':
        case 'auth/user-not-found':
        case 'auth/invalid-email':
            return 'Incorrect email or password.';
        case 'auth/too-many-requests':
            return 'Too many attempts. Please wait a few minutes, or reset your password.';
        case 'auth/user-disabled':
            return 'This account has been disabled.';
        case 'auth/network-request-failed':
            return 'Couldn’t reach the server. Check your connection and try again.';
        default:
            return 'Couldn’t sign in. Please try again.';
    }
}

export function renderLogin(root) {
    clear(root);

    const errorLine = h('p', { class: 'field-error', role: 'alert', hidden: true });
    const email = h('input', { type: 'email', id: 'login-email', autocomplete: 'username', required: true });
    const password = h('input', { type: 'password', id: 'login-password', autocomplete: 'current-password', required: true });
    const submit = h('button', { type: 'submit', class: 'btn' }, 'Sign in');
    const forgot = h('button', { type: 'button', class: 'btn btn-ghost btn-small' }, 'Forgot password?');

    function showError(message) {
        errorLine.textContent = message;
        errorLine.hidden = false;
    }

    const form = h('form', { novalidate: true },
        h('div', { class: 'field' }, h('label', { for: 'login-email' }, 'Email'), email),
        h('div', { class: 'field' }, h('label', { for: 'login-password' }, 'Password'), withPasswordToggle(password)),
        errorLine,
        h('div', { class: 'actions' }, submit, forgot)
    );

    form.addEventListener('submit', async function (event) {
        event.preventDefault();
        errorLine.hidden = true;

        if (!email.value.trim() || !password.value) {
            showError('Enter your email and password.');
            return;
        }

        try {
            // On success, onAuthStateChanged in app.js takes over.
            await withBusy(submit, 'Signing in…', function () {
                return signInWithEmailAndPassword(auth, email.value.trim(), password.value);
            });
        } catch (err) {
            showError(describeAuthError(err));
        }
    });

    forgot.addEventListener('click', async function () {
        errorLine.hidden = true;
        if (!email.value.trim()) {
            showError('Enter your email above first, then choose “Forgot password?”.');
            return;
        }
        try {
            await sendPasswordResetEmail(auth, email.value.trim());
        } catch (err) {
            // Deliberately ignored: showing a failure here would reveal which
            // addresses have accounts.
        }
        toast('If that address has an account, a password reset link is on its way.');
    });

    root.append(h('div', { class: 'card card-narrow' },
        h('h1', null, 'Sign in'),
        h('p', { class: 'muted' }, 'Booking admin for authorised staff only.'),
        form
    ));
    email.focus();
}

// Signed in with a correct password but the email isn't verified yet. The
// security rules require a verified email, so nothing loads until then.
export function renderVerifyEmail(root, user, { onRecheck }) {
    clear(root);

    const sendBtn = h('button', { type: 'button', class: 'btn btn-secondary' }, 'Send me the verification link');
    const doneBtn = h('button', { type: 'button', class: 'btn' }, 'I’ve verified — continue');
    const outBtn = h('button', { type: 'button', class: 'btn btn-ghost' }, 'Sign out');

    sendBtn.addEventListener('click', async function () {
        try {
            await withBusy(sendBtn, 'Sending…', function () { return sendEmailVerification(user); });
            toast('Verification link sent — check your inbox (and spam folder).');
        } catch (err) {
            toast(err && err.code === 'auth/too-many-requests'
                ? 'Too many requests — please wait a few minutes before asking again.'
                : 'Couldn’t send the email. Please try again.', 'error');
        }
    });

    doneBtn.addEventListener('click', async function () {
        await withBusy(doneBtn, 'Checking…', onRecheck);
    });

    outBtn.addEventListener('click', function () { signOut(auth); });

    root.append(h('div', { class: 'card card-narrow' },
        h('h1', null, 'Verify your email'),
        h('p', null, 'You’re signed in as ', h('strong', null, user.email), ', but that address hasn’t been verified yet. Admin access requires a verified email.'),
        h('p', { class: 'muted' }, 'Send yourself the link, open it, then come back here and continue.'),
        h('div', { class: 'actions' }, sendBtn, doneBtn, outBtn)
    ));
}

export function renderNoAccess(root, user) {
    clear(root);
    root.append(h('div', { class: 'card card-narrow' },
        h('h1', null, 'No admin access'),
        h('p', null, 'You’re signed in as ', h('strong', null, user.email), ', but this account isn’t set up as an admin for the booking system.'),
        h('p', { class: 'muted' }, 'If you should have access, ask the site owner to add your email to the admin list.'),
        h('div', { class: 'actions' }, h('button', { type: 'button', class: 'btn btn-ghost', onClick: function () { signOut(auth); } }, 'Sign out'))
    ));
}

export function renderNotConfigured(root) {
    clear(root);
    root.append(h('div', { class: 'card card-narrow' },
        h('h1', null, 'Setup needed'),
        h('p', null, 'This admin panel hasn’t been connected to the Firebase project yet.'),
        h('p', { class: 'muted' }, 'Set apiKey in js/config.js to the project’s Web API Key (Firebase console → Project settings → General).')
    ));
}

export function renderConnectionError(root, { onRetry }) {
    clear(root);
    root.append(h('div', { class: 'card card-narrow' },
        h('h1', null, 'Can’t reach the server'),
        h('p', { class: 'muted' }, 'Check your internet connection and try again.'),
        h('div', { class: 'actions' }, h('button', { type: 'button', class: 'btn', onClick: onRetry }, 'Try again'))
    ));
}
