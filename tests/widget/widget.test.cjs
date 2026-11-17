// Unit tests for the widget's pure helpers: `node --test tests/widget/`
const test = require("node:test");
const assert = require("node:assert/strict");
const { parseSSE, escapeHtml, renderAnswer, pages } = require("../../src/corpus/web/static/widget.js");

test("parseSSE returns complete events and keeps the unfinished tail", () => {
  const { events, rest } = parseSSE(
    'event: sources\ndata: [{"number":1}]\n\nevent: token\ndata: {"text":"Hel"}\n\nevent: tok',
  );
  assert.deepEqual(events, [
    { event: "sources", data: [{ number: 1 }] },
    { event: "token", data: { text: "Hel" } },
  ]);
  assert.equal(rest, "event: tok");
});

test("parseSSE handles CRLF and chunk boundaries", () => {
  let buffer = "";
  const seen = [];
  for (const chunk of ['event: token\r\nda', 'ta: {"text":"a"}\r\n', "\r\n"]) {
    buffer += chunk;
    const parsed = parseSSE(buffer);
    buffer = parsed.rest;
    seen.push(...parsed.events);
  }
  assert.deepEqual(seen, [{ event: "token", data: { text: "a" } }]);
});

test("renderAnswer escapes HTML before formatting", () => {
  const html = renderAnswer('<img src=x onerror=alert(1)> **bold**', 0);
  assert.ok(!html.includes("<img"));
  assert.ok(html.includes("&lt;img"));
  assert.ok(html.includes("<strong>bold</strong>"));
});

test("renderAnswer links only citations that match a shown source", () => {
  const html = renderAnswer("Two years [1]. Invented [7].", 2);
  assert.ok(html.includes('data-source="1"'));
  assert.ok(!html.includes('data-source="7"'));
  assert.ok(html.includes("[7]"));
});

test("renderAnswer turns blank lines into paragraphs", () => {
  assert.equal(renderAnswer("a\n\nb\nc", 0), "<p>a</p><p>b<br>c</p>");
});

test("escapeHtml and pages", () => {
  assert.equal(escapeHtml(`"'&`), "&quot;&#39;&amp;");
  assert.equal(pages({ page_start: 3, page_end: 3 }), "p. 3");
  assert.equal(pages({ page_start: 3, page_end: 5 }), "p. 3-5");
});
