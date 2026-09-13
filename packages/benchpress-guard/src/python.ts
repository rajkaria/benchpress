/**
 * Byte-for-byte reproductions of the Python behaviour the guard contract depends on:
 * `json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)`, `repr(str)` and `fnmatch.fnmatchcase`.
 */

/** A number that Python holds as a `float` (rendered `3.0`, not `3`). Used for receipt fields such as `latency_ms`. */
export class PyFloat {
  constructor(readonly value: number) {}
}

/** Python `json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)` with the default separators. */
export function pyJsonDumps(value: unknown): string {
  return dump(value);
}

function dump(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (value instanceof PyFloat) return pyNumber(value.value, true);
  switch (typeof value) {
    case "string":
      return pyJsonString(value);
    case "boolean":
      return value ? "true" : "false";
    case "number":
      return pyNumber(value, false);
    case "bigint":
      return value.toString();
    case "function":
    case "symbol":
      return pyJsonString(String(value));
    default:
      break;
  }
  if (Array.isArray(value)) {
    return "[" + value.map((item) => dump(item)).join(", ") + "]";
  }
  const withToJson = value as { toJSON?: () => unknown };
  if (typeof withToJson.toJSON === "function") return dump(withToJson.toJSON());
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record)
    .filter((key) => record[key] !== undefined)
    .sort(compareCodePoints);
  return "{" + keys.map((key) => pyJsonString(key) + ": " + dump(record[key])).join(", ") + "}";
}

/** Python sorts `str` keys by code point; JavaScript's default sort compares UTF-16 code units. */
export function compareCodePoints(a: string, b: string): number {
  const left = Array.from(a);
  const right = Array.from(b);
  const length = Math.min(left.length, right.length);
  for (let index = 0; index < length; index += 1) {
    const diff = (left[index]!.codePointAt(0) ?? 0) - (right[index]!.codePointAt(0) ?? 0);
    if (diff !== 0) return diff;
  }
  return left.length - right.length;
}

const JSON_ESCAPES: Record<string, string> = {
  '"': '\\"',
  "\\": "\\\\",
  "\b": "\\b",
  "\f": "\\f",
  "\n": "\\n",
  "\r": "\\r",
  "\t": "\\t",
};

