package agent

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"
)

// manualTicks replaces the progress ticker with ticks sent by hand, for
// the duration of the test: tick sends one and waits until it was handled.
type manualTicks struct {
	ch      chan time.Time
	handled chan struct{}
	mu      sync.Mutex
	made    []time.Duration
	stopped bool
}

func useManualTicks(t *testing.T) *manualTicks {
	t.Helper()
	m := &manualTicks{ch: make(chan time.Time), handled: make(chan struct{})}
	previousHandled := progressTickHandled
	progressTickHandled = func() { m.handled <- struct{}{} }
	t.Cleanup(func() { progressTickHandled = previousHandled })
	previous := newProgressTicker
	newProgressTicker = func(d time.Duration) (<-chan time.Time, func()) {
		m.mu.Lock()
		defer m.mu.Unlock()
		m.made = append(m.made, d)
		return m.ch, func() {
			m.mu.Lock()
			defer m.mu.Unlock()
			m.stopped = true
		}
	}
	t.Cleanup(func() { newProgressTicker = previous })
	return m
}

func (m *manualTicks) tick(t *testing.T) {
	t.Helper()
	select {
	case m.ch <- time.Now():
	case <-time.After(5 * time.Second):
		t.Fatal("nobody took the tick")
	}
	select {
	case <-m.handled:
	case <-time.After(5 * time.Second):
		t.Fatal("the tick wasn't handled")
	}
}

// recorder collects what was sent, from any goroutine, and lets a test wait
// for the nth message.
type recorder struct {
	mu   sync.Mutex
	sent []any
	more chan struct{}
}

func newRecorder() *recorder { return &recorder{more: make(chan struct{}, 100)} }

func (r *recorder) send(msg any) error {
	r.mu.Lock()
	r.sent = append(r.sent, msg)
	r.mu.Unlock()
	r.more <- struct{}{}
	return nil
}

func (r *recorder) messages() []any {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]any(nil), r.sent...)
}

func (r *recorder) waitFor(t *testing.T, n int) []any {
	t.Helper()
	deadline := time.After(5 * time.Second)
	for {
		if got := r.messages(); len(got) >= n {
			return got
		}
		select {
		case <-r.more:
		case <-deadline:
			t.Fatalf("got %d messages, want %d: %+v", len(r.messages()), n, r.messages())
		}
	}
}

func isProgress(msg any, id string) bool {
	p, ok := msg.(Progress)
	return ok && p.Type == "progress" && p.ID == id
}

func TestProgressIsSentEveryTenSeconds(t *testing.T) {
	if progressInterval != 10*time.Second {
		t.Fatalf("progressInterval = %s, want 10s", progressInterval)
	}
}

// scriptedCommand runs withProgress on a command whose serve does, one step
// at a time, what the test asks: read some bytes of its file (counted by
// the readMeter in its ctx), send a chunk, or end with its answer. step
// returns once the step was done.
type scriptedCommand struct {
	steps chan string
	acks  chan struct{}
	done  chan error
}

func runScripted(t *testing.T, cmd Command, send func(any) error) *scriptedCommand {
	t.Helper()
	s := &scriptedCommand{steps: make(chan string), acks: make(chan struct{}), done: make(chan error, 1)}
	go func() {
		s.done <- withProgress(context.Background(), cmd, send, func(ctx context.Context, send func(any) error) error {
			meter := readMeterFrom(ctx)
			if meter == nil {
				return errors.New("no read meter in the command's context")
			}
			for step := range s.steps {
				switch step {
				case "read":
					meter.add(4096)
				case "chunk":
					meter.add(4096)
					if err := send(FetchChunk{Type: "fetch_chunk", ID: cmd.ID}); err != nil {
						return err
					}
				case "end":
					return send(FetchEnd{Type: "fetch_result", ID: cmd.ID, Streamed: true})
				}
				s.acks <- struct{}{}
			}
			return nil
		})
	}()
	return s
}

func (s *scriptedCommand) step(t *testing.T, step string) {
	t.Helper()
	s.steps <- step
	if step == "end" {
		return
	}
	select {
	case <-s.acks:
	case err := <-s.done:
		t.Fatalf("the command ended early: %v", err)
	}
}

func (s *scriptedCommand) wait(t *testing.T) {
	t.Helper()
	select {
	case err := <-s.done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the command didn't end")
	}
}

