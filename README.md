# ⛅ SkyCast — Weather Predictor

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

## ✨ Features

- **🔍 City search** — look up current weather for any city worldwide.
- **📍 Use my location** — one tap uses the browser's Geolocation API to
  fetch weather for where you are.
- **🌡️ Live metrics** — temperature, "feels like", humidity, wind speed,
  atmospheric pressure, visibility, and sunrise / sunset times.
- **°C / °F toggle** — switch units instantly; conversion happens
  client-side with **no extra network request**.
- **🎨 Weather-aware UI** — the background gradient shifts for clear skies
  (day & night), clouds, rain, thunderstorms, snow, and mist.
- **⏳ Loading states** — an animated spinner while data is in flight.
- **⚠️ Robust error handling** — friendly, specific messages for invalid
  cities, denied location permission, network failures, and server issues.
- **📱 Fully responsive** — mobile-first layout that scales cleanly from
  phones to desktops, with reduced-motion support for accessibility.

---

## 🔐 How your API key stays private

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

## 🗂️ Project structure

```
Skycast-Weather-Predictor/
├── server.py            # Python stdlib proxy + static file server
├── .env                 # YOUR secret key (git-ignored, never committed)
├── .env.example         # Template — copy to .env and add your key
├── .gitignore           # Ensures .env never reaches Git/GitHub
├── README.md
└── public/              # The front-end (served by server.py)
    ├── index.html       # Markup & structure
    ├── css/
    │   └── style.css    # Responsive, weather-aware styling
    └── js/
        ├── api.js       # Data layer — talks only to the local proxy
        ├── ui.js        # View layer — all DOM rendering & formatting
        └── app.js       # Controller — wires events to API + UI
```

The JavaScript is split into three focused modules following a small
**API → UI → Controller** separation, so each file has a single
responsibility.

---

## 🚀 Getting started

### Prerequisites

- **Python 3.8+** (uses only the standard library — nothing to `pip install`)
- A free **OpenWeather API key** →
  [get one here](https://home.openweathermap.org/api_keys)

> ℹ️ A brand-new OpenWeather key can take a little while (up to ~2 hours)
> to activate. Until then the API returns a 401, and SkyCast will show
> *"Weather service rejected the API key."*

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

## 🛠️ Tech & concepts demonstrated

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

## 🌐 API reference (local proxy)

`GET /api/weather`

| Query param   | Description                        | Example                |
| ------------- | ---------------------------------- | ---------------------- |
| `city`        | City name to look up               | `?city=Tokyo`          |
| `lat`, `lon`  | Coordinates (used by geolocation)  | `?lat=51.5&lon=-0.12`  |

Returns the OpenWeather payload as JSON on success, or
`{ "error": "..." }` with an appropriate HTTP status on failure. The API
key is added by the server and is never part of this request or response.

---

## 📄 License

Free to use for learning and personal projects. Weather data is provided by
[OpenWeather](https://openweathermap.org/).
