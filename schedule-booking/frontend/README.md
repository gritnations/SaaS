# Booking module: customer-facing frontend

The pages a customer sees: choose a service, pick a date and time, enter details, pay (or confirm a free
booking), and later cancel or reschedule from the link in their email. Plain HTML, CSS and JavaScript, no build
step, and no dependency on any framework. It talks to the backend in `../functions`.

```
frontend/
  js/booking.js            the booking flow, and the screen a customer returns to after paying
  js/manage-booking.js     cancel / reschedule from the emailed link
  js/country-codes.js      calling codes for the mobile field
  css/booking.css          all styles, themed with CSS variables
  templates/               starting-point pages (booking, manage)
  sync-to-site.js          copies the above into a website folder
```

## Using it on a site

1. Deploy the backend (`../README.md`) and note its base URL (`https://<region>-<project>.cloudfunctions.net`).
2. Copy the files into the site: `node frontend/sync-to-site.js <site folder> --with-pages`. Scripts and the stylesheet are
   always refreshed; the page templates are only added if the pages don't exist yet.
3. Put the booking block into the site's own layout (header, footer, fonts). The templates are plain pages and can be used as they are.
4. Set the values in the small `<script>` at the bottom of each page:

| Setting | What it does |
| --- | --- |
| `window.BOOKING_API_BASE` | The backend base URL. Required |
| `window.BOOKING_CONTACT_EMAIL` | Optional. Shown in error messages; without it they say "contact us" |
| `window.BOOKING_DEFAULT_COUNTRY` | Optional. Preselected phone country, as an ISO code (default `SA`) |
| `window.BOOKING_PAGE_URL` | Optional, manage page only. Where its "Book a session" links point |

5. In the backend (`functions/lib/config.js`) set `PUBLIC_SITE_ORIGIN` to the site, add it to `ALLOWED_ORIGINS`, and make sure
   the manage page is served at `/pages/manage-booking.html`: the confirmation email links there.

## Look and feel

Colours are CSS variables with defaults; override them once in the site's stylesheet:

```css
:root { --booking-primary: #0B3D91; --booking-accent: #F2A900; }
```

Available: `--booking-primary`, `--booking-accent`, `--booking-text`, `--booking-border`, `--booking-surface`,
`--booking-link`, `--booking-success`, `--booking-error`. Fonts are inherited from the site. Styles are scoped to
booking classes and never restyle the host site.

## Rules to keep

- The scripts find their elements by `id`, so keep the ids in the markup when adapting a template.
- All text is inserted with `textContent`, never as HTML. Keep it that way.
- This folder is the source of truth. Fix things here and re-run `sync-to-site.js`, rather than editing the copies on a site.
- The scripts are tested by running them in the local demo (`../demo/start-demo.bat`), which serves these files at
  `/module/pages/book-a-session.html` and `/module/pages/manage-booking.html`.
