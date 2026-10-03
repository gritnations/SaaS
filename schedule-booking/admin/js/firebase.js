// The ONLY file that talks to the Firebase SDK. Everything else imports from
// here, so the SDK version is pinned in exactly one place (the URLs below) and
// can be swapped out wholesale for testing.
//
// The SDK is loaded from Google's own CDN, pinned to an exact version; the
// Content-Security-Policy header in firebase.json allows scripts from
// 'self' and www.gstatic.com only.

import { initializeApp } from 'https://www.gstatic.com/firebasejs/12.19.0/firebase-app.js';
import {
    getAuth, connectAuthEmulator, onAuthStateChanged, signInWithEmailAndPassword, signOut,
    sendEmailVerification, sendPasswordResetEmail
} from 'https://www.gstatic.com/firebasejs/12.19.0/firebase-auth.js';
import {
    getFirestore, connectFirestoreEmulator, collection, doc, getDoc, getDocs, setDoc, updateDoc,
    deleteDoc, query, where, writeBatch
} from 'https://www.gstatic.com/firebasejs/12.19.0/firebase-firestore.js';
import { firebaseConfig, emulator } from './config.js';

export const isConfigured = !String(firebaseConfig.apiKey).startsWith('REPLACE');

const app = initializeApp(firebaseConfig);
export const auth = getAuth(app);
export const db = getFirestore(app);

if (emulator) {
    connectAuthEmulator(auth, emulator.authUrl, { disableWarnings: true });
    connectFirestoreEmulator(db, emulator.firestoreHost, emulator.firestorePort);
}

export {
    onAuthStateChanged, signInWithEmailAndPassword, signOut, sendEmailVerification, sendPasswordResetEmail,
    collection, doc, getDoc, getDocs, setDoc, updateDoc, deleteDoc, query, where, writeBatch
};
