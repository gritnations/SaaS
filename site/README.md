# Pages for the password-protected staging site

The landing page (`home/`), owner sign-up (`signup/`), owner sign-in (`signin/`) and the owner's Today screen (`app/`)
are uploaded by hand to the staging site, which is behind a password while the product is not launched. They are not served by the API's host; the customer pages in
`public/` are.

## Upload layout

The pages sit next to the owner dashboard, which is the staging site's `index.html`:

```text
index.html            owner dashboard (not in this repository)
home/index.html       from site/home/
signup/index.html     from site/signup/
signin/index.html     from site/signin/
app/index.html        from site/app/
assets/               public/assets/gn.css, gn.js and fonts/, plus everything in site/assets/
assets/config.js      made from site/assets/config.example.js with the real values
```

## How sign-up works

The sign-up page creates the login with the identity provider and asks it to send the confirmation email. The link in
that email returns to the dashboard at the site root, which finishes setting up the business from the details saved
with the login.

The sign-in page signs the owner in, keeps the session for that browser tab (where the existing dashboard screens also
read it) and opens the Today screen. The Today screen's menu opens the existing dashboard screens with `#go=today`,
`#go=services` or `#go=payments` until those screens are redesigned. "Forgot password" sends a reset link back to the
sign-in page, which then asks for the new password. For that link to return there rather than to the dashboard, the
sign-in page's address must be on the identity provider's list of allowed redirect addresses.

`config.js` holds only the identity project URL and its publishable key, which are public by design. It is not
committed. Never put a secret or service-role key in it.
