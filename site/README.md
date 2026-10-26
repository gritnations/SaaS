# Pages for the password-protected staging site

These pages are uploaded by hand to the staging site, which is behind a password while the product is not
launched. They are not served by the API's host; the customer pages in `public/` are.

## Pages

| Path            | Page                                                                      |
| --------------- | ------------------------------------------------------------------------- |
| `index.html`    | The site root: no page of its own, it forwards each kind of link (below). |
| `home/`         | Landing page.                                                             |
| `signup/`       | Owner sign-up.                                                            |
| `signin/`       | Owner sign-in, forgotten password and new password.                       |
| `app/`          | Owner Today screen.                                                       |
| `app/bookings/` | Bookings: by day, look-up by reference, details, cancel.                  |
| `app/services/` | Services: edit and add.                                                   |
| `app/hours/`    | Opening hours: weekly editor.                                             |
| `app/settings/` | Business details, customer contact details and booking rules.             |
| `app/payments/` | Stripe connection and how customers pay.                                  |
| `app/plan/`     | Plan, upgrades and the subscription portal.                               |
| `app/setup/`    | Creates the business after a new owner confirms their email.              |

## Upload layout

Upload the contents of `site/` (without this README) to the staging site's root, plus:

```text
assets/gn.css, assets/gn.js, assets/fonts/   from public/assets/
assets/config.js                              made from site/assets/config.example.js with the real values
```

Keep the staging site's own `.htaccess` (password protection) and `.htpasswd`.

### Stamp a version before uploading

Browsers keep their own copy of scripts and stylesheets. Without a new address for each upload, a browser can run an
old script against a new page and stop with a half-drawn screen. Stamp a version onto the upload folder, never onto
the files in the repository:

```bash
node site/stamp-version.mjs <upload-folder> <version>   # for example the short commit id: 1afb199
```

It adds `?v=<version>` to every script and stylesheet link in the pages and to every import between scripts, and
replaces an earlier version if there is one. Use a new version for every upload.

## How the site root forwards links

| Arrives with                                          | Goes to                                     |
| ----------------------------------------------------- | ------------------------------------------- |
| `#access_token=…&type=signup` (confirmation email)    | `/app/` (then `/app/setup/` if no business) |
| `#access_token=…&type=recovery` (reset email)         | `/signin/` new-password step                |
| `#error=…` (expired email link)                       | `/signin/?link=expired`                     |
| `#billing=…` (Stripe plan checkout or portal)         | `/app/plan/`                                |
| `#payments=…` (Stripe account set-up)                 | `/app/payments/`                            |
| `?b=<business>` or `#manage=…` (older customer links) | The customer pages on the booking host      |
| Nothing                                               | `/app/` when signed in, otherwise `/home/`  |

## Sessions

Sign-in keeps the owner's session for the browser tab (`sessionStorage`) and every owner page reads it there. An
expired session is renewed once with the refresh token; otherwise the page returns to sign-in.

`config.js` holds only the identity project URL and its publishable key, which are public by design, and two web
addresses. It is not committed. Never put a secret or service-role key in it.
