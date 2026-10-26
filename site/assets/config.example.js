// Copy this file to config.js next to it, with the real values, when preparing an upload.
// config.js is not committed. Every value here is public by design: the identity project URL, its publishable key
// (which grants no data access on its own) and two web addresses. Never put a secret or service-role key here.

export const AUTH_URL = "https://your-project-id.supabase.co/auth/v1";
export const AUTH_KEY = "sb_publishable_your-key";

/** The platform API, without a trailing slash. */
export const API_URL = "https://api.example.com";

/** Where customer booking pages are served, without a trailing slash. */
export const BOOKING_URL = "https://book.example.com";
