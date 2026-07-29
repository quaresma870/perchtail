package agent

// Command is a single instruction relayed from the backend down the
// persistent WebSocket connection — the wire format's other end is
// backend/app/agent_registry.py's send_command_sync and
// backend/app/collectors/agent.py.
type Command struct {
	Type string `json:"type"`
	ID   string `json:"id"`
	Path string `json:"path"`
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

// FetchResult answers a "fetch" command; content is base64-encoded, same
// wire trade-off collectors/winrm.py already makes for file transfer over
// PowerShell.
type FetchResult struct {
	Type       string `json:"type"`
	ID         string `json:"id"`
	ContentB64 string `json:"content_b64"`
}

// ErrorResult reports a command failure. AgentRegistry.resolve_response
// (backend/app/agent_registry.py) treats any message whose Type ends in
// "_error" as a failure, regardless of which command it was answering.
type ErrorResult struct {
	Type  string `json:"type"`
	ID    string `json:"id"`
	Error string `json:"error"`
}
