import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["js/__tests__/**/*.test.js"],
    // The suite files stay plain CommonJS (matching the browser scripts'
    // module.exports guard), so pull in describe/it/expect/vi as globals
    // instead of `require("vitest")`, which Vitest 4 refuses to allow.
    globals: true,
  },
});
