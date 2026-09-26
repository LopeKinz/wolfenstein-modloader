package wolfsdk

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"strings"
	"testing"
)

// Shared test vectors (spec/vectors); override the location with
// WOLFSDK_VECTORS.
func vecDir() string {
	if d := os.Getenv("WOLFSDK_VECTORS"); d != "" {
		return d
	}
	return filepath.Join("..", "..", "spec", "vectors")
}

func readVec(t *testing.T, rel string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(vecDir(), filepath.FromSlash(rel)))
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func loadJSON(t *testing.T, rel string, v any) {
	t.Helper()
	if err := json.Unmarshal(readVec(t, rel), v); err != nil {
		t.Fatalf("%s: %v", rel, err)
	}
}

func sha(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

func unhex(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(s)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

var paddingRe = regexp.MustCompile(`^(\n// *)?$`)

// ---------------------------------------------------------------- decl

type declVectors struct {
	Parse []struct {
		File   string     `json:"file"`
		Paths  [][]string `json:"paths"`
		Blocks []string   `json:"blocks"`
	} `json:"parse"`
	Edits []struct {
		File   string  `json:"file"`
		Set    [][]any `json:"set"`
		Expect string  `json:"expect"`
	} `json:"edits"`
	ParseErrors []string `json:"parseErrors"`
	SetErrors   []struct {
		File  string `json:"file"`
		Path  string `json:"path"`
		Value any    `json:"value"`
	} `json:"setErrors"`
	Paths      [][]any  `json:"paths"`
	PathErrors []string `json:"pathErrors"`
}

func TestDeclParse(t *testing.T) {
	var v declVectors
	loadJSON(t, "decl.json", &v)
	for _, c := range v.Parse {
		text := string(readVec(t, "decl/"+c.File))
		d, err := ParseDecl(text)
		if err != nil {
			t.Fatalf("%s: %v", c.File, err)
		}
		if d.Text() != text {
			t.Errorf("%s: text not preserved", c.File)
		}
		got := [][]string{}
		for _, p := range d.Paths() {
			got = append(got, []string{p.Path, p.Kind, p.Raw})
		}
		if !reflect.DeepEqual(got, c.Paths) {
			t.Errorf("%s: paths\n got %q\nwant %q", c.File, got, c.Paths)
		}
		blocks := []string{}
		for _, n := range d.Nodes() {
			if n.Kind == KindBlock {
				blocks = append(blocks, n.PathString())
			}
		}
		if !reflect.DeepEqual(blocks, c.Blocks) {
			t.Errorf("%s: blocks\n got %q\nwant %q", c.File, blocks, c.Blocks)
		}
	}
}

func TestDeclEdits(t *testing.T) {
	var v declVectors
	loadJSON(t, "decl.json", &v)
	for _, c := range v.Edits {
		d, err := ParseDecl(string(readVec(t, "decl/"+c.File)))
		if err != nil {
			t.Fatal(err)
		}
		for _, s := range c.Set {
			if err := d.Set(s[0].(string), s[1]); err != nil {
				t.Fatalf("%s: set %v: %v", c.File, s, err)
			}
		}
		if want := string(readVec(t, "decl/"+c.Expect)); d.Text() != want {
			t.Errorf("%s -> %s:\n got %q\nwant %q", c.File, c.Expect, d.Text(), want)
		}
	}
}

func TestDeclErrors(t *testing.T) {
	var v declVectors
	loadJSON(t, "decl.json", &v)
	for _, text := range v.ParseErrors {
		if _, err := ParseDecl(text); !errors.Is(err, ErrDecl) {
			t.Errorf("ParseDecl(%q): got %v, want ErrDecl", text, err)
		}
	}
	for _, c := range v.SetErrors {
		d, err := ParseDecl(string(readVec(t, "decl/"+c.File)))
		if err != nil {
			t.Fatal(err)
		}
		if err := d.Set(c.Path, c.Value); !errors.Is(err, ErrDecl) {
			t.Errorf("%s: Set(%q): got %v, want ErrDecl", c.File, c.Path, err)
		}
	}
	for _, p := range v.PathErrors {
		if _, err := ParsePath(p); !errors.Is(err, ErrDecl) {
			t.Errorf("ParsePath(%q): got %v, want ErrDecl", p, err)
		}
	}
}

func TestDeclPaths(t *testing.T) {
	var v declVectors
	loadJSON(t, "decl.json", &v)
	for _, c := range v.Paths {
		s := c[0].(string)
		var segs []string
		for _, x := range c[1].([]any) {
			segs = append(segs, x.(string))
		}
		got, err := ParsePath(s)
		if err != nil || !reflect.DeepEqual(got, segs) {
			t.Errorf("ParsePath(%q) = %q, %v; want %q", s, got, err, segs)
		}
		if f := FormatPath(segs); f != s {
			t.Errorf("FormatPath(%q) = %q, want %q", segs, f, s)
		}
	}
}

func TestFormat(t *testing.T) {
	var cases []struct {
		Raw      string `json:"raw"`
		Kind     string `json:"kind"`
		Value    any    `json:"value"`
		Expected string `json:"expected"`
	}
	loadJSON(t, "format.json", &cases)
	for _, c := range cases {
		node := Node{Kind: c.Kind, Start: 0, End: len(c.Raw), Raw: c.Raw}
		got, err := FormatValue(node, c.Value)
		if err != nil || got != c.Expected {
			t.Errorf("FormatValue(%s %q, %v) = %q, %v; want %q", c.Kind, c.Raw, c.Value, got, err, c.Expected)
		}
	}
}

// --------------------------------------------------------------- images

type bimVectors struct {
	Decode []struct {
		File       string   `json:"file"`
		Hash       uint32   `json:"hash"`
		Header     []uint32 `json:"header"`
		Mips       [][]int  `json:"mips"`
		Width      int      `json:"width"`
		Height     int      `json:"height"`
		RgbaSha256 string   `json:"rgbaSha256"`
		RgbaHex    *string  `json:"rgbaHex"`
	} `json:"decode"`
	BadMagic []string `json:"badMagic"`
	Encode   struct {
		Template string `json:"template"`
		Width    int    `json:"width"`
		Height   int    `json:"height"`
		RgbaHex  string `json:"rgbaHex"`
		Expect   string `json:"expect"`
	} `json:"encode"`
	PNG []struct {
		File       string `json:"file"`
		Width      int    `json:"width"`
		Height     int    `json:"height"`
		RgbaSha256 string `json:"rgbaSha256"`
	} `json:"png"`
}

func TestBimDecode(t *testing.T) {
	var v bimVectors
	loadJSON(t, "bim.json", &v)
	for _, c := range v.Decode {
		b, err := ParseBim(readVec(t, "bim/"+c.File))
		if err != nil {
			t.Fatalf("%s: %v", c.File, err)
		}
		if b.Hash != c.Hash {
			t.Errorf("%s: hash %d want %d", c.File, b.Hash, c.Hash)
		}
		if !reflect.DeepEqual(b.Header[:], c.Header) {
			t.Errorf("%s: header %v want %v", c.File, b.Header, c.Header)
		}
		mips := [][]int{}
		for _, m := range b.Mips {
			mips = append(mips, []int{int(m.Width), int(m.Height), len(m.Data)})
		}
		if !reflect.DeepEqual(mips, c.Mips) {
			t.Errorf("%s: mips %v want %v", c.File, mips, c.Mips)
		}
		w, h, rgba, err := DecodeBim(b, 0)
		if err != nil {
			t.Fatalf("%s: %v", c.File, err)
		}
		if w != c.Width || h != c.Height {
			t.Errorf("%s: size %dx%d want %dx%d", c.File, w, h, c.Width, c.Height)
		}
		if sha(rgba) != c.RgbaSha256 {
			t.Errorf("%s: rgba sha256 mismatch", c.File)
		}
		if c.RgbaHex != nil && hex.EncodeToString(rgba) != *c.RgbaHex {
			t.Errorf("%s: rgba %x want %s", c.File, rgba, *c.RgbaHex)
		}
	}
	for _, f := range v.BadMagic {
		if _, err := ParseBim(readVec(t, "bim/"+f)); !errors.Is(err, ErrFormat) {
			t.Errorf("%s: got %v, want ErrFormat", f, err)
		}
	}
}

func TestBimEncode(t *testing.T) {
	var v bimVectors
	loadJSON(t, "bim.json", &v)
	e := v.Encode
	tmpl, err := ParseBim(readVec(t, "bim/"+e.Template))
	if err != nil {
		t.Fatal(err)
	}
	out, err := EncodeRGBA8(tmpl, e.Width, e.Height, unhex(t, e.RgbaHex))
	if err != nil {
		t.Fatal(err)
	}
	if want := readVec(t, "bim/"+e.Expect); string(out) != string(want) {
		t.Errorf("encoded BIM differs:\n got %x\nwant %x", out, want)
	}
}

func TestPNG(t *testing.T) {
	var v bimVectors
	loadJSON(t, "bim.json", &v)
	for _, c := range v.PNG {
		w, h, rgba, err := DecodePNG(readVec(t, "png/"+c.File))
		if err != nil {
			t.Fatalf("%s: %v", c.File, err)
		}
		if w != c.Width || h != c.Height || sha(rgba) != c.RgbaSha256 {
			t.Errorf("%s: got %dx%d sha %s", c.File, w, h, sha(rgba))
		}
		enc, err := EncodePNG(w, h, rgba)
		if err != nil {
			t.Fatal(err)
		}
		w2, h2, rgba2, err := DecodePNG(enc)
		if err != nil || w2 != w || h2 != h || string(rgba2) != string(rgba) {
			t.Errorf("%s: PNG round trip failed: %v", c.File, err)
		}
	}
}

// ---------------------------------------------------------- audio, save

func TestBsnf(t *testing.T) {
	var v struct {
		Bsnf []struct {
			File    string `json:"file"`
			Samples []struct {
				Language       string `json:"language"`
				Hash           uint32 `json:"hash"`
				Length         uint32 `json:"length"`
				Granule        uint32 `json:"granule"`
				FormatTag      uint16 `json:"format_tag"`
				Channels       uint16 `json:"channels"`
				SampleRate     uint32 `json:"sample_rate"`
				AvgBytesPerSec uint32 `json:"avg_bytes_per_sec"`
				BlockAlign     uint16 `json:"block_align"`
				BitsPerSample  uint16 `json:"bits_per_sample"`
			} `json:"samples"`
		} `json:"bsnf"`
	}
	loadJSON(t, "audio.json", &v)
	for _, c := range v.Bsnf {
		got, err := ParseBsnf(readVec(t, "audio/"+c.File))
		if err != nil {
			t.Fatalf("%s: %v", c.File, err)
		}
		if len(got) != len(c.Samples) {
			t.Fatalf("%s: %d samples, want %d", c.File, len(got), len(c.Samples))
		}
		for i, s := range c.Samples {
			if want := SampleInfo(s); got[i] != want {
				t.Errorf("%s[%d]: got %+v want %+v", c.File, i, got[i], want)
			}
		}
	}
}

type audioVectors struct {
	Ogg struct {
		File      string    `json:"file"`
		Streams   [][]int64 `json:"streams"`
		BadOffset int64     `json:"badOffset"`
	} `json:"ogg"`
	Containers [][]string `json:"containers"`
}

func TestOgg(t *testing.T) {
	var v audioVectors
	loadJSON(t, "audio.json", &v)
	o := v.Ogg
	data := readVec(t, "audio/"+o.File)
	for _, s := range o.Streams {
		if n, err := OggStreamLength(data, s[0]); err != nil || n != s[1] {
			t.Errorf("OggStreamLength(%d) = %d, %v; want %d", s[0], n, err, s[1])
		}
	}
	f, err := os.Open(filepath.Join(vecDir(), "audio", o.File))
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if n, err := OggStreamLengthReader(f, o.Streams[1][0]); err != nil || n != o.Streams[1][1] {
		t.Errorf("OggStreamLengthReader = %d, %v; want %d", n, err, o.Streams[1][1])
	}
	if _, err := OggStreamLength(data, o.BadOffset); !errors.Is(err, ErrFormat) {
		t.Errorf("bad offset: got %v, want ErrFormat", err)
	}
}

func TestContainers(t *testing.T) {
	var v audioVectors
	loadJSON(t, "audio.json", &v)
	for _, c := range v.Containers {
		if got := StreamContainerFor(c[0]); got != c[1] {
			t.Errorf("StreamContainerFor(%q) = %q, want %q", c[0], got, c[1])
		}
	}
}

func TestSave(t *testing.T) {
	var cases []struct {
		PayloadHex  string `json:"payloadHex"`
		ChecksumHex string `json:"checksumHex"`
	}
	loadJSON(t, "save.json", &cases)
	for _, c := range cases {
		p := unhex(t, c.PayloadHex)
		if got := hex.EncodeToString(SaveChecksum(p)); got != c.ChecksumHex {
			t.Errorf("checksum %s want %s", got, c.ChecksumHex)
		}
		good := append(unhex(t, c.ChecksumHex), p...)
		if !VerifySave(good) {
			t.Errorf("VerifySave(good) = false")
		}
		if c.ChecksumHex != "00000000" && VerifySave(append([]byte{0, 0, 0, 0}, p...)) {
			t.Errorf("VerifySave(zero header) = true")
		}
		fixed, err := FixSave(append([]byte{0xff, 0xff, 0xff, 0xff}, p...))
		if err != nil || string(fixed) != string(good) {
			t.Errorf("FixSave mismatch: %v", err)
		}
	}
	if _, err := FixSave([]byte{1, 2}); !errors.Is(err, ErrFormat) {
		t.Errorf("FixSave(short): got %v", err)
	}
}

// ---------------------------------------------------------- compression

func TestDeflateExact(t *testing.T) {
	var v struct {
		Files map[string]string `json:"files"`
		Cases []struct {
			Data    string `json:"data"`
			Delta   int    `json:"delta"`
			Pad     bool   `json:"pad"`
			MustFit bool   `json:"mustFit"`
		} `json:"cases"`
	}
	loadJSON(t, "compression.json", &v)
	for _, c := range v.Cases {
		data := readVec(t, v.Files[c.Data])
		s := DeflateSync(data)
		if !IsSyncFlushed(s) {
			t.Fatalf("%s: DeflateSync not sync-flushed", c.Data)
		}
		if back, err := Inflate(s, len(data)); err != nil || string(back) != string(data) {
			t.Fatalf("%s: inflate round trip: %v", c.Data, err)
		}
		csize := len(s) + c.Delta
		stream, payload, ok := DeflateExact(data, csize, c.Pad)
		if c.MustFit && !ok {
			t.Errorf("%+v: expected a fit", c)
		}
		if (c.Delta < 0 || (c.Delta > 0 && !c.Pad)) && ok {
			t.Errorf("%+v: expected no fit", c)
		}
		if !ok {
			continue
		}
		if len(stream) != csize || !IsSyncFlushed(stream) {
			t.Errorf("%+v: len %d sync %v", c, len(stream), IsSyncFlushed(stream))
		}
		if !strings.HasPrefix(string(payload), string(data)) || !paddingRe.Match(payload[len(data):]) {
			t.Errorf("%+v: bad payload padding", c)
		}
		if back, err := Inflate(stream, len(payload)); err != nil || string(back) != string(payload) {
			t.Errorf("%+v: stream does not inflate to payload: %v", c, err)
		}
	}
}

func TestInflateShort(t *testing.T) {
	if _, err := Inflate(DeflateSync([]byte("abc")), 10); !errors.Is(err, ErrFormat) {
		t.Errorf("got %v, want ErrFormat", err)
	}
}

// -------------------------------------------------------------- archive

type archiveVectors struct {
	Pairs  [][]string `json:"pairs"`
	Chunks []struct {
		Index         string                   `json:"index"`
		Resources     string                   `json:"resources"`
		CounterA      uint32                   `json:"counterA"`
		CounterB      uint32                   `json:"counterB"`
		ResourcesSize int64                    `json:"resourcesSize"`
		Entries       []map[string]interface{} `json:"entries"`
	} `json:"chunks"`
	Find         map[string][]string `json:"find"`
	WriteInPlace []struct {
		Asset  string `json:"asset"`
		Text   string `json:"text"`
		Expect string `json:"expect"`
	} `json:"writeInPlace"`
	Rebuild struct {
		Chunk string `json:"chunk"`
		Asset string `json:"asset"`
		Text  string `json:"text"`
	} `json:"rebuild"`
}

// copyGame copies spec/vectors/game into a temporary directory.
func copyGame(t *testing.T) string {
	t.Helper()
	src := filepath.Join(vecDir(), "game")
	dst := filepath.Join(t.TempDir(), "game")
	err := filepath.WalkDir(src, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(src, p)
		target := filepath.Join(dst, rel)
		if d.IsDir() {
			return os.MkdirAll(target, 0o755)
		}
		b, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		return os.WriteFile(target, b, 0o644)
	})
	if err != nil {
		t.Fatal(err)
	}
	return dst
}

func openGame(t *testing.T, root string) *Game {
	t.Helper()
	g, err := OpenGame(root)
	if err != nil {
		t.Fatal(err)
	}
	return g
}

func validate(t *testing.T, c *Chunk) {
	t.Helper()
	p, err := ValidateChunk(c)
	if err != nil {
		t.Fatal(err)
	}
	if len(p) != 0 {
		t.Errorf("%s: %v", c.IndexName, p)
	}
}

func TestMasterIndex(t *testing.T) {
	var v archiveVectors
	loadJSON(t, "archive.json", &v)
	data := readVec(t, "game/base/master.index")
	pairs, err := ParseMasterIndex(data)
	if err != nil {
		t.Fatal(err)
	}
	var got [][]string
	for _, p := range pairs {
		got = append(got, []string{p.Index, p.Resources})
	}
	if !reflect.DeepEqual(got, v.Pairs) {
		t.Errorf("pairs %v want %v", got, v.Pairs)
	}
	if string(BuildMasterIndex(pairs)) != string(data) {
		t.Errorf("BuildMasterIndex is not the inverse")
	}
	bad := append([]byte("XXXX"), data[4:]...)
	if _, err := ParseMasterIndex(bad); !errors.Is(err, ErrFormat) {
		t.Errorf("bad magic: %v", err)
	}
	if _, err := ParseMasterIndex(data[:20]); !errors.Is(err, ErrFormat) {
		t.Errorf("truncated: %v", err)
	}
}

func TestEntries(t *testing.T) {
	var v archiveVectors
	loadJSON(t, "archive.json", &v)
	g := openGame(t, copyGame(t))
	if len(g.Chunks) != len(v.Chunks) {
		t.Fatalf("%d chunks, want %d", len(g.Chunks), len(v.Chunks))
	}
	for ci, c := range g.Chunks {
		exp := v.Chunks[ci]
		if c.IndexName != exp.Index || c.ResourcesName != exp.Resources {
			t.Errorf("chunk %d: %s/%s", ci, c.IndexName, c.ResourcesName)
		}
		if c.Index.CounterA != exp.CounterA || c.Index.CounterB != exp.CounterB {
			t.Errorf("%s: counters %d/%d", c.IndexName, c.Index.CounterA, c.Index.CounterB)
		}
		if c.Index.ResourcesSize != exp.ResourcesSize {
			t.Errorf("%s: resources size %d", c.IndexName, c.Index.ResourcesSize)
		}
		if len(c.Entries()) != len(exp.Entries) {
			t.Fatalf("%s: %d entries, want %d", c.IndexName, len(c.Entries()), len(exp.Entries))
		}
		for i, e := range c.Entries() {
			payload, err := g.Read(Occurrence{c, e})
			if err != nil {
				t.Fatalf("%s: %v", e.Key(), err)
			}
			var stream any
			if so, ok := e.StreamOffset(); ok {
				stream = float64(so)
			}
			got := map[string]any{
				"index": float64(e.Index), "type": e.Type, "name": e.Name, "path": e.Path,
				"offset": float64(e.Offset), "usize": float64(e.USize), "csize": float64(e.CSize),
				"start": float64(e.Start), "tripleOffset": float64(e.TripleOffset),
				"trailerHex": hex.EncodeToString(e.Trailer), "streamOffset": stream,
				"compressed": e.Compressed(), "payloadSha256": sha(payload),
			}
			if !reflect.DeepEqual(got, exp.Entries[i]) {
				t.Errorf("%s entry %d:\n got %v\nwant %v", c.IndexName, i, got, exp.Entries[i])
			}
		}
		// an index without resources size must give the same entries
		ci2, err := ParseChunkIndex(c.Index.Bytes(), -1)
		if err != nil || len(ci2.Entries) != len(exp.Entries) {
			t.Errorf("%s: without resources size: %v", c.IndexName, err)
		}
		validate(t, c)
	}
	for key, chunks := range v.Find {
		typ, name, _ := strings.Cut(key, ":")
		got := []string{}
		for _, o := range g.Find(typ, name) {
			got = append(got, o.Chunk.IndexName)
		}
		if !reflect.DeepEqual(got, chunks) {
			t.Errorf("Find(%s) = %v want %v", key, got, chunks)
		}
	}
}

func resourcesSizes(t *testing.T, g *Game) map[string]int64 {
	out := map[string]int64{}
	for _, c := range g.Chunks {
		st, err := os.Stat(c.ResourcesPath)
		if err != nil {
			t.Fatal(err)
		}
		out[c.ResourcesName] = st.Size()
	}
	return out
}

type slotInfo struct {
	chunk         string
	offset, csize uint32
}

func slotsOf(g *Game, typ, name string) []slotInfo {
	var out []slotInfo
	for _, o := range g.Find(typ, name) {
		out = append(out, slotInfo{o.Chunk.IndexName, o.Entry.Offset, o.Entry.CSize})
	}
	return out
}

func TestWriteInPlace(t *testing.T) {
	var v archiveVectors
	loadJSON(t, "archive.json", &v)
	root := copyGame(t)
	for _, c := range v.WriteInPlace {
		g := openGame(t, root)
		typ, name, _ := strings.Cut(c.Asset, ":")
		data := []byte(c.Text)
		sizes := resourcesSizes(t, g)
		before := slotsOf(g, typ, name)
		_, err := g.WriteAsset(typ, name, data)
		if c.Expect == "nofit" {
			if !errors.Is(err, ErrNoFit) {
				t.Errorf("%s: got %v, want ErrNoFit", c.Asset, err)
			}
			continue
		}
		if err != nil {
			t.Fatalf("%s: %v", c.Asset, err)
		}
		g2 := openGame(t, root)
		if after := slotsOf(g2, typ, name); !reflect.DeepEqual(before, after) {
			t.Errorf("%s: slots moved: %v -> %v", c.Asset, before, after)
		}
		for _, o := range g2.Find(typ, name) {
			payload, err := g2.Read(o)
			if err != nil {
				t.Fatal(err)
			}
			if !strings.HasPrefix(string(payload), c.Text) || !paddingRe.Match(payload[len(data):]) {
				t.Errorf("%s: payload %q", c.Asset, payload)
			}
		}
		if !reflect.DeepEqual(resourcesSizes(t, g2), sizes) {
			t.Errorf("%s: resources sizes changed", c.Asset)
		}
		for _, ch := range g2.Chunks {
			validate(t, ch)
		}
	}
}

func TestRebuild(t *testing.T) {
	var v archiveVectors
	loadJSON(t, "archive.json", &v)
	r := v.Rebuild
	root := copyGame(t)
	g := openGame(t, root)
	var chunk *Chunk
	for _, c := range g.Chunks {
		if c.IndexName == r.Chunk {
			chunk = c
		}
	}
	if chunk == nil {
		t.Fatalf("chunk %s not found", r.Chunk)
	}
	typ, name, _ := strings.Cut(r.Asset, ":")
	target := chunk.Index.Find(typ, name)[0]
	before := map[int][]byte{}
	for _, e := range chunk.Entries() {
		b, err := g.Read(Occurrence{chunk, e})
		if err != nil {
			t.Fatal(err)
		}
		before[e.Index] = b
	}
	if err := g.Rebuild(chunk, map[int][]byte{target.Index: []byte(r.Text)}, ""); err != nil {
		t.Fatal(err)
	}
	g2 := openGame(t, root)
	var chunk2 *Chunk
	for _, c := range g2.Chunks {
		if c.IndexName == r.Chunk {
			chunk2 = c
		}
	}
	validate(t, chunk2)
	for _, e := range chunk2.Entries() {
		want := before[e.Index]
		if e.Index == target.Index {
			want = []byte(r.Text)
		}
		got, err := g2.Read(Occurrence{chunk2, e})
		if err != nil || string(got) != string(want) {
			t.Errorf("%s: payload differs after rebuild: %v", e.Key(), err)
		}
	}
}

// ------------------------------------------------------------------ mod

type modVectors struct {
	Base        map[string]string `json:"base"`
	Order       []string          `json:"order"`
	Sorted      []string          `json:"sorted"`
	Results     map[string]string `json:"results"`
	ResultOrder []string          `json:"resultOrder"`
	Conflicts   []Conflict        `json:"conflicts"`
	Errors      []struct {
		Asset string `json:"asset"`
		Mod   string `json:"mod"`
	} `json:"errors"`
	Invalid [][]string `json:"invalid"`
}

func TestModApply(t *testing.T) {
	var v modVectors
	loadJSON(t, "mod.json", &v)
	base := map[string]string{}
	for k, p := range v.Base {
		base[k] = string(readVec(t, "mod/"+p))
	}
	var mods []*Mod
	for _, n := range v.Order {
		m, err := LoadMod(filepath.Join(vecDir(), "mod", n))
		if err != nil {
			t.Fatal(err)
		}
		mods = append(mods, m)
	}
	var sorted []string
	for _, m := range SortMods(mods) {
		sorted = append(sorted, m.Name)
	}
	if !reflect.DeepEqual(sorted, v.Sorted) {
		t.Errorf("sorted %v want %v", sorted, v.Sorted)
	}
	res := ApplyMods(mods, func(typ, name string) (string, bool) {
		s, ok := base[typ+":"+name]
		return s, ok
	}, nil)
	var order []string
	for _, r := range res.Results {
		order = append(order, r.Key)
	}
	if !reflect.DeepEqual(order, v.ResultOrder) {
		t.Errorf("result order %v want %v", order, v.ResultOrder)
	}
	for k, p := range v.Results {
		got, _ := res.Result(k)
		if want := string(readVec(t, "mod/"+p)); got != want {
			t.Errorf("%s:\n got %q\nwant %q", k, got, want)
		}
	}
	if !reflect.DeepEqual(res.Conflicts, v.Conflicts) {
		gj, _ := json.Marshal(res.Conflicts)
		wj, _ := json.Marshal(v.Conflicts)
		t.Errorf("conflicts\n got %s\nwant %s", gj, wj)
	}
	if len(res.Errors) != len(v.Errors) {
		t.Fatalf("errors %+v want %+v", res.Errors, v.Errors)
	}
	for i, e := range v.Errors {
		if res.Errors[i].Asset != e.Asset || res.Errors[i].Mod != e.Mod {
			t.Errorf("error %d: %+v want %+v", i, res.Errors[i], e)
		}
	}
}

func TestModInvalid(t *testing.T) {
	var v modVectors
	loadJSON(t, "mod.json", &v)
	for _, c := range v.Invalid {
		if _, err := ParseMod([]byte(c[1]), "", ""); !errors.Is(err, ErrMod) {
			t.Errorf("%s (%s): got %v, want ErrMod", c[0], c[1], err)
		}
	}
}
