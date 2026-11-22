package agent

import (
	"context"
	"io"
	"io/fs"
	"os"
	"sync"
	"sync/atomic"
	"time"
)

// progressInterval is how often a content command asked for progress
// (Command.Progress, FeatureProgress) says it is still reading when it sent
// nothing else meanwhile: well within the server's 30 seconds of waiting for
// a command's next message (DEFAULT_TIMEOUT_SECONDS in
// backend/app/agent_registry.py), so a command that reads for a long time
// before its first chunk, or between two, isn't given up as unanswered.
const progressInterval = 10 * time.Second

// newProgressTicker returns the ticks progress is checked on and what stops
// them. A variable so tests can tick by hand.
var newProgressTicker = func(d time.Duration) (<-chan time.Time, func()) {
	ticker := time.NewTicker(d)
	return ticker.C, ticker.Stop
}

// progressTickHandled is called once a tick was handled. A variable so
// tests know when to look.
var progressTickHandled = func() {}

// wantsProgress says whether cmd is a content command the server asked
// progress for. Only a server that takes Progress messages asks: an older
// one would take one for the command's answer.
func wantsProgress(cmd Command) bool {
	if !cmd.Progress {
		return false
	}
	switch cmd.Type {
	case "fetch", "read_range", "fetch_archive":
		return true
	}
	return false
}

// readMeter counts the bytes a command read from the file it serves (the
// file itself, or the archive, up to and through the member asked for).
// Progress is only reported while it moves, so a read that hangs (a stuck
// disk or network share) still runs into the server's wait, as without
// progress. A nil readMeter counts nothing.
type readMeter struct{ n atomic.Int64 }

func (m *readMeter) add(n int) {
	if m != nil && n > 0 {
		m.n.Add(int64(n))
	}
}

func (m *readMeter) read() int64 { return m.n.Load() }

// contentFile is what the content commands read a file through: an
// *os.File, or one whose reads a readMeter counts (meteredFile).
type contentFile interface {
	io.Reader
	io.ReaderAt
	io.Seeker
	Stat() (fs.FileInfo, error)
}

// meteredFile counts every byte read from f, sequentially or at an offset.
type meteredFile struct {
	*os.File
	meter *readMeter
}

func (f meteredFile) Read(p []byte) (int, error) {
	n, err := f.File.Read(p)
	f.meter.add(n)
	return n, err
}

func (f meteredFile) ReadAt(p []byte, off int64) (int, error) {
	n, err := f.File.ReadAt(p, off)
	f.meter.add(n)
	return n, err
}

// meter returns f with its reads counted by m, or f as it is when m is nil.
func (m *readMeter) meter(f *os.File) contentFile {
	if m == nil {
		return f
	}
	return meteredFile{File: f, meter: m}
}

type readMeterKey struct{}

// readMeterFrom is the readMeter withProgress put in ctx, or nil.
func readMeterFrom(ctx context.Context) *readMeter {
	m, _ := ctx.Value(readMeterKey{}).(*readMeter)
	return m
}

// withProgress runs serve, which answers cmd through the send it is given.
// For a command that wants progress, serve gets a ctx carrying a readMeter
// (readMeterFrom), and a Progress message goes out on every tick of
// progressInterval during which the command read more of its file and sent
// nothing itself, until serve returns; then the ticks stop. A tick without
// reading sends nothing, so a read that is stuck times out on the server as
// it would without progress. A Progress message may cross the command's last
// answer, and the server ignores one for a command it no longer waits on. An
// error from sending a Progress message is left to the command's own writes,
// which fail as well on a broken connection.
func withProgress(
	ctx context.Context, cmd Command, send func(any) error,
	serve func(ctx context.Context, send func(any) error) error,
) error {
	if !wantsProgress(cmd) {
		return serve(ctx, send)
	}
	meter := &readMeter{}
	var sent atomic.Bool
	tracked := func(msg any) error {
		sent.Store(true)
		return send(msg)
	}
	ticks, stopTicks := newProgressTicker(progressInterval)
	stop := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		defer stopTicks()
		var seen int64
		for {
			select {
			case <-stop:
				return
			case <-ticks:
				read := meter.read()
				advanced := read != seen
				seen = read
				// No progress when the command itself said something since
				// the last tick, or read nothing more.
				if !sent.Swap(false) && advanced {
					_ = send(Progress{Type: "progress", ID: cmd.ID})
				}
				progressTickHandled()
			}
		}
	}()
	err := serve(context.WithValue(ctx, readMeterKey{}, meter), tracked)
	close(stop)
	wg.Wait()
	return err
}
