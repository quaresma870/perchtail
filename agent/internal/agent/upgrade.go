package agent

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/x509"
	_ "embed" // the release signing key below
	"encoding/hex"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// Upgrades from the portal (#319). The server only ever says which version
// to upgrade to; everything else is fixed here, so a server an attacker
// controls can't make the agent run anything but a genuine, newer
// PerchTail release:
//   - the binary comes from the PerchTail server's own copy of the release
//     (/agent/binaries/ next to the agent's connect URL), or from the
//     release on GitHub, never from a URL the server names;
//   - SHA256SUMS must carry a valid Ed25519 signature from the release key
//     embedded below, and the binary must match its line in it;
//   - the version must be newer than the agent's own;
//   - the host can refuse every upgrade (PERCHTAIL_REMOTE_UPGRADE=false).

// upgradeSigningKeyPEM is the release signing key's public half (PEM,
// SubjectPublicKeyInfo), embedded at build time. Empty in a build without
// it, which then refuses every upgrade from the portal. See CONTRIBUTING.md
// for how the maintainer creates the key pair.
//
//go:embed upgrade_signing_key.pem
var upgradeSigningKeyPEM []byte

// githubReleaseBase is where a release's assets are on GitHub; %s is the
// version, without the "v" of the tag.
const githubReleaseBase = "https://github.com/quaresma870/perchtail/releases/download/v%s/"

const (
	sumsFile = "SHA256SUMS"
	sigFile  = "SHA256SUMS.sig"
	// Caps on what is downloaded: SHA256SUMS has a handful of lines, the
	// signature is 64 bytes and the binaries are around 10 MB.
	maxSumsBytes   = 64 << 10
	maxSigBytes    = 4 << 10
	maxBinaryBytes = 256 << 20
	// The longest one download may take.
	upgradeHTTPTimeout = 10 * time.Minute
)

// ExitCodeRestart is the exit code of an agent that installed an upgrade
// and needs its service manager to start it again (Windows, where a
// process can't replace itself). Distinct from 1, a fatal error.
const ExitCodeRestart = 3

// ErrRestart is returned by Run once an upgrade was installed and the agent
// must exit for its service manager to start the new binary.
var ErrRestart = errors.New("upgrade installed; exiting so the service manager starts the new binary")

var errNoSigningKey = errors.New(
	"this agent was built without the release signing key, so it can't verify an upgrade; upgrade it by hand")

// binarySource is one place the release's files can be downloaded from:
// base + file name.
type binarySource struct {
	label string
	base  string
	token string // sent as a bearer token; only to the PerchTail server
}

type upgrader struct {
	current       string
	goos, goarch  string
	allowed       bool
	signingKeyPEM []byte
	sources       func(version string) []binarySource
	client        *http.Client
	maxBinary     int64
	executable    func() (string, error)
	// exec restarts the agent in place with the new binary; it only returns
	// on failure. Nil on Windows: the agent exits with ExitCodeRestart.
	exec func(exe string) error

	running     atomic.Bool
	restartOnce sync.Once
	restartCh   chan struct{}
}

func newUpgrader(cfg Config) *upgrader {
	serverBase := serverBinariesURL(cfg.ServerURL)
	u := &upgrader{
		current:       Version,
		goos:          runtime.GOOS,
		goarch:        runtime.GOARCH,
		allowed:       cfg.RemoteUpgrade,
		signingKeyPEM: upgradeSigningKeyPEM,
		client:        &http.Client{Timeout: upgradeHTTPTimeout},
		maxBinary:     maxBinaryBytes,
		executable:    currentExecutable,
		restartCh:     make(chan struct{}),
	}
	if restartByExec {
		u.exec = execSelf
	}
	u.sources = func(version string) []binarySource {
		var sources []binarySource
		if serverBase != "" {
			sources = append(sources, binarySource{label: "the PerchTail server", base: serverBase, token: cfg.Token})
		}
		return append(sources, binarySource{label: "GitHub", base: fmt.Sprintf(githubReleaseBase, version)})
	}
	return u
}

// serverBinariesURL is where the PerchTail server serves the agent binaries
// bundled with it: /agent/binaries/ next to the agent's connect URL, over
// HTTPS for wss:// (HTTP only for a ws:// URL the host allowed). Empty if
// the URL can't be read.
func serverBinariesURL(serverURL string) string {
	u, err := url.Parse(serverURL)
	if err != nil || u.Host == "" {
		return ""
	}
	switch strings.ToLower(u.Scheme) {
	case "wss":
		u.Scheme = "https"
	case "ws":
		u.Scheme = "http"
	default:
		return ""
	}
	p := path.Join(path.Dir(u.Path), "binaries") + "/"
	if !strings.HasPrefix(p, "/") {
		p = "/" + p
	}
	u.Path, u.RawPath, u.RawQuery, u.Fragment, u.User = p, "", "", "", nil
	return u.String()
}

