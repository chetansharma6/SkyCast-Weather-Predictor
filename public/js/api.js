/* ============================================================
   api.js — the data layer.

   The browser NEVER talks to OpenWeather directly and never
   sees the API key. It only calls our own backend proxy at
   /api/weather, which injects the secret key server-side.
   ============================================================ */

const SkyCastAPI = (() => {
  "use strict";

  /**
   * Low-level fetch against our proxy.
   * @param {Record<string,string>} params query params (city OR lat+lon)
   * @returns {Promise<object>} parsed OpenWeather payload
   * @throws {Error} with a human-friendly message on any failure
   */
  async function requestWeather(params) {
    const query = new URLSearchParams(params).toString();
    let response;

    try {
      response = await fetch(`/api/weather?${query}`);
    } catch (networkErr) {
      // fetch() only rejects on genuine network failure (offline, DNS, CORS…)
      throw new Error(
        "Network error — you appear to be offline. Check your connection."
      );
    }

    // Parse JSON defensively; the proxy always sends JSON, but be safe.
    let data;
    try {
      data = await response.json();
    } catch {
      throw new Error("Received an unexpected response from the server.");
    }

    if (!response.ok) {
      // Our proxy sends { error: "..." }; fall back to a generic message.
      throw new Error(data.error || `Request failed (HTTP ${response.status}).`);
    }

    return data;
  }

  /** Fetch current weather by city name. */
  function getByCity(city) {
    return requestWeather({ city });
  }

  /** Fetch current weather by geographic coordinates. */
  function getByCoords(lat, lon) {
    return requestWeather({ lat: String(lat), lon: String(lon) });
  }

  return { getByCity, getByCoords };
})();
