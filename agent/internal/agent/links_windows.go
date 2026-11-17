//go:build windows

package agent

import (
	"os"
	"strings"
	"syscall"
	"unsafe"
)

// kernel32 is a known DLL, always loaded from the system directory.
var procGetFinalPathNameByHandleW = syscall.NewLazyDLL("kernel32.dll").NewProc("GetFinalPathNameByHandleW")

// finalPath is where the open file f really is, symlinks and junctions
// resolved (GetFinalPathNameByHandleW), so it is the file that was opened,
// in its normalized long name (an 8.3 short name is resolved too).
func finalPath(f *os.File) (string, error) {
	buf := make([]uint16, 512)
	for {
		// FILE_NAME_NORMALIZED | VOLUME_NAME_DOS (both 0).
		n, _, callErr := procGetFinalPathNameByHandleW.Call(
			f.Fd(), uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)), 0, // #nosec G103 -- Windows API call
		)
		if n == 0 {
			return "", callErr
		}
		if int(n) < len(buf) {
			return trimLongPrefix(syscall.UTF16ToString(buf[:n])), nil
		}
		buf = make([]uint16, n)
	}
}

// trimLongPrefix turns `\\?\C:\x` into `C:\x` and `\\?\UNC\host\share` into
// `\\host\share`, as the base path is compared in the same form.
func trimLongPrefix(p string) string {
	if strings.HasPrefix(p, `\\?\UNC\`) {
		return `\\` + p[len(`\\?\UNC\`):]
	}
	return strings.TrimPrefix(p, `\\?\`)
}
