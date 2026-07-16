# SkyCast — Weather Predictor

**Real-time weather for any city in the world.** SkyCast is a responsive
web application that fetches live conditions from the
[OpenWeather API](https://openweathermap.org/api) and presents them in a
clean, glassmorphic interface that adapts its background to the current
weather.

The project pairs a **pure HTML / CSS / JavaScript front-end** with a
**tiny Python proxy server** whose only job is to keep your API key
secret — the key lives in a git-ignored `.env` file, is injected
server-side, and **never reaches the browser or the Git history.**

---

## Features

- ** City search** — look up current weather for any city worldwide.
- ** Use my location** — one tap uses the browser's Geolocation API to
  fetch weather for where you are.
- ** Live metrics** — temperature, "feels like", humidity, wind speed,
  atmospheric pressure, visibility, and sunrise / sunset times.
- **°C / °F toggle** — switch units instantly; conversion happens
  client-side with **no extra network request**.
- ** Weather-aware UI** — the background gradient shifts for clear skies
  (day & night), clouds, rain, thunderstorms, snow, and mist.
- ** Loading states** — an animated spinner while data is in flight.
- ** Robust error handling** — friendly, specific messages for invalid
  cities, denied location permission, network failures, and server issues.
- ** Fully responsive** — mobile-first layout that scales cleanly from
  phones to desktops, with reduced-motion support for accessibility.

---

## How your API key stays private

This is the core design decision of the project.

A pure front-end app **cannot** truly hide an API key — it would travel in
the browser's network request for anyone to read in DevTools. SkyCast
solves this with a thin proxy:

```
Browser  ──►  /api/weather  ──►  server.py (adds secret key)  ──►  OpenWeather
   ▲                                                                    │
   └──────────────────  weather JSON (no key)  ◄────────────────────────┘
```

- The browser only ever calls **`/api/weather`** on your own server.
- `server.py` reads `OPENWEATHER_API_KEY` from `.env` and adds it to the
  upstream request **server-side**.
- The key is **never** sent to the browser and **never** committed, because
  `.env` is listed in [`.gitignore`](.gitignore).

---

## Project structure

```
Skycast-Weather-Predictor/
├── server.py            # Python stdlib proxy + static file server
├── requirements.txt     # Empty (no deps) — lets Render detect Python
├── render.yaml          # Render deployment blueprint
├── .env                 # YOUR secret key (git-ignored, never committed)
├── .env.example         # Template — copy to .env and add your key
├── .gitignore           # Ensures .env never reaches Git/GitHub
├── README.md
├── index.html           # Markup & structure
├── css/
│   └── style.css        # Responsive, weather-aware styling
└── js/
    ├── api.js           # Data layer — talks only to the local proxy
    ├── ui.js            # View layer — all DOM rendering & formatting
    └── app.js           # Controller — wires events to API + UI
```

The front-end lives at the project root with `css/` and `js/` as separate
top-level folders. The JavaScript is split into three focused modules
following a small **API → UI → Controller** separation, so each file has a
single responsibility. `server.py` serves these static files through a
strict allow-list (only `index.html`, `css/`, and `js/` are public), so
secrets like `.env` can never be served.

---

## Getting started

### Prerequisites

- **Python 3.8+** (uses only the standard library — nothing to `pip install`)
- A free **OpenWeather API key** →
  [get one here](https://home.openweathermap.org/api_keys)

> A brand-new OpenWeather key can take a little while (up to ~2 hours) to activate. Until then the API returns a 401, and SkyCast will show *"Weather service rejected the API key."*

### Setup

1. **Add your key.** Copy the template and paste your key into the new file:

   ```powershell
   Copy-Item .env.example .env
   ```

   Then open `.env` and set:

   ```
   OPENWEATHER_API_KEY=your_real_key_here
   ```

2. **Start the server:**

   ```powershell
   python server.py
   ```

3. **Open the app** in your browser:

   ```
   http://localhost:8000
   ```

That's it — search for a city or tap 📍 to use your location.

---

## Deploying live on Render

SkyCast is built to deploy on [Render](https://render.com) as a single
**Web Service** — the same `server.py` that serves the front-end locally
also serves it in production and proxies the API calls. Here's how it works
and how to do it.

### Why it "just works" on Render

Render runs your process and hands it two things through the environment:

1. **`$PORT`** — the port your app must listen on. `server.py` already reads
   it (`PORT = int(os.environ.get("PORT", "8000"))`) and binds `0.0.0.0`,
   which is exactly what Render requires.
2. **Your environment variables** — including `OPENWEATHER_API_KEY`. The
   `.env` loader uses `os.environ.setdefault`, so a real environment
   variable set in Render's dashboard takes precedence and **no `.env` file
   is needed in production.** The key stays encrypted in Render and never
   enters the repo.

Because the app has **zero dependencies**, there's nothing to build — the
included `requirements.txt` is empty and exists only so Render detects the
project as Python.

### Option A — Blueprint (one click, uses `render.yaml`)

The repo ships a [`render.yaml`](render.yaml) blueprint:

1. Push this repo to GitHub.
2. In Render: **New +  →  Blueprint**, then select your repo.
3. Render reads `render.yaml` and creates the service. When prompted, paste
   your **`OPENWEATHER_API_KEY`** (it's marked `sync: false`, so Render asks
   for it and keeps it secret).
4. Click **Apply** — your site goes live at
   `https://skycast-xxxx.onrender.com`.

### Option B — Manual Web Service

1. In Render: **New +  →  Web Service**, connect your GitHub repo.
2. Configure:
   - **Runtime:** `Python 3`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python server.py`
3. Under **Environment**, add a variable:
   - **Key:** `OPENWEATHER_API_KEY`  **Value:** *your real key*
4. Click **Create Web Service**.

> **Your key is never in the code or Git.** Locally it's in the
> git-ignored `.env`; on Render it's a dashboard secret. In both cases it's
> injected server-side and never reaches the browser.

> On Render's free plan the service sleeps after inactivity, so the first
> request after idle may take ~30 seconds to wake up.

---

## Tech & concepts demonstrated

| Area                     | What's used                                                        |
| ------------------------ | ------------------------------------------------------------------ |
| **Async JavaScript**     | `async` / `await`, `fetch`, `Promise`-based flows                  |
| **REST API integration** | OpenWeather Current Weather endpoint via a server-side proxy       |
| **DOM manipulation**     | Cached element refs, dynamic rendering, view state switching       |
| **Error handling**       | Try/catch across network, HTTP, and geolocation failure modes      |
| **Responsive CSS**       | Mobile-first, `clamp()` fluid type, CSS grid, media queries        |
| **Web APIs**             | Geolocation, `URLSearchParams`, client-side unit conversion        |
| **Security**             | Secret kept server-side; strict param allow-list; path-traversal guard |

---

## API reference (local proxy)

`GET /api/weather`

| Query param   | Description                        | Example                |
| ------------- | ---------------------------------- | ---------------------- |
| `city`        | City name to look up               | `?city=Tokyo`          |
| `lat`, `lon`  | Coordinates (used by geolocation)  | `?lat=51.5&lon=-0.12`  |

Returns the OpenWeather payload as JSON on success, or
`{ "error": "..." }` with an appropriate HTTP status on failure. The API
key is added by the server and is never part of this request or response.

---

## License

Free to use for learning and personal projects. Weather data is provided by
[OpenWeather](https://openweathermap.org/).
