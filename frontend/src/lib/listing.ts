// Folder and archive listings (#347): the server returns at most
// `listing_max_entries` entries and says when there were more; an
// archive's members come in one flat list the explorer keeps while the
// archive is open, instead of fetching the archive again for every folder
// clicked inside it.

import { api } from './api'
import type { BrowseEntry } from './types'

export interface Listing {
  entries: BrowseEntry[]
  /** The folder has more entries than these. */
  truncated: boolean
  /** The cap the server applied, when it said. */
  limit: number | null
}

export function readListing(entries: BrowseEntry[], headers: Headers): Listing {
  const limit = Number(headers.get('x-listing-limit'))
  return {
    entries,
    truncated: headers.get('x-listing-truncated') === '1',
    limit: headers.has('x-listing-limit') && Number.isFinite(limit) ? limit : null,
  }
}

export async function fetchListing(url: string): Promise<Listing> {
  const { data, headers } = await api.getWithHeaders<BrowseEntry[]>(url)
  return readListing(data, headers)
}

export function truncatedNotice(listing: Listing): string {
  const shown = (listing.limit ?? listing.entries.length).toLocaleString('en')
  return `Showing the first ${shown} entries; narrow with the search.`
}

/** The URL listing every member of an archive at once. */
export function archiveMembersUrl(sourceId: number, archivePath: string): string {
  return `/sources/${sourceId}/browse?path=${encodeURIComponent(archivePath)}&all=true`
}

function parentOf(path: string): string {
  const cut = path.lastIndexOf('/')
  return cut === -1 ? '' : path.slice(0, cut)
}

/** The direct children of `dirPath` (the archive's own path for its top
 * level, or a folder inside it) from an archive's flat member list. */
export function archiveChildren(members: BrowseEntry[], dirPath: string): BrowseEntry[] {
  return members.filter((member) => parentOf(member.path) === dirPath)
}
