// Folder and archive listings (#347): the server returns at most
// `listing_max_entries` entries and says when there were more; an
// archive's members come in one flat list the explorer keeps while the
// archive is open, instead of fetching the archive again for every folder
// clicked inside it.

import { api } from './api'
import type { BrowseEntry } from './types'

/** Who cut a truncated listing when the cap didn't (X-Listing-Cut-By):
 * the archive's reader, or the source (an agent stops at 50,000 entries). */
export type ListingCutBy = 'archive' | 'source'

export interface Listing {
  entries: BrowseEntry[]
  /** The folder has more entries than these. */
  truncated: boolean
  /** The cap the server applied, when it said. */
  limit: number | null
  /** Null when the cap cut it, or nothing did. */
  cutBy: ListingCutBy | null
}

export function readListing(entries: BrowseEntry[], headers: Headers): Listing {
  const limit = Number(headers.get('x-listing-limit'))
  const cutBy = headers.get('x-listing-cut-by')
  return {
    entries,
    truncated: headers.get('x-listing-truncated') === '1',
    limit: headers.has('x-listing-limit') && Number.isFinite(limit) ? limit : null,
    cutBy: cutBy === 'archive' || cutBy === 'source' ? cutBy : null,
  }
}

export async function fetchListing(url: string): Promise<Listing> {
  const { data, headers } = await api.getWithHeaders<BrowseEntry[]>(url)
  return readListing(data, headers)
}

/** What the explorer says under a truncated listing: the server says who
 * cut it when the cap didn't (X-Listing-Cut-By), an archive's reader or
 * the source, and then what's shown is what they read, not the cap. */
export function truncatedNotice(listing: Listing): string {
  const shownCount = listing.entries.length.toLocaleString('en')
  if (listing.cutBy === 'archive') {
    return `This archive has more entries than were read; showing the ${shownCount} read.`
  }
  if (listing.cutBy === 'source') {
    return `The source listed only part of this folder; showing the ${shownCount} entries it listed.`
  }
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
