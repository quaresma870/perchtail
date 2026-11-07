/** Up to two letters for the user menu's avatar (#298): the first letter of
 * the first two words of the name ("Ana Maria Costa" -> "AM", "joao.silva"
 * -> "JS"), or the first two letters of a single word ("admin" -> "AD"). */
export function userInitials(name: string): string {
  const words = name
    .trim()
    .split(/[\s._@-]+/)
    .filter((w) => /\p{L}|\p{N}/u.test(w))
  if (words.length === 0) return '?'
  const letters = (w: string) => Array.from(w).filter((c) => /\p{L}|\p{N}/u.test(c))
  if (words.length === 1) return letters(words[0]).slice(0, 2).join('').toUpperCase()
  return (letters(words[0])[0] + letters(words[1])[0]).toUpperCase()
}
