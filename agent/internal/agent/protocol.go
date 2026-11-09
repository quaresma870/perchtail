package agent

// OSHeader carries runtime.GOOS on the WebSocket handshake, read by
// backend/app/api/agent_ws.py.
const OSHeader = "X-PerchTail-Agent-OS"

// VersionHeader carries Version on the handshake (#215), so the server can
// show which agent version a source runs and warn when it's out of date.
const VersionHeader = "X-PerchTail-Agent-Version"

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

// ErrorResult reports a command failure. AgentRegistry.resolve_response
// (backend/app/agent_registry.py) treats any message whose Type ends in
// "_error" as a failure, regardless of which command it was answering.
// Code "too_large" (with Size) lets the backend answer 413, as it does
// for the other connectors.
type ErrorResult struct {
	Type  string `json:"type"`
	ID    string `json:"id"`
	Error string `json:"error"`
	Code  string `json:"code,omitempty"`
	Size  int64  `json:"size,omitempty"`
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