// A content command that reads for a long time before its first chunk, or
// between two, says it is still working on every tick during which it read
// more and sent nothing, and stops once it is answered (F5).
func TestACommandThatReadsSendsProgressUntilItEnds(t *testing.T) {
	for _, kind := range []string{"fetch", "read_range", "fetch_archive"} {
		t.Run(kind, func(t *testing.T) {
			ticks := useManualTicks(t)
			rec := newRecorder()
			s := runScripted(t, Command{Type: kind, ID: "c", Path: "big.tgz", Progress: true}, rec.send)

			// Reading up to the member asked for: every tick is a progress
			// message.
			for i := 1; i <= 3; i++ {
				s.step(t, "read")
				ticks.tick(t)
				if got := rec.waitFor(t, i); !isProgress(got[i-1], "c") {
					t.Fatalf("message %d = %+v, want progress", i, got[i-1])
				}
			}
			// A chunk went out since the last tick: no progress for this one.
			s.step(t, "chunk")
			ticks.tick(t)
			// Working between two chunks: progress again.
			s.step(t, "read")
			ticks.tick(t)
			got := rec.waitFor(t, 5)
			if _, ok := got[3].(FetchChunk); !ok || !isProgress(got[4], "c") {
				t.Fatalf("messages = %+v, want the chunk then one progress", got)
			}
			s.step(t, "end")
			s.wait(t)
			got = rec.messages()
			if len(got) != 6 {
				t.Fatalf("messages = %+v, want 6", got)
			}
			if _, ok := got[5].(FetchEnd); !ok {
				t.Fatalf("last message = %+v, want the answer", got[5])
			}
			ticks.mu.Lock()
			defer ticks.mu.Unlock()
			if !ticks.stopped || len(ticks.made) != 1 || ticks.made[0] != progressInterval {
				t.Fatalf("ticker made %v, stopped %v; want one of %s, stopped", ticks.made, ticks.stopped, progressInterval)
			}
			// Once answered, nobody takes ticks any more.
			select {
			case ticks.ch <- time.Now():
				t.Fatal("a tick was taken after the command ended")
			case <-time.After(50 * time.Millisecond):
			}
		})
	}
}

// A command whose read stops (a stuck disk or share) stops sending
// progress, so the server gives up on it after its wait, as without
// progress; once the read moves again, so does progress (Decisões,
// 2026-11-21, option b).
func TestACommandWhoseReadStopsSendsNoProgress(t *testing.T) {
	for _, kind := range []string{"fetch", "read_range", "fetch_archive", "list_archive"} {
		t.Run(kind, func(t *testing.T) {
			ticks := useManualTicks(t)
			rec := newRecorder()
			s := runScripted(t, Command{Type: kind, ID: "c", Path: "big.tgz", Progress: true}, rec.send)

			// Not one byte read yet: no progress.
			ticks.tick(t)
			s.step(t, "read")
			ticks.tick(t)
			// The read is stuck: no progress on any of these ticks.
			for range 5 {
				ticks.tick(t)
			}
			s.step(t, "read")
			ticks.tick(t)
			s.step(t, "end")
			s.wait(t)
			got := rec.messages()
			if len(got) != 3 || !isProgress(got[0], "c") || !isProgress(got[1], "c") {
				t.Fatalf("messages = %+v, want one progress for each tick after a read, then the answer", got)
			}
		})
	}
}

// Every content command counts what it reads of its file in the readMeter
// withProgress gives it, the archive included up to and through the member
// asked for, so its progress follows the read itself.
func TestContentCommandsCountWhatTheyRead(t *testing.T) {
	base := t.TempDir()
	filler := make([]byte, 256<<10)
	for i := range filler {
		filler[i] = byte('a' + i%26)
	}
	archive := tarGzOf(t, []tarMember{
		{name: "first.log", content: string(filler)},
		{name: "last.log", content: secretLog},
	})
	writeFile(t, base, "big.tgz", archive)
	writeFile(t, base, "big.zip", zipOf(t, []zipMember{{name: "first.log", content: string(filler)}, {name: "last.log", content: secretLog}}))
	writeFile(t, base, "app.log", []byte(secretLog+string(filler)))

	redactBlock := redactJSON(t, testRules, "")
	for _, tc := range []struct {
		name    string
		cmd     Command
		atLeast int64
	}{
		{"streamed fetch", Command{Type: "fetch", Path: "app.log", ChunkSize: 64 << 10}, int64(len(secretLog) + len(filler))},
		{"unstreamed fetch", Command{Type: "fetch", Path: "app.log"}, int64(len(secretLog) + len(filler))},
		{"redacted fetch", Command{Type: "fetch", Path: "app.log", ChunkSize: 64 << 10, Redact: redactBlock}, int64(len(secretLog) + len(filler))},
		{"tar.gz member", Command{Type: "fetch", Path: "big.tgz", Member: "last.log", ChunkSize: 64 << 10, Redact: redactBlock}, int64(len(archive))},
		{"zip member", Command{Type: "fetch", Path: "big.zip", Member: "last.log", ChunkSize: 64 << 10, Redact: redactBlock}, 1},
		{"fetch_archive", Command{Type: "fetch_archive", Path: "big.tgz", Members: []string{"last.log"}, ChunkSize: 64 << 10, Redact: redactBlock}, int64(len(archive))},
		{"read_range", Command{Type: "read_range", Path: "app.log", Offset: 0, Length: 4096, HeadLength: 64}, 4096},
		{"redacted read_range", Command{Type: "read_range", Path: "app.log", Offset: 0, Length: 4096, Redact: redactBlock}, 4096},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tc.cmd.ID = "c"
			meter := &readMeter{}
			ctx := context.WithValue(context.Background(), readMeterKey{}, meter)
			var last any
			if err := serveCommand(ctx, Config{BasePath: base}, tc.cmd, func(msg any) error { last = msg; return nil }); err != nil {
				t.Fatal(err)
			}
			if result, ok := last.(ErrorResult); ok {
				t.Fatalf("refused: %+v", result)
			}
			if got := meter.read(); got < tc.atLeast {
				t.Fatalf("counted %d bytes read, want at least %d", got, tc.atLeast)
			}
		})
	}
}

