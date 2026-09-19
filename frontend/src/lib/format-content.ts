import type { FileLanguage } from './file-language'

export type FormatMode = 'none' | 'pretty' | 'minified'

/** Languages the "Beautify"/"Minify" toolbar actions support -- JS is
 * ROADMAP.md's explicitly lower-priority third case, deliberately deferred
 * (see ROADMAP.md's notes): a real JS beautifier needs an actual parser
 * (e.g. `prettier/standalone`), a meaningfully heavier dependency than the
 * couple of regex/DOM passes JSON and XML need, for a format this tool's
 * logs rarely embed compared to JSON/XML payloads. */
export function canFormat(language: FileLanguage): language is 'json' | 'xml' {
  return language === 'json' || language === 'xml'
}

function beautifyJson(text: string): string | null {
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return null
  }
}

function minifyJson(text: string): string | null {
  try {
    return JSON.stringify(JSON.parse(text))
  } catch {
    return null
  }
}

function isXmlWellFormed(text: string): boolean {
  try {
    const doc = new DOMParser().parseFromString(text, 'application/xml')
    return doc.getElementsByTagName('parsererror').length === 0
  } catch {
    return false
  }
}

/** True for a token that both opens and closes the same element on its own
 * (`<tag>text</tag>`, `<tag attr="x"></tag>`) -- these should print on one
 * line at the current depth rather than opening a new indent level that a
 * matching close token will never arrive to close (there isn't one; it's
 * already right there). */
function isOpenAndCloseSameToken(token: string): boolean {
  const match = /^<([a-zA-Z_][\w:.-]*)[^>]*>[\s\S]*<\/\1>$/.exec(token)
  return match !== null
}

function isDeclarationOrComment(token: string): boolean {
  return token.startsWith('<?') || (token.startsWith('<!--') && token.endsWith('-->'))
}

function isSelfClosing(token: string): boolean {
  return token.endsWith('/>')
}

function isClosingTag(token: string): boolean {
  return token.startsWith('</')
}

function isOpeningTag(token: string): boolean {
  return (
    token.startsWith('<') &&
    !isClosingTag(token) &&
    !isSelfClosing(token) &&
    !isDeclarationOrComment(token) &&
    !isOpenAndCloseSameToken(token)
  )
}

/** Simple indent-by-nesting-depth XML pretty-printer -- there's no
 * `document.formatXml` browser built-in, and pulling in a dedicated XML
 * formatting library for this one display-only toggle wasn't worth it.
 * Splits on tag boundaries (after stripping inter-tag whitespace) and
 * indents purely from each token's own shape (opening/closing/self-closing/
 * open-and-close-in-one), which is enough for the well-formed,
 * non-mixed-content XML this tool actually opens (config exports, SOAP
 * payloads, etc.) -- not a general-purpose XML formatter for arbitrary
 * mixed text/element content. */
function beautifyXml(text: string): string | null {
  if (!isXmlWellFormed(text)) return null

  const tokens = text
    .trim()
    .replace(/>\s+</g, '><')
    .split(/(?<=>)(?=<)/)
    .filter((t) => t.length > 0)

  let depth = 0
  const lines: string[] = []
  for (const token of tokens) {
    if (isClosingTag(token)) {
      depth = Math.max(depth - 1, 0)
      lines.push('  '.repeat(depth) + token)
    } else {
      lines.push('  '.repeat(depth) + token)
      if (isOpeningTag(token)) depth += 1
    }
  }
  return lines.join('\n')
}

function minifyXml(text: string): string | null {
  if (!isXmlWellFormed(text)) return null
  return text.trim().replace(/>\s+</g, '><')
}

/** Applies a display-only reformat (ROADMAP.md: "never touches the file on
 * disk") for the given language and mode. Returns null when the language
 * isn't supported or the content isn't valid for it (e.g. a `.json` file
 * that's actually corrupt/truncated) -- the caller falls back to the raw
 * content and surfaces that as a toolbar error rather than silently doing
 * nothing or crashing on a malformed parse. */
export function formatContent(content: string, language: FileLanguage, mode: FormatMode): string | null {
  if (mode === 'none') return content
  if (!canFormat(language)) return null

  if (language === 'json') {
    return mode === 'pretty' ? beautifyJson(content) : minifyJson(content)
  }
  return mode === 'pretty' ? beautifyXml(content) : minifyXml(content)
}
