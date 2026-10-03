// Schedule tab: when customers can book. Three independent cards —
// weekly working hours, blocked dates, and the Google Meet link.

import { h, clear, toast, withBusy, describeError } from '../ui.js';
import {
    loadRules, replaceRules, loadBlockedDates, blockDate, unblockDate, countActiveBookingsOn,
    loadMeetLink, saveMeetLink, loadBreakMinutes, saveBreakMinutes
} from '../data.js';
import { validateMeetLink, validateDateKey, validateBreakMinutes } from '../lib/validate.js';
import * as sched from '../lib/schedule.js';
import * as fmt from '../lib/format.js';

// ---------------------------------------------------------------- weekly hours

function weeklyHoursCard() {
    let week = sched.emptyWeek();
    let existingIds = [];
    let loading = true;
    let failed = false;

    const problems = h('div');
    const body = h('div');
    const saveBtn = h('button', { type: 'button', class: 'btn' }, 'Save weekly hours');

    async function load() {
        loading = true;
        failed = false;
        paint();
        try {
            const rules = await loadRules();
            existingIds = rules.map(function (r) { return r.id; });
            week = sched.rulesToWeek(rules);
        } catch (err) {
            failed = true;
            toast(describeError(err), 'error');
        }
        loading = false;
        paint();
    }

    function dayRow(day) {
        const blocks = week[day];
        const open = h('input', { type: 'checkbox', id: 'open-' + day, checked: blocks.length > 0 });
        open.addEventListener('change', function () {
            week[day] = open.checked ? [{ start: '09:00', end: '17:00' }] : [];
            paint();
        });

        const blockEls = blocks.map(function (block, i) {
            const start = h('input', { type: 'time', 'aria-label': fmt.WEEKDAY_NAMES[day] + ' block ' + (i + 1) + ' start', value: block.start });
            const end = h('input', { type: 'time', 'aria-label': fmt.WEEKDAY_NAMES[day] + ' block ' + (i + 1) + ' end', value: block.end });
            start.addEventListener('input', function () { block.start = start.value; });
            end.addEventListener('input', function () { block.end = end.value; });

            return h('div', { class: 'block' },
                start, ' to ', end,
                h('button', {
                    type: 'button', class: 'btn btn-ghost btn-small', 'aria-label': 'Remove these hours',
                    onClick: function () { week[day].splice(i, 1); paint(); }
                }, 'Remove')
            );
        });

        const addBtn = blocks.length > 0
            ? h('button', {
                type: 'button', class: 'btn btn-ghost btn-small',
                onClick: function () { week[day].push(sched.nextBlock(week[day])); paint(); }
            }, '+ Add hours (e.g. after a break)')
            : null;

        return h('div', { class: 'day-row' },
            h('label', { class: 'check', for: 'open-' + day }, open, h('strong', null, fmt.WEEKDAY_NAMES[day])),
            h('div', { class: 'day-blocks' },
                blocks.length === 0 ? h('span', { class: 'day-closed' }, 'Closed — no bookings') : null,
                blockEls, addBtn
            )
        );
    }

    function paint() {
        clear(body);
        clear(problems);

        if (loading) {
            body.append(h('p', { class: 'muted' }, 'Loading…'));
            saveBtn.disabled = true;
            return;
        }
        if (failed) {
            body.append(h('div', { class: 'banner banner-bad' }, 'Couldn’t load the hours. ', h('button', { type: 'button', class: 'btn btn-secondary btn-small', onClick: load }, 'Try again')));
            saveBtn.disabled = true;
            return;
        }

        saveBtn.disabled = false;
        for (let day = 0; day < 7; day++) body.append(dayRow(day));
    }

    saveBtn.addEventListener('click', async function () {
        clear(problems);
        const found = sched.validateWeek(week, fmt.WEEKDAY_NAMES);
        if (found.length > 0) {
            problems.append(h('div', { class: 'banner banner-bad', role: 'alert' }, h('ul', null, found.map(function (p) { return h('li', null, p); }))));
            return;
        }
        try {
            await withBusy(saveBtn, 'Saving…', function () { return replaceRules(existingIds, sched.weekToRules(week)); });
            toast('Weekly hours saved.');
            load();
        } catch (err) {
            toast(describeError(err), 'error');
        }
    });

    load();

    return h('div', { class: 'card' },
        h('h2', null, 'Weekly hours'),
        h('p', { class: 'muted' }, 'The times customers can book, for each day of the week. Add a second block of hours for a break in the middle of the day. Changes apply to the booking page straight away; bookings that already exist are not affected.'),
        body, problems,
        h('div', { class: 'actions' }, saveBtn)
    );
}

