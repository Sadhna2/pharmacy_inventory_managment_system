# Running the system on your own laptop

Written for someone who has never used Docker. No prior knowledge assumed.

---

## Part 1 — What Docker actually is

Skip to Part 2 if you just want the commands.

This project is not one program. It is **four** things that have to run at the
same time and talk to each other:

| Piece | What it does |
|---|---|
| **Database** | Stores everything — products, stock, users, history |
| **Migrate** | Sets up the database tables, then stops. Runs once. |
| **API** | The brain. Answers questions like "how much stock is at Bandra?" |
| **Web** | The screens you look at in the browser |

Installing all four by hand means installing PostgreSQL, then Python, then the
right Python version, then Node.js, then hoping your laptop's versions match
everyone else's. That is where most of a project's setup time goes.

**Docker skips all of it.** Each piece is packaged into a sealed box that
already contains everything it needs. You do not install PostgreSQL — you run
a box that has PostgreSQL inside it.

Two words you will see:

- **Image** — the *recipe*. A frozen, complete copy of one piece. Nothing runs.
  Like a `.dmg` installer file sitting in Downloads.
- **Container** — the recipe *actually running*. Like the app open on screen.

One image can start many containers. Deleting a container does not delete the
image, so starting again is instant.

**Docker Compose** is the fourth word. Instead of starting four boxes by hand
and wiring them together, `docker-compose.yml` describes all four and the
connections between them. One command starts the lot.

> **On this laptop specifically:** Docker Desktop would not install without an
> admin password, so this machine uses **Colima** instead. Colima does the same
> job — it runs a tiny invisible Linux machine for the boxes to live in. The
> only difference for you is one extra command at the start (`colima start`).

---

## Part 2 — Running it, from scratch

Open **Terminal** (press ⌘+Space, type "Terminal", press Enter).

### Step 1 — Go to the project folder

```bash
cd ~/Documents/sadhna
```

### Step 2 — Start Colima

This wakes up the little Linux machine the boxes run in. Takes ~30 seconds the
first time and is instant after that. **Nothing works until this is running.**

```bash
colima start
```

If it says it is already running, that is fine — carry on.

### Step 3 — Create your settings file

Only needed **once**, ever. It copies the example settings into a real file.

```bash
cp .env.example .env
```

This is where the demo password lives. It is deliberately kept out of the
shared code so real passwords never get published by accident.

### Step 4 — Start everything

```bash
docker compose up -d --build
```

What is happening:

- `--build` — bake the four recipes from the source code
- `up` — start them
- `-d` — "detached", meaning it runs in the background and gives your
  Terminal back instead of filling it with logs

**First time: 3–5 minutes** (it downloads PostgreSQL, Python, Node and Caddy,
then builds the demo dataset — two years of trading history, about half a
minute of that total).
**After that: 10–30 seconds**, because everything unchanged is reused and the
seed sees the data is already there and does nothing.

This is the same stack, the same images and the same seed command that run on
the server, so what you see here is what is deployed.

### Step 5 — Open it

```
http://localhost:8080
```

Sign in with any of these:

All three share one password — the `SEED_PASSWORD` line in the `.env` file you
made in Step 3. To read it back:

```bash
grep SEED_PASSWORD .env
```

| Email | What they can see |
|---|---|
| `admin@pharmacy.co.in` | Everything, including Users + Audit trail |
| `manager@pharmacy.co.in` | Whole chain, can approve orders |
| `staff@pharmacy.co.in` | Andheri branch only, cannot approve |

Signing in as all three is the fastest way to see the permission system
working — the sidebar itself changes.

The API's own documentation is at **http://localhost:8080/docs**.

---

## Part 3 — Everyday commands

### Is it running?

```bash
docker compose ps
```

You want to see `db`, `api` and `web` as **Up**. `migrate` showing **Exited**
is correct — it does its job once and stops.

### Stop it

```bash
docker compose stop
```

Everything shuts down. Your data is kept.

### Start it again

```bash
docker compose start
```

Seconds, because nothing needs rebuilding.

### I changed some code — how do I see it?

```bash
docker compose up -d --build
```

The same command as Step 4. It rebuilds only what changed.

### Something is broken, show me why

```bash
docker compose logs api --tail 50
```

Swap `api` for `web` or `db` to look at a different piece.

### Start completely fresh (deletes all data)

```bash
docker compose down -v
docker compose up -d --build
```

`down -v` removes the containers **and the stored data**. The database is
rebuilt and re-seeded from scratch. Use this when data gets into a mess during
testing — it is the reset button.

> `-v` is the dangerous flag. `docker compose down` on its own stops
> everything but **keeps** your data. Only add `-v` when you actually want the
> data gone.

---

## Part 4 — When something goes wrong

**"Cannot connect to the Docker daemon"**
Colima is not running. `colima start`.

**"port is already allocated"**
Something else is using port 8080. Either close it, or open `.env` and change
`WEB_PORT=8080` to `WEB_PORT=8090`, then `docker compose up -d`. The site moves
to `http://localhost:8090`.

**The page loads but nothing appears / login fails**
The API probably has not finished starting. Wait 15 seconds and refresh. If it
persists: `docker compose logs api --tail 50`.

**"Database already seeded — skipping" / "History already present — skipping"**
Not errors. Each seed step checks whether it has already run, so starting the
stack a second time leaves your work alone instead of doubling every balance.
To build it fresh, reset with `docker compose down -v`.

**Everything is confusing, just reset**

```bash
docker compose down -v && docker compose up -d --build
```

A few minutes and you are back to a clean, known state — the same one the
server builds. This is safe: nothing outside this folder is touched.

---

## Part 5 — Local versus the live site

| | Your laptop | AWS |
|---|---|---|
| Address | http://localhost:8080 | https://13-204-222-250.nip.io |
| Who can reach it | Only you | Anyone with the link |
| Data | Yours, separate | Separate, shared |
| Padlock in browser | No (plain http) | Yes (real certificate) |

They are **completely separate systems** running identical code. Anything you
create locally — products, orders, users — exists only on your laptop. You
cannot break the live site by experimenting locally.

That is the point of doing it this way: try anything you like here first.
