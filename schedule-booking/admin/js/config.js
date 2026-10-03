// Public Firebase web configuration. These values identify the project; they
// are NOT secrets — what actually protects the data is Firebase Authentication
// plus firestore.rules (only verified, listed admin emails can read or write).
//
// SETUP: replace apiKey with this project's "Web API Key"
// (Firebase console -> Project settings -> General). For extra safety, restrict
// that key to your admin site's domain in Google Cloud console ->
// APIs & Services -> Credentials.
export const firebaseConfig = {
    apiKey: 'YOUR_WEB_API_KEY',
    authDomain: 'your-project-id.firebaseapp.com',
    projectId: 'your-project-id'
};

// Where the Cloud Functions live. Cancelling a booking (refund + emails) is a
// function call, not a database write. The Content-Security-Policy in
// firebase.json must allow this origin in connect-src.
export const functionsBase = 'https://us-central1-your-project-id.cloudfunctions.net';

// Must match CURRENCY in functions/lib/config.js.
export const CURRENCY = 'SAR';

// Local testing only: point the app at the Firebase emulators, e.g.
//   { authUrl: 'http://127.0.0.1:9199', firestoreHost: '127.0.0.1', firestorePort: 8180 }
// Leave null for the real project.
export const emulator = null;
