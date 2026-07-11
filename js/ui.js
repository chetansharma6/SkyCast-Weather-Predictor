/* ============================================================
   ui.js — everything that touches the DOM.

   Rendering, formatting, unit conversion, and toggling the
   loading / error / weather views live here. app.js owns the
   events and calls into this module.
   ============================================================ */

const SkyCastUI = (() => {
  "use strict";

  // ---- Cache element references once ----
  const el = {
    body: document.body,
    form: document.getElementById("search-form"),
    input: document.getElementById("city-input"),
    searchBtn: document.getElementById("search-btn"),
    geoBtn: document.getElementById("geo-btn"),
    unitBtns: Array.from(document.querySelectorAll(".units__btn")),
    loader: document.getElementById("loader"),
    error: document.getElementById("error"),
    errorText: document.getElementById("error-text"),
    hint: document.getElementById("hint"),
    weather: document.getElementById("weather"),
    lastUpdated: document.getElementById("last-updated"),
    // Weather card fields
    city: document.getElementById("w-city"),
    desc: document.getElementById("w-desc"),
    time: document.getElementById("w-time"),
    icon: document.getElementById("w-icon"),
    temp: document.getElementById("w-temp"),
    tempUnit: document.getElementById("w-temp-unit"),
    feels: document.getElementById("w-feels"),
    humidity: document.getElementById("w-humidity"),
    wind: document.getElementById("w-wind"),
    pressure: document.getElementById("w-pressure"),
    visibility: document.getElementById("w-visibility"),
    sunrise: document.getElementById("w-sunrise"),
    sunset: document.getElementById("w-sunset"),
  };

  // The most recent payload, kept so the °C/°F toggle can re-render
  // instantly without hitting the network again.
  let lastData = null;

  /* ---------- unit conversion helpers ---------- */
  // The proxy always requests metric, so we convert client-side.
  const toF = (c) => (c * 9) / 5 + 32;
  const round = (n) => Math.round(n);

  function formatTemp(celsius, unit) {
    return unit === "imperial" ? round(toF(celsius)) : round(celsius);
  }

  function formatWind(metersPerSec, unit) {
    // metric wind arrives in m/s.
    return unit === "imperial"
      ? `${(metersPerSec * 2.23694).toFixed(1)} mph`
      : `${(metersPerSec * 3.6).toFixed(1)} km/h`;
  }

  function formatTime(unixSeconds, timezoneOffsetSec) {
    // Convert to the queried city's local time using its UTC offset.
    const localMs = (unixSeconds + timezoneOffsetSec) * 1000;
    const d = new Date(localMs);
    let h = d.getUTCHours();
    const m = String(d.getUTCMinutes()).padStart(2, "0");
    const ampm = h >= 12 ? "PM" : "AM";
    h = h % 12 || 12;
    return `${h}:${m} ${ampm}`;
  }

  /* ---------- view switching ---------- */
  function hideAll() {
    el.loader.hidden = true;
    el.error.hidden = true;
    el.hint.hidden = true;
    el.weather.hidden = true;
  }

  function showLoading() {
    hideAll();
    el.loader.hidden = false;
    el.searchBtn.disabled = true;
    el.geoBtn.disabled = true;
  }

  function showError(message) {
    hideAll();
    el.errorText.textContent = message;
    el.error.hidden = false;
    releaseButtons();
  }

  function releaseButtons() {
    el.searchBtn.disabled = false;
    el.geoBtn.disabled = false;
  }

  /* ---------- weather-aware background ---------- */
  function themeFor(data) {
    const main = (data.weather?.[0]?.main || "").toLowerCase();
    const icon = data.weather?.[0]?.icon || "";
    const isNight = icon.endsWith("n");

    if (main.includes("thunder")) return "thunder";
    if (main.includes("drizzle") || main.includes("rain")) return "rain";
    if (main.includes("snow")) return "snow";
    if (
      main.includes("mist") ||
      main.includes("fog") ||
      main.includes("haze") ||
      main.includes("smoke")
    )
      return "mist";
    if (main.includes("cloud")) return "clouds";
    // Clear (or anything else): pick day vs night.
    return isNight ? "clear-night" : "clear-day";
  }

  /* ---------- main render ---------- */
  function render(data, unit) {
    lastData = data;

    const tz = data.timezone || 0;
    const unitSuffix = unit === "imperial" ? "°F" : "°C";

    el.city.textContent = `${data.name}, ${data.sys?.country || ""}`.replace(
      /, $/,
      ""
    );
    el.desc.textContent = data.weather?.[0]?.description || "—";
    // Date.now() is UTC epoch; adding the city's tz offset gives its local time.
    const nowUtc = Math.floor(Date.now() / 1000);
    el.time.textContent = `Local time ${formatTime(nowUtc, tz)}`;

    // Weather icon from OpenWeather's CDN.
    const iconCode = data.weather?.[0]?.icon;
    if (iconCode) {
      el.icon.src = `https://openweathermap.org/img/wn/${iconCode}@2x.png`;
      el.icon.alt = data.weather?.[0]?.description || "weather icon";
    }

    el.temp.textContent = formatTemp(data.main.temp, unit);
    el.tempUnit.textContent = unitSuffix;
    el.feels.textContent = `${formatTemp(
      data.main.feels_like,
      unit
    )}${unitSuffix}`;

    el.humidity.textContent = `${data.main.humidity}%`;
    el.wind.textContent = formatWind(data.wind?.speed || 0, unit);
    el.pressure.textContent = `${data.main.pressure} hPa`;
    el.visibility.textContent =
      data.visibility != null
        ? `${(data.visibility / 1000).toFixed(1)} km`
        : "—";
    el.sunrise.textContent = data.sys?.sunrise
      ? formatTime(data.sys.sunrise, tz)
      : "—";
    el.sunset.textContent = data.sys?.sunset
      ? formatTime(data.sys.sunset, tz)
      : "—";

    el.body.dataset.theme = themeFor(data);
    el.lastUpdated.textContent = `Updated ${formatTime(nowUtc, tz)}`;

    hideAll();
    el.weather.hidden = false;
    releaseButtons();
  }

  /** Re-render the cached payload in a new unit (no network call). */
  function rerender(unit) {
    if (lastData) render(lastData, unit);
  }

  function setActiveUnit(unit) {
    el.unitBtns.forEach((btn) =>
      btn.classList.toggle("is-active", btn.dataset.unit === unit)
    );
  }

  return {
    el,
    showLoading,
    showError,
    render,
    rerender,
    setActiveUnit,
    hasData: () => lastData !== null,
  };
})();
