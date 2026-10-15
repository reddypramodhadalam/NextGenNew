/**
 * lib/testDataImport.js
 *
 * Turns whatever a user drops into the "Import JSON" control for a
 * scenario's test data into a flat { key: string } object that
 * saveScenarioTestData() can write — even when the file is not perfectly
 * formed JSON.
 *
 * WHY THIS EXISTS: a reviewer importing test data is very likely pasting
 * something exported from elsewhere (a JDE screen scrape, a spreadsheet
 * export, a hand-edited config) rather than something written specifically
 * for this tool. Rejecting anything that is not already a flat, strict
 * JSON object of scalars means the "Import JSON" button only works for
 * files that were already typed by hand in the exact right shape — which
 * defeats the point of an import feature. This module repairs and
 * reshapes common near-misses instead of failing on them, and reports
 * exactly what it changed so nothing is silently altered without the
 * reviewer seeing it before Save.
 *
 * Handles, in order:
 *   1. Strict JSON.parse (the common, already-correct case).
 *   2. Common syntax repairs, then parse again:
 *        - byte-order-mark removal
 *        - // line comments and /* block comments *\/
 *        - trailing commas before } or ]
 *        - unquoted object keys        { ItemNumber: "1" } -> { "ItemNumber": "1" }
 *        - single-quoted strings       'ItemNumber': '1'   -> "ItemNumber": "1"
 *   3. key=value / key: value line format (one pair per line), for files
 *      that are not JSON at all.
 *   4. Shape normalisation on whatever parsed successfully:
 *        - array of [key, value] pairs           -> object
 *        - array of {key, value} / {name, value} objects -> object
 *        - array of plain record objects         -> merged object
 *          (later records win on a shared key)
 *        - nested objects as values              -> flattened up one or
 *          more levels, dropping the grouping key (test data is stored
 *          flat; a key actually referenced by a script, e.g. "ItemNumber",
 *          must exist at the top level to be found by get_data() at
 *          runtime — see service/grounded_runtime.py)
 *        - array values                          -> JSON-stringified,
 *          since scenario test data values are scalars
 *
 * Every adjustment made in step 4 is recorded in the returned `notes`
 * array so the UI can show the reviewer exactly what was changed before
 * they click Save — this function never silently reinterprets data
 * without that being visible.
 *
 * @param {string} rawText - The raw contents of the uploaded file.
 * @returns {{ content: Record<string, string>, notes: string[] }}
 * @throws {Error} if the text cannot be salvaged into any usable shape.
 */
export function normalizeImportedTestData(rawText) {
  const notes = []
  const text = String(rawText || '').replace(/^\uFEFF/, '') // strip BOM

  let parsed = tryStrictJson(text)
  if (parsed === undefined) {
    const repaired = repairJsonText(text)
    parsed = tryStrictJson(repaired)
    if (parsed !== undefined && repaired !== text) {
      notes.push('The file was not strictly valid JSON (comments, trailing commas, unquoted keys, or single quotes) — it was repaired automatically before import.')
    }
  }
  if (parsed === undefined) {
    parsed = tryKeyValueLines(text)
    if (parsed !== undefined) {
      notes.push('The file did not look like JSON at all — it was read as one key/value pair per line instead.')
    }
  }
  if (parsed === undefined) {
    throw new Error(
      'Could not read this file as test data. Expected JSON (an object of key/value pairs), ' +
      'or one "key=value" pair per line.'
    )
  }

  const { content, flattenNotes } = normalizeShape(parsed)
  notes.push(...flattenNotes)

  return { content, notes }
}

function tryStrictJson(text) {
  try {
    return JSON.parse(text)
  } catch {
    return undefined
  }
}

