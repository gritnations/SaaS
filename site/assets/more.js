// Owner More screen: the phone menu's list of screens that do not fit the bottom bar, and Sign out.

import { signOut, startOwnerPage } from "./owner.js";

/** Start the page. */
async function start() {
  const page = await startOwnerPage("more");
  if (!page) return;
  document.getElementById("sign-out").addEventListener("click", signOut);
}

start();
