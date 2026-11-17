package agent

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"runtime"
	"strings"
)

// FeatureLinks: the agent says where links lead (FeatureLinks in the
// handshake). A list answer carries, for each entry a link leads elsewhere
// (the entry itself, or a folder above it), its Target, the path where it
// really is relative to the base path, and RealDir for the folder listed;
// symlinks and, on Windows, junctions alike. A command that reads content
// is answered only when the path really is at ExpectReal (by default the
// path itself): otherwise it is refused, before anything is sent, with an
// ErrorResult coded "link_target" carrying Real (or Outside when it leads
// out of the base path). The server checks its rules on Real and, when they
// show it, asks again with ExpectReal set to it. The check and the read use
// the same open file, so the link can't be swapped between them.
const FeatureLinks = "links/1"

// LinkTargetError refuses a read of Path that really is somewhere other
// than where the server expected: at Real (relative to the base path), or
// out of the base path (Outside).
type LinkTargetError struct {
	Path    string
	Real    string
	Outside bool
}

func (e *LinkTargetError) Error() string {
	if e.Outside {
		return fmt.Sprintf("%s: leads out of the base path", e.Path)
	}
	return fmt.Sprintf("%s: really is %s", e.Path, e.Real)
}

// errNoFinalPath: this system can't say from an open file where it really
// is (finalPath); realPaths then falls back to resolving the path itself.
var errNoFinalPath = errors.New("the real path of an open file is not available")

// pathsIgnoreCase: Windows paths are compared ignoring case, as NTFS does,
// so a path asked for in another case isn't taken for a link.
var pathsIgnoreCase = runtime.GOOS == "windows"

func samePath(a, b string) bool {
	if pathsIgnoreCase {
		return strings.EqualFold(a, b)
	}
	return a == b
}

// realPaths says where files under one base path really are, relative to
// the base path as resolved (every link in it followed).
type realPaths struct {
	basePath string
	base     string
}

func newRealPaths(basePath string) (*realPaths, error) {
	dir, err := os.Open(basePath) // #nosec G304 -- the agent's own configured base path
	if err != nil {
		return nil, err
	}
	defer dir.Close()
	base, err := finalPath(dir)
	if errors.Is(err, errNoFinalPath) {
		base, err = filepath.EvalSymlinks(basePath)
		if err == nil {
			base, err = filepath.Abs(base)
		}
	}
	if err != nil {
		return nil, fmt.Errorf("resolving the base path: %w", err)
	}
	return &realPaths{basePath: basePath, base: base}, nil
}

// of returns where f, opened as relPath under the base path, really is,
// relative to the resolved base path with "/" separators ("" for the base
// itself). ok is false when it is out of the base path.
func (r *realPaths) of(f *os.File, relPath string) (real string, ok bool, err error) {
	full, err := finalPath(f)
	if errors.Is(err, errNoFinalPath) {
		full, err = resolveSame(f, filepath.Join(r.basePath, filepath.FromSlash(rootName(relPath))), r.base)
	}
	if err != nil {
		return "", false, err
	}
	if samePath(full, r.base) {
		return "", true, nil
	}
	prefix := r.base
	if !strings.HasSuffix(prefix, string(filepath.Separator)) {
		prefix += string(filepath.Separator)
	}
	if len(full) <= len(prefix) || !samePath(full[:len(prefix)], prefix) {
		return "", false, nil
	}
	return filepath.ToSlash(full[len(prefix):]), true, nil
}

// resolveSame resolves name (every symlink followed) and checks it is the
// file f is: where finalPath isn't available, this keeps the answer about
// the file that was opened, not one swapped in since. A name that resolves
// out of base is returned as it is, without looking at what it leads to:
// the caller only learns from it that the file is out of the base path.
func resolveSame(f *os.File, name, base string) (string, error) {
	resolved, err := filepath.EvalSymlinks(name)
	if err != nil {
		return "", err
	}
	resolved, err = filepath.Abs(resolved)
	if err != nil {
		return "", err
	}
	resolved = filepath.Clean(resolved)
	prefix := base
	if !strings.HasSuffix(prefix, string(filepath.Separator)) {
		prefix += string(filepath.Separator)
	}
	if resolved != base && !strings.HasPrefix(resolved, prefix) {
		return resolved, nil
	}
	opened, err := f.Stat()
	if err != nil {
		return "", err
	}
	found, err := os.Stat(resolved)
	if err != nil {
		return "", err
	}
	if !os.SameFile(opened, found) {
		return "", errors.New("the file changed while it was opened")
	}
	return resolved, nil
}

// cleanRel is a relative path as the server writes it, cleaned, "" for the
// base itself.
func cleanRel(relPath string) string {
	cleaned := path.Clean(strings.ReplaceAll(relPath, "\\", "/"))
	if cleaned == "." {
		return ""
	}
	return cleaned
}

// isEscape says whether os.Root refused a path because it (a link on it)
// leads out of the root.
func isEscape(err error) bool {
	return err != nil && !errors.Is(err, fs.ErrNotExist) && strings.Contains(err.Error(), "path escapes from parent")
}

// openFile opens relPath under basePath through os.Root, so neither the
// path nor a link on it can lead out of basePath (a LinkTargetError with
// Outside if one does), and checks it really is at expectReal (relPath
// when empty), as FeatureLinks says: a LinkTargetError with where it is
// otherwise, before anything is read. It must be a file.
func openFile(basePath, relPath, expectReal string) (*os.File, func(), error) {
	if !IsSafeRelativePath(relPath) {
		return nil, nil, fmt.Errorf("unsafe path: %q", relPath)
	}
	if expectReal == "" {
		expectReal = relPath
	} else if !IsSafeRelativePath(expectReal) {
		return nil, nil, fmt.Errorf("unsafe path: %q", expectReal)
	}
	reals, err := newRealPaths(basePath)
	if err != nil {
		return nil, nil, err
	}
	root, err := os.OpenRoot(basePath)
	if err != nil {
		return nil, nil, err
	}
	f, err := root.Open(rootName(relPath))
	if err != nil {
		_ = root.Close()
		if isEscape(err) {
			return nil, nil, &LinkTargetError{Path: relPath, Outside: true}
		}
		return nil, nil, err
	}
	closeAll := func() {
		_ = f.Close()
		_ = root.Close()
	}
	real, inside, err := reals.of(f, relPath)
	if err != nil {
		closeAll()
		return nil, nil, err
	}
	if !inside {
		closeAll()
		return nil, nil, &LinkTargetError{Path: relPath, Outside: true}
	}
	if !samePath(real, cleanRel(expectReal)) {
		closeAll()
		return nil, nil, &LinkTargetError{Path: relPath, Real: real}
	}
	st, err := f.Stat()
	if err != nil {
		closeAll()
		return nil, nil, err
	}
	if st.IsDir() {
		closeAll()
		return nil, nil, fmt.Errorf("%s: is a directory", relPath)
	}
	return f, closeAll, nil
}
