// The slot grid for a session length: every time in the booking window, with
// booked ones marked. Shared by the public availableSlots endpoint and by the
// reschedule page (which passes excludeBookingId so a booking's own current
// time doesn't block the times next to it).

const {
    buildSlotsByDate, bookingWindowStart, dateKeyFromDate
} = require('./availability');
const { BOOKING_WINDOW_DAYS } = require('./config');
const { getRulesByDay, getBlockedDateKeys, getGeneralSettings } = require('./settings');
const { loadBookedIntervalsByDate } = require('./bookings');

async function computeSlotsByDate(db, options) {
    const durationMinutes = options.durationMinutes;
    const now = options.now || new Date();

    const startDate = bookingWindowStart(now);
    const endDate = new Date(startDate.getTime());
    endDate.setDate(endDate.getDate() + BOOKING_WINDOW_DAYS - 1);

    const [rulesByDay, blockedDateKeys, bookedByDate, settings] = await Promise.all([
        getRulesByDay(db),
        getBlockedDateKeys(db),
        loadBookedIntervalsByDate(db, dateKeyFromDate(startDate), dateKeyFromDate(endDate), options.excludeBookingId),
        getGeneralSettings(db)
    ]);

    return buildSlotsByDate({
        startDate,
        numDays: BOOKING_WINDOW_DAYS,
        durationMinutes,
        rulesByDay,
        blockedDateKeys,
        bookedByDate,
        breakMinutes: settings.breakMinutes
    });
}

module.exports = { computeSlotsByDate };