func currentExecutable() (string, error) {
	exe, err := os.Executable()
	if err != nil {
		return "", err
	}
	return filepath.EvalSymlinks(exe)
}

// binaryName is the release asset for one platform, as the release
// workflow names it.
func binaryName(version, goos, goarch string) string {
	name := fmt.Sprintf("perchtail-agent-%s-%s-%s", version, goos, goarch)
	if goos == "windows" {
		name += ".exe"
	}
	return name
}

// parseSigningKey reads the embedded release key (PEM SubjectPublicKeyInfo).
func parseSigningKey(pemBytes []byte) (ed25519.PublicKey, error) {
	if len(bytes.TrimSpace(pemBytes)) == 0 {
		return nil, errNoSigningKey
	}
	block, _ := pem.Decode(pemBytes)
	if block == nil || block.Type != "PUBLIC KEY" {
		return nil, errors.New("the embedded release signing key is not a PEM public key")
	}
	key, err := x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("the embedded release signing key can't be read: %w", err)
	}
	edKey, ok := key.(ed25519.PublicKey)
	if !ok {
		return nil, errors.New("the embedded release signing key is not an Ed25519 key")
	}
	return edKey, nil
}

// verifySums checks the release key's signature over SHA256SUMS.
func verifySums(sums, sig []byte, key ed25519.PublicKey) error {
	if len(sig) != ed25519.SignatureSize {
		return errors.New("SHA256SUMS.sig is not an Ed25519 signature")
	}
	if !ed25519.Verify(key, sums, sig) {
		return errors.New("the signature over SHA256SUMS doesn't verify: it wasn't signed with the PerchTail release key")
	}
	return nil
}

// expectedHash is name's SHA-256 from SHA256SUMS (sha256sum's format).
func expectedHash(sums []byte, name string) ([]byte, error) {
	var found []byte
	for _, line := range strings.Split(string(sums), "\n") {
		fields := strings.Fields(line)
		if len(fields) != 2 || strings.TrimPrefix(fields[1], "*") != name {
			continue
		}
		if found != nil {
			return nil, fmt.Errorf("SHA256SUMS lists %s more than once", name)
		}
		sum, err := hex.DecodeString(fields[0])
		if err != nil || len(sum) != sha256.Size {
			return nil, fmt.Errorf("SHA256SUMS has a malformed line for %s", name)
		}
		found = sum
	}
	if found == nil {
		return nil, fmt.Errorf("SHA256SUMS has no line for %s", name)
	}
	return found, nil
}

// logSafe keeps a message on one log line: text that came from the server
// or the network can't start a forged one.
func logSafe(s string) string {
	s = strings.ReplaceAll(s, "\n", " ")
	return strings.ReplaceAll(s, "\r", " ")
}

// precheck refuses an upgrade the agent won't make, before anything is
// downloaded. It returns the target version rebuilt by canonicalVersion,
// the only form used from then on.
func (u *upgrader) precheck(requested string) (string, error) {
	if !u.allowed {
		return "", fmt.Errorf("this host doesn't allow upgrades from the portal (%s=false)", remoteUpgradeEnv)
	}
	if _, err := parseSigningKey(u.signingKeyPEM); err != nil {
		return "", err
	}
	target, ok := canonicalVersion(requested)
	if !ok {
		return "", errors.New("the server asked for a version that isn't a PerchTail release")
	}
	current, ok := canonicalVersion(u.current)
	if !ok {
		return "", fmt.Errorf("this agent's version is not a release, so it can't tell whether %s is newer; upgrade it by hand", target)
	}
	if !isNewerVersion(target, current) {
		return "", fmt.Errorf("%s is not newer than this agent's %s; agents only upgrade to a newer release", target, current)
	}
	return target, nil
}

func (u *upgrader) get(ctx context.Context, src binarySource, file string) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, src.base+file, nil)
	if err != nil {
		return nil, err
	}
	if src.token != "" {
		req.Header.Set("Authorization", "Bearer "+src.token)
	}
	req.Header.Set("User-Agent", "perchtail-agent/"+u.current)
	resp, err := u.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("download %s: %w", file, err)
	}
	if resp.StatusCode != http.StatusOK {
		_ = resp.Body.Close()
		return nil, fmt.Errorf("download %s: %s", file, resp.Status)
	}
	return resp, nil
}

