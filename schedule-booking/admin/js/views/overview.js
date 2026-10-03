// Overview tab: headline numbers for one month, computed from that month's
// bookings.

import { h, clear, toast, describeError } from '../ui.js';
import { loadMonthBookings } from '../data.js';
import { CURRENCY } from '../config.js';
import * as fmt from '../lib/format.js';
import * as bm from '../lib/bookingsModel.js';

export function renderOverview(root) {
    const today = new Date();
    let cursor = { year: today.getFullYear(), month: today.getMonth() };
    let stats = null;
    let loading = true;
    let failed = false;

    const toolbar = h('div', { class: 'toolbar' });
    const body = h('div');

    clear(root);
    root.append(h('h2', null, 'Overview'), toolbar, body);

    async function load() {
        const mine = cursor;
        loading = true;
        failed = false;
        paint();
        try {
            const bookings = await loadMonthBookings(mine.year, mine.month);
            if (mine !== cursor) return;
            stats = bm.computeStats(bookings, new Date());
        } catch (err) {
            if (mine !== cursor) return;
            failed = true;
            toast(describeError(err), 'error');
        }
        loading = false;
        paint();
    }

    function go(delta) {
        cursor = fmt.shiftMonth(cursor.year, cursor.month, delta);
        load();
    }

    function tile(value, label, bad) {
        return h('div', { class: 'tile' + (bad ? ' tile-bad' : '') },
            h('div', { class: 'tile-value' }, value),
            h('div', { class: 'tile-label' }, label)
        );
    }

    function paint() {
        clear(toolbar);
        toolbar.append(
            h('button', { type: 'button', class: 'btn btn-secondary btn-small', 'aria-label': 'Previous month', onClick: function () { go(-1); } }, '‹'),
            h('span', { class: 'month-label', 'aria-live': 'polite' }, fmt.monthLabel(cursor.year, cursor.month)),
            h('button', { type: 'button', class: 'btn btn-secondary btn-small', 'aria-label': 'Next month', onClick: function () { go(1); } }, '›')
        );

        clear(body);
        if (loading) {
            body.append(h('p', { class: 'muted' }, 'Loading…'));
            return;
        }
        if (failed) {
            body.append(h('div', { class: 'banner banner-bad' }, 'Couldn’t load the numbers. ', h('button', { type: 'button', class: 'btn btn-secondary btn-small', onClick: load }, 'Try again')));
            return;
        }

        const s = stats;
        if (s.needsRefund > 0) {
            body.append(h('div', { class: 'banner banner-bad', role: 'alert' },
                h('strong', null, s.needsRefund + (s.needsRefund === 1 ? ' payment needs' : ' payments need') + ' a refund. '),
                'Open the Bookings tab and filter by “Needs refund”, refund them in Paymob, then press “Mark refunded”.'));
        }

        const rate = s.noShowRate === null ? '—' : Math.round(s.noShowRate * 100) + '%';
        body.append(
            h('div', { class: 'tiles' },
                tile(String(s.upcoming), 'Upcoming sessions'),
                tile(String(s.completed), 'Sessions held'),
                tile(fmt.formatMoney(s.revenue, s.currency || CURRENCY), 'Revenue'),
                tile(String(s.noShows), 'No-shows'),
                tile(rate, 'No-show rate'),
                tile(String(s.cancelled), 'Cancelled'),
                tile(String(s.awaitingPayment), 'Awaiting payment'),
                tile(String(s.needsRefund), 'Need a refund', s.needsRefund > 0)
            ),
            h('p', { class: 'muted small' }, 'Revenue counts paid sessions that are confirmed or were no-shows. Cancelled bookings are refunded in full, so they aren’t counted. No-show rate is no-shows out of sessions whose time has passed.')
        );
    }

    load();
}
