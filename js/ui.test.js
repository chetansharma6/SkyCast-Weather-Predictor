const { loadIndexDom } = require("./dom-fixture.js");

const CLEAR_DAY = {
  name: "London",
  sys: { country: "GB", sunrise: 1_700_000_000, sunset: 1_700_040_000 },
  weather: [{ main: "Clear", description: "clear sky", icon: "01d" }],
  main: { temp: 18, feels_like: 17, humidity: 60, pressure: 1012 },
  wind: { speed: 3.1 },
  visibility: 10000,
  timezone: 0,
};

const RAINY = {
  ...CLEAR_DAY,
  weather: [{ main: "Rain", description: "light rain", icon: "10n" }],
};

describe("SkyCastUI", () => {
  let SkyCastUI;

  beforeEach(async () => {
    // ui.js keeps rendered state (lastData) in a module-level closure, so
    // each test needs a truly fresh module instance, not a cached one.
    // vi.resetModules() only invalidates modules re-fetched via dynamic
    // import(), not plain require(), hence the dynamic import + cache-bust
    // query param here.
    vi.resetModules();
    loadIndexDom();
    SkyCastUI = (
      await import(/* @vite-ignore */ `../ui.js?t=${Date.now()}-${Math.random()}`)
    ).default;
  });

  it("renders temperature in Celsius by default", () => {
    SkyCastUI.render(CLEAR_DAY, "metric");
    expect(SkyCastUI.el.temp.textContent).toBe("18");
    expect(SkyCastUI.el.tempUnit.textContent).toBe("°C");
    expect(SkyCastUI.el.city.textContent).toBe("London, GB");
  });

  it("converts temperature to Fahrenheit for imperial units", () => {
    SkyCastUI.render(CLEAR_DAY, "imperial");
    expect(SkyCastUI.el.temp.textContent).toBe("64"); // 18C -> 64.4F, rounded
    expect(SkyCastUI.el.tempUnit.textContent).toBe("°F");
  });

  it("converts wind speed from m/s to km/h (metric) and mph (imperial)", () => {
    SkyCastUI.render(CLEAR_DAY, "metric");
    expect(SkyCastUI.el.wind.textContent).toBe("11.2 km/h");

    SkyCastUI.render(CLEAR_DAY, "imperial");
    expect(SkyCastUI.el.wind.textContent).toBe("6.9 mph");
  });

  it("formats visibility in km and handles a missing value", () => {
    SkyCastUI.render(CLEAR_DAY, "metric");
    expect(SkyCastUI.el.visibility.textContent).toBe("10.0 km");

    SkyCastUI.render({ ...CLEAR_DAY, visibility: undefined }, "metric");
    expect(SkyCastUI.el.visibility.textContent).toBe("—");
  });

  it("picks a weather-aware theme from the conditions", () => {
    SkyCastUI.render(CLEAR_DAY, "metric");
    expect(SkyCastUI.el.body.dataset.theme).toBe("clear-day");

    SkyCastUI.render(RAINY, "metric");
    expect(SkyCastUI.el.body.dataset.theme).toBe("rain");
  });

  it("shows the loading state and disables the action buttons", () => {
    SkyCastUI.showLoading();
    expect(SkyCastUI.el.loader.hidden).toBe(false);
    expect(SkyCastUI.el.weather.hidden).toBe(true);
    expect(SkyCastUI.el.searchBtn.disabled).toBe(true);
    expect(SkyCastUI.el.geoBtn.disabled).toBe(true);
  });

  it("shows an error message and re-enables the action buttons", () => {
    SkyCastUI.showError("City not found.");
    expect(SkyCastUI.el.error.hidden).toBe(false);
    expect(SkyCastUI.el.errorText.textContent).toBe("City not found.");
    expect(SkyCastUI.el.searchBtn.disabled).toBe(false);
    expect(SkyCastUI.el.geoBtn.disabled).toBe(false);
  });

  it("rerenders the last payload in a new unit without needing fresh data", () => {
    SkyCastUI.render(CLEAR_DAY, "metric");
    SkyCastUI.rerender("imperial");
    expect(SkyCastUI.el.temp.textContent).toBe("64");
  });

  it("reports whether a payload has been rendered yet", () => {
    expect(SkyCastUI.hasData()).toBe(false);
    SkyCastUI.render(CLEAR_DAY, "metric");
    expect(SkyCastUI.hasData()).toBe(true);
  });

  it("toggles the active state on the selected unit button only", () => {
    SkyCastUI.setActiveUnit("imperial");
    const metricBtn = SkyCastUI.el.unitBtns.find((b) => b.dataset.unit === "metric");
    const imperialBtn = SkyCastUI.el.unitBtns.find(
      (b) => b.dataset.unit === "imperial"
    );
    expect(imperialBtn.classList.contains("is-active")).toBe(true);
    expect(metricBtn.classList.contains("is-active")).toBe(false);
  });
});
