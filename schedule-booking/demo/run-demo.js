// One-click local demo of the whole booking system — nothing here touches the
// live project, the internet, or any real account.
//
//   node demo/run-demo.js          start everything and open the browser
//   node demo/run-demo.js --stop   stop anything left running and clean up
//   (or just double-click start-demo.bat / stop-demo.bat)
//
// It starts the Firebase emulators (Cloud Functions, Firestore, Pub/Sub, Auth),
// fills them with demo data (dates are relative to today), and serves:
//   the customer booking pages  http://localhost:8899   (+ fake checkout and mailbox)
//   the admin panel             http://127.0.0.1:5200
// The site and admin are COPIED into demo/.build and pointed at the emulators,
// so the real files are never edited.

const fs = require('fs');
const path = require('path');
const http = require('http');
const net = require('net');
const { spawn, spawnSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');                       // booking-backend
const SITE_SRC = path.resolve(ROOT, '..', 'Website-online-booking');
const BUILD = path.join(__dirname, '.build');
const JRE_HOME = path.join(__dirname, '.tools', 'jre');
const SECRET_FILE = path.join(ROOT, 'functions', '.secret.local');
const IS_WIN = process.platform === 'win32';

const PROJECT = 'demo-booking';
const PORTS = { site: 8899, admin: 5200, functions: 5101, firestore: 8180, pubsub: 8185, auth: 9199 };
const API = 'http://127.0.0.1:' + PORTS.functions + '/' + PROJECT + '/us-central1';
const ADMIN_EMAIL = 'owner@example.com';
const ADMIN_PASSWORD = 'demo-password-1';

const log = function (msg) { console.log(msg); };

// ---------------------------------------------------------------- helpers

function portInUse(port) {
    return new Promise(function (resolve) {
        const s = net.connect({ port: port, host: '127.0.0.1' });
        s.once('connect', function () { s.destroy(); resolve(true); });
        s.once('error', function () { resolve(false); });
    });
}

async function waitFor(check, ms, label) {
    const end = Date.now() + ms;
    while (Date.now() < end) {
        if (await check()) return;
        await new Promise(function (r) { setTimeout(r, 400); });
    }
    throw new Error('Timed out waiting for ' + label + '.');
}

function pidsOnPort(port) {
    if (!IS_WIN) return [];
    const out = spawnSync('netstat', ['-ano'], { encoding: 'utf8' }).stdout || '';
    const pids = new Set();
    out.split(/\r?\n/).forEach(function (line) {
        if (line.indexOf('LISTENING') < 0) return;
        const parts = line.trim().split(/\s+/);
        if (/:(\d+)$/.exec(parts[1] || '') && parts[1].split(':').pop() === String(port)) pids.add(parts[parts.length - 1]);
    });
    return Array.from(pids);
}

function killTree(pid) {
    if (IS_WIN) spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' });
    else { try { process.kill(-pid); } catch (e) { /* already gone */ } }
}

function stopEverything() {
    // Stops whatever holds the demo's ports (including a previous run that was closed abruptly).
    Object.keys(PORTS).forEach(function (k) { pidsOnPort(PORTS[k]).forEach(killTree); });
    [4400, 4500, 9150].forEach(function (p) { pidsOnPort(p).forEach(killTree); });
    try { fs.rmSync(SECRET_FILE, { force: true }); } catch (e) { /* ignore */ }
}

function findJavaDir() {
    const bundled = path.join(JRE_HOME, 'bin', IS_WIN ? 'java.exe' : 'java');
    if (fs.existsSync(bundled)) return path.join(JRE_HOME, 'bin');
    const probe = spawnSync('java', ['-version'], { stdio: 'ignore', shell: true });
    if (probe.status === 0) return null;                            // Java is on PATH
    throw new Error('Java is needed for the Firestore emulator. Put a Java 21 runtime in demo/.tools/jre (so that demo/.tools/jre/bin/java exists), or install Java 21 and add it to PATH.');
}

// ---------------------------------------------------------------- build the demo copies

function copyInto(src, dst) {
    fs.cpSync(src, dst, { recursive: true, filter: function (p) { return !/node_modules/.test(p) && !/[\\/]test[\\/]?$/.test(p); } });
}

function replaceOnce(text, pattern, replacement, what) {
    if (!pattern.test(text)) throw new Error('Could not adapt ' + what + ' for the demo (its format changed).');
    return text.replace(pattern, replacement);
}

function buildSite() {
    if (!fs.existsSync(SITE_SRC)) throw new Error('Website folder not found: ' + SITE_SRC);
    const dst = path.join(BUILD, 'site');
    ['assets', 'css', 'js', 'pages'].forEach(function (d) { copyInto(path.join(SITE_SRC, d), path.join(dst, d)); });
    ['index.html', 'favicon.ico'].forEach(function (f) { if (fs.existsSync(path.join(SITE_SRC, f))) fs.copyFileSync(path.join(SITE_SRC, f), path.join(dst, f)); });
    fs.mkdirSync(path.join(dst, 'demo'), { recursive: true });      // fake checkout + mailbox pages
    ['checkout.html', 'mailbox.html'].forEach(function (f) { fs.copyFileSync(path.join(__dirname, f), path.join(dst, 'demo', f)); });

    ['book-a-session.html', 'manage-booking.html'].forEach(function (name) {
        const p = path.join(dst, 'pages', name);
        const html = fs.readFileSync(p, 'utf8');
        fs.writeFileSync(p, replaceOnce(html, /window\.ZA_BOOKING_API_BASE = '[^']*';/, "window.ZA_BOOKING_API_BASE = '" + API + "';", name));
    });
}

// The site's booking pages run the module's scripts (frontend/js), so what is demonstrated is
// exactly what the module ships. The module's templates are also served on their own,
// with the module's stylesheet, at /module/pages/... to prove they work without any site around them.
function buildModule() {
    const frontend = path.join(ROOT, 'frontend');
    if (!fs.existsSync(frontend)) return;
    const site = path.join(BUILD, 'site');

    fs.readdirSync(path.join(frontend, 'js')).forEach(function (f) { fs.copyFileSync(path.join(frontend, 'js', f), path.join(site, 'js', f)); });

    const dst = path.join(site, 'module');
    copyInto(path.join(frontend, 'js'), path.join(dst, 'js'));
    copyInto(path.join(frontend, 'css'), path.join(dst, 'css'));
    copyInto(path.join(frontend, 'templates'), path.join(dst, 'pages'));
    fs.readdirSync(path.join(dst, 'pages')).forEach(function (name) {
        const p = path.join(dst, 'pages', name);
        const html = fs.readFileSync(p, 'utf8');
        fs.writeFileSync(p, replaceOnce(html, /window\.BOOKING_API_BASE = '[^']*';/, "window.BOOKING_API_BASE = '" + API + "';", name));
    });
}

function buildAdmin() {
    const dst = path.join(BUILD, 'admin');
    copyInto(path.join(ROOT, 'admin'), dst);
    const p = path.join(dst, 'js', 'config.js');
    let cfg = fs.readFileSync(p, 'utf8');
    cfg = replaceOnce(cfg, /apiKey: '[^']*'/, "apiKey: 'fake-demo-key'", 'admin config (apiKey)');
    cfg = replaceOnce(cfg, /authDomain: '[^']*'/, "authDomain: '" + PROJECT + ".firebaseapp.com'", 'admin config (authDomain)');
    cfg = replaceOnce(cfg, /projectId: '[^']*'/, "projectId: '" + PROJECT + "'", 'admin config (projectId)');
    cfg = replaceOnce(cfg, /export const functionsBase = '[^']*';/, "export const functionsBase = '" + API + "';", 'admin config (functionsBase)');
    cfg = replaceOnce(cfg, /export const emulator = [^;]*;/, "export const emulator = { authUrl: 'http://127.0.0.1:" + PORTS.auth + "', firestoreHost: '127.0.0.1', firestorePort: " + PORTS.firestore + ' };', 'admin config (emulator)');
    fs.writeFileSync(p, cfg);
}

// The admin panel is served with the same security headers Firebase Hosting would
// send (from firebase.json), widened only so it may talk to the local emulators.
function adminHeaders() {
    const config = JSON.parse(fs.readFileSync(path.join(ROOT, 'firebase.json'), 'utf8'));
    const local = 'http://127.0.0.1:' + PORTS.auth + ' http://127.0.0.1:' + PORTS.firestore + ' http://127.0.0.1:' + PORTS.functions;
    return config.hosting.headers.map(function (rule) {
        return {
            source: rule.source,
            headers: rule.headers.map(function (h) {
                return h.key === 'Content-Security-Policy'
                    ? { key: h.key, value: h.value.replace(/connect-src [^;]*/, function (m) { return m + ' ' + local; }) }
                    : h;
            })
        };
    });
}

const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'application/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.jpg': 'image/jpeg', '.svg': 'image/svg+xml', '.ico': 'image/x-icon', '.json': 'application/json', '.woff2': 'font/woff2', '.woff': 'font/woff', '.webp': 'image/webp', '.txt': 'text/plain' };

function serveStatic(root, port, rules) {
    const matches = function (source, urlPath) {
        if (source === '**') return true;
        if (source === '**/*.@(html|js|css)') return /\.(html|js|css)$/.test(urlPath);
        return false;
    };
    return new Promise(function (resolve, reject) {
        const server = http.createServer(function (req, res) {
            let urlPath = decodeURIComponent(new URL(req.url, 'http://x').pathname);
            if (urlPath === '/') urlPath = '/index.html';
            const file = path.join(root, urlPath);
            if (!file.startsWith(root) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
                res.writeHead(404, { 'Content-Type': 'text/plain' }); res.end('Not found'); return;
            }
            const headers = { 'Content-Type': TYPES[path.extname(file).toLowerCase()] || 'application/octet-stream', 'Cache-Control': 'no-store' };
            (rules || []).forEach(function (rule) {
                if (matches(rule.source, urlPath)) rule.headers.forEach(function (h) { headers[h.key] = h.value; });
            });
            res.writeHead(200, headers);
            fs.createReadStream(file).pipe(res);
        });
        server.once('error', reject);
        server.listen(port, '127.0.0.1', function () { resolve(server); });   // this machine only, never the network
    });
}

// ---------------------------------------------------------------- demo data

function isoDate(d) { return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }

// A working day (Sun-Thu) about `offset` days from today, so demo bookings never
// sit on the weekend the client doesn't work.
function workday(offset) {
    const d = new Date();
    d.setHours(12, 0, 0, 0);
    d.setDate(d.getDate() + offset);
    while (d.getDay() === 5 || d.getDay() === 6) d.setDate(d.getDate() + 1);
    return isoDate(d);
}

async function seedDemoData() {
    process.env.FIRESTORE_EMULATOR_HOST = '127.0.0.1:' + PORTS.firestore;
    const admin = require(path.join(ROOT, 'functions', 'node_modules', 'firebase-admin'));
    const { seedIfEmpty } = require(path.join(ROOT, 'functions', 'scripts', 'seedDefaults'));

    // The admin sign-in (Auth emulator only; this password exists nowhere else).
    const AUTH = 'http://127.0.0.1:' + PORTS.auth + '/identitytoolkit.googleapis.com/v1';
    const owner = { Authorization: 'Bearer owner', 'Content-Type': 'application/json' };
    const signUp = await fetch(AUTH + '/accounts:signUp?key=demo', { method: 'POST', headers: owner, body: JSON.stringify({ email: ADMIN_EMAIL, password: ADMIN_PASSWORD, returnSecureToken: true }) });
    const user = await signUp.json();
    if (!signUp.ok) throw new Error('Could not create the demo admin user: ' + JSON.stringify(user));
    await fetch(AUTH + '/accounts:update?key=demo', { method: 'POST', headers: owner, body: JSON.stringify({ localId: user.localId, emailVerified: true }) });

    if (!admin.apps.length) admin.initializeApp({ projectId: PROJECT });
    const db = admin.firestore();
    await seedIfEmpty(db);                                           // the real seed: 1 service + Sun-Thu 9-5
    await db.collection('services').doc('default-session').update({ name: 'Executive Advisory Session', durationMinutes: 60, amount: 50000 });
    await db.collection('services').doc('free-intro').set({ name: 'Intro Call', description: 'A short, free first conversation.', durationMinutes: 30, amount: 0, active: true, sortOrder: 2 });
    await db.collection('services').doc('svc-review').set({ name: 'Business Review', description: 'A structured review of where the business stands.', durationMinutes: 90, amount: 120000, active: true, sortOrder: 3 });
    await db.collection('blockedDates').doc(workday(20)).set({ reason: 'Public holiday' });

    const base = { currency: 'SAR', customerMobile: '+966500000000', manageToken: 'demo' };
    const at = function (date, startMinutes, minutes) {
        return { slotDate: date, slotTime: String(Math.floor(startMinutes / 60)).padStart(2, '0') + ':' + String(startMinutes % 60).padStart(2, '0'), startMinutes: startMinutes, endMinutes: startMinutes + minutes };
    };
    const ES = 'Executive Advisory Session'; const IC = 'Intro Call'; const BR = 'Business Review';
    // 20-character ids, like real ones (the cancel function rejects anything else)
    const rows = [
        ['DemoPastConfirmed00001', workday(-6), 600, 60, { status: 'confirmed', serviceName: ES, customerName: 'Layla Hassan', customerEmail: 'layla@example.com', isPaid: true, amount: 50000 }],
        ['DemoPastFreeCall000002', workday(-5), 540, 30, { status: 'confirmed', serviceName: IC, customerName: 'Omar Farouk', customerEmail: 'omar@example.com', isPaid: false, amount: 0 }],
        ['DemoPastNoShow0000003', workday(-4), 780, 90, { status: 'no_show', serviceName: BR, customerName: 'Sara Al-Qahtani', customerEmail: 'sara@example.com', isPaid: true, amount: 120000 }],
        ['DemoUpcomingPaid000004', workday(3), 600, 60, { status: 'confirmed', serviceName: ES, customerName: 'Khalid Nasser', customerEmail: 'khalid@example.com', isPaid: true, amount: 50000, paymobTransactionId: '50001' }],
        ['DemoUpcomingFree000005', workday(4), 660, 30, { status: 'confirmed', serviceName: IC, customerName: 'Nora Salem', customerEmail: 'nora@example.com', isPaid: false, amount: 0 }],
        ['DemoCancelledPaid000006', workday(5), 900, 90, { status: 'cancelled', serviceName: BR, customerName: 'Yousef Adel', customerEmail: 'yousef@example.com', isPaid: true, amount: 120000, refundAmount: 120000, refundStatus: 'refunded', cancelledBy: 'customer' }],
        ['DemoNeedsRefund00000007', workday(6), 540, 60, { status: 'cancelled', serviceName: ES, customerName: 'Rania Khalil', customerEmail: 'rania@example.com', isPaid: true, amount: 50000, paymobTransactionId: '50002', refundAmount: 50000, refundStatus: 'needs_manual', cancelledBy: 'customer' }],
        ['DemoSlotConflict000008', workday(7), 660, 60, { status: 'paid_slot_conflict', serviceName: ES, customerName: 'Huda Mansour', customerEmail: 'huda@example.com', isPaid: true, amount: 50000, paymobTransactionId: '50003' }],
        ['DemoUpcomingPaid000009', workday(8), 600, 60, { status: 'confirmed', serviceName: ES, customerName: 'Tariq Haddad', customerEmail: 'tariq@example.com', isPaid: true, amount: 50000, paymobTransactionId: '50004' }]
    ];
    for (const r of rows) {
        await db.collection('bookings').doc(r[0]).set(Object.assign({}, base, at(r[1], r[2], r[3]), r[4]));
    }
}

// ---------------------------------------------------------------- main

async function stop() {
    log('Stopping the demo...');
    stopEverything();
    log('Done. The demo is stopped and its temporary files are removed.');
}

async function start() {
    log('\n=== Booking system: LOCAL DEMO ===\n');

    const busy = [];
    for (const k of Object.keys(PORTS)) if (await portInUse(PORTS[k])) busy.push(k + ' (port ' + PORTS[k] + ')');
    if (busy.length) {
        log('These demo ports are already in use: ' + busy.join(', '));
        log('Is the demo already running? Close it first (stop-demo.bat), then start again.');
        process.exit(1);
    }

    const javaDir = findJavaDir();
    const cli = spawnSync('firebase', ['--version'], { stdio: 'ignore', shell: true });
    if (cli.status !== 0) throw new Error('The Firebase CLI is needed (npm install -g firebase-tools).');

    log('1/5 Preparing the demo copies of the site and admin...');
    fs.rmSync(BUILD, { recursive: true, force: true });
    fs.mkdirSync(BUILD, { recursive: true });
    buildSite();
    buildModule();
    buildAdmin();

    log('2/5 Starting the backend emulators (this takes about 30 seconds)...');
    fs.writeFileSync(SECRET_FILE, ['PAYMOB_SECRET_KEY', 'PAYMOB_PUBLIC_KEY', 'PAYMOB_HMAC_SECRET', 'PAYMOB_WEBHOOK_SHARED_SECRET', 'RESEND_API_KEY', 'GOOGLE_CALENDAR_KEY'].map(function (k) { return k + '=placeholder'; }).join('\n') + '\n');
    const emulatorLog = path.join(BUILD, 'emulators.log');
    const out = fs.openSync(emulatorLog, 'w');
    const env = Object.assign({}, process.env);
    if (javaDir) env.PATH = javaDir + path.delimiter + env.PATH;
    const emulators = spawn('firebase', ['emulators:start', '--only', 'functions,firestore,pubsub,auth', '--project', PROJECT], { cwd: ROOT, env: env, shell: true, stdio: ['ignore', out, out], detached: !IS_WIN });

    let shuttingDown = false;
    const shutdown = function (code) {
        if (shuttingDown) return;
        shuttingDown = true;
        log('\nShutting the demo down...');
        try { killTree(emulators.pid); } catch (e) { /* ignore */ }
        stopEverything();
        process.exit(code || 0);
    };
    process.on('SIGINT', function () { shutdown(0); });
    process.on('SIGTERM', function () { shutdown(0); });
    emulators.on('exit', function () { if (!shuttingDown) { log('The emulators stopped unexpectedly. See ' + emulatorLog); shutdown(1); } });

    try {
        await waitFor(async function () { return /All emulators ready/.test(fs.readFileSync(emulatorLog, 'utf8')); }, 180000, 'the emulators to start');
    } catch (err) {
        log(err.message + ' See ' + emulatorLog);
        shutdown(1);
        return;
    }

    log('3/5 Loading demo data...');
    await seedDemoData();

    log('4/5 Starting the website and admin servers...');
    await serveStatic(path.join(BUILD, 'site'), PORTS.site, []);
    await serveStatic(path.join(BUILD, 'admin'), PORTS.admin, adminHeaders());

    const urls = {
        book: 'http://localhost:' + PORTS.site + '/pages/book-a-session.html',
        mailbox: 'http://localhost:' + PORTS.site + '/demo/mailbox.html',
        module: 'http://localhost:' + PORTS.site + '/module/pages/book-a-session.html',
        admin: 'http://127.0.0.1:' + PORTS.admin + '/'
    };

    log('5/5 Opening your browser...\n');
    if (IS_WIN && !process.env.DEMO_NO_BROWSER) [urls.book, urls.admin, urls.mailbox].forEach(function (u) { spawn('cmd', ['/c', 'start', '', u], { stdio: 'ignore', detached: true }).unref(); });

    log('THE DEMO IS RUNNING');
    log('  Customer booking page : ' + urls.book);
    log('  Demo mailbox (emails) : ' + urls.mailbox);
    log('  Module template page  : ' + urls.module + '   (the reusable page, no site around it)');
    log('  Admin panel           : ' + urls.admin);
    log('      sign in with  ' + ADMIN_EMAIL + '  /  ' + ADMIN_PASSWORD + '   (demo only)');
    log('');
    log('  Paid bookings go to a FAKE checkout page (no real payment). Emails are shown in the mailbox, not sent.');
    log('  Leave this window open while you present. To stop: close it, or press Ctrl+C.');
}

const args = process.argv.slice(2);
(args.indexOf('--stop') >= 0 ? stop() : start()).catch(function (err) {
    console.error('\nCould not start the demo: ' + err.message);
    stopEverything();
    process.exit(1);
});
