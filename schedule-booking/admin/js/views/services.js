// Services tab: what customers can book, how long it lasts and what it costs.
// A price of 0 makes a service FREE — it confirms instantly with no payment.

import { h, clear, toast, withBusy, describeError } from '../ui.js';
import { loadServices, saveService, setServiceActive, deleteService } from '../data.js';
import { CURRENCY } from '../config.js';
import { validateService } from '../lib/validate.js';
import * as fmt from '../lib/format.js';

function priceToInput(amount) {
    const n = Number(amount) || 0;
    return (n / 100).toFixed(n % 100 === 0 ? 0 : 2);
}

export function renderServices(root) {
    let services = [];
    let editing = null; // null | 'new' | a service id
    let loading = true;
    let failed = false;

    const banner = h('div');
    const formHost = h('div');
    const list = h('div');

    clear(root);
    root.append(
        h('div', { class: 'card-title-row' },
            h('h2', null, 'Services'),
            h('button', { type: 'button', class: 'btn', onClick: function () { editing = 'new'; paintForm(); paint(); } }, 'Add a service')
        ),
        h('p', { class: 'muted' }, 'Each service has its own length and price. Set the price to 0 to make a service free — free services confirm instantly with no payment step.'),
        banner, formHost, list
    );

    async function load() {
        loading = true;
        failed = false;
        paint();
        try {
            services = await loadServices();
        } catch (err) {
            failed = true;
            toast(describeError(err), 'error');
        }
        loading = false;
        paint();
    }

    function serviceForm(existing) {
        const isNew = !existing;
        const errors = {};

        function field(id, label, control, hint) {
            errors[id] = h('p', { class: 'field-error', hidden: true });
            return h('div', { class: 'field' },
                h('label', { for: 'svc-' + id }, label), control, hint ? h('p', { class: 'field-hint' }, hint) : null, errors[id]);
        }

        const name = h('input', { type: 'text', id: 'svc-name', maxlength: '120', value: existing ? existing.name : '' });
        const description = h('textarea', { id: 'svc-description', maxlength: '500' }, existing ? existing.description || '' : '');
        const duration = h('input', { type: 'number', id: 'svc-durationMinutes', min: '5', max: '480', step: '5', inputmode: 'numeric', value: existing ? String(existing.durationMinutes) : '60' });
        const price = h('input', { type: 'text', id: 'svc-price', inputmode: 'decimal', value: existing ? priceToInput(existing.amount) : '0' });
        const sortOrder = h('input', { type: 'number', id: 'svc-sortOrder', step: '1', value: existing ? String(Number(existing.sortOrder) || 0) : '0' });
        const active = h('input', { type: 'checkbox', id: 'svc-active', checked: existing ? existing.active !== false : true });

        const saveBtn = h('button', { type: 'submit', class: 'btn' }, isNew ? 'Add service' : 'Save changes');
        const cancelBtn = h('button', { type: 'button', class: 'btn btn-secondary', onClick: function () { editing = null; paintForm(); paint(); } }, 'Cancel');

        const form = h('form', { novalidate: true, class: 'card' },
            h('h3', null, isNew ? 'New service' : 'Edit service'),
            field('name', 'Name', name),
            field('description', 'Description (shown to customers)', description),
            h('div', { class: 'form-row' },
                field('durationMinutes', 'Length (minutes)', duration),
                field('price', 'Price (' + CURRENCY + ')', price, '0 = free. Use e.g. 500 or 149.50.')
            ),
            h('div', { class: 'form-row' },
                field('sortOrder', 'Display order', sortOrder, 'Lower numbers are shown first.'),
                h('div', { class: 'field' }, h('label', { class: 'check', for: 'svc-active' }, active, 'Available to book'))
            ),
            h('div', { class: 'actions' }, saveBtn, cancelBtn)
        );

        form.addEventListener('submit', async function (event) {
            event.preventDefault();

            const result = validateService({
                name: name.value, description: description.value, durationMinutes: duration.value,
                price: price.value, sortOrder: sortOrder.value, active: active.checked
            });

            Object.keys(errors).forEach(function (k) {
                errors[k].hidden = !result.errors[k];
                errors[k].textContent = result.errors[k] || '';
            });
            if (!result.ok) {
                const firstBad = Object.keys(result.errors)[0];
                const el = document.getElementById('svc-' + firstBad);
                if (el) el.focus();
                return;
            }

            try {
                await withBusy(saveBtn, 'Saving…', function () { return saveService(existing ? existing.id : null, result.value); });
                toast(isNew ? 'Service added.' : 'Service saved.');
                editing = null;
                paintForm();
                load();
            } catch (err) {
                toast(describeError(err), 'error');
            }
        });

        return form;
    }

    function paintForm() {
        // Only the NEW-service form lives up here; editing an existing service
        // happens in place of that service's own card (see paint()).
        clear(formHost);
        if (editing === 'new') {
            formHost.append(serviceForm(null));
            formHost.querySelector('input').focus();
        }
    }

    function serviceCard(s) {
        const amount = Number(s.amount) || 0;
        const isActive = s.active !== false;

        const toggleBtn = h('button', { type: 'button', class: 'btn btn-secondary btn-small' }, isActive ? 'Pause' : 'Make available');
        toggleBtn.addEventListener('click', async function () {
            try {
                await withBusy(toggleBtn, 'Saving…', function () { return setServiceActive(s.id, !isActive); });
                toast(isActive ? 'Service paused — customers can no longer book it.' : 'Service is available to book.');
                load();
            } catch (err) {
                toast(describeError(err), 'error');
            }
        });

        const deleteBtn = h('button', { type: 'button', class: 'btn btn-danger btn-small' }, 'Delete');
        deleteBtn.addEventListener('click', async function () {
            const ok = window.confirm('Delete “' + s.name + '”?\n\nExisting bookings keep their record of it. To hide a service without losing it, use Pause instead.');
            if (!ok) return;
            try {
                await withBusy(deleteBtn, 'Deleting…', function () { return deleteService(s.id); });
                toast('Service deleted.');
                load();
            } catch (err) {
                toast(describeError(err), 'error');
            }
        });

        return h('div', { class: 'card' + (isActive ? '' : ' service-inactive') },
            h('div', { class: 'card-title-row' },
                h('div', null,
                    h('div', { class: 'service-head' },
                        h('h3', null, s.name),
                        h('span', { class: 'badge ' + (isActive ? 'badge-ok' : 'badge-quiet') }, isActive ? 'Available' : 'Paused'),
                        amount === 0 ? h('span', { class: 'badge badge-wait' }, 'Free') : null
                    ),
                    h('div', { class: 'service-meta' }, fmt.formatDuration(Number(s.durationMinutes) || 0) + ' · ' + fmt.formatPrice(amount, CURRENCY)),
                    s.description ? h('p', { class: 'muted small' }, s.description) : null
                ),
                h('div', { class: 'actions' },
                    h('button', { type: 'button', class: 'btn btn-secondary btn-small', onClick: function () { editing = s.id; paintForm(); paint(); const first = list.querySelector('form input'); if (first) first.focus(); } }, 'Edit'),
                    toggleBtn, deleteBtn
                )
            )
        );
    }

    function paint() {
        clear(banner);
        clear(list);

        if (loading) {
            list.append(h('p', { class: 'muted' }, 'Loading…'));
            return;
        }
        if (failed) {
            list.append(h('div', { class: 'banner banner-bad' }, 'Couldn’t load services. ', h('button', { type: 'button', class: 'btn btn-secondary btn-small', onClick: load }, 'Try again')));
            return;
        }

        if (!services.some(function (s) { return s.active !== false; })) {
            banner.append(h('div', { class: 'banner banner-warn' }, 'No service is available to book right now, so customers can’t book online. Add a service, or make an existing one available.'));
        }
        if (services.length === 0) {
            list.append(h('p', { class: 'muted' }, 'No services yet.'));
        }
        services.forEach(function (s) { list.append(editing === s.id ? serviceForm(s) : serviceCard(s)); });
    }

    load();
}
