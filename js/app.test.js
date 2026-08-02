// app.js is a plain script (no module system): it reads SkyCastAPI and
// SkyCastUI off the global object and wires DOM listeners immediately on
// load, exactly as it does in the browser via <script> tag order. Tests
// stand in for api.js/ui.js with mocks assigned to `global` before require.
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function makeBtn(unit) {
  const classes = new Set();
  return {
    dataset: { unit },
    classList: {
      toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
      contains: (name) => classes.has(name),
    },
    addEventListener: vi.fn(),
  };
}

describe("app.js controller", () => {
  let listeners;
  let mockApi;
  let mockUi;

  beforeEach(async () => {
    vi.resetModules();
    listeners = {};

    const form = { addEventListener: (_evt, handler) => (listeners.submit = handler) };
    const geoBtn = { addEventListener: (_evt, handler) => (listeners.geo = handler) };
    const retryBtn = {
      addEventListener: (_evt, handler) => (listeners.retry = handler),
    };
    const unitC = makeBtn("metric");
    const unitF = makeBtn("imperial");
    unitC.addEventListener = (_evt, handler) => (listeners.unitC = handler);
    unitF.addEventListener = (_evt, handler) => (listeners.unitF = handler);

    mockApi = { getByCity: vi.fn(), getByCoords: vi.fn() };
    mockUi = {
      el: { form, input: { value: "" }, geoBtn, retryBtn, unitBtns: [unitC, unitF] },
      showLoading: vi.fn(),
      showError: vi.fn(),
      render: vi.fn(),
      rerender: vi.fn(),
      setActiveUnit: vi.fn(),
      hasData: vi.fn(() => false),
    };

    global.SkyCastAPI = mockApi;
    global.SkyCastUI = mockUi;
    mockApi.getByCity.mockResolvedValue({ name: "London" });

    // app.js wires listeners as a side effect of being loaded, so — like
    // ui.js — it needs a genuinely fresh module each test. vi.resetModules()
    // only busts the cache for dynamic import(), not require(), hence the
    // cache-busting query param.
    await import(/* @vite-ignore */ `../app.js?t=${Date.now()}-${Math.random()}`);
    await flush(); // let the startup searchCity("London") settle
  });

  afterEach(() => {
    delete global.navigator.geolocation;
  });

  it("performs an initial search for London on startup", () => {
    expect(mockApi.getByCity).toHaveBeenCalledWith("London");
    expect(mockUi.render).toHaveBeenCalledWith({ name: "London" }, "metric");
  });

  it("shows an error instead of searching when the city field is blank", () => {
    mockApi.getByCity.mockClear();
    mockUi.el.input.value = "   ";
    listeners.submit({ preventDefault: vi.fn() });

    expect(mockUi.showError).toHaveBeenCalledWith(
      expect.stringMatching(/type a city/i)
    );
    expect(mockApi.getByCity).not.toHaveBeenCalled();
  });

  it("searches for the typed city on submit", async () => {
    mockApi.getByCity.mockResolvedValue({ name: "Tokyo" });
    mockUi.el.input.value = "  Tokyo  ";
    const preventDefault = vi.fn();
    listeners.submit({ preventDefault });
    await flush();

    expect(preventDefault).toHaveBeenCalled();
    expect(mockApi.getByCity).toHaveBeenCalledWith("Tokyo");
    expect(mockUi.render).toHaveBeenCalledWith({ name: "Tokyo" }, "metric");
  });

  it("shows the API's error message when the search fails", async () => {
    mockApi.getByCity.mockRejectedValue(new Error("City not found."));
    mockUi.el.input.value = "Nowhereville";
    listeners.submit({ preventDefault: vi.fn() });
    await flush();

    expect(mockUi.showError).toHaveBeenCalledWith("City not found.");
  });

  it("re-renders cached data on unit toggle without an extra network call", () => {
    mockApi.getByCity.mockClear();
    mockUi.hasData.mockReturnValue(true);

    listeners.unitF();

    expect(mockUi.setActiveUnit).toHaveBeenCalledWith("imperial");
    expect(mockUi.rerender).toHaveBeenCalledWith("imperial");
    expect(mockApi.getByCity).not.toHaveBeenCalled();
    expect(mockApi.getByCoords).not.toHaveBeenCalled();
  });

  it("ignores a click on the already-active unit button", () => {
    listeners.unitC(); // metric is active on load
    expect(mockUi.setActiveUnit).not.toHaveBeenCalled();
    expect(mockUi.rerender).not.toHaveBeenCalled();
  });

  it("shows a friendly error when geolocation isn't supported", () => {
    listeners.geo();
    expect(mockUi.showError).toHaveBeenCalledWith(
      expect.stringMatching(/geolocation/i)
    );
  });

  it("searches by coordinates when geolocation succeeds", async () => {
    Object.defineProperty(global.navigator, "geolocation", {
      value: {
        getCurrentPosition: (success) =>
          success({ coords: { latitude: 51.5, longitude: -0.12 } }),
      },
      configurable: true,
    });
    mockApi.getByCoords.mockResolvedValue({ name: "London" });

    listeners.geo();
    await flush();

    expect(mockApi.getByCoords).toHaveBeenCalledWith(51.5, -0.12);
  });

  it("shows a friendly error when geolocation permission is denied", () => {
    Object.defineProperty(global.navigator, "geolocation", {
      value: {
        getCurrentPosition: (_success, error) => error({ code: 1 }),
      },
      configurable: true,
    });

    listeners.geo();

    expect(mockUi.showError).toHaveBeenCalledWith(
      expect.stringMatching(/permission denied/i)
    );
  });

  it("retries the last city search when 'Try again' is clicked", async () => {
    mockApi.getByCity.mockRejectedValueOnce(new Error("Could not reach the weather service."));
    mockUi.el.input.value = "Berlin";
    listeners.submit({ preventDefault: vi.fn() });
    await flush();
    expect(mockUi.showError).toHaveBeenCalledWith(
      "Could not reach the weather service."
    );

    mockApi.getByCity.mockResolvedValueOnce({ name: "Berlin" });
    listeners.retry();
    await flush();

    expect(mockApi.getByCity).toHaveBeenLastCalledWith("Berlin");
    expect(mockUi.render).toHaveBeenLastCalledWith({ name: "Berlin" }, "metric");
  });

  it("retries the last geolocation lookup when 'Try again' is clicked", async () => {
    Object.defineProperty(global.navigator, "geolocation", {
      value: {
        getCurrentPosition: (success) =>
          success({ coords: { latitude: 51.5, longitude: -0.12 } }),
      },
      configurable: true,
    });
    mockApi.getByCoords.mockRejectedValueOnce(new Error("Network error."));
    listeners.geo();
    await flush();
    expect(mockUi.showError).toHaveBeenCalledWith("Network error.");

    mockApi.getByCoords.mockResolvedValueOnce({ name: "London" });
    listeners.retry();
    await flush();

    expect(mockApi.getByCoords).toHaveBeenLastCalledWith(51.5, -0.12);
  });
});
