package agent

// OSHeader carries runtime.GOOS on the WebSocket handshake, read by
// backend/app/api/agent_ws.py.
const OSHeader = "X-PerchTail-Agent-OS"

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
}

// DirEntryWire mirrors what collectors/agent.py's list_directory expects
// from each entry in a list_result's "entries" array.
type DirEntryWire struct {
	Name  string `json:"name"`
	IsDir bool   `json:"is_dir"`
	Size  int64  `json:"size"`
}

// ListResult answers a "list" command.
type ListResult struct {
	Type    string         `json:"type"`
	ID      string         `json:"id"`
	Entries []DirEntryWire `json:"entries"`
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
