package agent

import "encoding/json"

// OSHeader carries runtime.GOOS on the WebSocket handshake, read by
// backend/app/api/agent_ws.py.
const OSHeader = "X-PerchTail-Agent-OS"

// VersionHeader carries Version on the handshake (#215), so the server can
// show which agent version a source runs and warn when it's out of date.
const VersionHeader = "X-PerchTail-Agent-Version"

// FeaturesHeader lists, comma-separated, what this agent can do beyond the
// basic commands (#382, #249), so the server never relies on comparing
// versions (a local build reports "dev"). The server sends a `redact` block
// only to an agent that announced FeatureRedaction.
const FeaturesHeader = "X-PerchTail-Agent-Features"

// FeatureRedaction: the agent redacts on its host what it sends for a
// command with a `redact` block, with engine 1 of agent/internal/redact
// (the same behaviour as the server's), and lists and extracts archive
// members and .gz files itself (list_archive, fetch with member or
// decompress, fetch_archive), so the original never leaves the host.
const FeatureRedaction = "redaction/1"

// Version is set at build time by the Release workflow
// (-ldflags "-X github.com/quaresma870/perchtail/agent/internal/agent.Version=X.Y.Z").
// A local or CI build reports "dev", which the server doesn't compare.
var Version = "dev"

// Command is a single instruction relayed from the backend down the
// persistent WebSocket connection — the wire format's other end is
// backend/app/agent_registry.py's send_command_sync and
// backend/app/collectors/agent.py.
type Command struct {
	Type string `json:"type"`
	ID   string `json:"id"`
	Path string `json:"path"`
	// Set on "fetch" by backends from 0.5.3 on (#205): send the file in
	// fetch_chunk messages of at most ChunkSize bytes, then a FetchEnd,
	// instead of the whole file base64-encoded in one message. MaxBytes > 0
	// refuses a larger file with an ErrorResult coded "too_large". Older
	// backends leave both at 0 and get the single FetchResult they expect.
	ChunkSize int64 `json:"chunk_size,omitempty"`
	MaxBytes  int64 `json:"max_bytes,omitempty"`
	// Set on "read_range" (#254, backends from 0.6): read up to Length bytes
	// from Offset, and the file's first HeadLength bytes, which tell the
	// backend whether it is still the file the viewer has.
	Offset     int64 `json:"offset,omitempty"`
	Length     int64 `json:"length,omitempty"`
	HeadLength int64 `json:"head_length,omitempty"`
	// Set on "list" by backends from 0.7 (#347): list at most this many
	// entries (capped at MaxListEntries; 0 means MaxListEntries).
	MaxEntries int `json:"max_entries,omitempty"`
	// Set on "upgrade" (#319): the version to upgrade to, and nothing else.
	// Where the binary comes from is fixed in the agent (upgrade.go), never
	// taken from the server.
	Version string `json:"version,omitempty"`

	// Set on "fetch", "read_range" and "fetch_archive" when redaction
	// applies to the source (FeatureRedaction): the rules and options the
	// content is redacted with before it is sent (see redactBlock). Kept
	// raw and decoded by parseRedaction, with numbers as written, so the
	// rules are refused exactly as the server's engine refuses them. A
	// command without it is answered as before.
	Redact json.RawMessage `json:"redact,omitempty"`
	// Set on "fetch" (FeatureRedaction): send the archive member Member of
	// the .zip/.tar.gz at Path, and/or the content decompressed
	// (Decompress "gzip", for a .gz file or member), instead of the bytes
	// as stored. MaxDecompressed > 0 caps what decompression may produce
	// (a "too_large" error past it), as the server's MAX_DECOMPRESSED_MB.
	Member          string `json:"member,omitempty"`
	Decompress      string `json:"decompress,omitempty"`
	MaxDecompressed int64  `json:"max_decompressed,omitempty"`
	// Set on "fetch_archive": the members of the archive at Path to send,
	// by the names list_archive gave. The server picks them after applying
	// the visibility rules, which stay on the server.
	Members []string `json:"members,omitempty"`
	// Set on a "read_range" with a `redact` block, for the alerts: also say
	// where each redacted line was in the original (ChangedLines).
	WithLineOffsets bool `json:"with_line_offsets,omitempty"`
}

// DirEntryWire mirrors what collectors/agent.py's list_directory expects
// from each entry in a list_result's "entries" array.
type DirEntryWire struct {
	Name  string `json:"name"`
	IsDir bool   `json:"is_dir"`
	Size  int64  `json:"size"`
	// Last modification in Unix milliseconds (#273, agents from 0.6), for
	// sorting the explorer by time.
	MTimeMs int64 `json:"mtime_ms"`
}

