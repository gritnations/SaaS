// Minimal in-memory stand-in for the slice of Firestore that lib/bookings.js
// and friends use, so their LOGIC (overlap decisions, status transitions) can
// be tested with plain `node` — no emulator, no Java, no credentials.
//
// It does NOT model Firestore's concurrency (that's Firestore's job); it
// applies a transaction's writes only if the transaction function resolves.

let idCounter = 0;

function wrapForRead(data) {
    const out = {};
    Object.keys(data).forEach(function (k) {
        const v = data[k];
        // Real Firestore hands back Timestamps with .toDate(); mimic that.
        out[k] = v instanceof Date ? { toDate: function () { return v; } } : v;
    });
    return out;
}

function unwrapForStore(data) {
    const out = {};
    Object.keys(data).forEach(function (k) {
        const v = data[k];
        out[k] = v && typeof v.toDate === 'function' && !(v instanceof Date) ? v.toDate() : v;
    });
    return out;
}

function createFakeDb() {
    const store = new Map(); // collection -> Map(id -> data)

    function col(name) {
        if (!store.has(name)) store.set(name, new Map());
        return store.get(name);
    }

    function docRef(collectionName, id) {
        return {
            id: id,
            _collection: collectionName,
            get: async function () { return snapshot(collectionName, id); },
            set: async function (data) { col(collectionName).set(id, unwrapForStore(data)); },
            update: async function (data) { applyUpdate(collectionName, id, data); }
        };
    }

    function applyUpdate(collectionName, id, data) {
        const existing = col(collectionName).get(id);
        if (!existing) throw new Error('No document to update: ' + collectionName + '/' + id);
        col(collectionName).set(id, Object.assign({}, existing, unwrapForStore(data)));
    }

    function snapshot(collectionName, id) {
        const data = col(collectionName).get(id);
        return {
            id: id,
            exists: data !== undefined,
            ref: docRef(collectionName, id),
            data: function () { return data === undefined ? undefined : wrapForRead(data); }
        };
    }

    function query(collectionName, filters) {
        return {
            _isQuery: true,
            where: function (field, op, value) { return query(collectionName, filters.concat([{ field, op, value }])); },
            get: async function () { return runQuery(collectionName, filters); }
        };
    }

    function matches(data, f) {
        const v = data[f.field];
        if (f.op === '==') return v === f.value;
        if (f.op === 'in') return f.value.indexOf(v) !== -1;
        if (v === undefined || v === null) return false; // inequality filters skip missing/null fields, like Firestore
        if (f.op === '>=') return v >= f.value;
        if (f.op === '<=') return v <= f.value;
        throw new Error('Unsupported operator in fake: ' + f.op);
    }

    function runQuery(collectionName, filters) {
        const docs = [];
        col(collectionName).forEach(function (data, id) {
            if (filters.every(function (f) { return matches(data, f); })) docs.push(snapshot(collectionName, id));
        });
        return { docs: docs, empty: docs.length === 0, size: docs.length };
    }

    return {
        _store: store,
        // test helper: insert a raw doc, bypassing the code under test
        seed: function (collectionName, id, data) { col(collectionName).set(id, unwrapForStore(data)); },
        // test helper: read a raw stored doc
        raw: function (collectionName, id) { return col(collectionName).get(id); },
        collection: function (name) {
            const q = query(name, []);
            q.doc = function (id) { return docRef(name, id || 'auto' + (++idCounter)); };
            return q;
        },
        runTransaction: async function (fn) {
            const writes = [];
            const tx = {
                get: async function (target) {
                    if (target._isQuery) return target.get();
                    return target.get();
                },
                set: function (ref, data) { writes.push({ type: 'set', ref, data }); },
                update: function (ref, data) { writes.push({ type: 'update', ref, data }); }
            };
            const result = await fn(tx);
            writes.forEach(function (w) {
                if (w.type === 'set') col(w.ref._collection).set(w.ref.id, unwrapForStore(w.data));
                else applyUpdate(w.ref._collection, w.ref.id, w.data);
            });
            return result;
        },
        batch: function () {
            const ops = [];
            return {
                update: function (ref, data) { ops.push({ ref, data }); },
                commit: async function () { ops.forEach(function (o) { applyUpdate(o.ref._collection, o.ref.id, o.data); }); }
            };
        }
    };
}

module.exports = { createFakeDb };
