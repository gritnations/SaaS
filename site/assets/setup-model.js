// The rules behind the Today screen's set-up box. A new business starts with a default service and default opening
// hours, so having them says nothing about what the owner has done. A step counts as done when the owner has changed
// the defaults, or has opened that screen in this browser.

/** The one service sign-up creates. Kept in step with the backend by a test. */
export const DEFAULT_SERVICE = { slug: "intro", name: "Introductory call", duration_minutes: 30, price_minor: 0 };

/** The days sign-up opens: Monday to Friday, where 0 is Sunday. Kept in step with the backend by a test. */
export const DEFAULT_DAYS = [1, 2, 3, 4, 5];

/** The opening and closing time sign-up gives each of those days. */
export const DEFAULT_START = "09:00";
export const DEFAULT_END = "17:00";

/**
 * Whether the services differ from what sign-up created.
 * @param {object[]} services The owner's services.
 * @returns {boolean} True when anything was added or edited.
 */
export function servicesChanged(services) {
  if (services.length !== 1) return true;
  const [only] = services;
  return only.active !== true || Object.entries(DEFAULT_SERVICE).some(([key, value]) => only[key] !== value);
}

/**
 * Whether the opening hours differ from what sign-up created.
 * @param {{weekday: number, start: string, end: string}[]} blocks The owner's working blocks.
 * @returns {boolean} True when any day or time was changed.
 */
export function hoursChanged(blocks) {
  if (blocks.length !== DEFAULT_DAYS.length) return true;
  return DEFAULT_DAYS.some(
    (day) =>
      !blocks.some(
        (b) => b.weekday === day && b.start.slice(0, 5) === DEFAULT_START && b.end.slice(0, 5) === DEFAULT_END,
      ),
  );
}

/**
 * Whether the services step is done. There must be a service customers can book.
 * @param {object[]|null} services The owner's services, or null if they could not be loaded.
 * @param {boolean} seen Whether the owner has opened the Services screen.
 * @returns {boolean} Whether to tick the step.
 */
export function servicesDone(services, seen) {
  if (!Array.isArray(services) || !services.some((s) => s.active)) return false;
  return seen || servicesChanged(services);
}

/**
 * Whether the opening hours step is done. At least one day must be open.
 * @param {{blocks: object[]}|null} hours The owner's hours, or null if they could not be loaded.
 * @param {boolean} seen Whether the owner has opened the Opening hours screen.
 * @returns {boolean} Whether to tick the step.
 */
export function hoursDone(hours, seen) {
  if (!hours || !Array.isArray(hours.blocks) || !hours.blocks.length) return false;
  return seen || hoursChanged(hours.blocks);
}

/**
 * The browser-storage key of a set-up flag.
 * @param {"seen_services"|"seen_hours"|"setup_hidden"} name Which flag.
 * @param {string} slug The business slug.
 * @returns {string} The key.
 */
const flagKey = (name, slug) => `gn_${name}_${slug}`;

/**
 * Read a set-up flag.
 * @param {"seen_services"|"seen_hours"|"setup_hidden"} name Which flag.
 * @param {string} slug The business slug.
 * @returns {boolean} Whether it is set. False when storage is unavailable.
 */
export function readFlag(name, slug) {
  try {
    return localStorage.getItem(flagKey(name, slug)) === "1";
  } catch {
    return false;
  }
}

/**
 * Set or clear a set-up flag.
 * @param {"seen_services"|"seen_hours"|"setup_hidden"} name Which flag.
 * @param {string} slug The business slug.
 * @param {boolean} [on] False to clear it.
 */
export function writeFlag(name, slug, on = true) {
  try {
    if (on) localStorage.setItem(flagKey(name, slug), "1");
    else localStorage.removeItem(flagKey(name, slug));
  } catch {
    // Storage unavailable: the flag lasts no longer than the page.
  }
}
