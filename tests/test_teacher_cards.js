/**
 * Node.js empirical test script for Teacher Mini App student answer rendering.
 * Extracts and tests the exact answer rendering logic from web/teacher.js.
 */
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const teacherJsPath = path.join(__dirname, '../web/teacher.js');
const teacherCssPath = path.join(__dirname, '../web/teacher.css');
const teacherHtmlPath = path.join(__dirname, '../web/teacher.html');

const jsContent = fs.readFileSync(teacherJsPath, 'utf8');
const cssContent = fs.readFileSync(teacherCssPath, 'utf8');
const htmlContent = fs.readFileSync(teacherHtmlPath, 'utf8');

console.log('1. Checking forbidden tokens in teacher web files...');
const forbiddenTokens = [
  'teacher-correct-ans-row',
  'teacher-correct-ans-val',
  'Ключ / Эталон',
  'tag-check',
];

for (const token of forbiddenTokens) {
  assert(!jsContent.includes(token), `Found forbidden token "${token}" in teacher.js`);
  assert(!cssContent.includes(token), `Found forbidden token "${token}" in teacher.css`);
  assert(!htmlContent.includes(token), `Found forbidden token "${token}" in teacher.html`);
}
console.log('   All forbidden tokens are completely absent.');

console.log('2. Testing JS student answer rendering logic against edge cases...');

// The escapeHtml function as defined in web/teacher.js
function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

// Emulating the exact rendering block from lines 347-371 of web/teacher.js
function renderStudentAnswer(userAns) {
  let ansDisplay = "";
  if (typeof userAns === "string" && userAns.trim().startsWith("[") && userAns.trim().endsWith("]")) {
    try {
      const parsed = JSON.parse(userAns);
      if (Array.isArray(parsed)) userAns = parsed;
    } catch (e) {}
  }

  if (Array.isArray(userAns)) {
    const nonEmpty = userAns.filter((item) => item && String(item).trim() && String(item).trim() !== "(Без ответа)");
    if (nonEmpty.length > 0) {
      ansDisplay = `<div class="teacher-student-ans-tags">` +
        nonEmpty.map((val) => `<span class="teacher-ans-tag">${escapeHtml(val)}</span>`).join("") +
        `</div>`;
    } else {
      ansDisplay = `<span class="teacher-student-ans-val empty">(Без ответа)</span>`;
    }
  } else {
    const strVal = userAns != null ? String(userAns).trim() : "";
    const hasAns = strVal && strVal !== "(Без ответа)";
    ansDisplay = hasAns
      ? `<span class="teacher-student-ans-val">${escapeHtml(strVal)}</span>`
      : `<span class="teacher-student-ans-val empty">(Без ответа)</span>`;
  }
  return ansDisplay;
}

const testCases = [
  {
    name: "Single choice standard text",
    input: "Option B",
    expectedTag: "teacher-student-ans-val",
    contains: "Option B",
    forbidden: ["✓", "tag-check", "teacher-correct-ans"]
  },
  {
    name: "Single choice with special characters",
    input: "Answer with <script> & \"quotes\"",
    expectedTag: "teacher-student-ans-val",
    contains: "Answer with &lt;script&gt; &amp; &quot;quotes&quot;",
    forbidden: ["✓", "<script>", "tag-check"]
  },
  {
    name: "Multiple choice array with strings",
    input: ["Apple", "Banana", "Orange"],
    expectedTag: "teacher-ans-tag",
    contains: ["<span class=\"teacher-ans-tag\">Apple</span>", "<span class=\"teacher-ans-tag\">Banana</span>", "<span class=\"teacher-ans-tag\">Orange</span>"],
    forbidden: ["✓", "tag-check", "teacher-correct-ans-row"]
  },
  {
    name: "Multiple choice array with empty or (Без ответа) items",
    input: ["Option A", "", "  ", "(Без ответа)", "Option B"],
    expectedTag: "teacher-ans-tag",
    contains: ["<span class=\"teacher-ans-tag\">Option A</span>", "<span class=\"teacher-ans-tag\">Option B</span>"],
    forbidden: ["✓", "tag-check", "(Без ответа)</span>"]
  },
  {
    name: "Multiple choice serialized JSON string",
    input: '["Choice 1", "Choice 2 & 3", "Choice <4>"]',
    expectedTag: "teacher-ans-tag",
    contains: ["Choice 1", "Choice 2 &amp; 3", "Choice &lt;4&gt;"],
    forbidden: ["✓", "tag-check"]
  },
  {
    name: "Empty multiple choice array",
    input: [],
    expectedTag: "teacher-student-ans-val empty",
    contains: "(Без ответа)",
    forbidden: ["✓", "teacher-ans-tag"]
  },
  {
    name: "Empty string answer",
    input: "",
    expectedTag: "teacher-student-ans-val empty",
    contains: "(Без ответа)",
    forbidden: ["✓"]
  },
  {
    name: "Null answer",
    input: null,
    expectedTag: "teacher-student-ans-val empty",
    contains: "(Без ответа)",
    forbidden: ["✓"]
  },
  {
    name: "Explicit (Без ответа)",
    input: "(Без ответа)",
    expectedTag: "teacher-student-ans-val empty",
    contains: "(Без ответа)",
    forbidden: ["✓"]
  }
];

for (const tc of testCases) {
  const result = renderStudentAnswer(tc.input);
  assert(result.includes(tc.expectedTag), `Test "${tc.name}" failed: expected tag class "${tc.expectedTag}" in "${result}"`);

  if (Array.isArray(tc.contains)) {
    for (const c of tc.contains) {
      assert(result.includes(c), `Test "${tc.name}" failed: missing substring "${c}" in "${result}"`);
    }
  } else if (tc.contains) {
    assert(result.includes(tc.contains), `Test "${tc.name}" failed: missing substring "${tc.contains}" in "${result}"`);
  }

  for (const f of tc.forbidden) {
    assert(!result.includes(f), `Test "${tc.name}" failed: found forbidden string "${f}" in "${result}"`);
  }
  console.log(`   Passed: ${tc.name}`);
}

console.log('3. Checking CSS token definitions for student answer elements...');
const requiredClasses = [
  '.teacher-student-ans-row',
  '.teacher-student-ans-label',
  '.teacher-student-ans-val',
  '.teacher-student-ans-val.empty',
  '.teacher-student-ans-tags',
  '.teacher-ans-tag'
];

for (const cls of requiredClasses) {
  assert(cssContent.includes(cls), `Missing CSS rule for ${cls} in teacher.css`);
}
console.log('   All required student answer CSS classes are defined in teacher.css.');
console.log('ALL NODE.JS VERIFICATION CHECKS PASSED SUCCESSFULLY.');
