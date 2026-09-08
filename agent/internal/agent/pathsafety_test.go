package agent

import "testing"

func TestIsSafeRelativePath(t *testing.T) {
	cases := []struct {
		path string
		safe bool
	}{
		{"app.log", true},
		{"nested/app.log", true},
		{"", true},
		{"/etc/passwd", false},
		{"\\Windows\\System32", false},
		{"C:\\Windows\\System32", false},
		{"../../etc/passwd", false},
		{"nested/../../etc/passwd", false},
		{"nested\\..\\..\\etc\\passwd", false},
		{"..", false},
		{"a..b/c", true}, // ".." as a substring of a segment, not the whole segment
	}

	for _, tc := range cases {
		if got := IsSafeRelativePath(tc.path); got != tc.safe {
			t.Errorf("IsSafeRelativePath(%q) = %v, want %v", tc.path, got, tc.safe)
		}
	}
}
