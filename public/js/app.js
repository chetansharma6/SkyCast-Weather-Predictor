/* ============================================================
   app.js — the controller.

   Wires user interactions (search, geolocation, unit toggle)
   to the API layer (api.js) and the view layer (ui.js).
   ============================================================ */

(() => {
  "use strict";

  const { el } = SkyCastUI;

  // Current unit system: "metric" (°C) or "imperial" (°F).
  let unit = "metric";

  /* ---------- core flows ---------- */

  async function searchCity(city) {
    const trimmed = city.trim();
    if (!trimmed) {
      SkyCastUI.showError("Please type a city name to search.");
      return;
    }
    SkyCastUI.showLoading();
    try {
      const data = await SkyCastAPI.getByCity(trimmed);
      SkyCastUI.render(data, unit);
    } catch (err) {
      SkyCastUI.showError(err.message);
    }
  }

  async function searchCoords(lat, lon) {
    SkyCastUI.showLoading();
    try {
      const data = await SkyCastAPI.getByCoords(lat, lon);
      SkyCastUI.render(data, unit);
    } catch (err) {
      SkyCastUI.showError(err.message);
    }
  }

  function useMyLocation() {
    if (!("geolocation" in navigator)) {
      SkyCastUI.showError("Geolocation isn't supported by this browser.");
      return;
    }
    SkyCastUI.showLoading();
    navigator.geolocation.getCurrentPosition(
      (pos) => searchCoords(pos.coords.latitude, pos.coords.longitude),
      (geoErr) => {
        const messages = {
          1: "Location permission denied. Allow access or search by city.",
          2: "Your location is unavailable right now. Try searching by city.",
          3: "Getting your location timed out. Please try again.",
        };
        SkyCastUI.showError(
          messages[geoErr.code] || "Couldn't get your location."
        );
      },
      { enableHighAccuracy: false, timeout: 10000, maximumAge: 300000 }
    );
  }

  /* ---------- event wiring ---------- */

  el.form.addEventListener("submit", (e) => {
    e.preventDefault();
    searchCity(el.input.value);
  });

  el.geoBtn.addEventListener("click", useMyLocation);

  el.unitBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      const next = btn.dataset.unit;
      if (next === unit) return;
      unit = next;
      SkyCastUI.setActiveUnit(unit);
      // Re-render cached data instantly — no extra API call.
      if (SkyCastUI.hasData()) SkyCastUI.rerender(unit);
    });
  });

  /* ---------- startup ---------- */
  // A friendly default so the app isn't empty on first load.
  searchCity("London");
})();
