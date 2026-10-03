// Admin panel controller. The auth state decides which screen is shown:
//
//   signed out              -> sign-in form
//   signed in, unverified   -> "verify your email" (rules require it)
//   verified, not an admin  -> "no admin access"
//   verified admin          -> the tabbed panel
//
// Being an admin is decided by firestore.rules, not by anything in this file —
// hiding a screen here would protect nothing; the rules are the lock.

import { isConfigured, auth, onAuthStateChanged, signOut } from './firebase.js';
import { checkAdminAccess } from './data.js';
import { h, clear } from './ui.js';
import { renderLogin, renderVerifyEmail, renderNoAccess, renderNotConfigured, renderConnectionError } from './views/auth.js';
import { renderBookings } from './views/bookings.js';
import { renderServices } from './views/services.js';
import { renderSchedule } from './views/schedule.js';
import { renderOverview } from './views/overview.js';

const app = document.getElementById('app');
const topbarUser = document.getElementById('topbar-user');
const userEmail = document.getElementById('user-email');
const signOutBtn = document.getElementById('sign-out-btn');

const TABS = [
    { id: 'bookings', label: 'Bookings', render: renderBookings },
    { id: 'services', label: 'Services', render: renderServices },
    { id: 'schedule', label: 'Schedule', render: renderSchedule },
    { id: 'overview', label: 'Overview', render: renderOverview }
];

function setSignedInChrome(user) {
    topbarUser.hidden = !user;
    userEmail.textContent = user ? user.email : '';
}

function renderPanel(root) {
    clear(root);

    const tabBar = h('div', { class: 'tabs', role: 'tablist' });
    const view = h('section', { role: 'tabpanel' });
    root.append(tabBar, view);

    function show(tabId) {
        TABS.forEach(function (t) {
            const btn = tabBar.querySelector('[data-tab="' + t.id + '"]');
            if (btn) btn.setAttribute('aria-selected', t.id === tabId ? 'true' : 'false');
        });
        clear(view);
        TABS.find(function (t) { return t.id === tabId; }).render(view);
    }

    TABS.forEach(function (t) {
        tabBar.append(h('button', {
            type: 'button', class: 'tab', role: 'tab', 'data-tab': t.id, 'aria-selected': 'false',
            onClick: function () { show(t.id); }
        }, t.label));
    });

    show('bookings');
}

let routeSeq = 0;

async function route(user) {
    // Only the most recent call may paint: if the user signs out while an
    // access check is still in flight, its late result must not put the admin
    // panel back on screen over the sign-in form.
    const mine = ++routeSeq;
    setSignedInChrome(user);

    if (!user) {
        renderLogin(app);
        return;
    }

    if (!user.emailVerified) {
        renderVerifyEmail(app, user, {
            // Verifying happens in another tab, so this tab's copy of the user
            // and its sign-in token are stale until refreshed.
            onRecheck: async function () {
                await user.reload();
                await user.getIdToken(true);
                await route(auth.currentUser);
            }
        });
        return;
    }

    clear(app);
    app.append(h('p', { class: 'muted' }, 'Checking access…'));

    let access;
    try {
        access = await checkAdminAccess();
    } catch (err) {
        if (mine !== routeSeq) return;
        renderConnectionError(app, { onRetry: function () { route(auth.currentUser); } });
        return;
    }
    if (mine !== routeSeq) return;

    if (access !== 'admin') {
        renderNoAccess(app, user);
        return;
    }

    renderPanel(app);
}

signOutBtn.addEventListener('click', function () { signOut(auth); });

if (!isConfigured) {
    renderNotConfigured(app);
} else {
    onAuthStateChanged(auth, function (user) { route(user); });
}