// ListResult answers a "list" command. Truncated (#347) says the folder
// has more entries than were sent: the listing stopped at the entry or
// size cap (ListDirectory).
type ListResult struct {
	Type      string         `json:"type"`
	ID        string         `json:"id"`
	Entries   []DirEntryWire `json:"entries"`
	Truncated bool           `json:"truncated,omitempty"`
}

// FetchResult answers a "fetch" command from a backend that doesn't ask
// for chunks (older than 0.5.3); content is the whole file base64-encoded,
// same wire trade-off collectors/winrm.py makes for file transfer over
// PowerShell.
type FetchResult struct {
	Type       string `json:"type"`
	ID         string `json:"id"`
	ContentB64 string `json:"content_b64"`
}

// FetchChunk carries one piece of a streamed fetch (#205), Seq counting
// from 0, so the agent never holds more than one chunk of the file.
type FetchChunk struct {
	Type    string `json:"type"`
	ID      string `json:"id"`
	Seq     int    `json:"seq"`
	DataB64 string `json:"data_b64"`
}

// FetchEnd closes a streamed fetch: Chunks messages, Size bytes in all.
// Type is "fetch_result", so it resolves the command like any answer.
type FetchEnd struct {
	Type     string `json:"type"`
	ID       string `json:"id"`
	Streamed bool   `json:"streamed"`
	Chunks   int    `json:"chunks"`
	Size     int64  `json:"size"`
}

// RedactionInfo is what redacting a fetch gave (#382, #249). Redacted and
// Digest (of the rules applied, as the server's rules_digest) are what the
// server checks before it uses the content; SourceSize is how many bytes of
// the original were read, SourceOffset where a Tail goes on from (the start
// of an unfinished last line), TrailingPartial how many bytes at the end of
// the content stand for that line, Counts the values replaced by rule name,
// and HeadDigest identifies the file by its first bytes (HMAC with the
// block's head_key) without sending them.
type RedactionInfo struct {
	Redacted        bool           `json:"redacted"`
	Digest          string         `json:"digest"`
	SourceSize      int64          `json:"source_size"`
	SourceOffset    int64          `json:"source_offset"`
	TrailingPartial int            `json:"trailing_partial"`
	Counts          map[string]int `json:"counts"`
	HeadDigest      string         `json:"head_digest,omitempty"`
}

// RedactedFetchEnd closes a fetch or a fetch_archive sent with a `redact`
// block: Chunks and Size count the redacted bytes sent.
type RedactedFetchEnd struct {
	FetchEnd
	RedactionInfo
}

// ArchiveMemberWire is one member of an archive in a list_archive_result,
// as the server's app.archives.list_members gives it: the name as stored,
// the size and, when the archive records one, the last modification in
// Unix milliseconds. A tar's links, devices and FIFOs aren't listed.
type ArchiveMemberWire struct {
	Name    string `json:"name"`
	IsDir   bool   `json:"is_dir"`
	Size    int64  `json:"size"`
	MTimeMs int64  `json:"mtime_ms,omitempty"`
}

// ListArchiveResult answers a "list_archive" command, with the same caps
// as a ListResult (Truncated past them).
type ListArchiveResult struct {
	Type      string              `json:"type"`
	ID        string              `json:"id"`
	Members   []ArchiveMemberWire `json:"members"`
	Truncated bool                `json:"truncated,omitempty"`
}

// ArchiveMember starts one member of a fetch_archive: the fetch_chunk
// messages that follow, up to the next ArchiveMember or the closing
// FetchEnd, are its content. Seq counts the ArchiveMember and fetch_chunk
// messages of the command together, from 0. Gunzipped says the member was
// a .gz file sent decompressed (redacted, it can't be sent as stored).
// Skipped, with no content after it, says why a member asked for isn't
// sent: "not_found" (missing, a folder, or a link or device in a tar),
// "encrypted", "nested_archive" (an archive can't be redacted as it is),
// "unsupported_encoding" (UTF-32), "unsupported_format" (compressed in a
// format redaction doesn't decompress), "too_large" (over max_decompressed
// once decompressed) or "unreadable" (a corrupt member or .gz member). A
// member found too large or unreadable while it is sent is announced again,
// right after its content, as skipped: that withdraws what was sent of it.
type ArchiveMember struct {
	Type      string `json:"type"`
	ID        string `json:"id"`
	Seq       int    `json:"seq"`
	Name      string `json:"name"`
	MTimeMs   int64  `json:"mtime_ms,omitempty"`
	Gunzipped bool   `json:"gunzipped,omitempty"`
	Skipped   string `json:"skipped,omitempty"`
}

