# Supreme Control — mandatory 6-digit access code

The Supreme Control page shows one six-digit code that changes every ten
minutes. An administrator signs in with **username + password + that code**. Any
one of the three missing or wrong, and there is no session.

This directory is standalone: standard library only, no framework, no
dependency. Copy the three modules into your project and wire two routes.

| File | What it is |
|---|---|
| `otp.py` | Derives and verifies the rotating code (TOTP, 600 s period) |
| `store.py` | SQLite schema, password hashing, secret storage, audit rows |
| `auth.py` | `login()` — the gate that enforces all three factors |
| `demo.py` | Walks the whole flow with a simulated clock |
| `test_supreme_otp.py` | 30 tests, including "password alone must fail" |

```console
$ python -m pytest test_supreme_otp.py -q
30 passed

$ python demo.py
SUPREME CONTROL — what the page displays
  access code : 996 174
  valid for   : 600 s  (rotates every 10 minutes)

ADMINISTRATOR SIGN-IN
  [DENIED ] om-2028, correct password, no code  (missing_code)
  [DENIED ] om-2028, correct password, wrong code  (bad_code)
  [DENIED ] om-2028, wrong password, right code  (bad_password)
  [GRANTED] anurag, correct password, right code
```

## Why the code is derived, not random

A random code every ten minutes would have to be **stored** for the login to
check it, would break across multiple worker processes, and would be readable by
anyone with database access. Deriving it from a secret plus the clock means the
page and the login each compute the same value independently, and nothing
sensitive is ever persisted in plaintext.

Only the **secret** is stored (32 bytes, in `supreme_settings`). Rotating it
invalidates every outstanding code immediately — that is the panic button.

## Wiring it in

### 1. Schema

`store.connect()` creates everything, including a `last_otp_counter` column on
your administrators table. If you already have an admins table, add just that:

```sql
ALTER TABLE administrators ADD COLUMN last_otp_counter INTEGER NOT NULL DEFAULT -1;
```

It records which window each admin last spent a code in, so a shoulder-surfed
code is not reusable.

### 2. The supreme page shows the code

```python
# Flask
@app.get("/owner/access-code")
@require_owner                     # MUST be behind your owner check
def access_code():
    window = code_at(ensure_secret(conn))
    return {
        "code": window.formatted,
        "secondsLeft": window.seconds_left,
        "expiresAt": window.expires_at,
    }
```

```python
# FastAPI
@app.get("/owner/access-code")
async def access_code(owner=Depends(require_owner)):
    window = code_at(ensure_secret(conn))
    return {"code": window.formatted, "secondsLeft": window.seconds_left,
            "expiresAt": window.expires_at}
```

The digits must never be rendered into the page's HTML — fetch them, so they are
only ever sent to an already-authenticated supreme session.

### 3. The page counts down and re-fetches

```js
async function refreshCode() {
  const res = await fetch('/owner/access-code', { credentials: 'same-origin' });
  if (!res.ok) return;
  const { code, secondsLeft } = await res.json();

  document.querySelector('#otp-code').textContent = code;
  document.querySelector('#otp-meta').textContent = `expires in ${secondsLeft}s`;
  document.querySelector('#otp-bar').style.width = `${(secondsLeft / 600) * 100}%`;

  clearTimeout(refreshCode._t);
  refreshCode._t = setTimeout(refreshCode, (secondsLeft + 1) * 1000);
}
refreshCode();
```

### 4. The login requires it

```python
@app.post("/login")
def do_login():
    result = login(
        conn, ensure_secret(conn),
        username=request.form["username"],
        password=request.form["password"],
        access_code=request.form["access_code"],
    )
    if not result.ok:
        flash(result.message)          # generic on purpose
        return redirect("/login")
    session["admin_id"] = result.admin.id
    session["role"] = result.admin.role
    return redirect("/owner")
```

Add one field to the form. If it is absent or empty, `login()` returns
`missing_code` and nobody gets in.

## What it protects against, honestly

**Stops:**

- Someone with a leaked admin password — they still cannot produce the code
  unless they can also see the supreme page.
- Replay: a code is spent once per administrator per window.
- Brute force: five failures a minute locks that username for the rest of it.
- Code theft after ten minutes: expired codes are rejected.

**Does not stop:**

- Anyone who already **has** supreme access. They can read the code and
  impersonate any administrator — it is one shared code, by design. If you later
  want per-person 2FA, keep this algorithm and store one secret per admin
  instead of one global secret.
- An attacker who reads the `supreme_settings` row from your database. Treat that
  table like a password store.

## Two knobs worth knowing

`otp.DRIFT_WINDOWS = 1` accepts the previous and next window, so a code entered
at 9:59 is not rejected at 10:00 and two servers with slightly different clocks
still agree. Set it to `0` for strictness.

`login(..., allow_code_reuse=True)` lets one admin sign in twice inside a window
(two devices). Off by default, because on by default would make a shoulder-
surfed code a key.
