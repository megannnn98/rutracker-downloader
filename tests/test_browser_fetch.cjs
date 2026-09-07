const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

test("download URLs stay alive until the delayed cleanup", async () => {
  const blobs = new Map();
  const downloads = [];
  const cleanup = [];
  const revoked = [];
  class TestURL extends URL {
    static createObjectURL(blob) {
      const url = `blob:test-${blobs.size}`;
      blobs.set(url, blob);
      return url;
    }
    static revokeObjectURL(url) {
      revoked.push(url);
    }
  }
  const script = fs.readFileSync(
    path.join(__dirname, "../scripts/browser_fetch.js"), "utf8",
  );
  await vm.runInNewContext(script, {
    location: new URL("https://rutracker.net/forum/tracker.php?nm=book"),
    URL: TestURL,
    Blob,
    TextDecoder,
    Uint8Array,
    console: { log() {}, warn() {}, error(message) { assert.fail(message); } },
    document: {
      createElement() {
        return { click() { downloads.push({ url: this.href, name: this.download }); } };
      },
    },
    DOMParser: class {
      parseFromString() {
        return {
          querySelectorAll(selector) {
            return selector.includes("dl.php")
              ? [{ getAttribute: () => "dl.php?t=123" }]
              : [];
          },
        };
      }
    },
    fetch: async () => ({ ok: true, arrayBuffer: async () => new ArrayBuffer(4) }),
    setTimeout(callback, delay) {
      if (delay === 1000) callback();
      else cleanup.push({ callback, delay });
    },
  });
  assert.deepEqual(downloads.map((item) => item.name), ["rutracker-page-01.html", "123.torrent"]);
  assert.deepEqual(revoked, []);
  assert.equal(cleanup.length, 2);
  for (const timer of cleanup) {
    assert.equal(timer.delay, 60_000);
    timer.callback();
  }
  assert.deepEqual(revoked, downloads.map((item) => item.url));
  assert.ok([...blobs.values()].every((blob) => blob.size === 4));
});
