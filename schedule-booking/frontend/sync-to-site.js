// Copies the booking module's customer-facing files into a website folder.
//
//   node frontend/sync-to-site.js <site folder>                 scripts + stylesheet only
//   node frontend/sync-to-site.js <site folder> --with-pages    also the page templates, into <site>/pages
//                                                               (never overwrites an existing page)
//   node frontend/sync-to-site.js <site folder> --with-pages --force   ... and overwrite existing pages
//
// This folder is the source of truth. The website gets COPIES; change the module
// here, then run this again, rather than editing the copies on the site.

const fs = require('fs');
const path = require('path');

const args = process.argv.slice(2);
const target = args.find(function (a) { return a.indexOf('--') !== 0; });
const withPages = args.indexOf('--with-pages') >= 0;
const force = args.indexOf('--force') >= 0;

if (!target || !fs.existsSync(target) || !fs.statSync(target).isDirectory()) {
    console.error('Usage: node frontend/sync-to-site.js <existing website folder> [--with-pages] [--force]');
    process.exit(1);
}

const here = __dirname;
const site = path.resolve(target);
let copied = 0;

function copy(from, to, overwrite) {
    if (fs.existsSync(to) && !overwrite) { console.log('  kept (already exists): ' + path.relative(site, to)); return; }
    fs.mkdirSync(path.dirname(to), { recursive: true });
    fs.copyFileSync(from, to);
    copied++;
    console.log('  copied: ' + path.relative(site, to));
}

console.log('Syncing the booking module into ' + site);
fs.readdirSync(path.join(here, 'js')).forEach(function (f) { copy(path.join(here, 'js', f), path.join(site, 'js', f), true); });
copy(path.join(here, 'css', 'booking.css'), path.join(site, 'css', 'booking.css'), true);

if (withPages) {
    fs.readdirSync(path.join(here, 'templates')).forEach(function (f) { copy(path.join(here, 'templates', f), path.join(site, 'pages', f), force); });
}

console.log('Done: ' + copied + ' file(s) copied.');
if (!withPages) console.log('Page templates were not copied (add --with-pages to include them).');