// ---------------------------------------------------------------- blocked dates

function blockedDatesCard() {
    let blocked = [];
    let loading = true;

    const list = h('div');
    const dateInput = h('input', { type: 'date', id: 'block-date' });
    const reasonInput = h('input', { type: 'text', id: 'block-reason', maxlength: '200', placeholder: 'Optional — e.g. Public holiday' });
    const error = h('p', { class: 'field-error', hidden: true });
    const addBtn = h('button', { type: 'button', class: 'btn' }, 'Block this date');

    const todayKey = fmt.dateKeyFromDate(new Date());
    dateInput.min = todayKey;

    async function load() {
        loading = true;
        paint();
        try {
            blocked = await loadBlockedDates();
        } catch (err) {
            toast(describeError(err), 'error');
        }
        loading = false;
        paint();
    }

    function paint() {
        clear(list);
        if (loading) {
            list.append(h('p', { class: 'muted' }, 'Loading…'));
            return;
        }

        // Past dates have no effect any more, so they're left out of view.
        const upcoming = blocked.filter(function (b) { return b.dateKey >= todayKey; });
        if (upcoming.length === 0) {
            list.append(h('p', { class: 'muted' }, 'No dates are blocked.'));
            return;
        }

        upcoming.forEach(function (b) {
            const removeBtn = h('button', { type: 'button', class: 'btn btn-ghost btn-small' }, 'Unblock');
            removeBtn.addEventListener('click', async function () {
                try {
                    await withBusy(removeBtn, 'Removing…', function () { return unblockDate(b.dateKey); });
                    toast('Date unblocked.');
                    load();
                } catch (err) {
                    toast(describeError(err), 'error');
                }
            });
            list.append(h('div', { class: 'list-row' },
                h('span', null, h('strong', null, fmt.formatDateLabel(b.dateKey)), b.reason ? h('span', { class: 'muted' }, ' — ' + b.reason) : null),
                removeBtn
            ));
        });
    }

    addBtn.addEventListener('click', async function () {
        error.hidden = true;
        const key = dateInput.value;

        if (!validateDateKey(key)) {
            error.textContent = 'Pick a date to block.';
            error.hidden = false;
            return;
        }
        if (key < todayKey) {
            error.textContent = 'That date has already passed.';
            error.hidden = false;
            return;
        }

        try {
            // Blocking stops NEW bookings only. If people have already booked
            // that day, say so before going ahead.
            const existing = await countActiveBookingsOn(key);
            if (existing > 0) {
                const ok = window.confirm(existing + (existing === 1 ? ' booking already exists' : ' bookings already exist') + ' on ' + fmt.formatDateLabel(key) + '.\n\nBlocking the date stops new bookings but does NOT cancel the existing one' + (existing === 1 ? '' : 's') + '. Block it anyway?');
                if (!ok) return;
            }
            await withBusy(addBtn, 'Blocking…', function () { return blockDate(key, reasonInput.value.trim()); });
            toast('Date blocked.');
            dateInput.value = '';
            reasonInput.value = '';
            load();
        } catch (err) {
            toast(describeError(err), 'error');
        }
    });

    load();

    return h('div', { class: 'card' },
        h('h2', null, 'Blocked dates'),
        h('p', { class: 'muted' }, 'Days off, holidays or time away. Customers can’t book a blocked date. Existing bookings on it are not cancelled.'),
        list,
        h('div', { class: 'form-row' },
            h('div', { class: 'field' }, h('label', { for: 'block-date' }, 'Date'), dateInput),
            h('div', { class: 'field' }, h('label', { for: 'block-reason' }, 'Reason'), reasonInput)
        ),
        error,
        h('div', { class: 'actions' }, addBtn)
    );
}