/** Best-effort repair of common near-JSON authoring mistakes. */
function repairJsonText(text) {
  let t = text

  // Strip // line comments (naive — does not special-case // inside
  // strings, which is an accepted limitation for this recovery path).
  t = t.replace(/(^|[^:])\/\/.*$/gm, '$1')
  // Strip /* block */ comments.
  t = t.replace(/\/\*[\s\S]*?\*\//g, '')
  // Remove trailing commas before } or ].
  t = t.replace(/,(\s*[}\]])/g, '$1')
  // Quote unquoted object keys: { ItemNumber: 1 } -> { "ItemNumber": 1 }
  t = t.replace(/([{,]\s*)([A-Za-z_$][A-Za-z0-9_$]*)\s*:/g, '$1"$2":')
  // Convert simple single-quoted strings to double-quoted. Skips values
  // that themselves contain a double quote, to avoid mangling those.
  t = t.replace(/'([^'"\\]*)'/g, '"$1"')

  return t
}

/** Fallback for files that are not JSON at all: one pair per line. */
function tryKeyValueLines(text) {
  const lines = text.split(/\r?\n/).map(l => l.trim()).filter(l => l && !l.startsWith('#'))
  if (lines.length === 0) return undefined
  const out = {}
  let matched = 0
  for (const line of lines) {
    const m = line.match(/^([^=:]+)[:=](.*)$/)
    if (!m) continue
    const key = m[1].trim()
    let value = m[2].trim()
    // Strip a single layer of surrounding quotes, if present.
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1)
    }
    if (key) { out[key] = value; matched += 1 }
  }
  return matched > 0 ? out : undefined
}

/**
 * Reshape whatever successfully parsed into a flat object of scalar
 * values, recording every structural change made along the way.
 */
function normalizeShape(parsed) {
  const flattenNotes = []

  if (Array.isArray(parsed)) {
    if (parsed.length === 0) return { content: {}, flattenNotes }

    if (parsed.every(row => Array.isArray(row) && row.length === 2)) {
      flattenNotes.push('The file was a list of [key, value] pairs — converted to an object.')
      const out = {}
      for (const [k, v] of parsed) out[String(k)] = v
      return { content: flattenScalars(out), flattenNotes }
    }

    const kvShape = parsed.every(row =>
      row && typeof row === 'object' &&
      ('key' in row || 'name' in row || 'field' in row) &&
      'value' in row
    )
    if (kvShape) {
      flattenNotes.push('The file was a list of {key, value} entries — converted to an object.')
      const out = {}
      for (const row of parsed) {
        const key = row.key ?? row.name ?? row.field
        out[String(key)] = row.value
      }
      return { content: flattenScalars(out), flattenNotes }
    }

    if (parsed.every(row => row && typeof row === 'object' && !Array.isArray(row))) {
      flattenNotes.push(
        `The file was a list of ${parsed.length} record(s) — merged into one set of key/value pairs ` +
        '(later records override earlier ones for a shared key).'
      )
      const merged = Object.assign({}, ...parsed)
      return { content: flattenScalars(merged), flattenNotes }
    }

    throw new Error('This looks like a JSON array, but not one this importer recognises (expected key/value pairs or records).')
  }

  if (parsed && typeof parsed === 'object') {
    return { content: flattenScalars(parsed), flattenNotes }
  }

  throw new Error('This file is valid JSON but is not an object or a list of entries.')

  /**
   * Recursively lift nested-object values to the top level, since
   * scenario test data is stored flat and get_data() looks a key up by
   * its bare name (service/grounded_runtime.py) — a key buried inside a
   * grouping object would never be found at runtime.
   */
  function flattenScalars(obj) {
    const out = {}
    const collisions = new Set()
    const walk = (o) => {
      for (const [k, v] of Object.entries(o || {})) {
        if (k.startsWith('_')) continue
        if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
          walk(v) // drop the grouping key, lift its children to top level
        } else {
          if (Object.prototype.hasOwnProperty.call(out, k)) collisions.add(k)
          out[k] = Array.isArray(v) ? JSON.stringify(v) : (v == null ? '' : String(v))
        }
      }
    }
    walk(obj)
    if (collisions.size > 0) {
      flattenNotes.push(
        `Nested grouping was flattened; ${collisions.size} key(s) appeared more than once ` +
        `(${Array.from(collisions).join(', ')}) — the last value seen for each was kept.`
      )
    }
    return out
  }
}