func (u *upgrader) getSmall(ctx context.Context, src binarySource, file string, limit int) ([]byte, error) {
	resp, err := u.get(ctx, src, file)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, int64(limit)+1))
	if err != nil {
		return nil, fmt.Errorf("download %s: %w", file, err)
	}
	if len(data) > limit {
		return nil, fmt.Errorf("%s is larger than %d bytes", file, limit)
	}
	return data, nil
}

// downloadBinary writes the binary to a new file in dir, next to the
// running one so the swap is a rename on the same file system, checking
// its SHA-256 as it arrives. The file is removed unless it matches.
func (u *upgrader) downloadBinary(ctx context.Context, src binarySource, name, dir string, want []byte) (string, error) {
	resp, err := u.get(ctx, src, name)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.ContentLength > u.maxBinary {
		return "", fmt.Errorf("%s is larger than %d bytes", name, u.maxBinary)
	}
	tmp, err := os.CreateTemp(dir, ".perchtail-agent-upgrade-*")
	if err != nil {
		return "", fmt.Errorf("can't write next to the agent's binary in %s: %w", dir, err)
	}
	keep := false
	defer func() {
		if !keep {
			_ = tmp.Close()
			_ = os.Remove(tmp.Name())
		}
	}()
	hash := sha256.New()
	n, err := io.Copy(io.MultiWriter(tmp, hash), io.LimitReader(resp.Body, u.maxBinary+1))
	if err != nil {
		return "", fmt.Errorf("download %s: %w", name, err)
	}
	if n > u.maxBinary {
		return "", fmt.Errorf("%s is larger than %d bytes", name, u.maxBinary)
	}
	if !bytes.Equal(hash.Sum(nil), want) {
		return "", fmt.Errorf("%s doesn't match its SHA-256 in the signed SHA256SUMS", name)
	}
	if err := tmp.Chmod(0o755); err != nil {
		return "", fmt.Errorf("make %s executable: %w", name, err)
	}
	if err := tmp.Sync(); err != nil {
		return "", fmt.Errorf("write %s: %w", name, err)
	}
	if err := tmp.Close(); err != nil {
		return "", fmt.Errorf("write %s: %w", name, err)
	}
	keep = true
	return tmp.Name(), nil
}

// fetchVerified downloads the release's SHA256SUMS and its signature from
// src, verifies the signature, then downloads this platform's binary into
// dir and checks it against its line. It returns the new binary's path.
func (u *upgrader) fetchVerified(ctx context.Context, src binarySource, target, dir string, key ed25519.PublicKey) (string, error) {
	sums, err := u.getSmall(ctx, src, sumsFile, maxSumsBytes)
	if err != nil {
		return "", err
	}
	sig, err := u.getSmall(ctx, src, sigFile, maxSigBytes)
	if err != nil {
		return "", err
	}
	if err := verifySums(sums, sig, key); err != nil {
		return "", err
	}
	name := binaryName(target, u.goos, u.goarch)
	want, err := expectedHash(sums, name)
	if err != nil {
		return "", err
	}
	return u.downloadBinary(ctx, src, name, dir, want)
}

// installBinary puts newPath in place of exe and keeps the previous binary
// as exe + ".old". Elsewhere than Windows, the backup is a hard link (or a
// copy) and the new binary is renamed over exe, so exe is never missing.
// Windows can't replace a running executable, but can rename it: it moves
// aside first, and comes back if the new one can't be moved in.
func installBinary(exe, newPath, goos string) (string, error) {
	backup := exe + ".old"
	if err := os.Remove(backup); err != nil && !errors.Is(err, os.ErrNotExist) {
		return "", fmt.Errorf("remove the previous backup %s: %w", backup, err)
	}
	if goos == "windows" {
		if err := os.Rename(exe, backup); err != nil {
			return "", fmt.Errorf("move the running binary aside: %w", err)
		}
		if err := os.Rename(newPath, exe); err != nil {
			if restoreErr := os.Rename(backup, exe); restoreErr != nil {
				return "", fmt.Errorf("move the new binary in place: %w (and putting the previous one back failed: %v)", err, restoreErr)
			}
			return "", fmt.Errorf("move the new binary in place: %w", err)
		}
		return backup, nil
	}
	if err := linkOrCopy(exe, backup); err != nil {
		return "", fmt.Errorf("keep a backup of the current binary: %w", err)
	}
	if err := os.Rename(newPath, exe); err != nil {
		_ = os.Remove(backup)
		return "", fmt.Errorf("move the new binary in place: %w", err)
	}
	syncDir(filepath.Dir(exe))
	return backup, nil
}

// restoreBinary puts the backup back in place of exe, after a failed
// restart.
func restoreBinary(exe, backup string) error {
	if err := os.Rename(backup, exe); err != nil {
		return err
	}
	syncDir(filepath.Dir(exe))
	return nil
}

