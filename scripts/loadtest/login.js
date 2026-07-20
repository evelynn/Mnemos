// k6 login flow: reusable helper for authenticated scenarios.
//
// Imported by baseline.js. Do a one-time POST /api/v1/auth/login in
// ``setup()`` and pass the cookie to every VU via the returned data
// object so we don't re-login on every iteration (which would swamp
// the auth endpoint and distort the measurement).
//
// Usage:
//   import { loginOnce, authCookie } from "./login.js";
//   export function setup() { return loginOnce(); }
//   export default function (data) {
//     http.get(url, { headers: authCookie(data) });
//   }

import http from "k6/http";

const BASE = __ENV.BASE_URL || "http://localhost:16401";
const USERNAME = __ENV.MNEMOS_USERNAME || "admin";
const PASSWORD = __ENV.MNEMOS_PASSWORD || "";
const COOKIE_NAME = __ENV.MNEMOS_COOKIE_NAME || "mnemos_session";

export function loginOnce() {
  if (!PASSWORD) {
    throw new Error("MNEMOS_PASSWORD required for authenticated scenarios");
  }
  const r = http.post(
    `${BASE}/api/v1/auth/login`,
    JSON.stringify({ username: USERNAME, password: PASSWORD }),
    { headers: { "Content-Type": "application/json" } }
  );
  if (r.status !== 200) {
    throw new Error(`login failed (HTTP ${r.status}): ${r.body}`);
  }
  const setCookie = r.headers["Set-Cookie"] || "";
  const match = setCookie.match(new RegExp(`${COOKIE_NAME}=([^;]+)`));
  if (!match) {
    throw new Error("login ok but no session cookie in Set-Cookie header");
  }
  return { cookie: match[1] };
}

export function authCookie(data) {
  return { Cookie: `${COOKIE_NAME}=${data.cookie}` };
}
