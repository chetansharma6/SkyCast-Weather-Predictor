// Loads the real index.html markup into jsdom's document, so tests exercise
// the actual element IDs ui.js binds to instead of a hand-maintained copy
// that could silently drift out of sync with the markup.
const fs = require("fs");
const path = require("path");

const INDEX_HTML_PATH = path.join(__dirname, "..", "..", "index.html");

function loadIndexDom() {
  const html = fs.readFileSync(INDEX_HTML_PATH, "utf-8");
  const bodyMatch = html.match(/<body>([\s\S]*)<\/body>/);
  if (!bodyMatch) {
    throw new Error("Could not find <body> in index.html");
  }
  document.body.innerHTML = bodyMatch[1];
}

module.exports = { loadIndexDom };