// ---------------------------------------------------------------- break between sessions

function breakCard() {
    const input = h('input', { type: 'number', id: 'break-minutes', min: '0', max: '120', step: '5', inputmode: 'numeric', value: '0' });
    const error = h('p', { class: 'field-error', hidden: true });
    const example = h('p', { class: 'field-hint' });
    const saveBtn = h('button', { type: 'button', class: 'btn' }, 'Save break');
    input.disabled = true;
    saveBtn.disabled = true;

    // A live example so the effect is obvious before saving.
    function paintExample() {
        const result = validateBreakMinutes(input.value);
        const gap = result.ok ? result.value : 0;
        example.textContent = gap === 0
            ? 'No break: sessions are offered back to back.'
            : 'Example: 45 minute sessions would be offered ' + fmt.formatTimeRange(9 * 60, 45) + ', then ' + fmt.formatTimeRange(9 * 60 + 45 + gap, 45) + ', and so on.';
    }
    input.addEventListener('input', paintExample);

    (async function () {
        try {
            input.value = String(await loadBreakMinutes());
            input.disabled = false;
            saveBtn.disabled = false;
            paintExample();
        } catch (err) {
            toast(describeError(err), 'error');
        }
    })();

    saveBtn.addEventListener('click', async function () {
        error.hidden = true;
        const result = validateBreakMinutes(input.value);
        if (!result.ok) {
            error.textContent = result.error;
            error.hidden = false;
            return;
        }
        try {
            await withBusy(saveBtn, 'Saving…', function () { return saveBreakMinutes(result.value); });
            toast(result.value === 0 ? 'Break removed — sessions are offered back to back.' : 'Break saved: ' + result.value + ' minutes between sessions.');
        } catch (err) {
            toast(describeError(err), 'error');
        }
    });

    return h('div', { class: 'card' },
        h('h2', null, 'Break between sessions'),
        h('p', { class: 'muted' }, 'Time kept free before and after every session, so you can reset between clients. Customers don’t see it and aren’t charged for it — they book and pay for the session length only. It applies to new bookings; existing ones don’t move.'),
        h('div', { class: 'field' }, h('label', { for: 'break-minutes' }, 'Break (minutes)'), input, example, error),
        h('div', { class: 'actions' }, saveBtn)
    );
}

// ---------------------------------------------------------------- meet link

function meetLinkCard() {
    const input = h('input', { type: 'url', id: 'meet-link', placeholder: 'https://meet.google.com/abc-defg-hij', autocomplete: 'off' });
    const error = h('p', { class: 'field-error', hidden: true });
    const saveBtn = h('button', { type: 'button', class: 'btn' }, 'Save link');
    input.disabled = true;
    saveBtn.disabled = true;

    (async function () {
        try {
            input.value = await loadMeetLink();
            input.disabled = false;
            saveBtn.disabled = false;
        } catch (err) {
            toast(describeError(err), 'error');
        }
    })();

    saveBtn.addEventListener('click', async function () {
        error.hidden = true;
        const result = validateMeetLink(input.value);
        if (!result.ok) {
            error.textContent = result.error;
            error.hidden = false;
            return;
        }
        try {
            await withBusy(saveBtn, 'Saving…', function () { return saveMeetLink(result.value); });
            input.value = result.value;
            toast(result.value ? 'Meet link saved.' : 'Meet link removed from confirmation emails.');
        } catch (err) {
            toast(describeError(err), 'error');
        }
    });

    return h('div', { class: 'card' },
        h('h2', null, 'Google Meet link'),
        h('p', { class: 'muted' }, 'Your permanent Meet room. It’s included in every booking confirmation email as a “Join Google Meet” button. Leave it empty to leave the button out.'),
        h('div', { class: 'field' }, h('label', { for: 'meet-link' }, 'Meet link'), input, error),
        h('div', { class: 'actions' }, saveBtn)
    );
}

export function renderSchedule(root) {
    clear(root);
    root.append(weeklyHoursCard(), breakCard(), blockedDatesCard(), meetLinkCard());
}
