// Booking flow for book-a-session.html:
//   1. choose a service (each has its own duration and price, or is free)
//   2. pick a month/day from a calendar, then a time for that day
//   3. fill in details, then either
//        - a FREE service is confirmed instantly by the backend, or
//        - a PAID service is handed off to the payment provider's hosted
//          checkout (nothing is confirmed until payment clears, server-side).
//
// window.BOOKING_API_BASE (or the older window.ZA_BOOKING_API_BASE) sets the backend base URL (same override
// pattern as window.GN_CHAT_API_BASE in chat-widget.js). Until the backend
// is deployed, this file falls back to local preview data so the page can
// still be reviewed visually — that fallback is clearly labeled and must
// not be mistaken for real availability.

(function () {
    var API_BASE = window.BOOKING_API_BASE || window.ZA_BOOKING_API_BASE || null;
    // Something@domain.tld: no spaces, a dot-separated domain with no empty parts, and a
    // last part of at least 2 characters. Deliberately a shape check, not a delivery check.
    var EMAIL_PATTERN = /^[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)*\.[^\s@.]{2,}$/;
    var MOBILE_PATTERN = /^[+\d][\d\s-]{6,20}$/;

    var MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June',
        'July', 'August', 'September', 'October', 'November', 'December'];
    var MONTH_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
        'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    var WEEKDAY_SHORT = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

    // Preview-only sample data (used ONLY when no backend is configured):
    // one paid and one free service, with different durations, so every
    // branch of the UI can be reviewed without a backend.
    var PREVIEW_SERVICES = [
        { id: 'preview-review', name: 'Business Review', description: 'A structured assessment of where the business stands and what to prioritise.', durationMinutes: 60, amount: 50000, isFree: false },
        { id: 'preview-intro', name: 'Introductory Call', description: 'A short conversation to see whether we are the right fit.', durationMinutes: 30, amount: 0, isFree: true },
        { id: 'preview-deep', name: 'Strategy Deep-Dive', description: 'An extended working session on one important decision.', durationMinutes: 90, amount: 120000, isFree: false }
    ];
    var PREVIEW_DAY_START_MINUTES = 9 * 60;
    var PREVIEW_DAY_END_MINUTES = 17 * 60;

    var services = [];
    var servicesCurrency = 'SAR';
    var selectedService = null;

    // slotsByDate: { 'YYYY-MM-DD': [ { id, time, label, status }, ... ] }
    // status is 'available' or 'booked' — the backend returns every slot in
    // the business day (not just open ones) so booked times can be shown,
    // greyed out, instead of silently disappearing.
    var slotsByDate = {};
    var slotsRequestId = 0;
    var calendarViewYear = 0;
    var calendarViewMonth = 0;
    var todayKey = '';
    var selectedDateKey = null;
    var selectedSlot = null;

    function pad2(n) {
        return n < 10 ? '0' + n : String(n);
    }

    function dateKey(year, month, day) {
        return year + '-' + pad2(month + 1) + '-' + pad2(day);
    }

    function el(id) {
        return document.getElementById(id);
    }

    function show(node) {
        if (node) node.hidden = false;
    }

    function hide(node) {
        if (node) node.hidden = true;
    }

    function scrollToNode(node) {
        if (node && node.scrollIntoView) {
            node.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
    }

    function isHttpsUrl(url) {
        return typeof url === 'string' && /^https:\/\//i.test(url);
    }

    function formatDateLabel(key) {
        var parts = key.split('-');
        var d = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
        return WEEKDAY_SHORT[d.getDay()] + ', ' + d.getDate() + ' ' + MONTH_SHORT[d.getMonth()];
    }

    function format12Hour(totalMinutes) {
        var hour24 = Math.floor(totalMinutes / 60) % 24;
        var minute = totalMinutes % 60;
        var suffix = hour24 < 12 ? 'AM' : 'PM';
        var hour12 = hour24 % 12;
        if (hour12 === 0) hour12 = 12;
        return { text: hour12 + ':' + pad2(minute), suffix: suffix };
    }

    function formatSessionLabel(startMinutes, durationMinutes) {
        var start = format12Hour(startMinutes);
        var end = format12Hour(startMinutes + durationMinutes);
        // Only show one AM/PM suffix when both ends share it (e.g. "9:00 - 10:00 AM").
        if (start.suffix === end.suffix) {
            return start.text + ' – ' + end.text + ' ' + end.suffix;
        }
        return start.text + ' ' + start.suffix + ' – ' + end.text + ' ' + end.suffix;
    }

    function formatDuration(minutes) {
        if (minutes < 60) return minutes + ' min';
        var h = Math.floor(minutes / 60);
        var m = minutes % 60;
        return h + ' hr' + (m ? ' ' + m + ' min' : '');
    }

    function formatPrice(service) {
        if (service.isFree) return 'Free';
        var major = service.amount / 100;
        return servicesCurrency + ' ' + (service.amount % 100 === 0 ? String(major) : major.toFixed(2));
    }

    // Sample availability for the preview: Sun-Thu, 9-5, back-to-back
    // sessions of the chosen service's length, with a random ~30% marked
    // 'booked' purely to demonstrate that state — the real backend decides
    // this from actual reservations, not chance.
    function buildPreviewAvailability(durationMinutes) {
        var map = {};
        var today = new Date();
        today.setHours(0, 0, 0, 0);

        for (var i = 1; i <= 42; i++) {
            var d = new Date(today.getTime());
            d.setDate(d.getDate() + i);

            var weekday = d.getDay(); // 0 = Sun ... 6 = Sat
            if (weekday === 5 || weekday === 6) continue; // Fri/Sat weekend

            var key = dateKey(d.getFullYear(), d.getMonth(), d.getDate());
            var daySlots = [];
            for (var m = PREVIEW_DAY_START_MINUTES; m + durationMinutes <= PREVIEW_DAY_END_MINUTES; m += durationMinutes) {
                var time = pad2(Math.floor(m / 60)) + ':' + pad2(m % 60);
                daySlots.push({
                    id: key + '-' + time,
                    time: time,
                    label: formatSessionLabel(m, durationMinutes),
                    status: Math.random() < 0.3 ? 'booked' : 'available'
                });
            }
            map[key] = daySlots;
        }
        return map;
    }

    // ------------------------------------------------------------------
    // Step 1: services
    // ------------------------------------------------------------------

    function renderServices() {
        var grid = el('booking-services-grid');
        if (!grid) return;
        grid.textContent = '';

        services.forEach(function (service) {
            var card = document.createElement('button');
            card.type = 'button';
            card.className = 'booking-service-card';

            var name = document.createElement('span');
            name.className = 'booking-service-name';
            name.textContent = service.name;
            card.appendChild(name);

            if (service.description) {
                var desc = document.createElement('span');
                desc.className = 'booking-service-desc';
                desc.textContent = service.description;
                card.appendChild(desc);
            }

            var meta = document.createElement('span');
            meta.className = 'booking-service-meta' + (service.isFree ? ' booking-service-free' : '');
            meta.textContent = formatDuration(service.durationMinutes) + ' · ' + formatPrice(service);
            card.appendChild(meta);

            card.addEventListener('click', function () {
                selectService(service);
            });
            grid.appendChild(card);
        });
    }

    function loadServices() {
        var loading = el('booking-services-loading');
        var errorMsg = el('booking-services-error');
        var grid = el('booking-services-grid');

        if (!API_BASE) {
            // Backend not deployed yet — show local preview data so the
            // page layout can still be reviewed, clearly labeled as such.
            services = PREVIEW_SERVICES;
            servicesCurrency = 'SAR';
            hide(loading);

            var notice = document.createElement('p');
            notice.className = 'booking-status-text booking-status-preview';
            notice.textContent = 'Preview mode — showing sample services and availability. Not yet connected to the live booking backend.';
            grid.parentNode.insertBefore(notice, grid);

            renderServices();
            show(grid);
            return;
        }

        fetch(API_BASE + '/listServices')
            .then(function (response) {
                if (!response.ok) throw new Error('Request failed');
                return response.json();
            })
            .then(function (data) {
                hide(loading);
                services = (data && data.services) || [];
                servicesCurrency = (data && data.currency) || 'SAR';

                if (services.length === 0) {
                    show(errorMsg);
                    return;
                }
                renderServices();
                show(grid);
            })
            .catch(function () {
                hide(loading);
                show(errorMsg);
            });
    }

    // Tailors the Step 3 button and reassurance copy to whether the chosen
    // service takes payment.
    function updateDetailsCopy() {
        var submitBtn = el('booking-submit-btn');
        var note = el('booking-details-note');
        var free = !!(selectedService && selectedService.isFree);

        if (submitBtn) submitBtn.textContent = free ? 'Confirm Booking' : 'Proceed to Payment';
        if (note) {
            note.textContent = free
                ? 'Your details are sent over a secure, encrypted connection. Your booking is confirmed straight away and the details are emailed to you.'
                : 'Your details are sent over a secure, encrypted connection. You’ll be redirected to our secure payment provider to complete payment. This confirms your appointment — nothing is booked until payment clears.';
        }
    }

    function selectService(service) {
        selectedService = service;

        hide(el('booking-services-grid'));

        var summaryText = el('booking-service-summary-text');
        if (summaryText) {
            summaryText.textContent = 'Selected: ' + service.name + ' — ' + formatDuration(service.durationMinutes) + ' · ' + formatPrice(service);
        }
        show(el('booking-service-summary'));

        updateDetailsCopy();
        show(el('booking-step-slots'));
        loadSlots();
        scrollToNode(el('booking-step-slots'));
    }

    // Back to the start: clears everything chosen after the service.
    function resetService() {
        slotsRequestId++; // ignore any availability request still in flight
        selectedService = null;
        selectedSlot = null;
        selectedDateKey = null;
        slotsByDate = {};

        hide(el('booking-service-summary'));
        hide(el('booking-step-slots'));
        hide(el('booking-step-details'));
        hide(el('booking-confirmation'));
        show(el('booking-services-grid'));
    }

    // ------------------------------------------------------------------
    // Step 2: calendar + times
    // ------------------------------------------------------------------

    function renderCalendar() {
        var grid = el('booking-cal-grid');
        var weekdaysRow = el('booking-cal-weekdays');
        var monthLabel = el('booking-cal-month-label');
        var prevBtn = el('booking-cal-prev');
        if (!grid || !weekdaysRow || !monthLabel) return;

        monthLabel.textContent = MONTH_NAMES[calendarViewMonth] + ' ' + calendarViewYear;

        if (weekdaysRow.childElementCount === 0) {
            for (var w = 0; w < WEEKDAY_SHORT.length; w++) {
                var wLabel = document.createElement('span');
                wLabel.textContent = WEEKDAY_SHORT[w];
                weekdaysRow.appendChild(wLabel);
            }
        }

        grid.textContent = '';

        var firstOfMonth = new Date(calendarViewYear, calendarViewMonth, 1);
        var daysInMonth = new Date(calendarViewYear, calendarViewMonth + 1, 0).getDate();
        var leadingBlanks = firstOfMonth.getDay();

        for (var b = 0; b < leadingBlanks; b++) {
            var blank = document.createElement('span');
            blank.className = 'booking-calendar-day is-empty';
            grid.appendChild(blank);
        }

        var todayParts = todayKey.split('-').map(Number);
        var todayValue = todayParts[0] * 10000 + todayParts[1] * 100 + todayParts[2];

        for (var day = 1; day <= daysInMonth; day++) {
            var key = dateKey(calendarViewYear, calendarViewMonth, day);
            var dayValue = calendarViewYear * 10000 + (calendarViewMonth + 1) * 100 + day;

            var daySlots = slotsByDate[key] || [];
            var hasOpenSlot = daySlots.some(function (s) { return s.status === 'available'; });
            var isPast = dayValue < todayValue;

            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'booking-calendar-day';
            btn.textContent = String(day);
            btn.setAttribute('aria-label', formatDateLabel(key));

            if (isPast || !hasOpenSlot) {
                btn.disabled = true;
                btn.className += ' is-disabled';
            } else {
                btn.className += ' is-available';
                if (key === selectedDateKey) btn.className += ' is-selected';
                (function (k) {
                    btn.addEventListener('click', function () {
                        selectDate(k);
                    });
                })(key);
            }

            grid.appendChild(btn);
        }

        if (prevBtn) {
            var isCurrentMonthInView = (calendarViewYear === Number(todayParts[0]) && calendarViewMonth === Number(todayParts[1]) - 1);
            prevBtn.disabled = isCurrentMonthInView;
        }
    }

    function goToMonth(delta) {
        calendarViewMonth += delta;
        if (calendarViewMonth > 11) {
            calendarViewMonth = 0;
            calendarViewYear++;
        } else if (calendarViewMonth < 0) {
            calendarViewMonth = 11;
            calendarViewYear--;
        }
        renderCalendar();
    }

    function renderTimesForDate(key) {
        var label = el('booking-day-times-label');
        var emptyMsg = el('booking-day-times-empty');
        var grid = el('booking-slots-grid');
        if (!grid) return;

        grid.textContent = '';
        if (label) label.textContent = 'Times on ' + formatDateLabel(key);

        var slots = slotsByDate[key] || [];
        if (slots.length === 0) {
            show(emptyMsg);
            hide(grid);
            return;
        }
        hide(emptyMsg);
        show(grid);

        for (var i = 0; i < slots.length; i++) {
            (function (slot) {
                var isBooked = slot.status === 'booked';

                var btn = document.createElement('button');
                btn.type = 'button';
                btn.className = isBooked ? 'booking-slot-btn is-booked' : 'booking-slot-btn';

                var timeText = document.createElement('span');
                timeText.textContent = slot.label;
                btn.appendChild(timeText);

                if (isBooked) {
                    btn.disabled = true;
                    var badge = document.createElement('span');
                    badge.className = 'booking-slot-badge';
                    badge.textContent = 'Booked';
                    btn.appendChild(badge);
                } else {
                    btn.addEventListener('click', function () {
                        selectSlot(slot, key);
                    });
                }

                grid.appendChild(btn);
            })(slots[i]);
        }
    }

    function selectDate(key) {
        selectedDateKey = key;
        renderCalendar();
        renderTimesForDate(key);
        hide(el('booking-day-times-placeholder'));
        show(el('booking-day-times'));
    }

    function selectSlot(slot, key) {
        selectedSlot = slot;
        hide(el('booking-slots-notice'));

        hide(el('booking-calendar'));
        hide(el('booking-day-times'));

        var summaryText = el('booking-selected-summary-text');
        if (summaryText) summaryText.textContent = 'Selected: ' + formatDateLabel(key) + ' — ' + slot.label;
        show(el('booking-selected-summary'));

        show(el('booking-step-details'));
        scrollToNode(el('booking-step-details'));
    }

    function resetSlotSelection() {
        selectedSlot = null;
        selectedDateKey = null;
        hide(el('booking-selected-summary'));
        hide(el('booking-step-details'));
        hide(el('booking-day-times'));
        show(el('booking-day-times-placeholder'));
        show(el('booking-calendar'));
        renderCalendar();
    }

    function initCalendarState() {
        var today = new Date();
        today.setHours(0, 0, 0, 0);
        todayKey = dateKey(today.getFullYear(), today.getMonth(), today.getDate());
        calendarViewYear = today.getFullYear();
        calendarViewMonth = today.getMonth();
    }

    // Puts Step 2 back into its "loading" state before (re)fetching.
    function prepareSlotsStep() {
        selectedSlot = null;
        selectedDateKey = null;
        show(el('booking-slots-loading'));
        hide(el('booking-slots-error'));
        hide(el('booking-slots-empty'));
        hide(el('booking-slots-notice'));
        hide(el('booking-calendar'));
        hide(el('booking-day-times'));
        show(el('booking-day-times-placeholder'));
        hide(el('booking-selected-summary'));
        hide(el('booking-step-details'));
    }

    function loadSlots() {
        var loading = el('booking-slots-loading');
        var errorMsg = el('booking-slots-error');
        var emptyMsg = el('booking-slots-empty');
        var calendar = el('booking-calendar');

        if (!selectedService) return;

        prepareSlotsStep();
        initCalendarState();

        if (!API_BASE) {
            slotsByDate = buildPreviewAvailability(selectedService.durationMinutes);
            hide(loading);
            renderCalendar();
            show(calendar);
            return;
        }

        // Ignore a response if the customer has since picked something else.
        var myRequest = ++slotsRequestId;

        fetch(API_BASE + '/availableSlots?serviceId=' + encodeURIComponent(selectedService.id))
            .then(function (response) {
                if (!response.ok) throw new Error('Request failed');
                return response.json();
            })
            .then(function (data) {
                if (myRequest !== slotsRequestId) return;
                hide(loading);

                slotsByDate = (data && data.slotsByDate) || {};
                var hasAny = Object.keys(slotsByDate).some(function (k) {
                    return slotsByDate[k] && slotsByDate[k].some(function (s) { return s.status === 'available'; });
                });

                if (!hasAny) {
                    show(emptyMsg);
                    return;
                }

                renderCalendar();
                show(calendar);
            })
            .catch(function () {
                if (myRequest !== slotsRequestId) return;
                hide(loading);
                show(errorMsg);
            });
    }

    // ------------------------------------------------------------------
    // Step 3: details + submit
    // ------------------------------------------------------------------

    function validateDetails(name, email, mobile) {
        if (!name) return 'Please enter your name.';
        if (!EMAIL_PATTERN.test(email)) return 'Please enter a valid email address.';
        if (!mobile || !MOBILE_PATTERN.test(mobile)) return 'Please enter a valid mobile number for the selected country code.';
        return null;
    }

    // ---- Mobile: country code + number ------------------------------------
    // The backend takes ONE string, so the two fields are joined as +<code><number>
    // (for example +966501234567). Spaces, dashes and brackets in what was typed are
    // ignored, and the leading 0 many countries write before the local number is
    // dropped (0501234567 becomes 501234567).
    var DEFAULT_COUNTRY = window.BOOKING_DEFAULT_COUNTRY || 'SA';   // ISO code, e.g. 'GB'

    function allCountries() {
        var c = window.ZA_COUNTRIES || { priority: [], others: [] };
        return c.priority.concat(c.others);
    }

    function fillCountrySelect() {
        var select = el('booking-country');
        if (!select) return;
        var lists = window.ZA_COUNTRIES;
        if (!lists) return;

        function addOptions(entries) {
            entries.forEach(function (c) {
                var option = document.createElement('option');
                option.value = c[0];                       // the ISO abbreviation is the value, so shared codes like +1 stay distinct
                option.textContent = c[1] + ' (+' + c[2] + ')';   // full name: understood by everyone
                select.appendChild(option);
            });
        }
        addOptions(lists.priority);                        // Saudi Arabia first ...

        var divider = document.createElement('option');    // ... then a line ...
        divider.disabled = true;
        divider.textContent = '──────────────';
        select.appendChild(divider);

        addOptions(lists.others);                          // ... then every other country A-Z
        select.value = DEFAULT_COUNTRY;
    }

    function selectedDialCode() {
        var iso = el('booking-country') && el('booking-country').value;
        var match = allCountries().filter(function (c) { return c[0] === iso; })[0];
        return match ? match[2] : '';
    }

    // Digits only, without the leading 0 / 00 trunk prefix. Returns '' when the text
    // holds anything but digits and the usual separators.
    function nationalDigits(text) {
        var cleaned = String(text || '').replace(/[\s\-().]/g, '');
        if (!/^\d+$/.test(cleaned)) return '';
        return cleaned.replace(/^0+/, '');
    }

    // The single string sent to the backend, or null if the number isn't usable.
    function buildMobile() {
        var dial = selectedDialCode();
        var digits = nationalDigits(el('booking-mobile').value);
        if (!dial || digits.length < 5 || digits.length > 13) return null;
        return '+' + dial + digits;
    }

    // If someone pastes a full international number (+971 50 123 4567 or 00971...), pick the
    // matching country and leave just the local part in the field.
    function absorbInternationalPrefix() {
        var field = el('booking-mobile');
        var text = field.value.trim();
        var m = /^(?:\+|00)\s*([\d\s\-().]+)$/.exec(text);
        if (!m) return;

        var digits = m[1].replace(/[^\d]/g, '');
        var best = null;
        allCountries().forEach(function (c) {
            if (digits.indexOf(c[2]) === 0 && digits.length > c[2].length && (!best || c[2].length > best[2].length)) best = c;
        });
        if (!best) return;

        // Codes shared by several countries (+1, +7) keep the country already chosen when it matches.
        var current = allCountries().filter(function (c) { return c[0] === el('booking-country').value; })[0];
        el('booking-country').value = (current && current[2] === best[2]) ? current[0] : best[0];
        field.value = digits.slice(best[2].length);
    }

    function setFieldState(input, hint, state, message) {
        input.classList.toggle('is-valid', state === 'valid');
        input.classList.toggle('is-invalid', state === 'invalid');
        input.setAttribute('aria-invalid', state === 'invalid' ? 'true' : 'false');
        if (hint) {
            hint.textContent = message || '';
            hint.hidden = !message;
        }
    }

    // ---- Email: a tick once it looks valid -----------------------------------
    function initEmailField() {
        var input = el('booking-email');
        var tick = el('booking-email-tick');
        var hint = el('booking-email-hint');
        if (!input || !tick) return;

        function update(showProblem) {
            var value = input.value.trim();
            var ok = value !== '' && EMAIL_PATTERN.test(value);
            tick.hidden = !ok;
            if (ok) setFieldState(input, hint, 'valid', '');
            else if (value !== '' && showProblem) setFieldState(input, hint, 'invalid', 'Please enter a valid email address, like name@example.com.');
            else setFieldState(input, hint, 'none', '');
        }

        input.addEventListener('input', function () { update(false); });
        input.addEventListener('blur', function () { update(true); });
    }

    function initMobileField() {
        var input = el('booking-mobile');
        var select = el('booking-country');
        var hint = el('booking-mobile-hint');
        if (!input || !select) return;

        fillCountrySelect();

        function check(showProblem) {
            var value = input.value.trim();
            if (value === '') { setFieldState(input, hint, 'none', ''); select.classList.remove('is-invalid'); return; }
            if (buildMobile()) { setFieldState(input, hint, 'none', ''); select.classList.remove('is-invalid'); return; }
            if (showProblem) setFieldState(input, hint, 'invalid', 'Please check the number. Use digits only, without the country code (which you choose on the left).');
        }

        input.addEventListener('input', function () { absorbInternationalPrefix(); check(false); });
        input.addEventListener('blur', function () { absorbInternationalPrefix(); check(true); });
        select.addEventListener('change', function () { check(false); });
    }

    // Refuses to submit name/email/mobile unless both this page AND the
    // backend endpoint are HTTPS, so a hosting/config mistake can never
    // silently send personal details in the clear. localhost is exempted
    // so local preview (no TLS) still works during development.
    function isLocalHost(hostname) {
        return hostname === 'localhost' || hostname === '127.0.0.1';
    }

    function isConnectionSecure(apiBaseUrl) {
        var pageIsSecure = window.location.protocol === 'https:' || isLocalHost(window.location.hostname);
        if (!pageIsSecure) return false;

        try {
            var apiUrl = new URL(apiBaseUrl, window.location.href);
            return apiUrl.protocol === 'https:' || isLocalHost(apiUrl.hostname);
        } catch (e) {
            return false;
        }
    }

    function showFormError(message) {
        var errorMsg = el('booking-form-error');
        if (!errorMsg) return;
        errorMsg.textContent = message;
        show(errorMsg);
    }

    function appendConfirmationLink(container, url, label) {
        var a = document.createElement('a');
        a.className = 'booking-confirmation-link';
        a.href = url;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        a.textContent = label;
        container.appendChild(a);
    }

    // Shown when a FREE service was confirmed instantly by the backend.
    function showConfirmation(data) {
        var text = el('booking-confirmation-text');
        var links = el('booking-confirmation-links');

        if (text && selectedService && selectedSlot && selectedDateKey) {
            text.textContent = selectedService.name + ' — ' + formatDateLabel(selectedDateKey) + ', ' + selectedSlot.label
                + '. We’ve emailed you the details.';
        }

        if (links) {
            links.textContent = '';
            // Only ever link to https URLs the backend hands back.
            if (isHttpsUrl(data.meetLink)) appendConfirmationLink(links, data.meetLink, 'Join Google Meet');
            if (isHttpsUrl(data.manageUrl)) appendConfirmationLink(links, data.manageUrl, 'Manage my booking');
        }

        hide(el('booking-step-service'));
        hide(el('booking-step-slots'));
        hide(el('booking-step-details'));
        show(el('booking-confirmation'));
        scrollToNode(el('booking-confirmation'));
    }

    // ---- Coming back from the payment provider ---------------------------
    // The provider sends the customer to this page with ?bookingId=... . The
    // confirmation itself arrives separately (server-to-server), possibly a
    // few seconds AFTER the customer lands here, so ask the backend for the
    // booking's status and keep checking briefly until it settles.
    var RETURN_POLL_MS = 2000;
    var RETURN_MAX_POLLS = 20;
    // Optional support address shown in error messages (window.BOOKING_CONTACT_EMAIL); without it the messages say "contact us".
    var CONTACT_EMAIL = window.BOOKING_CONTACT_EMAIL || '';
    var CONTACT_US = CONTACT_EMAIL ? 'email ' + CONTACT_EMAIL : 'contact us';
    var CONTACT_US_CAP = CONTACT_US.charAt(0).toUpperCase() + CONTACT_US.slice(1);

    function paymentReturnId() {
        var match = /[?&]bookingId=([A-Za-z0-9]{10,40})(?:&|$)/.exec(window.location.search);
        return match ? match[1] : null;
    }

    function showPaymentResult(title, text, links) {
        el('booking-payment-result-title').textContent = title;
        el('booking-payment-result-text').textContent = text;

        var linkBox = el('booking-payment-result-links');
        linkBox.textContent = '';
        (links || []).forEach(function (l) { appendConfirmationLink(linkBox, l.url, l.label); });

        show(el('booking-payment-result'));
    }

    // Opens the booking page fresh (without the ?bookingId= that got us here).
    function bookAgainLink() {
        return { url: window.location.pathname, label: 'Book a session' };
    }

    function describeSettledStatus(info) {
        var when = (info.slotDateLabel && info.slotTimeLabel) ? info.slotDateLabel + ', ' + info.slotTimeLabel : '';
        var what = info.serviceName + (when ? ' — ' + when : '');

        if (info.status === 'confirmed') {
            showPaymentResult('You’re booked', what + '. We’ve emailed you the details.');
        } else if (info.status === 'conflict') {
            showPaymentResult('We couldn’t confirm your time',
                'Your payment reached us after that time had been taken by another booking, so it wasn’t confirmed. We’ll refund you in full and be in touch. Questions? ' + CONTACT_US_CAP + '.',
                [bookAgainLink()]);
        } else if (info.status === 'expired' || info.status === 'cancelled') {
            showPaymentResult('Payment wasn’t completed',
                'No booking was made and the time has been released. You can choose a time again.',
                [bookAgainLink()]);
        } else {
            showPaymentResult('We couldn’t check your booking',
                'If you paid, you’ll receive a confirmation email shortly. Otherwise you can start again. Questions? ' + CONTACT_US_CAP + '.',
                [bookAgainLink()]);
        }
    }

    function pollBookingStatus(bookingId, attempt) {
        fetch(API_BASE + '/bookingStatus?bookingId=' + encodeURIComponent(bookingId))
            .then(function (response) {
                if (!response.ok) throw new Error('Request failed');
                return response.json();
            })
            .then(function (info) {
                if (info.status !== 'pending') {
                    describeSettledStatus(info);
                    return;
                }
                if (attempt >= RETURN_MAX_POLLS) {
                    showPaymentResult('Still confirming your payment',
                        'Your payment is taking a little longer than usual. There’s nothing more you need to do — you’ll get a confirmation email as soon as it clears. If it hasn’t arrived within 15 minutes, ' + CONTACT_US + '.');
                    return;
                }
                window.setTimeout(function () { pollBookingStatus(bookingId, attempt + 1); }, RETURN_POLL_MS);
            })
            .catch(function () {
                describeSettledStatus({ status: 'unknown' });
            });
    }

    function handlePaymentReturn(bookingId) {
        hide(el('booking-step-service'));
        hide(el('booking-step-slots'));
        hide(el('booking-step-details'));
        showPaymentResult('Confirming your payment…', 'This usually takes a few seconds. Please keep this page open.');
        pollBookingStatus(bookingId, 0);
    }

    function submitBooking(event) {
        event.preventDefault();

        hide(el('booking-form-error'));

        if (!selectedService) {
            showFormError('Please choose a service first.');
            return;
        }
        if (!selectedSlot) {
            showFormError('Please choose a time first.');
            return;
        }

        var name = el('booking-name').value.trim();
        var email = el('booking-email').value.trim();
        var mobile = buildMobile();

        var validationError = validateDetails(name, email, mobile);
        if (validationError) {
            showFormError(validationError);
            return;
        }

        if (!API_BASE) {
            showFormError(selectedService.isFree
                ? 'Booking isn’t connected yet in preview mode.'
                : 'Online payment isn’t connected yet in preview mode.');
            return;
        }

        if (!isConnectionSecure(API_BASE)) {
            showFormError('This connection isn’t secure, so we can’t send your details. Please reload the page and try again, or contact us directly.');
            return;
        }

        var submitBtn = el('booking-submit-btn');
        var idleLabel = selectedService.isFree ? 'Confirm Booking' : 'Proceed to Payment';
        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.textContent = 'Please wait…';
        }

        fetch(API_BASE + '/bookingRequest', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                serviceId: selectedService.id,
                slotId: selectedSlot.id,
                date: selectedDateKey,
                name: name,
                email: email,
                mobile: mobile
            })
        })
            .then(function (response) {
                if (response.status === 409) {
                    var taken = new Error('Slot taken');
                    taken.slotTaken = true;
                    throw taken;
                }
                if (!response.ok) throw new Error('Request failed');
                return response.json();
            })
            .then(function (data) {
                if (data && data.confirmed) {
                    showConfirmation(data);
                    return;
                }
                if (!data || !data.checkoutUrl) throw new Error('Missing checkout URL');
                window.location.href = data.checkoutUrl;
            })
            .catch(function (err) {
                if (submitBtn) {
                    submitBtn.disabled = false;
                    submitBtn.textContent = idleLabel;
                }

                if (err && err.slotTaken) {
                    // Someone else got that time first. Send the customer back
                    // to the calendar with fresh availability rather than
                    // leaving them stuck on a form that can't succeed.
                    loadSlots();
                    var notice = el('booking-slots-notice');
                    if (notice) {
                        notice.textContent = 'That time was just taken by someone else. Please choose another.';
                        show(notice);
                    }
                    scrollToNode(el('booking-step-slots'));
                    return;
                }

                showFormError('Something went wrong reserving that slot. Please try again, or get in touch directly.');
            });
    }

    document.addEventListener('DOMContentLoaded', function () {
        var calendar = el('booking-calendar');
        var form = el('booking-details-form');
        var servicesGrid = el('booking-services-grid');

        if (!calendar || !form || !servicesGrid) return;

        var returnedBookingId = paymentReturnId();
        if (returnedBookingId && API_BASE) {
            handlePaymentReturn(returnedBookingId);
            return;
        }

        try {
            loadServices();
        } catch (e) {
            hide(el('booking-services-loading'));
            show(el('booking-services-error'));
        }

        var prevBtn = el('booking-cal-prev');
        var nextBtn = el('booking-cal-next');
        var changeSlotBtn = el('booking-change-slot-btn');
        var changeServiceBtn = el('booking-change-service-btn');

        if (prevBtn) {
            prevBtn.addEventListener('click', function () {
                goToMonth(-1);
            });
        }
        if (nextBtn) {
            nextBtn.addEventListener('click', function () {
                goToMonth(1);
            });
        }
        if (changeSlotBtn) {
            changeSlotBtn.addEventListener('click', function () {
                resetSlotSelection();
            });
        }
        if (changeServiceBtn) {
            changeServiceBtn.addEventListener('click', function () {
                resetService();
            });
        }

        initEmailField();
        initMobileField();

        form.addEventListener('submit', submitBooking);
    });
})();
