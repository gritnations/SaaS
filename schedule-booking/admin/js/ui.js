// Tiny DOM helpers. Text is ALWAYS inserted as text nodes (never as HTML), so
// nothing customer-typed or admin-typed can inject markup into this page.

// h('button', { class: 'btn', onClick: fn, disabled: true }, 'Label', childNode)
export function h(tag, props, ...children) {
    const el = document.createElement(tag);

    Object.keys(props || {}).forEach(function (key) {
        const value = props[key];
        if (value === undefined || value === null || value === false) return;

        if (key === 'class') el.className = value;
        else if (key === 'text') el.textContent = value;
        else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2).toLowerCase(), value);
        else if (key === 'value') el.value = value;
        else if (key === 'checked' || key === 'disabled' || key === 'hidden' || key === 'selected') el[key] = true;
        else el.setAttribute(key, value === true ? '' : String(value));
    });

    children.flat(Infinity).forEach(function (child) {
        if (child === null || child === undefined || child === false) return;
        el.append(child.nodeType ? child : document.createTextNode(String(child)));
    });

    return el;
}

// Wraps a password <input> with a button that shows / hides what was typed.
// The icons are built as SVG nodes (not markup strings), so this stays inside
// the strict Content-Security-Policy and never parses HTML.
const SVG_NS = 'http://www.w3.org/2000/svg';

function eyeIcon(crossedOut) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    [['viewBox', '0 0 24 24'], ['width', '20'], ['height', '20'], ['fill', 'none'], ['stroke', 'currentColor'],
        ['stroke-width', '2'], ['stroke-linecap', 'round'], ['stroke-linejoin', 'round'], ['aria-hidden', 'true']]
        .forEach(function (a) { svg.setAttribute(a[0], a[1]); });

    const paths = ['M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z'];
    if (crossedOut) paths.push('M3 3l18 18');
    paths.forEach(function (d) {
        const p = document.createElementNS(SVG_NS, 'path');
        p.setAttribute('d', d);
        svg.appendChild(p);
    });
    const pupil = document.createElementNS(SVG_NS, 'circle');
    pupil.setAttribute('cx', '12'); pupil.setAttribute('cy', '12'); pupil.setAttribute('r', '3');
    svg.appendChild(pupil);
    return svg;
}

export function withPasswordToggle(input) {
    const button = h('button', { type: 'button', class: 'password-toggle', 'aria-label': 'Show password', 'aria-pressed': 'false' });
    button.appendChild(eyeIcon(false));

    button.addEventListener('click', function () {
        const showing = input.type === 'text';
        input.type = showing ? 'password' : 'text';
        button.setAttribute('aria-pressed', showing ? 'false' : 'true');
        button.setAttribute('aria-label', showing ? 'Show password' : 'Hide password');
        clear(button);
        button.appendChild(eyeIcon(!showing));
        input.focus();
    });

    return h('div', { class: 'password-wrap' }, input, button);
}

export function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
}

// kind: 'ok' | 'error'
export function toast(message, kind) {
    const region = document.getElementById('toasts');
    if (!region) return;

    const item = h('div', { class: 'toast toast-' + (kind || 'ok'), role: kind === 'error' ? 'alert' : 'status' }, message);
    region.append(item);
    setTimeout(function () { item.remove(); }, kind === 'error' ? 8000 : 4000);
}

// Disables a button and swaps its label while an async action runs.
export async function withBusy(button, busyLabel, action) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = busyLabel;
    try {
        return await action();
    } finally {
        button.disabled = false;
        button.textContent = original;
    }
}

// A friendly message for a failed Firestore call.
export function describeError(err) {
    const code = err && err.code ? String(err.code) : '';
    if (code === 'permission-denied') return 'You don’t have permission to do that.';
    if (code === 'unavailable' || code === 'failed-precondition') return 'Couldn’t reach the server. Check your connection and try again.';
    return 'Something went wrong. Please try again.';
}
