// Bookings tab: a quarter at a time as three monthly calendars (a highlighted day shows
// how many sessions are booked that day), and below them the full booking details for
// whatever is selected: a day, a month, or the whole quarter. From the list you can
// mark finished sessions as no-shows, cancel upcoming ones (the server refunds, emails and
// clears the calendar), and record refunds the owner had to make by hand.

import { h, clear, toast, withBusy, describeError } from '../ui.js';
import { loadQuarterBookings, markNoShow, cancelBooking, markRefunded } from '../data.js';
import { CURRENCY } from '../config.js';
import * as fmt from '../lib/format.js';
import * as bm from '../lib/bookingsModel.js';

const WEEKDAY_INITIALS = ['S', 'M', 'T', 'W', 'T', 'F', 'S'];

export function renderBookings(root) {
    const today = new Date();
    const todayKey = fmt.dateKeyFromDate(today);

    function currentQuarter() { return fmt.quarterOf(today.getFullYear(), today.getMonth()); }
    // The month a quarter opens on: this month if it is in the quarter, else its first month.
    function defaultScope(q) {
        const months = fmt.quarterMonths(q.quarter);
        const month = (q.year === today.getFullYear() && months.indexOf(today.getMonth()) !== -1) ? today.getMonth() : months[0];
        return { kind: 'month', year: q.year, month: month };
    }

    let cursor = currentQuarter();
    let scope = defaultScope(cursor);
    let filterId = 'all';
    let bookings = [];
    let loading = true;
    let failed = false;

    const toolbar = h('div', { class: 'toolbar' });
    const calendar = h('div');
    const chips = h('div', { class: 'chips' });
    const info = h('div', { class: 'showing' });
    const list = h('div');

    clear(root);
    root.append(h('h2', null, 'Bookings'), toolbar, calendar, chips, info, list);

    async function load() {
        const mine = cursor; // a stale response for a quarter we've since left is ignored
        loading = true;
        failed = false;
        paint();

        try {
            const result = await loadQuarterBookings(mine.year, mine.quarter);
            if (mine !== cursor) return;
            bookings = bm.sortBookings(result);
        } catch (err) {
            if (mine !== cursor) return;
            failed = true;
            toast(describeError(err), 'error');
        }

        loading = false;
        paint();
    }

    function chooseQuarter(q) {
        cursor = q;
        scope = defaultScope(q);
        filterId = 'all';
        load();
    }

    function chooseScope(next) {
        scope = next;
        filterId = 'all';
        paint();
    }

    function paintToolbar() {
        clear(toolbar);
        const now = currentQuarter();
        const isCurrent = cursor.year === now.year && cursor.quarter === now.quarter;

        toolbar.append(
            h('button', { type: 'button', class: 'btn btn-secondary btn-small', 'aria-label': 'Previous quarter', onClick: function () { chooseQuarter(fmt.shiftQuarter(cursor.year, cursor.quarter, -1)); } }, '‹'),
            h('span', { class: 'month-label quarter-label', 'aria-live': 'polite' }, fmt.quarterLabel(cursor.year, cursor.quarter)),
            h('button', { type: 'button', class: 'btn btn-secondary btn-small', 'aria-label': 'Next quarter', onClick: function () { chooseQuarter(fmt.shiftQuarter(cursor.year, cursor.quarter, 1)); } }, '›')
        );
        // (Native append() would print a null as the text "null", so optional
        // items are added conditionally rather than passed as null.)
        if (!isCurrent) toolbar.append(h('button', { type: 'button', class: 'btn btn-ghost btn-small', onClick: function () { chooseQuarter(currentQuarter()); } }, 'This quarter'));
        toolbar.append(h('button', { type: 'button', class: 'btn btn-ghost btn-small', onClick: load }, 'Refresh'));
    }

    function monthBlock(month, counts) {
        const isMonthSelected = scope.kind === 'month' && scope.month === month && scope.year === cursor.year;
        const grid = h('div', { class: 'qgrid' });
        WEEKDAY_INITIALS.forEach(function (w) { grid.append(h('span', { class: 'qwd', 'aria-hidden': 'true' }, w)); });

        const layout = fmt.monthGrid(cursor.year, month);
        for (let i = 0; i < layout.leadingBlanks; i++) grid.append(h('span', { class: 'qday is-empty', 'aria-hidden': 'true' }));

        for (let day = 1; day <= layout.daysInMonth; day++) {
            const key = cursor.year + '-' + fmt.pad2(month + 1) + '-' + fmt.pad2(day);
            const n = counts[key] || 0;
            const classes = 'qday' + (key === todayKey ? ' is-today' : '') + (scope.kind === 'day' && scope.date === key ? ' is-selected' : '');

            if (n === 0) {
                grid.append(h('span', { class: classes }, h('span', { class: 'qnum' }, String(day))));
                continue;
            }
            grid.append(h('button', {
                type: 'button',
                class: classes + ' has',
                'aria-label': day + ' ' + fmt.monthName(month) + ': ' + n + (n === 1 ? ' booking' : ' bookings'),
                'aria-pressed': scope.kind === 'day' && scope.date === key ? 'true' : 'false',
                onClick: function () { chooseScope({ kind: 'day', date: key }); }
            }, h('span', { class: 'qnum' }, String(day)), h('span', { class: 'qbadge' }, String(n))));
        }

        return h('div', { class: 'qmonth' },
            h('button', {
                type: 'button', class: 'qmonth-title', 'aria-pressed': isMonthSelected ? 'true' : 'false',
                onClick: function () { chooseScope({ kind: 'month', year: cursor.year, month: month }); }
            }, fmt.monthName(month) + ' ' + cursor.year),
            grid
        );
    }

    function paintCalendar() {
        clear(calendar);
        if (loading) {
            calendar.append(h('p', { class: 'muted' }, 'Loading…'));
            return;
        }
        if (failed) return;

        const counts = bm.countsByDay(bookings);
        const quarter = h('div', { class: 'quarter' });
        fmt.quarterMonths(cursor.quarter).forEach(function (m) { quarter.append(monthBlock(m, counts)); });
        calendar.append(
            h('p', { class: 'muted small' }, 'The number on a highlighted day is how many sessions are booked that day. Select a day or a month name to see its bookings below.'),
            quarter
        );
    }

    function scopeLabel() {
        if (scope.kind === 'day') return fmt.formatDateLabel(scope.date);
        if (scope.kind === 'month') return fmt.monthLabel(scope.year, scope.month);
        return fmt.quarterLabel(cursor.year, cursor.quarter);
    }

    function paintChips() {
        clear(chips);
        if (loading || failed) return;
        const counts = bm.countByFilter(bm.scopeBookings(bookings, scope));
        bm.FILTERS.forEach(function (f) {
            // Don't show empty categories, except the one currently selected.
            if (f.id !== 'all' && f.id !== filterId && counts[f.id] === 0) return;
            chips.append(h('button', {
                type: 'button',
                class: 'chip',
                'aria-pressed': f.id === filterId ? 'true' : 'false',
                onClick: function () { filterId = f.id; paint(); }
            }, f.label, h('span', { class: 'chip-count' }, '(' + counts[f.id] + ')')));
        });
    }

    function paintInfo() {
        clear(info);
        if (loading || failed) return;
        const inScope = bm.scopeBookings(bookings, scope);
        info.append(h('span', null, 'Showing ' + scopeLabel() + ' (' + inScope.length + ')'));
        if (scope.kind !== 'quarter') {
            info.append(h('button', { type: 'button', class: 'link-btn', onClick: function () { chooseScope({ kind: 'quarter' }); } }, 'Show the whole quarter'));
        }
    }

    function bookingRow(b) {
        const meta = bm.bookingMeta(b);
        const time = b.startMinutes === null ? '' : fmt.formatTimeRange(b.startMinutes, b.endMinutes - b.startMinutes);
        const canMarkNoShow = b.status === 'confirmed' && bm.hasStarted(b, new Date());

        const noShowBtn = canMarkNoShow
            ? h('button', { type: 'button', class: 'btn btn-danger btn-small' }, 'Mark no-show')
            : null;

        if (noShowBtn) {
            noShowBtn.addEventListener('click', async function () {
                const ok = window.confirm('Mark ' + (b.customerName || 'this customer') + ' (' + fmt.formatDateLabel(b.slotDate) + ', ' + time + ') as a no-show?\n\nThis can’t be undone.');
                if (!ok) return;
                try {
                    await withBusy(noShowBtn, 'Saving…', function () { return markNoShow(b.id); });
                    toast('Marked as a no-show.');
                    load();
                } catch (err) {
                    toast(describeError(err), 'error');
                }
            });
        }

        const label = (b.customerName || 'this customer') + ' (' + fmt.formatDateLabel(b.slotDate) + ', ' + time + ')';
        const money = fmt.formatMoney(b.amount, b.currency || CURRENCY);

        const cancelBtn = bm.canCancel(b, new Date())
            ? h('button', { type: 'button', class: 'btn btn-secondary btn-small' }, 'Cancel booking')
            : null;
        if (cancelBtn) {
            cancelBtn.addEventListener('click', async function () {
                const ok = window.confirm('Cancel ' + label + '?\n\n'
                    + (b.isPaid ? 'They will be refunded ' + money + ' automatically. ' : '')
                    + 'They will be emailed and the calendar event removed.\n\nThis can’t be undone.');
                if (!ok) return;
                try {
                    const result = await withBusy(cancelBtn, 'Cancelling…', function () { return cancelBooking(b.id); });
                    if (result.refundStatus === 'needs_manual') {
                        toast('Cancelled — but the automatic refund of ' + fmt.formatMoney(result.refundAmount, b.currency || CURRENCY) + ' didn’t go through. Please refund it in Paymob, then press “Mark refunded”.', 'error');
                    } else if (result.outcome === 'already_cancelled') {
                        toast('That booking was already cancelled.');
                    } else {
                        toast(result.refundStatus === 'refunded' ? 'Cancelled. A refund of ' + fmt.formatMoney(result.refundAmount, b.currency || CURRENCY) + ' was sent to the customer.' : 'Cancelled.');
                    }
                    load();
                } catch (err) {
                    toast(describeError(err), 'error');
                }
            });
        }

        const refundBtn = bm.canMarkRefunded(b)
            ? h('button', { type: 'button', class: 'btn btn-secondary btn-small' }, 'Mark refunded')
            : null;
        if (refundBtn) {
            refundBtn.addEventListener('click', async function () {
                const ok = window.confirm('Only do this once you have refunded ' + (b.isPaid ? money : 'the customer') + ' to ' + label + ' in Paymob'
                    + (b.paymobTransactionId ? ' (transaction ' + b.paymobTransactionId + ')' : '') + '.\n\nMark it as refunded?');
                if (!ok) return;
                try {
                    await withBusy(refundBtn, 'Saving…', function () { return markRefunded(b); });
                    toast('Marked as refunded.');
                    load();
                } catch (err) {
                    toast(describeError(err), 'error');
                }
            });
        }

        const contact = h('div', { class: 'booking-contact' }, b.customerName);
        if (b.customerEmail) contact.append(' · ', h('a', { href: 'mailto:' + b.customerEmail }, b.customerEmail));
        if (b.customerMobile) contact.append(' · ', h('a', { href: 'tel:' + b.customerMobile.replace(/[^\d+]/g, '') }, b.customerMobile));

        const ref = 'Ref ' + b.id
            + (bm.refundOutstanding(b) && b.paymobTransactionId ? ' · Paymob transaction ' + b.paymobTransactionId : '')
            + (b.status === 'cancelled' && b.cancelledBy ? ' · cancelled by ' + (b.cancelledBy === 'admin' ? 'you' : 'the customer') : '');

        return h('div', { class: 'card booking' + (bm.refundOutstanding(b) ? ' booking-flag' : '') },
            h('div', { class: 'booking-when' }, fmt.formatDateLabel(b.slotDate), time ? h('span', { class: 'booking-time' }, time) : null),
            h('div', null,
                h('div', { class: 'booking-service' }, b.serviceName),
                contact,
                h('div', { class: 'booking-ref' }, ref)
            ),
            h('div', { class: 'booking-side' },
                h('span', { class: 'badge badge-' + meta.tone }, meta.label),
                h('span', { class: 'booking-money' }, b.isPaid ? fmt.formatMoney(b.amount, b.currency || CURRENCY) : 'Free'),
                noShowBtn,
                cancelBtn,
                refundBtn
            )
        );
    }

    function paintList() {
        clear(list);

        if (loading) {
            list.append(h('p', { class: 'muted' }, 'Loading…'));
            return;
        }
        if (failed) {
            list.append(h('div', { class: 'banner banner-bad' }, 'Couldn’t load bookings. ', h('button', { type: 'button', class: 'btn btn-secondary btn-small', onClick: load }, 'Try again')));
            return;
        }

        // Refunds still to do are flagged for the WHOLE quarter, so one never hides behind the selected day.
        const needRefund = bookings.filter(bm.refundOutstanding).length;
        if (needRefund > 0) {
            list.append(h('div', { class: 'banner banner-bad', role: 'alert' },
                h('strong', null, needRefund + (needRefund === 1 ? ' booking needs' : ' bookings need') + ' a refund in Paymob. '),
                'Either the customer paid for a time that had been taken by someone else, or a cancellation’s automatic refund didn’t go through. Refund them in Paymob, then press “Mark refunded” — filter by “Needs refund” to see who.'
            ));
        }

        const inScope = bm.scopeBookings(bookings, scope);
        const shown = bm.filterBookings(inScope, filterId);
        if (shown.length === 0) {
            list.append(h('p', { class: 'muted' }, inScope.length === 0 ? 'No bookings in ' + scopeLabel() + '.' : 'No bookings match this filter.'));
            return;
        }
        shown.forEach(function (b) { list.append(bookingRow(b)); });
    }

    function paint() {
        paintToolbar();
        paintCalendar();
        paintChips();
        paintInfo();
        paintList();
    }

    load();
}