// Listing a large archive (list_archive, before a browse or a whole
// download) reads it to its end before its one answer: with progress asked
// for, every tick during which it read more says it is still working; a tick
// without reading says nothing (P2 of the 58bce44 preview retest).
func TestListArchiveSendsProgressWhileItReads(t *testing.T) {
	ticks := useManualTicks(t)
	rec := newRecorder()
	s := runScripted(t, Command{Type: "list_archive", ID: "l", Path: "big.tgz", Progress: true}, rec.send)

	s.step(t, "read")
	ticks.tick(t)
	s.step(t, "read")
	ticks.tick(t)
	// Stuck: nothing.
	ticks.tick(t)
	ticks.tick(t)
	s.step(t, "end")
	s.wait(t)
	got := rec.messages()
	if len(got) != 3 || !isProgress(got[0], "l") || !isProgress(got[1], "l") {
		t.Fatalf("messages = %+v, want two progress messages then the answer", got)
	}
}

// list_archive counts what it reads of the archive, a zip as a tar.gz, so
// its progress follows the listing.
func TestListArchiveCountsWhatItReads(t *testing.T) {
	base := t.TempDir()
	filler := make([]byte, 256<<10)
	for i := range filler {
		filler[i] = byte('a' + i%26)
	}
	archive := tarGzOf(t, []tarMember{{name: "first.log", content: string(filler)}, {name: "last.log", content: secretLog}})
	writeFile(t, base, "big.tgz", archive)
	writeFile(t, base, "big.zip", zipOf(t, []zipMember{{name: "first.log", content: string(filler)}, {name: "last.log", content: secretLog}}))
	for _, tc := range []struct {
		path    string
		atLeast int64
	}{{"big.tgz", int64(len(archive))}, {"big.zip", 1}} {
		t.Run(tc.path, func(t *testing.T) {
			meter := &readMeter{}
			ctx := context.WithValue(context.Background(), readMeterKey{}, meter)
			var last any
			cmd := Command{Type: "list_archive", ID: "l", Path: tc.path, Progress: true}
			if err := serveCommand(ctx, Config{BasePath: base}, cmd, func(msg any) error { last = msg; return nil }); err != nil {
				t.Fatal(err)
			}
			result, ok := last.(ListArchiveResult)
			if !ok || len(result.Members) != 2 {
				t.Fatalf("answer = %+v, want both members", last)
			}
			if got := meter.read(); got < tc.atLeast {
				t.Fatalf("counted %d bytes read, want at least %d", got, tc.atLeast)
			}
		})
	}
}

// Without the server asking (an older server, which would take a progress
// message for the answer), or for a command that isn't a content command,
// no progress is ever sent.
func TestNoProgressUnlessAskedForAContentCommand(t *testing.T) {
	for _, cmd := range []Command{
		{Type: "fetch", ID: "c"},
		{Type: "read_range", ID: "c"},
		{Type: "fetch_archive", ID: "c"},
		{Type: "list", ID: "c", Progress: true},
		{Type: "list_archive", ID: "c"},
	} {
		ticks := useManualTicks(t)
		rec := newRecorder()
		err := withProgress(context.Background(), cmd, rec.send, func(ctx context.Context, send func(any) error) error {
			if readMeterFrom(ctx) != nil {
				t.Errorf("%s (progress %v): reads are counted", cmd.Type, cmd.Progress)
			}
			return send(ErrorResult{Type: cmd.Type + "_error", ID: "c"})
		})
		if err != nil {
			t.Fatal(err)
		}
		if got := rec.messages(); len(got) != 1 {
			t.Errorf("%s (progress %v): sent %+v, want only the answer", cmd.Type, cmd.Progress, got)
		}
		if len(ticks.made) != 0 {
			t.Errorf("%s (progress %v): a ticker was started", cmd.Type, cmd.Progress)
		}
	}
}