// ReadRangeResult answers a "read_range" command (#254): the file's size
// now, its first bytes, and the bytes read from the requested offset
// (empty at or past the end), base64-encoded.
type ReadRangeResult struct {
	Type    string `json:"type"`
	ID      string `json:"id"`
	Size    int64  `json:"size"`
	HeadB64 string `json:"head_b64"`
	DataB64 string `json:"data_b64"`
}

// RedactedReadRangeResult answers a "read_range" command sent with a
// `redact` block (#382, #249), as the server's redacted Tail does: the
// read starts up to followContextBytes before Offset (read only to know
// whether Offset falls inside a private key, never sent), only whole lines
// are redacted and sent, and the first bytes of the file are never sent,
// only their digest.
//
// DataB64 is the redacted content, at most maxRangeLength bytes.
// NextOffset is where the next poll starts, in bytes of the original;
// ReadEnd where the read stopped (more is waiting when it is below Size).
// HeadDigest and NextHeadDigest identify the file by its first
// min(64, Offset) and min(64, NextOffset) bytes. Aligned says the content
// starts at the line after Offset (Offset wasn't the start of a line);
// TrailingPartial is how many bytes at its end stand for an unfinished
// last line, which the next poll sends again.
//
// ChangedLines, only when the command asked WithLineOffsets (the alerts),
// maps the redacted whole lines (those before the trailing partial) back to
// the original: [index, original length] for each line whose length, newline
// included, isn't the original's (a value replaced, UTF-16 sent as UTF-8, a
// line with no end skipped), in order; every other line is as long as its
// original. The lines are the original's from NextOffset back, so the
// server knows where each one starts in the file.
type RedactedReadRangeResult struct {
	Type            string         `json:"type"`
	ID              string         `json:"id"`
	Size            int64          `json:"size"`
	DataB64         string         `json:"data_b64"`
	Redacted        bool           `json:"redacted"`
	Digest          string         `json:"digest"`
	NextOffset      int64          `json:"next_offset"`
	ReadEnd         int64          `json:"read_end"`
	HeadDigest      string         `json:"head_digest"`
	NextHeadDigest  string         `json:"next_head_digest"`
	Counts          map[string]int `json:"counts"`
	TrailingPartial int            `json:"trailing_partial"`
	Aligned         bool           `json:"aligned"`
	ChangedLines    *[][2]int64    `json:"changed_lines,omitempty"`
}

// ErrorResult reports a command failure. AgentRegistry.resolve_response
// (backend/app/agent_registry.py) treats any message whose Type ends in
// "_error" as a failure, regardless of which command it was answering.
// Code "too_large" (with Size) lets the backend answer 413, as it does
// for the other connectors; "not_found" an archive member that isn't
// there. A command with a `redact` block that can't be redacted is
// answered with Type "redaction_error" before anything else is sent (see
// redactionError for its codes); Rule is then the id of the rule that
// can't run, when one can't.
type ErrorResult struct {
	Type  string `json:"type"`
	ID    string `json:"id"`
	Error string `json:"error"`
	Code  string `json:"code,omitempty"`
	Size  int64  `json:"size,omitempty"`
	Rule  any    `json:"rule,omitempty"`
}

// Pong answers the server's application-level "ping" (#333), which it
// sends so it can tell a dead connection from an idle one. Older agents
// answer "ping" with an unknown_error, which proves they're alive as well.
type Pong struct {
	Type string `json:"type"`
	ID   string `json:"id"`
}

// UpgradeAccepted answers an "upgrade" command (#319) the agent will carry
// out: the host allows it, the build has a signing key and To is newer than
// From. A refusal is an ErrorResult of type "upgrade_error" with Code
// "refused". What follows is reported with UpgradeProgress messages and, if
// it fails, an UpgradeFailed; a successful upgrade ends with the agent
// reconnecting with the new version.
type UpgradeAccepted struct {
	Type string `json:"type"`
	ID   string `json:"id"`
	From string `json:"from"`
	To   string `json:"to"`
}

// UpgradeProgress reports a stage of an accepted upgrade: "downloading",
// "verifying", "installing" or "restarting".
type UpgradeProgress struct {
	Type   string `json:"type"`
	ID     string `json:"id"`
	Stage  string `json:"stage"`
	Detail string `json:"detail,omitempty"`
}

// UpgradeFailed ends an accepted upgrade that didn't happen; the agent
// keeps running its current version. Not "_error": the command was
// already answered by UpgradeAccepted.
type UpgradeFailed struct {
	Type  string `json:"type"`
	ID    string `json:"id"`
	Error string `json:"error"`
}
