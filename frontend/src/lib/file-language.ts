export type FileLanguage = 'json' | 'xml' | 'javascript' | null

const EXTENSION_MAP: Record<string, FileLanguage> = {
  json: 'json',
  xml: 'xml',
  html: 'xml',
  htm: 'xml',
  svg: 'xml',
  js: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  jsx: 'javascript',
  ts: 'javascript',
  tsx: 'javascript',
}

/** Picks a syntax-highlighting language from a filename's extension --
 * pure and testable independently of CodeMirror, same "extract the pure
 * function" pattern as file-type detection elsewhere in this codebase.
 * `name` is the file's own display name (e.g. an archive member's name,
 * not the archive's), matching what FolderTree already emits on open.
 * Returns null for anything unrecognized -- most log files are plain
 * text, and this only ever adds highlighting on top, never required for
 * a file to open and display correctly. */
export function languageForFilename(name: string): FileLanguage {
  const match = /\.([a-zA-Z0-9]+)$/.exec(name)
  if (!match) return null
  return EXTENSION_MAP[match[1].toLowerCase()] ?? null
}
