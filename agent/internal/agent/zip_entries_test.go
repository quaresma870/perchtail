package agent

import (
	"archive/zip"
	"bytes"
	"encoding/binary"
	"fmt"
	"testing"
)

// stating returns content with its end-of-central-directory record stating
// entries.
func stating(t *testing.T, content []byte, entries uint16) []byte {
	t.Helper()
	at := bytes.LastIndex(content, zipEndSignature)
	if at < 0 {
		t.Fatal("no end record")
	}
	out := bytes.Clone(content)
	binary.LittleEndian.PutUint16(out[at+8:], entries)
	binary.LittleEndian.PutUint16(out[at+10:], entries)
	return out
}

// zip64Stating returns content, which must already have a ZIP64 record,
// with that record stating entries.
func zip64Stating(t *testing.T, content []byte, entries uint64) []byte {
	t.Helper()
	at := bytes.LastIndex(content, zip64EndSignature)
	if at < 0 {
		t.Fatal("no ZIP64 record")
	}
	out := bytes.Clone(content)
	binary.LittleEndian.PutUint64(out[at+24:], entries)
	binary.LittleEndian.PutUint64(out[at+32:], entries)
	return out
}

// manyEntries is a zip of count empty members.
func manyEntries(t *testing.T, count int) []byte {
	t.Helper()
	var buf bytes.Buffer
	w := zip.NewWriter(&buf)
	for i := range count {
		if _, err := w.CreateHeader(&zip.FileHeader{Name: fmt.Sprintf("m%d.log", i), Method: zip.Store}); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func TestZipEntryCountReadsTheEndRecordAndTheZip64Record(t *testing.T) {
	small := zipOf(t, []zipMember{{name: "app.log", content: "a line\n"}, {name: "b.log", content: "b\n"}})
	if n, ok := zipEntryCount(bytes.NewReader(small), int64(len(small))); !ok || n != 2 {
		t.Errorf("small: %d %v", n, ok)
	}
	lying := stating(t, small, 60000)
	if n, ok := zipEntryCount(bytes.NewReader(lying), int64(len(lying))); !ok || n != 60000 {
		t.Errorf("stating 60000: %d %v", n, ok)
	}
	var buf bytes.Buffer
	w := zip.NewWriter(&buf)
	if err := w.SetComment("PK" + string(bytes.Repeat([]byte("x"), 5000))); err != nil {
		t.Fatal(err)
	}
	if _, err := w.Create("a"); err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	if n, ok := zipEntryCount(bytes.NewReader(buf.Bytes()), int64(buf.Len())); !ok || n != 1 {
		t.Errorf("behind a comment: %d %v", n, ok)
	}
	many := manyEntries(t, 0xFFFF+1)
	if !bytes.Contains(many[len(many)-200:], zip64EndSignature) {
		t.Fatal("no ZIP64 record written")
	}
	if n, ok := zipEntryCount(bytes.NewReader(many), int64(len(many))); !ok || n != 0xFFFF+1 {
		t.Errorf("ZIP64: %d %v", n, ok)
	}
	for _, content := range [][]byte{nil, []byte("not a zip at all, just text")} {
		if _, ok := zipEntryCount(bytes.NewReader(content), int64(len(content))); ok {
			t.Errorf("%q has a count", content)
		}
	}
}

// A .zip stating more than MaxZipEntries entries, or holding more than it
// states, is answered with archive_unreadable when it is listed and when a
// member is fetched, as the server refuses it; one at the ceiling is listed.
func TestAZipOverTheEntryCeilingIsUnreadable(t *testing.T) {
	base := t.TempDir()
	small := zipOf(t, []zipMember{{name: "app.log", content: "a line\n"}})
	writeFile(t, base, "stated.zip", stating(t, small, MaxZipEntries+1))
	writeFile(t, base, "real.zip", manyEntries(t, MaxZipEntries+1))
	// archive/zip itself refuses a record that doesn't match the index, so
	// the ceiling is checked with as many real entries.
	writeFile(t, base, "atceiling.zip", manyEntries(t, MaxZipEntries))
	// 65537 entries whose ZIP64 record says 1: archive/zip only compares
	// the counts' low 16 bits, so it reads them all.
	writeFile(t, base, "hidden.zip", zip64Stating(t, manyEntries(t, 0xFFFF+2), 1))
	cfg := Config{BasePath: base}

	for _, name := range []string{"stated.zip", "real.zip", "hidden.zip"} {
		result, ok := handleCommand(cfg, Command{Type: "list_archive", ID: "l", Path: name}).(ErrorResult)
		if !ok || result.Type != "list_archive_error" || result.Code != "archive_unreadable" {
			t.Errorf("list_archive %s: %#v", name, result)
		}
		cmd := Command{Type: "fetch", ID: "f", Path: name, Member: "app.log", ChunkSize: minChunkSize, MaxDecompressed: 1 << 20}
		sent := collect(t, base, cmd)
		if result, ok := sent[len(sent)-1].(ErrorResult); !ok || result.Code != "archive_unreadable" {
			t.Errorf("fetch %s ended with %#v", name, sent[len(sent)-1])
		}
	}

	listed, ok := handleCommand(cfg, Command{Type: "list_archive", ID: "l", Path: "atceiling.zip"}).(ListArchiveResult)
	if !ok || len(listed.Members) != MaxZipEntries || listed.Truncated {
		t.Errorf("list_archive atceiling.zip: %d members, truncated %v", len(listed.Members), listed.Truncated)
	}
}
