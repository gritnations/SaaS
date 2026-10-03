// Manage-my-booking page: view a booking from the private link in the
// confirmation email, cancel it, or move it to another time.
//
//   manageBooking      GET   the booking + what can be done with it
//   manageSlots        GET   free times for moving it (same length)
//   manageCancel       POST  cancel (the server refunds a paid booking)
//   manageReschedule   POST  move to another free time (no payment involved)
//
// The link carries ?bookingId=...&token=... . The token is the only credential,
// so it is only ever sent to the booking backend. All text goes in with
// textContent, never as HTML.

(function () {
    var API_BASE = window.BOOKING_API_BASE || window.ZA_BOOKING_API_BASE || null;
    // Optional support address for error messages; without it they say "contact us".
    var CONTACT_EMAIL = window.BOOKING_CONTACT_EMAIL || '';
    var CONTACT_US = CONTACT_EMAIL ? 'email ' + CONTACT_EMAIL : 'contact us';
    var CONTACT_US_CAP = CONTACT_US.charAt(0).toUpperCase() + CONTACT_US.slice(1);
    // Where "Book a session" links point (the booking page of the same site).
    var BOOK_URL = window.BOOKING_PAGE_URL || 'book-a-session.html';

    var MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June',
        'July', 'August', 'September', 'October', 'November', 'December'];
    var MONTH_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
        'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    var WEEKDAY_SHORT = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

    var bookingId = null;
    var token = null;
    var view = null;

    var slotsByDate = {};
    var viewYear = 0;
    var viewMonth = 0;
    var todayKey = '';
    var selectedDateKey = null;
    var selectedSlot = null;
    var busy = false;

    // ---------------------------------------------------------------- helpers

    function el(id) { return document.getElementById(id); }
    function show(node) { if (node) node.hidden = false; }
    function hide(node) { if (node) node.hidden = true; }
    function pad2(n) { return n < 10 ? '0' + n : String(n); }
    function dateKey(y, m, d) { return y + '-' + pad2(m + 1) + '-' + pad2(d); }

    function formatDateLabel(key) {
        var p = key.split('-');
        var d = new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]));
        return WEEKDAY_SHORT[d.getDay()] + ', ' + d.getDate() + ' ' + MONTH_SHORT[d.getMonth()];
    }

    function formatMoney(amount, currency) {
        return (currency || 'SAR') + ' ' + (amount / 100).toFixed(2);
    }

    function isHttpsUrl(url) { return typeof url === 'string' && /^https:\/\//i.test(url); }

    function scrollTo(node) {
        if (node && node.scrollIntoView) node.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function addLink(container, url, label, external) {
        var a = document.createElement('a');
        a.className = 'booking-confirmation-link';
        a.href = url;
        if (external) { a.target = '_blank'; a.rel = 'noopener noreferrer'; }
        a.textContent = label;
        container.appendChild(a);
    }

    function setBusy(button, isBusy, busyLabel) {
        busy = isBusy;
        if (!button) return;
        if (isBusy) {
            button.setAttribute('data-label', button.textContent);
            button.textContent = busyLabel;
        } else if (button.getAttribute('data-label')) {
            button.textContent = button.getAttribute('data-label');
        }
        var all = document.querySelectorAll('#manage-view button');
        for (var i = 0; i < all.length; i++) all[i].disabled = isBusy;
    }

    function readLink() {
        var q = window.location.search;
        var id = /[?&]bookingId=([A-Za-z0-9]{10,40})(?:&|$)/.exec(q);
        var tok = /[?&]token=([a-f0-9]{48})(?:&|$)/.exec(q);
        return id && tok ? { id: id[1], token: tok[1] } : null;
    }

    // Returns a promise of { status, data }. Never rejects for an HTTP error —
    // only for a network failure.
    function call(name, method, body) {
        var url = API_BASE + '/' + name;
        var options = { method: method };
        if (method === 'GET') {
            url += '?bookingId=' + encodeURIComponent(bookingId) + '&token=' + encodeURIComponent(token);
        } else {
            options.headers = { 'Content-Type': 'application/json' };
            options.body = JSON.stringify(Object.assign({ bookingId: bookingId, token: token }, body || {}));
        }
        return fetch(url, options).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (data) {
                return { status: response.status, data: data };
            });
        });
    }

    // ---------------------------------------------------------------- screens

    function showFatal(message) {
        hide(el('manage-loading'));
        hide(el('manage-view'));
        var box = el('manage-error');
        box.textContent = message;
        show(box);
    }

    function showResult(title, text, links) {
        hide(el('manage-loading'));
        hide(el('manage-error'));
        hide(el('manage-view'));
        el('manage-result-title').textContent = title;
        el('manage-result-text').textContent = text;
        var box = el('manage-result-links');
        box.textContent = '';
        (links || []).forEach(function (l) { addLink(box, l.url, l.label, l.external); });
        show(el('manage-result'));
        scrollTo(el('manage-result'));
    }

    function bookAgain() {
        return { url: BOOK_URL, label: 'Book a session' };
    }

    function refundSentence(info) {
        if (!(info.refundAmount > 0)) return '';
        var amount = formatMoney(info.refundAmount, info.currency);
        if (info.refundStatus === 'refunded') {
            return 'A refund of ' + amount + ' has been issued to your original payment method. Banks can take several working days to show it.';
        }
        return 'We are processing your refund of ' + amount + ' and will email you as soon as it has been sent.';
    }

    function statusNote(v) {
        if (v.status === 'confirmed' && !v.canCancel) return 'This session has already taken place, so it can no longer be changed.';
        if (v.status === 'cancelled') return ('This booking has been cancelled. ' + refundSentence(v)).trim();
        if (v.status === 'pending') return 'This booking is still waiting for payment to be completed.';
        if (v.status === 'expired') return 'This booking expired before payment was completed, so no booking was made.';
        if (v.status === 'conflict') return 'Your payment reached us after this time had been taken, so it was not confirmed. We will refund you in full. Questions? ' + CONTACT_US_CAP + '.';
        return '';
    }

    function renderBooking() {
        hide(el('manage-loading'));
        hide(el('manage-error'));
        hide(el('manage-result'));
        hide(el('manage-cancel-panel'));
        hide(el('manage-reschedule-panel'));
        show(el('manage-view'));

        el('manage-summary').textContent = view.serviceName + ' — ' + view.slotDateLabel + ', ' + view.slotTimeLabel;

        var note = statusNote(view);
        el('manage-note').textContent = note;
        if (note) show(el('manage-note')); else hide(el('manage-note'));

        var links = el('manage-links');
        links.textContent = '';
        if (view.canCancel && isHttpsUrl(view.meetLink)) addLink(links, view.meetLink, 'Join Google Meet', true);
        if (view.status === 'cancelled' || view.status === 'expired' || view.status === 'conflict') addLink(links, BOOK_URL, 'Book a session', false);

        if (view.canCancel) show(el('manage-actions')); else hide(el('manage-actions'));
    }

    // ---------------------------------------------------------------- cancel

    function openCancel() {
        hide(el('manage-actions'));
        hide(el('manage-reschedule-panel'));
        hide(el('manage-cancel-error'));

        var text = view.refundAmount > 0
            ? 'If you cancel, you will be refunded ' + formatMoney(view.refundAmount, view.currency) + ' to your original payment method. This can’t be undone.'
            : 'If you cancel, this session will be released. This can’t be undone.';
        el('manage-cancel-text').textContent = text;
        show(el('manage-cancel-panel'));
        scrollTo(el('manage-cancel-panel'));
    }

    function closeCancel() {
        hide(el('manage-cancel-panel'));
        show(el('manage-actions'));
    }

    function confirmCancel() {
        if (busy) return;
        var button = el('manage-cancel-yes');
        hide(el('manage-cancel-error'));
        setBusy(button, true, 'Cancelling…');

        call('manageCancel', 'POST').then(function (res) {
            setBusy(button, false);
            if (res.status === 200) {
                var sentence = refundSentence({ refundAmount: res.data.refundAmount, refundStatus: res.data.refundStatus, currency: view.currency });
                showResult('Your booking has been cancelled',
                    (sentence ? sentence + ' ' : '') + 'We’ve emailed you a confirmation.',
                    [bookAgain()]);
                return;
            }
            var message = res.data && res.data.error
                ? res.data.error
                : 'Something went wrong. Please try again, or ' + CONTACT_US + '.';
            el('manage-cancel-error').textContent = message;
            show(el('manage-cancel-error'));
        }).catch(function () {
            setBusy(button, false);
            el('manage-cancel-error').textContent = 'We couldn’t reach the booking service. Check your connection and try again.';
            show(el('manage-cancel-error'));
        });
    }

    // ---------------------------------------------------------------- reschedule

    function initCalendarState() {
        var today = new Date();
        today.setHours(0, 0, 0, 0);
        todayKey = dateKey(today.getFullYear(), today.getMonth(), today.getDate());
        viewYear = today.getFullYear();
        viewMonth = today.getMonth();
    }

    function resetSelection() {
        selectedSlot = null;
        selectedDateKey = null;
        hide(el('manage-selected-summary'));
        hide(el('manage-day-times'));
        show(el('manage-day-times-placeholder'));
        show(el('manage-calendar'));
        hide(el('manage-reschedule-error'));
        renderCalendar();
    }

    function renderCalendar() {
        var grid = el('manage-cal-grid');
        var weekdays = el('manage-cal-weekdays');
        el('manage-cal-month-label').textContent = MONTH_NAMES[viewMonth] + ' ' + viewYear;

        if (weekdays.childElementCount === 0) {
            for (var w = 0; w < WEEKDAY_SHORT.length; w++) {
                var label = document.createElement('span');
                label.textContent = WEEKDAY_SHORT[w];
                weekdays.appendChild(label);
            }
        }

        grid.textContent = '';
        var daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate();
        var blanks = new Date(viewYear, viewMonth, 1).getDay();
        for (var b = 0; b < blanks; b++) {
            var blank = document.createElement('span');
            blank.className = 'booking-calendar-day is-empty';
            grid.appendChild(blank);
        }

        var t = todayKey.split('-').map(Number);
        var todayValue = t[0] * 10000 + t[1] * 100 + t[2];

        for (var day = 1; day <= daysInMonth; day++) {
            var key = dateKey(viewYear, viewMonth, day);
            var value = viewYear * 10000 + (viewMonth + 1) * 100 + day;
            var daySlots = slotsByDate[key] || [];
            var open = daySlots.some(function (s) { return s.status === 'available'; });

            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'booking-calendar-day';
            btn.textContent = String(day);
            btn.setAttribute('aria-label', formatDateLabel(key));

            if (value < todayValue || !open) {
                btn.disabled = true;
                btn.className += ' is-disabled';
            } else {
                btn.className += ' is-available';
                if (key === selectedDateKey) btn.className += ' is-selected';
                (function (k) { btn.addEventListener('click', function () { selectDate(k); }); })(key);
            }
            grid.appendChild(btn);
        }

        el('manage-cal-prev').disabled = (viewYear === t[0] && viewMonth === t[1] - 1);
    }

    function goToMonth(delta) {
        viewMonth += delta;
        if (viewMonth > 11) { viewMonth = 0; viewYear++; }
        else if (viewMonth < 0) { viewMonth = 11; viewYear--; }
        renderCalendar();
    }

    function selectDate(key) {
        selectedDateKey = key;
        renderCalendar();
        renderTimes(key);
        hide(el('manage-day-times-placeholder'));
        show(el('manage-day-times'));
    }

    function renderTimes(key) {
        var grid = el('manage-slots-grid');
        grid.textContent = '';
        el('manage-day-times-label').textContent = 'Times on ' + formatDateLabel(key);

        var slots = slotsByDate[key] || [];
        if (slots.length === 0) { show(el('manage-day-times-empty')); hide(grid); return; }
        hide(el('manage-day-times-empty'));
        show(grid);

        slots.forEach(function (slot) {
            var isBooked = slot.status === 'booked';
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = isBooked ? 'booking-slot-btn is-booked' : 'booking-slot-btn';

            var text = document.createElement('span');
            text.textContent = slot.label;
            btn.appendChild(text);

            if (isBooked) {
                btn.disabled = true;
                var badge = document.createElement('span');
                badge.className = 'booking-slot-badge';
                badge.textContent = 'Booked';
                btn.appendChild(badge);
            } else {
                btn.addEventListener('click', function () { selectSlot(slot, key); });
            }
            grid.appendChild(btn);
        });
    }

    function selectSlot(slot, key) {
        selectedSlot = slot;
        hide(el('manage-reschedule-error'));
        hide(el('manage-calendar'));
        hide(el('manage-day-times'));
        el('manage-selected-summary-text').textContent = 'New time: ' + formatDateLabel(key) + ' — ' + slot.label;
        show(el('manage-selected-summary'));
    }

    function loadSlots() {
        show(el('manage-slots-loading'));
        hide(el('manage-slots-error'));
        hide(el('manage-calendar'));
        hide(el('manage-day-times'));
        show(el('manage-day-times-placeholder'));
        hide(el('manage-selected-summary'));

        call('manageSlots', 'GET').then(function (res) {
            hide(el('manage-slots-loading'));
            if (res.status !== 200) {
                el('manage-slots-error').textContent = (res.data && res.data.error) || 'We couldn’t load the available times. Please try again.';
                show(el('manage-slots-error'));
                return;
            }
            slotsByDate = res.data.slotsByDate || {};
            var hasAny = Object.keys(slotsByDate).some(function (k) {
                return slotsByDate[k].some(function (s) { return s.status === 'available'; });
            });
            if (!hasAny) {
                el('manage-slots-error').textContent = 'There are no other open times right now. Please check again soon, or ' + CONTACT_US + '.';
                show(el('manage-slots-error'));
                return;
            }
            initCalendarState();
            resetSelection();
        }).catch(function () {
            hide(el('manage-slots-loading'));
            el('manage-slots-error').textContent = 'We couldn’t reach the booking service. Check your connection and try again.';
            show(el('manage-slots-error'));
        });
    }

    function openReschedule() {
        hide(el('manage-actions'));
        hide(el('manage-cancel-panel'));
        show(el('manage-reschedule-panel'));
        scrollTo(el('manage-reschedule-panel'));
        loadSlots();
    }

    function closeReschedule() {
        hide(el('manage-reschedule-panel'));
        show(el('manage-actions'));
    }

    function confirmReschedule() {
        if (busy || !selectedSlot || !selectedDateKey) return;
        var button = el('manage-confirm-btn');
        hide(el('manage-reschedule-error'));
        setBusy(button, true, 'Moving…');

        call('manageReschedule', 'POST', { date: selectedDateKey, slotId: selectedSlot.id }).then(function (res) {
            setBusy(button, false);
            if (res.status === 200) {
                var links = [];
                if (isHttpsUrl(res.data.meetLink)) links.push({ url: res.data.meetLink, label: 'Join Google Meet', external: true });
                showResult('Your session has been moved',
                    view.serviceName + ' — ' + res.data.slotDateLabel + ', ' + res.data.slotTimeLabel + '. We’ve emailed you the new details.',
                    links);
                return;
            }
            el('manage-reschedule-error').textContent = (res.data && res.data.error) || 'Something went wrong. Please try again, or ' + CONTACT_US + '.';
            show(el('manage-reschedule-error'));
            // A time that was just taken (or a booking that can no longer change) means the picture is stale.
            if (res.status === 409) loadSlots();
        }).catch(function () {
            setBusy(button, false);
            el('manage-reschedule-error').textContent = 'We couldn’t reach the booking service. Check your connection and try again.';
            show(el('manage-reschedule-error'));
        });
    }

    // ---------------------------------------------------------------- start

    function loadBooking() {
        call('manageBooking', 'GET').then(function (res) {
            if (res.status === 200) { view = res.data; renderBooking(); return; }
            if (res.status === 429) { showFatal('Too many attempts. Please try again in a few minutes.'); return; }
            showFatal('This link isn’t valid. Please use the “Manage my booking” link in your confirmation email, or ' + CONTACT_US + '.');
        }).catch(function () {
            showFatal('We couldn’t reach the booking service. Check your connection and refresh the page.');
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        if (!el('manage-loading')) return;

        var link = readLink();
        if (!API_BASE) { showFatal('Booking management isn’t available in preview mode.'); return; }
        if (!link) { showFatal('This link isn’t valid. Please use the “Manage my booking” link in your confirmation email, or ' + CONTACT_US + '.'); return; }
        bookingId = link.id;
        token = link.token;

        el('manage-reschedule-btn').addEventListener('click', openReschedule);
        el('manage-cancel-btn').addEventListener('click', openCancel);
        el('manage-cancel-yes').addEventListener('click', confirmCancel);
        el('manage-cancel-no').addEventListener('click', closeCancel);
        el('manage-reschedule-back').addEventListener('click', closeReschedule);
        el('manage-confirm-btn').addEventListener('click', confirmReschedule);
        el('manage-change-slot-btn').addEventListener('click', resetSelection);
        el('manage-cal-prev').addEventListener('click', function () { goToMonth(-1); });
        el('manage-cal-next').addEventListener('click', function () { goToMonth(1); });

        loadBooking();
    });
})();