func linkOrCopy(src, dst string) error {
	if err := os.Link(src, dst); err == nil {
		return nil
	}
	in, err := os.Open(src) // #nosec G304 -- the agent's own binary
	if err != nil {
		return err
	}
	defer in.Close()
	info, err := in.Stat()
	if err != nil {
		return err
	}
	out, err := os.OpenFile(dst, os.O_WRONLY|os.O_CREATE|os.O_EXCL, info.Mode().Perm()) // #nosec G304 -- its backup
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		_ = out.Close()
		_ = os.Remove(dst)
		return err
	}
	if err := out.Sync(); err != nil {
		_ = out.Close()
		_ = os.Remove(dst)
		return err
	}
	return out.Close()
}

// syncDir makes the renames in dir durable. Best effort: not every
// platform can sync a directory.
func syncDir(dir string) {
	if d, err := os.Open(dir); err == nil { // #nosec G304 -- the agent's own directory
		_ = d.Sync()
		_ = d.Close()
	}
}

// perform downloads, verifies and installs target, then restarts into it.
// Any failure before the swap leaves the current binary untouched; a
// failed restart puts it back. On success it doesn't return (exec), or
// returns nil after asking Run to exit (Windows).
func (u *upgrader) perform(ctx context.Context, target string, progress func(stage, detail string)) error {
	key, err := parseSigningKey(u.signingKeyPEM)
	if err != nil {
		return err
	}
	exe, err := u.executable()
	if err != nil {
		return fmt.Errorf("can't find the agent's own binary: %w", err)
	}
	var newPath string
	var failures []string
	for _, src := range u.sources(target) {
		progress("downloading", "from "+src.label)
		path, err := u.fetchVerified(ctx, src, target, filepath.Dir(exe), key)
		if err == nil {
			newPath = path
			break
		}
		if ctx.Err() != nil {
			return fmt.Errorf("upgrade interrupted: %w", ctx.Err())
		}
		log.Printf("agent: upgrade: %s: %s", src.label, logSafe(err.Error()))
		failures = append(failures, fmt.Sprintf("%s: %v", src.label, err))
	}
	if newPath == "" {
		return fmt.Errorf("no verified %s binary for this host: %s", target, strings.Join(failures, "; "))
	}

	progress("installing", "")
	backup, err := installBinary(exe, newPath, u.goos)
	if err != nil {
		_ = os.Remove(newPath)
		return err
	}
	progress("restarting", "")
	if u.exec == nil {
		u.requestRestart()
		return nil
	}
	if err := u.exec(exe); err != nil {
		if restoreErr := restoreBinary(exe, backup); restoreErr != nil {
			return fmt.Errorf("restart failed (%v), and putting the previous binary back failed too (%v): it is at %s",
				err, restoreErr, backup)
		}
		return fmt.Errorf("restart failed, the previous binary is back in place: %w", err)
	}
	return nil
}

// handle answers an "upgrade" command: refuses it, or accepts it and
// reports its progress and, on failure, why. closeConn ends the
// connection once an upgrade is installed and the agent must exit.
func (u *upgrader) handle(ctx context.Context, cmd Command, send func(any) error, closeConn func()) {
	target, err := u.precheck(cmd.Version)
	if err != nil {
		log.Printf("agent: upgrade refused: %s", logSafe(err.Error()))
		_ = send(ErrorResult{Type: "upgrade_error", ID: cmd.ID, Error: err.Error(), Code: "refused"})
		return
	}
	if !u.running.CompareAndSwap(false, true) {
		_ = send(ErrorResult{Type: "upgrade_error", ID: cmd.ID, Error: "an upgrade is already in progress", Code: "busy"})
		return
	}
	defer u.running.Store(false)
	_ = send(UpgradeAccepted{Type: "upgrade_accepted", ID: cmd.ID, From: u.current, To: target})
	log.Printf("agent: upgrading from %s to %s", logSafe(u.current), target)
	err = u.perform(ctx, target, func(stage, detail string) {
		_ = send(UpgradeProgress{Type: "upgrade_progress", ID: cmd.ID, Stage: stage, Detail: detail})
	})
	if err != nil {
		log.Printf("agent: upgrade to %s failed: %s", target, logSafe(err.Error()))
		_ = send(UpgradeFailed{Type: "upgrade_failed", ID: cmd.ID, Error: err.Error()})
		return
	}
	if u.restartRequested() {
		log.Printf("agent: upgrade to %s installed; exiting for the service manager to restart the agent", target)
		closeConn()
	}
}

func (u *upgrader) requestRestart() {
	u.restartOnce.Do(func() { close(u.restartCh) })
}

func (u *upgrader) restartRequested() bool {
	select {
	case <-u.restartCh:
		return true
	default:
		return false
	}
}
