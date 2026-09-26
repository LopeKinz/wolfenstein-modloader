# wolfsdk (Go)

Go binding of the Wolfenstein modding SDK for *Wolfenstein: The New Order*
(id Tech 5) and the Decl format shared with *Wolfenstein II: The New
Colossus*. Standard library only, Go ≥ 1.21.

```bash
go get github.com/LopeKinz/wolfenstein-modloader/sdk/go
```

```go
import wolfsdk "github.com/LopeKinz/wolfenstein-modloader/sdk/go"
```

The package implements the language-neutral contract in
[`spec/SPEC.md`](../../spec/SPEC.md) and runs the shared test vectors in
[`spec/vectors`](../../spec/vectors). Background on the file formats is in the
[project wiki](https://github.com/LopeKinz/wolfenstein-modloader/wiki).

The full command-line tool with a journaled patcher (`apply` / `revert`,
backups) lives in the [Python package](../python). This module is a library.

## Open a game and read an asset

```go
g, err := wolfsdk.OpenGame(`C:\Games\Wolfenstein The New Order`) // the directory containing base/
if err != nil {
	log.Fatal(err)
}
for _, o := range g.Find("weapon", "weapon/shotgun_base") { // every chunk that has it
	fmt.Println(o.Chunk.IndexName, o.Entry.Offset, o.Entry.CSize)
}
text, ok, err := g.ReadAsset("weapon", "weapon/shotgun_base")
```

## Edit a Decl

Edits replace only the span of the value; every other byte stays as it was.

```go
d, err := wolfsdk.ParseDecl(string(text))
if err != nil {
	log.Fatal(err) // errors.Is(err, wolfsdk.ErrDecl)
}
for _, p := range d.Paths() {
	fmt.Println(p.Path, p.Kind, p.Raw) // edit.validAmmoClips.item[0].clipSize word 20
}
_ = d.Set("edit.validAmmoClips.item[0].clipSize", 40)
_ = d.Set("edit.handsFovScale", 0.75)       // keeps the number style of the original (0.90 -> 0.75)
_ = d.Set("inherit", "weapon/other")         // strings are quoted/escaped automatically
_ = d.SetRaw("edit.firingIntervals", "{ num = 1; firingIntervals[0] = 100; }")

// Write it back into every slot, without moving anything in the archive.
if _, err := g.WriteAsset("weapon", "weapon/shotgun_base", []byte(d.Text())); errors.Is(err, wolfsdk.ErrNoFit) {
	chunk := g.Find("weapon", "weapon/shotgun_base")[0].Chunk
	idx := g.Find("weapon", "weapon/shotgun_base")[0].Entry.Index
	err = g.Rebuild(chunk, map[int][]byte{idx: []byte(d.Text())}, "")
}
```

Back up `base/` before writing to a real installation.

## Apply mods in memory

```go
low, _ := wolfsdk.LoadMod("mods/faster_shotgun")      // directory containing mod.json
high, _ := wolfsdk.ParseMod([]byte(`{"name": "x", "priority": 5,
	"assets": {"weapon:weapon/shotgun_base": {"set": {"edit.ammoPerShot": 2}}}}`), "", "")

res := wolfsdk.ApplyMods([]*wolfsdk.Mod{low, high}, func(typ, name string) (string, bool) {
	b, ok, err := g.ReadAsset(typ, name)
	return string(b), ok && err == nil
}, nil) // nil: "file" entries are read relative to each mod's directory

for _, r := range res.Results { // ordered by first appearance
	fmt.Println(r.Key, len(r.Text))
}
for _, c := range res.Conflicts { // "path", "file", "file-over-set"
	fmt.Println(c.Kind, c.Asset, c.Mods, "winner:", c.Winner)
}
for _, e := range res.Errors {
	fmt.Println(e.Asset, e.Mod, e.Message)
}
```

## BIM image → PNG

```go
raw, ok, err := g.ReadAsset("image", "ui/test_bc1")
bim, err := wolfsdk.ParseBim(raw)
w, h, rgba, err := wolfsdk.DecodeBim(bim, 0) // RGBA8, A8, BC1, BC3
png, err := wolfsdk.EncodePNG(w, h, rgba)
_ = os.WriteFile("out.png", png, 0o644)

// and back: PNG -> RGBA8 BIM using the original as header template
w, h, rgba, err = wolfsdk.DecodePNG(png)
newBim, err := wolfsdk.EncodeRGBA8(bim, w, h, rgba)
```

## Audio

```go
desc, _, _ := g.ReadAsset("sample", "sound/vo/english/x.wav")
samples, err := wolfsdk.ParseBsnf(desc) // language, length, granule, sampleRate, ...
container := wolfsdk.StreamContainerFor("sound/vo/english/x.wav") // "english.streamed"
f, _ := os.Open(filepath.Join(g.Base, container))
off, _ := g.Find("sample", "sound/vo/english/x.wav")[0].Entry.StreamOffset()
n, err := wolfsdk.OggStreamLengthReader(f, int64(off))
```

## Save-game checksum

```go
data, _ := os.ReadFile("checkpoint.dat")
if !wolfsdk.VerifySave(data) {
	fixed, _ := wolfsdk.FixSave(data)
	_ = os.WriteFile("checkpoint.dat", fixed, 0o644)
}
```

## Errors

All errors wrap one of the sentinels below; use `errors.Is`.

| Sentinel | Meaning |
|---|---|
| `ErrFormat` | Bad magic, truncated data, implausible structure |
| `ErrDecl` | Decl cannot be parsed, unknown path, bad path syntax |
| `ErrNoFit` | A replacement does not fit into its archive slot → `Rebuild` |
| `ErrMod` | Invalid `mod.json` |
| `ErrNotFound` | `WriteAsset` on an asset that does not exist |

## Development

```bash
cd sdk/go
go vet ./... && go test ./...
WOLFSDK_VECTORS=/path/to/spec/vectors go test ./...   # vectors elsewhere
```

## Releases

Releases of this module are tagged `sdk/go/vX.Y.Z` (Go's convention for a
module in a subdirectory), e.g.

```bash
go get github.com/LopeKinz/wolfenstein-modloader/sdk/go@v0.1.0
```
