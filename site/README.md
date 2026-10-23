# Pages for the password-protected staging site

The landing page (`home/`) and owner sign-up page (`signup/`) are uploaded by hand to the staging site, which is
behind a password while the product is not launched. They are not served by the API's host; the customer pages in
`public/` are.

## Upload layout

The pages sit next to the owner dashboard, which is the staging site's `index.html`:

```text
index.html            owner dashboard (not in this repository)
home/index.html       from site/home/
signup/index.html     from site/signup/
assets/               public/assets/gn.css and public/assets/fonts/, plus everything in site/assets/
assets/config.js      made from site/assets/config.example.js with the real values
```

## How sign-up works

The sign-up page creates the login with the identity provider and asks it to send the confirmation email. The link in
that email returns to the dashboard at the site root, which finishes setting up the business from the details saved
with the login. "Sign in" links go to the dashboard with `#signin`.

`config.js` holds only the identity project URL and its publishable key, which are public by design. It is not
committed. Never put a secret or service-role key in it.
