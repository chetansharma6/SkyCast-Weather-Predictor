const SkyCastAPI = require("../api.js");

function jsonResponse(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(payload),
  };
}

describe("SkyCastAPI", () => {
  beforeEach(() => {
    global.fetch = vi.fn();
  });

  it("requests the local proxy (never OpenWeather directly) with a city param", async () => {
    global.fetch.mockResolvedValue(jsonResponse(200, { name: "Paris" }));
    const data = await SkyCastAPI.getByCity("Paris");

    expect(data).toEqual({ name: "Paris" });
    const [url] = global.fetch.mock.calls[0];
    expect(url).toBe("/api/weather?city=Paris");
  });

  it("requests by lat/lon coordinates", async () => {
    global.fetch.mockResolvedValue(jsonResponse(200, { name: "London" }));
    await SkyCastAPI.getByCoords(51.5, -0.12);

    const [url] = global.fetch.mock.calls[0];
    expect(url).toBe("/api/weather?lat=51.5&lon=-0.12");
  });

  it("surfaces the server's error message on a non-ok response", async () => {
    global.fetch.mockResolvedValue(jsonResponse(404, { error: "City not found." }));
    await expect(SkyCastAPI.getByCity("Nowhereville")).rejects.toThrow(
      "City not found."
    );
  });

  it("falls back to a generic message when the error body has no message", async () => {
    global.fetch.mockResolvedValue(jsonResponse(500, {}));
    await expect(SkyCastAPI.getByCity("Paris")).rejects.toThrow(
      "Request failed (HTTP 500)."
    );
  });

  it("reports a friendly message when fetch itself rejects (offline/DNS/CORS)", async () => {
    global.fetch.mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(SkyCastAPI.getByCity("Paris")).rejects.toThrow(/network error/i);
  });

  it("reports a friendly message when the response body isn't valid JSON", async () => {
    global.fetch.mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.reject(new SyntaxError("Unexpected token")),
    });
    await expect(SkyCastAPI.getByCity("Paris")).rejects.toThrow(
      /unexpected response/i
    );
  });
});