function pyJsonString(text: string): string {
  // eslint-disable-next-line no-control-regex
  return '"' + text.replace(/[\u0000-\u001f"\\]/g, (char) => JSON_ESCAPES[char] ?? "\\u" + hex(char.charCodeAt(0), 4)) + '"';
}

function hex(code: number, width: number): string {
  return code.toString(16).padStart(width, "0");
}

/**
 * A JSON number as Python renders it. JavaScript cannot tell `2` from `2.0`: safe integers render as Python `int`,
 * every other finite number as Python `float` (`repr`), non-finite values as Python's `NaN` / `Infinity`.
 */
function pyNumber(value: number, forceFloat: boolean): string {
  if (Number.isNaN(value)) return "NaN";
  if (value === Infinity) return "Infinity";
  if (value === -Infinity) return "-Infinity";
  if (!forceFloat && Number.isSafeInteger(value)) return Object.is(value, -0) ? "0" : String(value);
  return pyFloatRepr(value);
}

/** Python `repr(float)`: shortest round-trip digits, exponent form when the decimal point is < -4 or > 16 places out. */
export function pyFloatRepr(value: number): string {
  if (value === 0) return Object.is(value, -0) ? "-0.0" : "0.0";
  const sign = value < 0 ? "-" : "";
  const [mantissa = "0", exponentText = "0"] = Math.abs(value).toExponential().split("e");
  const digits = mantissa.replace(".", "");
  const exponent = Number(exponentText);
  const point = exponent + 1;
  if (point <= -4 || point > 16) {
    const head = digits.length > 1 ? `${digits[0]}.${digits.slice(1)}` : digits;
    return `${sign}${head}e${exponent < 0 ? "-" : "+"}${String(Math.abs(exponent)).padStart(2, "0")}`;
  }
  if (point <= 0) return `${sign}0.${"0".repeat(-point)}${digits}`;
  if (point >= digits.length) return `${sign}${digits}${"0".repeat(point - digits.length)}.0`;
  return `${sign}${digits.slice(0, point)}.${digits.slice(point)}`;
}

const NON_PRINTABLE = /^[\p{C}\p{Z}]$/u;

/** Python `repr(str)`: single quotes unless the text holds `'` and no `"`; non-printable characters escaped. */
export function pyRepr(text: string): string {
  const quote = text.includes("'") && !text.includes('"') ? '"' : "'";
  let out = quote;
  for (const char of text) {
    const code = char.codePointAt(0) ?? 0;
    if (char === quote || char === "\\") out += "\\" + char;
    else if (char === "\t") out += "\\t";
    else if (char === "\n") out += "\\n";
    else if (char === "\r") out += "\\r";
    else if (char !== " " && (code < 0x20 || code === 0x7f || NON_PRINTABLE.test(char))) {
      if (code < 0x100) out += "\\x" + hex(code, 2);
      else if (code < 0x10000) out += "\\u" + hex(code, 4);
      else out += "\\U" + hex(code, 8);
    } else out += char;
  }
  return out + quote;
}

/** Python `json.dumps` of a non-string argument, the string itself otherwise (what argument regexes see). */
export function pyRender(value: unknown): string {
  return typeof value === "string" ? value : pyJsonDumps(value);
}

const globCache = new Map<string, RegExp>();

/** Python `fnmatch.fnmatchcase(name, pattern)`: `*`, `?`, `[seq]`, `[!seq]`, case-sensitive, whole name. */
export function fnmatchCase(name: string, pattern: string): boolean {
  let compiled = globCache.get(pattern);
  if (compiled === undefined) {
    compiled = new RegExp(`^(?:${translateGlob(pattern)})$`, "su");
    globCache.set(pattern, compiled);
  }
  return compiled.test(name);
}

function escapeRegExp(text: string): string {
  return text.replace(/[\\^$.*+?()[\]{}|/-]/g, "\\$&");
}

function translateGlob(pattern: string): string {
  const chars = Array.from(pattern);
  let out = "";
  let index = 0;
  while (index < chars.length) {
    const char = chars[index]!;
    index += 1;
    if (char === "*") {
      if (!out.endsWith(".*")) out += ".*";
    } else if (char === "?") {
      out += ".";
    } else if (char === "[") {
      let end = index;
      if (end < chars.length && chars[end] === "!") end += 1;
      if (end < chars.length && chars[end] === "]") end += 1;
      while (end < chars.length && chars[end] !== "]") end += 1;
      if (end >= chars.length) {
        out += "\\[";
      } else {
        let body = chars.slice(index, end);
        index = end + 1;
        let negate = false;
        if (body[0] === "!") {
          negate = true;
          body = body.slice(1);
        }
        if (body.length === 0) {
          out += negate ? "." : "(?!)";
          continue;
        }
        const set = body.map((c) => (c === "\\" || c === "]" || c === "[" || c === "^" ? "\\" + c : c)).join("");
        out += `[${negate ? "^" : ""}${set}]`;
      }
    } else {
      out += escapeRegExp(char);
    }
  }
  return out;
}

/**
 * Compile a Python `re` pattern for `re.fullmatch`. Python-only syntax with a direct JavaScript equivalent is
 * translated (`(?P<name>`, `(?P=name)`, `\A`, `\Z`, leading global flags `(?ims)`); anything JavaScript's `RegExp`
 * rejects throws.
 */
export function compilePythonFullmatch(pattern: string): RegExp {
  let source = pattern;
  let flags = "";
  const leading = /^\(\?([aiLmsux]+)\)/.exec(source);
  if (leading) {
    for (const flag of leading[1]!) {
      if (flag === "i" || flag === "m" || flag === "s") {
        if (!flags.includes(flag)) flags += flag;
      } else if (flag !== "u") {
        throw new Error(`inline flag (?${flag}) has no JavaScript equivalent`);
      }
    }
    source = source.slice(leading[0].length);
  }
  let translated = "";
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index]!;
    if (char === "\\") {
      const next = source[index + 1];
      if (next === "A") translated += "^";
      else if (next === "Z") translated += "$";
      else translated += char + (next ?? "");
      index += 1;
    } else if (source.startsWith("(?P<", index)) {
      translated += "(?<";
      index += 3;
    } else if (source.startsWith("(?P=", index)) {
      const close = source.indexOf(")", index);
      if (close === -1) throw new Error("unterminated (?P=name)");
      translated += `\\k<${source.slice(index + 4, close)}>`;
      index = close;
    } else {
      translated += char;
    }
  }
  return new RegExp(`^(?:${translated})$`, flags);
}
